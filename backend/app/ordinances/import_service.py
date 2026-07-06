import hashlib
import json
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from io import BytesIO
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.assistant.hermes_web import HermesWebUnavailableError, hermes_web_client
from app.core.config import settings
from app.db.session import SessionLocal
from app.municipalities.models import Municipality
from app.ordinances.bop_burgos import (
    BOP_BURGOS_DOMAIN,
    is_municipal_bopbur_announcement,
    is_normative_bopbur_announcement,
    search_bop_burgos_announcements,
)
from app.ordinances.embeddings import EmbeddingsUnavailableError, embed_text
from app.ordinances.models import (
    OfficialLegalSource,
    Ordinance,
    OrdinanceImportItem,
    OrdinanceImportJob,
    OrdinanceLegalChunk,
    OrdinanceReviewReport,
)

DATE_PATTERNS = (
    re.compile(r"\b(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4})\b"),
    re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b"),
)
ORDINANCE_TITLE_RE = re.compile(
    r"(ordenanza|reglamento|bando|norma)",
    re.IGNORECASE,
)
MAX_TITLE_CHARS = 500
MAX_SUMMARY_CHARS = 900


@dataclass(frozen=True)
class SourceCandidate:
    url: str
    municipality_id: int | None = None
    official_source_id: int | None = None
    title: str | None = None


class ImportSourceError(Exception):
    pass


def run_import_job(job_id: int, db: Session | None = None) -> None:
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        job = _get_job(db, job_id)
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.error_message = None
        db.commit()

        official_sources = _load_official_sources(db, job)
        municipalities = _load_municipalities(db, job)
        candidates = _load_seed_candidates(job)
        candidates.extend(_discover_candidates(job, official_sources, municipalities))

        if not candidates:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            job.error_message = (
                "No se encontraron fuentes oficiales. Añade URLs semilla o "
                "configura Hermes Web para búsqueda oficial."
            )
            db.commit()
            return

        item_ids = _create_items(db, job, candidates)
        for item_id in item_ids:
            _process_item(db, item_id, official_sources)

        db.refresh(job)
        statuses = {item.status for item in job.items}
        job.status = "failed" if statuses and statuses <= {"failed"} else "completed"
        job.finished_at = datetime.now(UTC)
        if job.status == "failed" and job.error_message is None:
            job.error_message = "Todas las fuentes fallaron."
        db.commit()
    except Exception as error:
        db.rollback()
        job = db.get(OrdinanceImportJob, job_id)
        if job is not None:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            job.error_message = str(error)[:2000]
            db.commit()
        raise
    finally:
        if owns_session:
            db.close()


def _get_job(db: Session, job_id: int) -> OrdinanceImportJob:
    job = db.scalar(
        select(OrdinanceImportJob)
        .options(selectinload(OrdinanceImportJob.items))
        .where(OrdinanceImportJob.id == job_id)
    )
    if job is None:
        raise ValueError(f"Import job {job_id} not found")
    return job


def _load_official_sources(
    db: Session,
    job: OrdinanceImportJob,
) -> list[OfficialLegalSource]:
    source_ids = _json_list(job.official_source_ids_json)
    query = select(OfficialLegalSource).where(OfficialLegalSource.status == "active")
    if source_ids:
        query = query.where(OfficialLegalSource.id.in_(source_ids))
    return list(db.scalars(query.order_by(OfficialLegalSource.id)))


def _load_municipalities(db: Session, job: OrdinanceImportJob) -> list[Municipality]:
    municipality_ids = _json_list(job.municipality_ids_json)
    if not municipality_ids:
        return []
    return list(
        db.scalars(
            select(Municipality)
            .where(Municipality.id.in_(municipality_ids))
            .order_by(Municipality.name)
        )
    )


def _load_seed_candidates(job: OrdinanceImportJob) -> list[SourceCandidate]:
    candidates: list[SourceCandidate] = []
    for raw in _json_list(job.source_urls_json):
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not url:
            continue
        candidates.append(
            SourceCandidate(
                url=url,
                municipality_id=_optional_int(raw.get("municipality_id")),
                official_source_id=_optional_int(raw.get("official_source_id")),
                title=_optional_text(raw.get("title")),
            )
        )
    return candidates


