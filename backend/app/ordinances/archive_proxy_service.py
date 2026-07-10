"""Standalone, authenticated archive proxy for the legacy BOP Burgos host."""

import fcntl
import hashlib
import hmac
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib import parse as urlparse

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.ordinances.archive_proxy import sign_archive_manifest
from app.ordinances.bop_burgos import BOP_BURGOS_DOMAIN
from app.ordinances.import_service import (
    ImportSourceError,
    _fetch_direct_source,
    validate_source_url,
)

app = FastAPI(title="BOP Burgos controlled archive proxy", version="1")


class ArchiveRequest(BaseModel):
    source_url: str = Field(min_length=1, max_length=2000)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if _service_configured() else "not_configured",
        "archive": "bop_burgos",
    }


@app.post("/v1/archive")
def archive_bop_source(
    payload: ArchiveRequest,
    authorization: str | None = Header(default=None),
) -> Response:
    _require_service_configuration()
    _authenticate(authorization)
    source_url = _validate_bop_source_url(payload.source_url)

    url_key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    with _archive_lock(url_key):
        record = None
        if not _source_requires_refresh(source_url):
            record = _load_latest_record(url_key, source_url)
        if record is None:
            record = _fetch_and_archive(source_url, url_key)
        content = _load_archived_content(record)

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "X-Archive-Content-Type": record["content_type"],
            "X-Archive-Fetched-At": record["fetched_at"],
            "X-Archive-SHA256": record["sha256"],
            "X-Archive-Signature": record["signature"],
            "X-Content-Type-Options": "nosniff",
        },
    )


def _service_configured() -> bool:
    return bool(
        settings.bop_archive_proxy_api_key
        and settings.bop_archive_proxy_signing_key
    )


def _require_service_configuration() -> None:
    if not _service_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Archive proxy is not configured",
        )


def _authenticate(authorization: str | None) -> None:
    expected = f"Bearer {settings.bop_archive_proxy_api_key or ''}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid archive proxy credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _validate_bop_source_url(source_url: str) -> str:
    try:
        validate_source_url(
            source_url,
            allowed_domains={BOP_BURGOS_DOMAIN},
            require_https=False,
            resolve_dns=False,
        )
        parsed = urlparse.urlsplit(source_url)
    except (ImportSourceError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid BOP source URL",
        ) from error
    if (parsed.hostname or "").lower().rstrip(".") != BOP_BURGOS_DOMAIN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid BOP source URL",
        )
    if parsed.port is not None or parsed.fragment:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid BOP source URL",
        )
    canonical_url = urlparse.urlunsplit(
        (
            parsed.scheme.lower(),
            BOP_BURGOS_DOMAIN,
            parsed.path,
            parsed.query,
            "",
        )
    )
    if source_url != canonical_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid BOP source URL",
        )
    return canonical_url


def _source_requires_refresh(source_url: str) -> bool:
    return urlparse.urlsplit(source_url).path.rstrip("/") == "/busqueda"


def _fetch_and_archive(source_url: str, url_key: str) -> dict:
    try:
        fetched = _fetch_direct_source(
            source_url,
            allowed_domains={BOP_BURGOS_DOMAIN},
            require_https=False,
            exact_domains=True,
        )
    except ImportSourceError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Official BOP source could not be archived",
        ) from error

    digest = hashlib.sha256(fetched.content).hexdigest()
    fetched_at = datetime.now(UTC).isoformat()
    content_type = _safe_content_type(fetched.content_type)
    record = {
        "content_type": content_type,
        "fetched_at": fetched_at,
        "sha256": digest,
        "source_url": source_url,
        "version": 1,
    }
    record["signature"] = sign_archive_manifest(
        source_url=source_url,
        fetched_at=fetched_at,
        content_type=content_type,
        digest=digest,
        signing_key=settings.bop_archive_proxy_signing_key or "",
    )

    root = _storage_root()
    object_path = root / "objects" / f"{digest}.bin"
    if not object_path.exists():
        _atomic_write_bytes(object_path, fetched.content)
    elif hashlib.sha256(object_path.read_bytes()).hexdigest() != digest:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Archive object integrity check failed",
        )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    record_path = root / "records" / url_key / f"{timestamp}-{digest}.json"
    _atomic_write_json(record_path, record)
    _atomic_write_json(root / "latest" / f"{url_key}.json", record)
    return record


def _load_latest_record(url_key: str, source_url: str) -> dict | None:
    path = _storage_root() / "latest" / f"{url_key}.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Archive manifest could not be read",
        ) from error
    if record.get("source_url") != source_url or record.get("version") != 1:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Archive manifest integrity check failed",
        )
    _verify_stored_manifest(record)
    return record


def _load_archived_content(record: dict) -> bytes:
    _verify_stored_manifest(record)
    digest = str(record.get("sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Archive manifest digest is invalid",
        )
    path = _storage_root() / "objects" / f"{digest}.bin"
    try:
        content = path.read_bytes()
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Archive object could not be read",
        ) from error
    if not digest or not hmac.compare_digest(
        hashlib.sha256(content).hexdigest(),
        digest,
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Archive object integrity check failed",
        )
    return content


def _verify_stored_manifest(record: dict) -> None:
    expected = sign_archive_manifest(
        source_url=str(record.get("source_url") or ""),
        fetched_at=str(record.get("fetched_at") or ""),
        content_type=str(record.get("content_type") or ""),
        digest=str(record.get("sha256") or ""),
        signing_key=settings.bop_archive_proxy_signing_key or "",
    )
    signature = str(record.get("signature") or "")
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Archive manifest signature check failed",
        )


def _storage_root() -> Path:
    root = Path(settings.bop_archive_proxy_storage_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _archive_lock(url_key: str):
    lock_path = _storage_root() / "locks" / f"{url_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _safe_content_type(value: str) -> str:
    sanitized = value.replace("\r", "").replace("\n", "").strip().lower()
    return (sanitized or "application/octet-stream")[:255]


def _atomic_write_json(path: Path, value: dict) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8"),
    )


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
