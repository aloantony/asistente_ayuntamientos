import hashlib
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from datetime import UTC, date, datetime
from http import client as http_client
from html.parser import HTMLParser
from io import BytesIO
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from pypdf import PdfReader
from rq import Retry
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.assistant.web_search import WebSearchUnavailableError, web_search_client
from app.core.config import settings
from app.core.jobs import get_default_queue
from app.db.session import SessionLocal
from app.municipalities.models import Municipality
from app.ordinances.bop_burgos import (
    BOP_BURGOS_DOMAIN,
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
ALLOWED_SOURCE_SCHEMES = {"http", "https"}
SOURCE_REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_SOURCE_REDIRECTS = 5
NAT64_TRANSLATION_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
logger = logging.getLogger(__name__)
STABLE_IMPORT_ITEM_STATUSES = {
    "pending_review",
    "approved",
    "rejected",
    "duplicate",
}


@dataclass(frozen=True)
class SourceCandidate:
    url: str
    municipality_id: int | None = None
    official_source_id: int | None = None
    title: str | None = None


class ImportSourceError(Exception):
    pass


def _canonical_domain(value: str) -> str:
    domain = value.strip().rstrip(".").lower()
    if not domain or any(character.isspace() for character in domain):
        raise ImportSourceError("El dominio de la fuente oficial no es válido.")
    try:
        canonical = domain.encode("idna").decode("ascii")
        ipaddress.ip_address(canonical)
    except UnicodeError as error:
        raise ImportSourceError(
            "El dominio de la fuente oficial no es válido."
        ) from error
    except ValueError:
        pass
    else:
        raise ImportSourceError("Una fuente oficial no puede usar una IP como dominio.")
    labels = canonical.split(".")
    if (
        len(labels) < 2
        or canonical == "localhost"
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        )
    ):
        raise ImportSourceError("El dominio de la fuente oficial no es válido.")
    return canonical