def _discover_candidates(
    job: OrdinanceImportJob,
    official_sources: list[OfficialLegalSource],
    municipalities: list[Municipality],
) -> list[SourceCandidate]:
    if not (job.search_query or job.topic) or not official_sources or not municipalities:
        return []
    bop_burgos_sources = [
        source
        for source in official_sources
        if source.domain.lower() == BOP_BURGOS_DOMAIN
    ]
    if bop_burgos_sources:
        return _discover_bop_burgos_candidates(
            job,
            bop_burgos_sources[0],
            municipalities,
        )
    if not hermes_web_client.enabled:
        return []

    candidates: list[SourceCandidate] = []
    query_base = " ".join(
        part for part in [job.search_query, job.topic, job.subtopic, "ordenanza"] if part
    )
    for municipality in municipalities:
        for source in official_sources:
            query = f"{query_base} {municipality.name} site:{source.domain}"
            try:
                results = hermes_web_client.search(
                    query=query,
                    limit=settings.ordinance_import_search_limit,
                )
            except HermesWebUnavailableError:
                return candidates
            for result in results:
                url = str(result.get("url") or "").strip()
                if url and _url_allowed(url, official_sources):
                    candidates.append(
                        SourceCandidate(
                            url=url,
                            municipality_id=municipality.id,
                            official_source_id=source.id,
                            title=_optional_text(result.get("title")),
                        )
                    )
    return candidates


def _discover_bop_burgos_candidates(
    job: OrdinanceImportJob,
    official_source: OfficialLegalSource,
    municipalities: list[Municipality],
) -> list[SourceCandidate]:
    candidates: list[SourceCandidate] = []
    query_base = " ".join(
        part for part in [job.search_query, job.topic, job.subtopic, "ordenanza"] if part
    )
    for municipality in municipalities:
        query = f"{query_base} {municipality.name}".strip()
        announcements = search_bop_burgos_announcements(
            query,
            limit=settings.ordinance_import_search_limit,
        )
        for announcement in announcements:
            if not is_municipal_bopbur_announcement(announcement, municipality.name):
                continue
            if not is_normative_bopbur_announcement(announcement):
                continue
            candidates.append(
                SourceCandidate(
                    url=announcement.pdf_url,
                    municipality_id=municipality.id,
                    official_source_id=official_source.id,
                    title=announcement.title,
                )
            )
    return candidates


def _create_items(
    db: Session,
    job: OrdinanceImportJob,
    candidates: list[SourceCandidate],
) -> list[int]:
    item_ids: list[int] = []
    seen: set[tuple[str, int | None]] = set()
    for candidate in candidates:
        key = (candidate.url, candidate.municipality_id)
        if key in seen:
            continue
        seen.add(key)
        existing = db.scalar(
            select(OrdinanceImportItem).where(
                OrdinanceImportItem.job_id == job.id,
                OrdinanceImportItem.source_url == candidate.url,
                OrdinanceImportItem.municipality_id == candidate.municipality_id,
            )
        )
        if existing is not None:
            item_ids.append(existing.id)
            continue
        item = OrdinanceImportItem(
            job_id=job.id,
            municipality_id=candidate.municipality_id,
            official_source_id=candidate.official_source_id,
            source_url=candidate.url,
            source_title=candidate.title,
        )
        db.add(item)
        db.flush()
        item_ids.append(item.id)
    db.commit()
    return item_ids


