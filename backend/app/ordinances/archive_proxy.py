"""Verified client contract for the controlled BOP archive proxy."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from app.core.config import settings


class ArchiveProxyError(Exception):
    pass


@dataclass(frozen=True)
class ArchivedSource:
    content: bytes
    content_type: str
    sha256: str
    fetched_at: str


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_ARCHIVE_PROXY_OPENER = urlrequest.build_opener(_NoRedirectHandler())


def fetch_archived_source(source_url: str) -> ArchivedSource:
    endpoint = _archive_proxy_endpoint()
    api_key = settings.bop_archive_proxy_api_key
    signing_key = settings.bop_archive_proxy_signing_key
    if not api_key or not signing_key:
        raise ArchiveProxyError("El proxy de archivo BOP no está configurado.")

    request = urlrequest.Request(
        endpoint,
        data=json.dumps(
            {"source_url": source_url},
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _ARCHIVE_PROXY_OPENER.open(
            request,
            timeout=settings.bop_archive_proxy_timeout_seconds,
        ) as response:
            digest = (response.headers.get("x-archive-sha256") or "").lower()
            fetched_at = response.headers.get("x-archive-fetched-at") or ""
            content_type = response.headers.get("x-archive-content-type") or ""
            signature = (response.headers.get("x-archive-signature") or "").lower()
            content = _read_limited(response)
    except urlerror.HTTPError as error:
        raise ArchiveProxyError(
            f"El proxy de archivo BOP respondió HTTP {error.code}."
        ) from error
    except (urlerror.URLError, TimeoutError) as error:
        raise ArchiveProxyError("No se pudo contactar con el proxy BOP.") from error

    _validate_archive_metadata(
        source_url=source_url,
        content=content,
        content_type=content_type,
        digest=digest,
        fetched_at=fetched_at,
        signature=signature,
        signing_key=signing_key,
    )
    return ArchivedSource(
        content=content,
        content_type=content_type,
        sha256=digest,
        fetched_at=fetched_at,
    )


def canonical_archive_manifest(
    *,
    source_url: str,
    fetched_at: str,
    content_type: str,
    digest: str,
) -> bytes:
    return json.dumps(
        {
            "content_type": content_type,
            "fetched_at": fetched_at,
            "sha256": digest,
            "source_url": source_url,
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sign_archive_manifest(
    *,
    source_url: str,
    fetched_at: str,
    content_type: str,
    digest: str,
    signing_key: str,
) -> str:
    manifest = canonical_archive_manifest(
        source_url=source_url,
        fetched_at=fetched_at,
        content_type=content_type,
        digest=digest,
    )
    return hmac.new(
        signing_key.encode("utf-8"),
        manifest,
        hashlib.sha256,
    ).hexdigest()


def _archive_proxy_endpoint() -> str:
    base_url = (settings.bop_archive_proxy_base_url or "").rstrip("/")
    try:
        parsed = urlparse.urlsplit(base_url)
        parsed.port
    except ValueError as error:
        raise ArchiveProxyError("La URL del proxy BOP no es válida.") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ArchiveProxyError("La URL del proxy BOP no es válida.")
    if settings.environment == "production" and parsed.scheme != "https":
        raise ArchiveProxyError("El proxy BOP debe usar HTTPS en producción.")
    return f"{base_url}/v1/archive"


def _read_limited(response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := response.read(1024 * 1024):
        size += len(chunk)
        if size > settings.ordinance_import_max_fetch_bytes:
            raise ArchiveProxyError("La respuesta del proxy BOP supera el límite.")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_archive_metadata(
    *,
    source_url: str,
    content: bytes,
    content_type: str,
    digest: str,
    fetched_at: str,
    signature: str,
    signing_key: str,
) -> None:
    actual_digest = hashlib.sha256(content).hexdigest()
    if not digest or not hmac.compare_digest(digest, actual_digest):
        raise ArchiveProxyError("El contenido archivado no coincide con su SHA-256.")
    try:
        parsed_fetched_at = datetime.fromisoformat(fetched_at)
    except ValueError as error:
        raise ArchiveProxyError("El manifiesto BOP no tiene fecha válida.") from error
    if parsed_fetched_at.tzinfo is None or not content_type:
        raise ArchiveProxyError("El manifiesto BOP está incompleto.")
    expected_signature = sign_archive_manifest(
        source_url=source_url,
        fetched_at=fetched_at,
        content_type=content_type,
        digest=digest,
        signing_key=signing_key,
    )
    if not signature or not hmac.compare_digest(signature, expected_signature):
        raise ArchiveProxyError("La firma del archivo BOP no es válida.")