def _validated_source_url_host(url: str) -> str:
    if not url or "\\" in url or any(ord(character) < 32 for character in url):
        raise ImportSourceError("La URL de la fuente oficial no es válida.")
    try:
        parsed = urlparse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ImportSourceError("La URL de la fuente oficial no es válida.") from error
    if (
        parsed.scheme.lower() not in ALLOWED_SOURCE_SCHEMES
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ImportSourceError("La URL de la fuente oficial no es válida.")
    expected_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != expected_port:
        raise ImportSourceError("La URL de la fuente oficial usa un puerto no permitido.")
    return _canonical_domain(parsed.hostname)


def _require_official_source_url(
    url: str,
    sources: list[OfficialLegalSource],
) -> None:
    host = _validated_source_url_host(url)
    for source in sources:
        try:
            domain = _canonical_domain(source.domain)
        except ImportSourceError:
            continue
        if host == domain or host.endswith(f".{domain}"):
            return
    raise ImportSourceError("La URL no pertenece a una fuente oficial permitida.")


def is_official_source_url(
    url: str,
    sources: list[OfficialLegalSource],
) -> bool:
    try:
        _require_official_source_url(url, sources)
    except ImportSourceError:
        return False
    return True


def is_valid_official_source_definition(base_url: str, domain: str) -> bool:
    try:
        canonical = _canonical_domain(domain)
        host = _validated_source_url_host(base_url)
    except ImportSourceError:
        return False
    return host == canonical or host.endswith(f".{canonical}")


def _require_public_unicast_ip(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ImportSourceError("La fuente oficial resolvió a una IP no válida.") from error
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if (
        not address.is_global
        or address.is_multicast
        or getattr(address, "is_site_local", False)
        or getattr(address, "sixtofour", None) is not None
        or getattr(address, "teredo", None) is not None
        or any(address in prefix for prefix in NAT64_TRANSLATION_PREFIXES)
    ):
        raise ImportSourceError(
            "La fuente oficial resolvió a una dirección de red no pública."
        )


def _create_public_connection(
    address,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
):
    host, port = address
    try:
        candidates = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise ImportSourceError("La fuente oficial no se pudo resolver.") from error
    if not candidates:
        raise ImportSourceError("La fuente oficial no se pudo resolver.")

    for _family, _socktype, _proto, _canonname, sockaddr in candidates:
        _require_public_unicast_ip(sockaddr[0])

    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in candidates:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            last_error = error
            if sock is not None:
                sock.close()
    raise ImportSourceError("No se pudo conectar con la fuente oficial.") from last_error


class _PinnedHTTPConnection(http_client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_public_connection


class _PinnedHTTPSConnection(http_client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_public_connection


class _PinnedHTTPHandler(urlrequest.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PinnedHTTPConnection, request)


class _PinnedHTTPSHandler(urlrequest.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PinnedHTTPSConnection, request)


class _RejectRedirects(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_SOURCE_OPENER = urlrequest.build_opener(
    urlrequest.ProxyHandler({}),
    _RejectRedirects(),
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(),
)


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
        try:
            candidates.extend(
                _discover_candidates(job, official_sources, municipalities)
            )
        except Exception:
            if not candidates:
                raise
            # A best-effort discovery failure must not discard explicit,
            # already validated seed URLs supplied by the operator.
            logger.warning(
                "Ordinance discovery failed; continuing with seed URLs",
                extra={"job_id": job.id},
                exc_info=True,
            )

        if not candidates:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            if not job.error_message:
                job.error_message = (
                    "No se encontraron fuentes oficiales. Añade URLs semilla o "
                    "configura el proveedor de búsqueda web oficial."
                )
            db.commit()
            return

        # A valid seed/discovered candidate makes a partial discovery warning
        # non-terminal; individual source failures remain on their import item.
        job.error_message = None

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
    if not web_search_client.enabled:
        return []

    candidates: list[SourceCandidate] = []
    query_base = " ".join(
        part for part in [job.search_query, job.topic, job.subtopic, "ordenanza"] if part
    )
    for municipality in municipalities:
        for source in official_sources:
            query = f"{query_base} {municipality.name} site:{source.domain}"
            try:
                results = web_search_client.search(
                    query=query,
                    limit=settings.ordinance_import_search_limit,
                )
            except WebSearchUnavailableError as error:
                if not candidates:
                    job.error_message = f"Proveedor de búsqueda no disponible: {error}"
                return candidates
            except ValueError as error:
                if not candidates:
                    job.error_message = f"Consulta de búsqueda no válida: {error}"
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
            fetch_content=lambda url: _fetch_source(
                url,
                [official_source],
            ).content,
            limit=settings.ordinance_import_search_limit,
        )
        for announcement in announcements:
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
    if item.ordinance_id is not None and item.status in STABLE_IMPORT_ITEM_STATUSES:
        # Re-running a completed or partially completed job must not fetch and
        # classify its own ordinance as a duplicate. Pending/approved records
        # may still need idempotent embedding recovery after a queue outage.
        if item.status in {"pending_review", "approved"}:
            dispatch_ordinance_embeddings(db, item.ordinance_id)
        return
    try:
        item_sources = (
            [
                source
                for source in official_sources
                if source.id == item.official_source_id
            ]
            if item.official_source_id is not None
            else official_sources
        )
        if not is_official_source_url(item.source_url, item_sources):
            raise ImportSourceError("La URL no pertenece a una fuente oficial permitida.")
        item.status = "fetching"
        db.commit()

        fetched = _fetch_source(item.source_url, item_sources)
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
        _create_chunks(db, ordinance, item)
        create_review_report(db, ordinance, item)
        db.commit()
        dispatch_ordinance_embeddings(db, ordinance.id)
    except Exception as error:
        db.rollback()
        item = db.get(OrdinanceImportItem, item_id)
        if item is None:
            raise
        item.status = "failed"
        item.error_message = str(error)[:2000]
        db.commit()


@dataclass(frozen=True)
class FetchedSource:
    content: bytes
    content_type: str


def _fetch_source(
    url: str,
    official_sources: list[OfficialLegalSource],
) -> FetchedSource:
    current_url = url
    for redirect_count in range(MAX_SOURCE_REDIRECTS + 1):
        _require_official_source_url(current_url, official_sources)
        request = urlrequest.Request(
            current_url,
            headers={"User-Agent": "AsistenteAyuntamientos/0.1 ordinance-import"},
            method="GET",
        )
        try:
            response = _SOURCE_OPENER.open(request, timeout=30)
        except urlerror.HTTPError as error:
            if error.code not in SOURCE_REDIRECT_CODES:
                raise ImportSourceError(
                    f"No se pudo descargar la fuente: HTTP {error.code}"
                ) from error
            location = error.headers.get("location")
            error.close()
            if not location:
                raise ImportSourceError(
                    "La fuente devolvió una redirección sin destino."
                )
            if redirect_count >= MAX_SOURCE_REDIRECTS:
                raise ImportSourceError("La fuente supera el máximo de redirecciones.")
            next_url = urlparse.urljoin(current_url, location)
            _require_official_source_url(next_url, official_sources)
            current_url = next_url
            continue
        except (urlerror.URLError, TimeoutError, OSError) as error:
            raise ImportSourceError("No se pudo descargar la fuente.") from error

        with response:
            content_type = (response.headers.get("content-type") or "").lower()
            chunks: list[bytes] = []
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > settings.ordinance_import_max_fetch_bytes:
                    raise ImportSourceError("La fuente supera el tamaño máximo.")
                chunks.append(chunk)
        return FetchedSource(content=b"".join(chunks), content_type=content_type)
    raise ImportSourceError("La fuente supera el máximo de redirecciones.")


def _extract_text(content: bytes, content_type: str, url: str) -> str:
    if "pdf" in content_type or url.lower().split("?", 1)[0].endswith(".pdf"):
        try:
            reader = PdfReader(BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as error:
            raise ImportSourceError(
                "El PDF no tiene texto extraíble; requiere OCR o revisión manual."
            ) from error
        return _normalize_text("\n\n".join(pages))
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
    rebuild_ordinance_chunks(
        db,
        ordinance,
        import_item=item,
        generate_embeddings=False,
    )


def rebuild_ordinance_chunks(
    db: Session,
    ordinance: Ordinance,
    *,
    import_item: OrdinanceImportItem | None = None,
    review_status: str = "pending_review",
    generate_embeddings: bool = True,
) -> None:
    """Replace derived chunks after legal text changes.

    Chunks are never kept approved across a text rewrite unless an authorized
    reviewer is creating an already-reviewed manual record explicitly.
    """

    text_content = (ordinance.text_content or "").strip()
    chunks = _split_chunks(text_content) if text_content else []
    if len(chunks) > settings.ordinance_import_max_chunks:
        raise ImportSourceError(
            "El texto de la ordenanza supera el máximo de fragmentos buscables."
        )

    for chunk in list(
        db.scalars(
            select(OrdinanceLegalChunk).where(
                OrdinanceLegalChunk.ordinance_id == ordinance.id
            )
        )
    ):
        db.delete(chunk)
    db.flush()
    if not text_content:
        return
    for index, text in enumerate(chunks):
        embedding = None
        embedding_model = settings.embeddings_model
        embedding_status = (
            "disabled" if settings.embeddings_runtime == "disabled" else "pending"
        )
        embedded_at = None
        if generate_embeddings:
            try:
                embedding, embedding_model, embedding_status = embed_text(text)
                embedded_at = (
                    datetime.now(UTC) if embedding_status == "ready" else None
                )
            except EmbeddingsUnavailableError:
                embedding_status = "failed"
        db.add(
            OrdinanceLegalChunk(
                ordinance_id=ordinance.id,
                import_item_id=import_item.id if import_item else None,
                chunk_index=index,
                heading=_chunk_heading(text),
                citation=f"Fragmento {index + 1}",
                text=text,
                source_url=ordinance.source_url,
                source_locator=f"fragmento-{index + 1}",
                review_status=review_status,
                embedding_model=embedding_model,
                embedding=embedding,
                embedding_status=embedding_status,
                embedded_at=embedded_at,
            )
        )


def embed_ordinance_chunks(
    ordinance_id: int,
    db: Session | None = None,
) -> dict[str, int]:
    """Generate pending embeddings without holding a database transaction.

    HTTP mutations enqueue this entrypoint for remote providers. The local
    deterministic runtime may invoke it inline after the ordinance transaction
    has committed.
    """

    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        pending = list(
            db.execute(
                select(
                    OrdinanceLegalChunk.id,
                    OrdinanceLegalChunk.text,
                )
                .where(
                    OrdinanceLegalChunk.ordinance_id == ordinance_id,
                    OrdinanceLegalChunk.embedding_status.in_(("pending", "failed")),
                )
                .order_by(OrdinanceLegalChunk.chunk_index)
            )
        )
        # End the read transaction before a potentially slow provider call.
        db.commit()

        ready = 0
        failed = 0
        skipped = 0
        for chunk_id, expected_text in pending:
            try:
                embedding, embedding_model, embedding_status = embed_text(expected_text)
            except EmbeddingsUnavailableError:
                embedding = None
                embedding_model = settings.embeddings_model
                embedding_status = "failed"

            chunk = db.get(OrdinanceLegalChunk, chunk_id)
            if (
                chunk is None
                or chunk.ordinance_id != ordinance_id
                or chunk.text != expected_text
                or chunk.embedding_status not in {"pending", "failed"}
            ):
                db.rollback()
                skipped += 1
                continue
            chunk.embedding = embedding
            chunk.embedding_model = embedding_model
            chunk.embedding_status = embedding_status
            chunk.embedded_at = (
                datetime.now(UTC) if embedding_status == "ready" else None
            )
            db.commit()
            if embedding_status == "ready":
                ready += 1
            elif embedding_status == "failed":
                failed += 1
            else:
                skipped += 1
        return {"ready": ready, "failed": failed, "skipped": skipped}
    finally:
        if owns_session:
            db.close()


def embed_ordinance_chunk(
    chunk_id: int,
    db: Session | None = None,
) -> str:
    """Embed one chunk as an idempotent, bounded RQ unit of work."""

    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        row = db.execute(
            select(
                OrdinanceLegalChunk.text,
                OrdinanceLegalChunk.embedding_status,
            ).where(OrdinanceLegalChunk.id == chunk_id)
        ).one_or_none()
        if row is None or row.embedding_status not in {"pending", "failed"}:
            db.rollback()
            return "skipped"
        expected_text = row.text
        db.commit()

        provider_error: EmbeddingsUnavailableError | None = None
        try:
            embedding, embedding_model, embedding_status = embed_text(expected_text)
        except EmbeddingsUnavailableError as error:
            embedding = None
            embedding_model = settings.embeddings_model
            embedding_status = "failed"
            provider_error = error

        chunk = db.get(OrdinanceLegalChunk, chunk_id)
        if (
            chunk is None
            or chunk.text != expected_text
            or chunk.embedding_status not in {"pending", "failed"}
        ):
            db.rollback()
            return "skipped"
        chunk.embedding = embedding
        chunk.embedding_model = embedding_model
        chunk.embedding_status = embedding_status
        chunk.embedded_at = (
            datetime.now(UTC) if embedding_status == "ready" else None
        )
        db.commit()
        if provider_error is not None:
            raise provider_error
        return embedding_status
    finally:
        if owns_session:
            db.close()


def dispatch_ordinance_embeddings(
    db: Session,
    ordinance_id: int,
    *,
    embedding_statuses: tuple[str, ...] = ("pending", "failed"),
) -> dict:
    """Run cheap local embeddings inline and queue network-backed providers."""

    if settings.embeddings_runtime != "openai_compatible":
        try:
            result = embed_ordinance_chunks(ordinance_id, db=db)
        except Exception:  # pragma: no cover - defensive local runtime boundary
            db.rollback()
            logger.exception(
                "Could not generate local ordinance embeddings",
                extra={"ordinance_id": ordinance_id},
            )
            return {"requested": 0, "queued": 0, "queue_failed": 0}
        return {
            "requested": result["ready"] + result["failed"] + result["skipped"],
            "queued": 0,
            "queue_failed": 0,
        }

    chunk_ids = list(
        db.scalars(
            select(OrdinanceLegalChunk.id)
            .where(
                OrdinanceLegalChunk.ordinance_id == ordinance_id,
                OrdinanceLegalChunk.embedding_status.in_(embedding_statuses),
            )
            .order_by(OrdinanceLegalChunk.chunk_index)
        )
    )
    db.commit()
    queued = 0
    try:
        queue = get_default_queue()
        for chunk_id in chunk_ids:
            queue.enqueue(
                embed_ordinance_chunk,
                chunk_id,
                job_timeout=int(
                    max(90, settings.embeddings_timeout_seconds + 30)
                ),
                retry=Retry(max=3),
            )
            queued += 1
    except Exception:  # pragma: no cover - depends on external Redis availability
        # Pending chunks remain outside the recoverable corpus and a later
        # dispatch can safely enqueue them again; chunk workers are idempotent.
        logger.exception(
            "Could not enqueue all ordinance embeddings",
            extra={
                "ordinance_id": ordinance_id,
                "queued": queued,
                "requested": len(chunk_ids),
            },
        )
    return {
        "requested": len(chunk_ids),
        "queued": queued,
        "queue_failed": len(chunk_ids) - queued,
    }


def create_review_report(
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
    return _split_oversized_chunk(text)


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
    limit = settings.ordinance_chunk_chars
    paragraphs = [
        paragraph.strip()
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    ]
    pieces = [
        piece.strip()
        for paragraph in paragraphs
        for piece in (
            [paragraph]
            if len(paragraph) <= limit
            else [
                paragraph[index : index + limit]
                for index in range(0, len(paragraph), limit)
            ]
        )
        if piece.strip()
    ]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if len(current) + len(piece) + 2 <= limit:
            current = f"{current}\n\n{piece}".strip()
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks or ([text[:limit]] if text else [])


def _chunk_heading(text: str) -> str | None:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:500] if first_line else None


def _url_allowed(url: str, official_sources: list[OfficialLegalSource]) -> bool:
    return is_official_source_url(url, official_sources)


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
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
    compact = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", compact).strip()


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