def _process_item(
    db: Session,
    item_id: int,
    official_sources: list[OfficialLegalSource],
) -> None:
    item = db.scalar(
        select(OrdinanceImportItem)
        .options(
            selectinload(OrdinanceImportItem.job),
            selectinload(OrdinanceImportItem.municipality),
        )
        .where(OrdinanceImportItem.id == item_id)
    )
    if item is None:
        return
    try:
        if not _url_allowed(item.source_url, official_sources):
            raise ImportSourceError("La URL no pertenece a una fuente oficial permitida.")
        item.status = "fetching"
        db.commit()

        fetched = _fetch_source(item.source_url)
        text = _extract_text(fetched.content, fetched.content_type, item.source_url)
        if len(text.strip()) < 80:
            raise ImportSourceError("No se pudo extraer texto suficiente de la fuente.")

        source_hash = hashlib.sha256(
            f"{item.source_url}\n{text}".encode("utf-8")
        ).hexdigest()
        duplicate = db.scalar(
            select(Ordinance).where(Ordinance.source_hash == source_hash)
        )
        if duplicate is not None:
            item.status = "duplicate"
            item.ordinance_id = duplicate.id
            item.source_hash = source_hash
            item.error_message = "Fuente duplicada de una ordenanza existente."
            db.commit()
            return

        metadata = _extract_metadata(item, text, fetched.content_type)
        ordinance = _create_pending_ordinance(db, item, text, metadata, source_hash)
        item.status = "pending_review"
        item.ordinance_id = ordinance.id
        item.raw_text = text
        item.extracted_metadata_json = json.dumps(metadata, ensure_ascii=False)
        item.source_hash = source_hash
        item.confidence_score = ordinance.confidence_score
        db.commit()

        _create_chunks(db, ordinance, item)
        _create_review_report(db, ordinance, item)
        db.commit()
    except Exception as error:
        db.rollback()
        item = db.get(OrdinanceImportItem, item_id)
        if item is not None:
            item.status = "failed"
            item.error_message = str(error)[:2000]
            db.commit()


@dataclass(frozen=True)
class FetchedSource:
    content: bytes
    content_type: str


def _fetch_source(url: str) -> FetchedSource:
    parsed_url = urlparse.urlsplit(url)
    safe_url = urlparse.urlunsplit(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            urlparse.quote(parsed_url.path, safe="/%"),
            urlparse.quote(parsed_url.query, safe="=&%"),
            parsed_url.fragment,
        )
    )
    request = urlrequest.Request(
        safe_url,
        headers={"User-Agent": "AsistenteAyuntamientos/0.1 ordinance-import"},
        method="GET",
    )
    context = None
    if parsed_url.netloc.endswith("bop.dipsoria.es"):
        # BOP Soria serves official PDF endpoints with a certificate chain that
        # fails Python's default verifier in the import container. Keep this
        # workaround scoped to that official host.
        context = ssl._create_unverified_context()
    try:
        with urlrequest.urlopen(request, timeout=30, context=context) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            chunks: list[bytes] = []
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > settings.ordinance_import_max_fetch_bytes:
                    raise ImportSourceError("La fuente supera el tamaño máximo.")
                chunks.append(chunk)
    except urlerror.HTTPError as error:
        raise ImportSourceError(f"No se pudo descargar la fuente: HTTP {error.code}") from error
    except (urlerror.URLError, TimeoutError) as error:
        raise ImportSourceError("No se pudo descargar la fuente.") from error
    return FetchedSource(content=b"".join(chunks), content_type=content_type)


