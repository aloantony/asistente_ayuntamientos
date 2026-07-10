import hashlib
import http.client
import ipaddress
import json
import re
import socket
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


def validate_source_url(
    url: str,
    *,
    allowed_domains: set[str] | None = None,
    require_https: bool = True,
    resolve_dns: bool = True,
) -> str:
    """Validate an outbound ordinance URL before any network access."""
    try:
        parsed = urlparse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ImportSourceError("La URL de la fuente no es válida.") from error

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ImportSourceError("La URL de la fuente debe usar HTTP o HTTPS.")
    if require_https and scheme != "https":
        raise ImportSourceError("La fuente oficial debe usar HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise ImportSourceError("La URL de la fuente no puede incluir credenciales.")
    if parsed.hostname is None:
        raise ImportSourceError("La URL de la fuente debe incluir un dominio.")

    host = _normalize_hostname(parsed.hostname)
    if _is_local_hostname(host):
        raise ImportSourceError("La URL de la fuente apunta a una red no permitida.")

    if allowed_domains is not None:
        normalized_domains = {_normalize_hostname(domain) for domain in allowed_domains}
        if not any(
            host == domain or host.endswith(f".{domain}")
            for domain in normalized_domains
        ):
            raise ImportSourceError(
                "La URL no pertenece a una fuente oficial permitida."
            )

    literal_address = _parse_ip_address(host)
    if literal_address is not None:
        _ensure_public_address(literal_address)
    elif resolve_dns:
        _validated_host_addresses(
            host,
            port or (443 if scheme == "https" else 80),
        )

    return url


def validate_official_source_configuration(base_url: str, domain: str) -> str:
    """Validate and normalize a superuser-configured official source."""
    normalized_domain = _normalize_official_domain(domain)
    allow_legacy_bop = (
        normalized_domain == BOP_BURGOS_DOMAIN
        and _is_legacy_bop_url(base_url)
        and bop_archive_proxy_enabled()
    )
    validate_source_url(
        base_url,
        allowed_domains={normalized_domain},
        require_https=not allow_legacy_bop,
        resolve_dns=False,
    )
    return normalized_domain


def source_url_allowed(
    url: str,
    official_sources: list[OfficialLegalSource],
) -> bool:
    try:
        allow_legacy_bop = _is_legacy_bop_url(url) and bop_archive_proxy_enabled()
        validate_source_url(
            url,
            allowed_domains={source.domain for source in official_sources},
            require_https=not allow_legacy_bop,
            resolve_dns=False,
        )
        if allow_legacy_bop:
            _ensure_exact_source_domain(
                url,
                {source.domain for source in official_sources},
            )
    except ImportSourceError:
        return False
    return True


def bop_archive_proxy_enabled() -> bool:
    return bool(
        settings.bop_archive_proxy_base_url
        and settings.bop_archive_proxy_api_key
        and settings.bop_archive_proxy_signing_key
    )


def _is_legacy_bop_url(url: str) -> bool:
    try:
        parsed = urlparse.urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "http"
        and _normalize_hostname(parsed.hostname or "") == BOP_BURGOS_DOMAIN
    )


def _normalize_official_domain(domain: str) -> str:
    raw_domain = domain.strip()
    if not raw_domain or any(character in raw_domain for character in "/@?#"):
        raise ImportSourceError("El dominio de la fuente oficial no es válido.")
    normalized = _normalize_hostname(raw_domain)
    if ":" in raw_domain or _parse_ip_address(normalized) is not None:
        raise ImportSourceError(
            "El dominio de la fuente oficial debe ser un nombre DNS público."
        )
    if _is_local_hostname(normalized):
        raise ImportSourceError("El dominio de la fuente oficial no es público.")
    labels = normalized.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        raise ImportSourceError("El dominio de la fuente oficial no es válido.")
    return normalized


def _normalize_hostname(host: str) -> str:
    normalized = host.strip().rstrip(".")
    try:
        return normalized.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ImportSourceError("El dominio de la fuente no es válido.") from error


def _is_local_hostname(host: str) -> bool:
    return host == "localhost" or host.endswith(
        (".localhost", ".local", ".internal", ".home.arpa")
    )


def _parse_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _ensure_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    if not address.is_global:
        raise ImportSourceError("La URL de la fuente apunta a una red no permitida.")