def _extract_text(content: bytes, content_type: str, url: str) -> str:
    if "pdf" in content_type or url.lower().split("?", 1)[0].endswith(".pdf"):
        try:
            reader = PdfReader(BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as error:
            raise ImportSourceError(
                "El PDF no tiene texto extraíble; requiere OCR o revisión manual."
            ) from error
        text = _normalize_text("\n\n".join(pages))
        if _readable_text_ratio(text) < 0.35:
            raise ImportSourceError(
                "El PDF no tiene texto legible suficiente; requiere OCR o revisión manual."
            )
        return text
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        decoded = content.decode("latin-1", errors="ignore")
    if "html" in content_type or "<html" in decoded.lower():
        parser = _TextHTMLParser()
        parser.feed(decoded)
        return _normalize_text(parser.text())
    return _normalize_text(decoded)


def _extract_metadata(
    item: OrdinanceImportItem,
    text: str,
    content_type: str,
) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = item.source_title or next(
        (line for line in lines[:80] if ORDINANCE_TITLE_RE.search(line)),
        lines[0] if lines else "Ordenanza importada",
    )
    publication_date = _extract_date(text)
    job = item.job
    return {
        "title": title[:MAX_TITLE_CHARS],
        "topic": job.topic or "sin clasificar",
        "subtopic": job.subtopic,
        "summary": _build_summary(text),
        "publication_date": publication_date.isoformat() if publication_date else None,
        "content_type": content_type,
    }


def _create_pending_ordinance(
    db: Session,
    item: OrdinanceImportItem,
    text: str,
    metadata: dict,
    source_hash: str,
) -> Ordinance:
    if item.municipality_id is None:
        raise ImportSourceError("La fuente no tiene municipio asociado.")
    confidence = _estimate_confidence(item, text, metadata)
    ordinance = Ordinance(
        municipality_id=item.municipality_id,
        title=metadata["title"],
        topic=metadata["topic"],
        subtopic=metadata.get("subtopic"),
        ordinance_type="ordinance",
        summary=metadata.get("summary"),
        source_url=item.source_url,
        official_bulletin=item.official_source.name if item.official_source else None,
        publication_date=_date_from_iso(metadata.get("publication_date")),
        status="unknown",
        curation_status="pending_review",
        import_job_id=item.job_id,
        source_hash=source_hash,
        extraction_status="extracted",
        confidence_score=confidence,
        text_content=text,
        notes="Importada automáticamente desde fuente oficial; pendiente de revisión humana.",
        legal_review_notes=None,
        created_by_id=item.job.created_by_id,
        updated_by_id=item.job.created_by_id,
    )
    db.add(ordinance)
    db.flush()
    return ordinance


def _create_chunks(
    db: Session,
    ordinance: Ordinance,
    item: OrdinanceImportItem,
) -> None:
    for chunk in list(
        db.scalars(
            select(OrdinanceLegalChunk).where(
                OrdinanceLegalChunk.ordinance_id == ordinance.id
            )
        )
    ):
        db.delete(chunk)
    chunks = _split_chunks(ordinance.text_content or "")
    for index, text in enumerate(chunks[: settings.ordinance_import_max_chunks]):
        embedding = None
        embedding_model = None
        embedding_status = "pending"
        embedded_at = None
        try:
            embedding, embedding_model, embedding_status = embed_text(text)
            embedded_at = datetime.now(UTC) if embedding_status == "ready" else None
        except EmbeddingsUnavailableError:
            embedding_status = "failed"
            embedding_model = settings.embeddings_model
        db.add(
            OrdinanceLegalChunk(
                ordinance_id=ordinance.id,
                import_item_id=item.id,
                chunk_index=index,
                heading=_chunk_heading(text),
                citation=f"Fragmento {index + 1}",
                text=text,
                source_url=item.source_url,
                source_locator=f"fragmento-{index + 1}",
                review_status="pending_review",
                embedding_model=embedding_model,
                embedding=embedding,
                embedding_status=embedding_status,
                embedded_at=embedded_at,
            )
        )


def _create_review_report(
    db: Session,
    ordinance: Ordinance,
    item: OrdinanceImportItem,
) -> None:
    checklist = _build_checklist(ordinance, item)
    passed = sum(1 for entry in checklist if entry["passed"])
    score = passed / len(checklist)
    if score >= 0.75:
        proposed_decision = "approve"
        doubts = None
    elif score >= 0.45:
        proposed_decision = "needs_changes"
        doubts = "Revisar los puntos no superados antes de aprobar."
    else:
        proposed_decision = "reject"
        doubts = "La extracción no alcanza confianza suficiente."

    report = OrdinanceReviewReport(
        ordinance_id=ordinance.id,
        import_item_id=item.id,
        proposed_decision=proposed_decision,
        confidence_score=round(score, 3),
        checklist_json=json.dumps(checklist, ensure_ascii=False),
        summary=(
            "Revisión automática según los criterios del job. "
            "La decisión final requiere validación humana."
        ),
        doubts=doubts,
        reviewed_by_agent=True,
    )
    ordinance.confidence_score = round(score, 3)
    item.confidence_score = round(score, 3)
    db.add(report)


def _build_checklist(ordinance: Ordinance, item: OrdinanceImportItem) -> list[dict]:
    text = ordinance.text_content or ""
    municipality_name = item.municipality.name if item.municipality else ""
    criteria = item.job.review_criteria.strip()
    return [
        {
            "key": "official_source",
            "label": "La fuente pertenece a un dominio oficial permitido",
            "passed": True,
            "detail": item.source_url,
        },
        {
            "key": "text_extracted",
            "label": "Se ha extraído texto legal suficiente",
            "passed": len(text) >= 500,
            "detail": f"{len(text)} caracteres extraídos",
        },
        {
            "key": "title_found",
            "label": "Se ha identificado un título de ordenanza o norma",
            "passed": bool(ORDINANCE_TITLE_RE.search(ordinance.title)),
            "detail": ordinance.title,
        },
        {
            "key": "municipality_match",
            "label": "El texto o metadatos mencionan el municipio esperado",
            "passed": bool(municipality_name and municipality_name.lower() in text.lower()),
            "detail": municipality_name or None,
        },
        {
            "key": "review_criteria_present",
            "label": "El job incluye criterios de revisión trazables",
            "passed": bool(criteria),
            "detail": criteria[:300],
        },
    ]


def _estimate_confidence(
    item: OrdinanceImportItem,
    text: str,
    metadata: dict,
) -> float:
    score = 0.2
    if item.source_title or ORDINANCE_TITLE_RE.search(metadata.get("title", "")):
        score += 0.2
    if item.municipality and item.municipality.name.lower() in text.lower():
        score += 0.2
    if len(text) >= 500:
        score += 0.2
    if metadata.get("publication_date"):
        score += 0.1
    if item.job.review_criteria:
        score += 0.1
    return min(round(score, 3), 1.0)


def _split_chunks(text: str) -> list[str]:
    article_chunks = _split_article_chunks(text)
    if article_chunks:
        return article_chunks
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= settings.ordinance_chunk_chars:
            current = f"{current}\n\n{paragraph}".strip()
            continue
        if current:
            chunks.append(current)
        current = paragraph
    if current:
        chunks.append(current)
    return chunks or [text[: settings.ordinance_chunk_chars]]


def _split_article_chunks(text: str) -> list[str]:
    marker = re.compile(
        r"(?im)^(?=(art(?:í|i)culo\s+\d+[.º°]?|art\.\s*\d+[.º°]?|"
        r"disposici(?:ó|o)n\s+(?:adicional|transitoria|final|derogatoria)|"
        r"cap(?:í|i)tulo\s+[ivxlcdm\d]+|anexo\b))"
    )
    starts = [match.start() for match in marker.finditer(text)]
    if len(starts) < 2:
        return []
    starts.append(len(text))
    chunks: list[str] = []
    preamble = text[: starts[0]].strip()
    for index in range(len(starts) - 1):
        chunk = text[starts[index] : starts[index + 1]].strip()
        if index == 0 and preamble:
            chunk = f"{preamble}\n\n{chunk}"
        if chunk:
            chunks.extend(_split_oversized_chunk(chunk))
    return chunks


def _split_oversized_chunk(text: str) -> list[str]:
    if len(text) <= settings.ordinance_chunk_chars:
        return [text]
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    if len(paragraphs) <= 1:
        return [
            text[index : index + settings.ordinance_chunk_chars].strip()
            for index in range(0, len(text), settings.ordinance_chunk_chars)
        ]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= settings.ordinance_chunk_chars:
            current = f"{current}\n\n{paragraph}".strip()
            continue
        if current:
            chunks.append(current)
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _chunk_heading(text: str) -> str | None:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:500] if first_line else None


def _url_allowed(url: str, official_sources: list[OfficialLegalSource]) -> bool:
    host = (urlparse.urlparse(url).hostname or "").lower()
    if not host:
        return False
    for source in official_sources:
        domain = source.domain.lower()
        if host == domain or host.endswith(f".{domain}"):
            return True
    return False


def _json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_text(text: str) -> str:
    text = "".join(
        char
        for char in text
        if char in "\n\t" or (ord(char) >= 32 and char != "\x7f")
    )
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
    compact = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", compact).strip()


def _readable_text_ratio(text: str) -> float:
    if not text:
        return 0.0
    readable = sum(
        1
        for char in text
        if char.isalnum() or char.isspace() or char in ".,;:¿?¡!()[]/%€ºª-_'\""
    )
    return readable / len(text)


def _build_summary(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentence.strip() for sentence in sentences[:4] if sentence.strip())
    return (summary or text[:MAX_SUMMARY_CHARS])[:MAX_SUMMARY_CHARS]


def _extract_date(text: str) -> date | None:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            continue
    return None


def _date_from_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)