def _resolve_host_addresses(host: str, port: int) -> set[str]:
    try:
        return {
            address[4][0]
            for address in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as error:
        raise ImportSourceError("No se pudo resolver el dominio de la fuente.") from error


def _validated_host_addresses(host: str, port: int) -> tuple[str, ...]:
    literal_address = _parse_ip_address(host)
    if literal_address is not None:
        _ensure_public_address(literal_address)
        return (str(literal_address),)

    addresses = _resolve_host_addresses(host, port)
    if not addresses:
        raise ImportSourceError("No se pudo resolver el dominio de la fuente.")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise ImportSourceError(
                "El dominio de la fuente devolvió una dirección no válida."
            ) from error
        _ensure_public_address(parsed_address)
    return tuple(sorted(addresses))


def _connect_to_resolved_addresses(
    addresses: tuple[str, ...],
    port: int,
    timeout: float | object,
    source_address: tuple[str, int] | None,
):
    last_error: OSError | None = None
    for address in addresses:
        parsed_address = ipaddress.ip_address(address)
        family = socket.AF_INET6 if parsed_address.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            destination = (
                (address, port, 0, 0)
                if family == socket.AF_INET6
                else (address, port)
            )
            sock.connect(destination)
            return sock
        except OSError as error:
            last_error = error
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("No public source addresses are available")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, resolved_addresses: tuple[str, ...], **kwargs):
        self._resolved_addresses = resolved_addresses
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        self.sock = _connect_to_resolved_addresses(
            self._resolved_addresses,
            self.port,
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, resolved_addresses: tuple[str, ...], **kwargs):
        self._resolved_addresses = resolved_addresses
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        self.sock = _connect_to_resolved_addresses(
            self._resolved_addresses,
            self.port,
            self.timeout,
            self.source_address,
        )
        server_hostname = self.host
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
        )


class _PinnedHTTPHandler(urlrequest.HTTPHandler):
    def __init__(self, allowed_domains: set[str], *, require_https: bool):
        super().__init__()
        self.allowed_domains = allowed_domains
        self.require_https = require_https

    def http_open(self, req):
        addresses = _connection_addresses(
            req.full_url,
            self.allowed_domains,
            require_https=self.require_https,
        )

        def connection_factory(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
            return _PinnedHTTPConnection(
                host,
                timeout=timeout,
                resolved_addresses=addresses,
                **kwargs,
            )

        return self.do_open(connection_factory, req)


class _PinnedHTTPSHandler(urlrequest.HTTPSHandler):
    def __init__(self, allowed_domains: set[str], *, require_https: bool):
        super().__init__()
        self.allowed_domains = allowed_domains
        self.require_https = require_https

    def https_open(self, req):
        addresses = _connection_addresses(
            req.full_url,
            self.allowed_domains,
            require_https=self.require_https,
        )

        def connection_factory(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
            return _PinnedHTTPSConnection(
                host,
                timeout=timeout,
                resolved_addresses=addresses,
                **kwargs,
            )

        return self.do_open(
            connection_factory,
            req,
            context=self._context,
        )


def _connection_addresses(
    url: str,
    allowed_domains: set[str],
    *,
    require_https: bool = True,
) -> tuple[str, ...]:
    validate_source_url(
        url,
        allowed_domains=allowed_domains,
        require_https=require_https,
        resolve_dns=False,
    )
    parsed = urlparse.urlsplit(url)
    host = _normalize_hostname(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return _validated_host_addresses(host, port)


class _SafeRedirectHandler(urlrequest.HTTPRedirectHandler):
    def __init__(
        self,
        allowed_domains: set[str],
        *,
        require_https: bool,
        exact_domains: bool,
    ):
        super().__init__()
        self.allowed_domains = allowed_domains
        self.require_https = require_https
        self.exact_domains = exact_domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirect_url = urlparse.urljoin(req.full_url, newurl)
        validate_source_url(
            redirect_url,
            allowed_domains=self.allowed_domains,
            require_https=self.require_https,
        )
        if self.exact_domains:
            _ensure_exact_source_domain(redirect_url, self.allowed_domains)
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            redirect_url,
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
                if url and source_url_allowed(url, official_sources):
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
            fetch_html=_fetch_bop_burgos_html,
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


def _fetch_bop_burgos_html(url: str) -> str:
    fetched = _fetch_source(url, allowed_domains={BOP_BURGOS_DOMAIN})
    try:
        return fetched.content.decode("utf-8")
    except UnicodeDecodeError:
        return fetched.content.decode("latin-1", errors="ignore")


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
        if not source_url_allowed(item.source_url, official_sources):
            raise ImportSourceError("La URL no pertenece a una fuente oficial permitida.")
        item.status = "fetching"
        db.commit()

        fetched = _fetch_source(
            item.source_url,
            allowed_domains={source.domain for source in official_sources},
        )
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

        _replace_chunks(
            db,
            ordinance,
            import_item_id=item.id,
            source_url=item.source_url,
        )
        _create_review_report(db, ordinance, item)
        db.commit()
    except Exception as error:
        item.status = "failed"
        item.error_message = str(error)[:2000]
        db.commit()


@dataclass(frozen=True)
class FetchedSource:
    content: bytes
    content_type: str


def _fetch_source(
    url: str,
    *,
    allowed_domains: set[str] | None = None,
) -> FetchedSource:
    if _is_legacy_bop_url(url):
        effective_domains = (
            allowed_domains if allowed_domains is not None else {BOP_BURGOS_DOMAIN}
        )
        validate_source_url(
            url,
            allowed_domains=effective_domains,
            require_https=False,
            resolve_dns=False,
        )
        _ensure_exact_source_domain(url, effective_domains)
        if not bop_archive_proxy_enabled():
            raise ImportSourceError(
                "La fuente BOP legacy requiere el proxy de archivo seguro."
            )
        from app.ordinances.archive_proxy import (
            ArchiveProxyError,
            fetch_archived_source,
        )

        try:
            archived = fetch_archived_source(url)
        except ArchiveProxyError as error:
            raise ImportSourceError(str(error)) from error
        return FetchedSource(
            content=archived.content,
            content_type=archived.content_type,
        )

    return _fetch_direct_source(
        url,
        allowed_domains=allowed_domains,
        require_https=True,
    )


def _fetch_direct_source(
    url: str,
    *,
    allowed_domains: set[str] | None = None,
    require_https: bool,
    exact_domains: bool = False,
) -> FetchedSource:
    initial_host = _normalize_hostname(urlparse.urlsplit(url).hostname or "")
    effective_domains = (
        allowed_domains if allowed_domains is not None else {initial_host}
    )
    validate_source_url(
        url,
        allowed_domains=effective_domains,
        require_https=require_https,
        resolve_dns=False,
    )
    if exact_domains:
        _ensure_exact_source_domain(url, effective_domains)
    request = urlrequest.Request(
        url,
        headers={"User-Agent": "AsistenteAyuntamientos/0.1 ordinance-import"},
        method="GET",
    )
    opener = urlrequest.build_opener(
        urlrequest.ProxyHandler({}),
        _PinnedHTTPHandler(effective_domains, require_https=require_https),
        _PinnedHTTPSHandler(effective_domains, require_https=require_https),
        _SafeRedirectHandler(
            effective_domains,
            require_https=require_https,
            exact_domains=exact_domains,
        ),
    )
    try:
        with opener.open(request, timeout=30) as response:
            validate_source_url(
                response.geturl(),
                allowed_domains=effective_domains,
                require_https=require_https,
            )
            if exact_domains:
                _ensure_exact_source_domain(
                    response.geturl(),
                    effective_domains,
                )
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


def _ensure_exact_source_domain(url: str, allowed_domains: set[str]) -> None:
    try:
        parsed = urlparse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ImportSourceError("La URL de la fuente no es válida.") from error
    host = _normalize_hostname(parsed.hostname or "")
    normalized_domains = {
        _normalize_official_domain(domain) for domain in allowed_domains
    }
    if host not in normalized_domains:
        raise ImportSourceError(
            "La URL no pertenece al host oficial exacto permitido."
        )
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if (port is not None and port != default_port) or parsed.fragment:
        raise ImportSourceError(
            "La URL usa un puerto o fragmento no permitido para la fuente oficial."
        )


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


def rebuild_ordinance_chunks(db: Session, ordinance: Ordinance) -> None:
    import_item_id = db.scalar(
        select(OrdinanceImportItem.id)
        .where(OrdinanceImportItem.ordinance_id == ordinance.id)
        .order_by(OrdinanceImportItem.id.desc())
        .limit(1)
    )
    _replace_chunks(
        db,
        ordinance,
        import_item_id=import_item_id,
        source_url=ordinance.source_url,
    )


def _replace_chunks(
    db: Session,
    ordinance: Ordinance,
    *,
    import_item_id: int | None,
    source_url: str | None,
) -> None:
    for chunk in list(
        db.scalars(
            select(OrdinanceLegalChunk).where(
                OrdinanceLegalChunk.ordinance_id == ordinance.id
            )
        )
    ):
        db.delete(chunk)
    db.flush()
    text_content = (ordinance.text_content or "").strip()
    if not text_content:
        return
    chunks = _split_chunks(text_content)
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
                import_item_id=import_item_id,
                chunk_index=index,
                heading=_chunk_heading(text),
                citation=f"Fragmento {index + 1}",
                text=text,
                source_url=source_url,
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
