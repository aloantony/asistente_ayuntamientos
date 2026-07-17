from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

from app.core.jobs import get_redis_connection

logger = logging.getLogger(__name__)

CACHE_PREFIX = "reference-wms:v1"
CACHE_FORMAT = b"reference-wms-cache-v1\n"
MAX_CACHE_HEADER_BYTES = 1024


@dataclass(frozen=True)
class CachedWMSResponse:
    body: bytes
    content_type: str
    etag: str
    stored_at: int
    fresh_for_seconds: int

    def is_fresh(self, *, now: int | None = None) -> bool:
        current = int(time.time()) if now is None else now
        return current <= self.stored_at + self.fresh_for_seconds


def build_wms_cache_key(parts: dict[str, Any]) -> str:
    canonical = json.dumps(
        parts,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{CACHE_PREFIX}:{digest}"


def get_cached_wms_response(key: str) -> CachedWMSResponse | None:
    try:
        raw = get_redis_connection().get(key)
    except RedisError:
        logger.warning("Reference WMS cache read failed", exc_info=True)
        return None
    if not isinstance(raw, bytes):
        return None
    try:
        return deserialize_cached_wms_response(raw)
    except ValueError:
        logger.warning("Reference WMS cache entry was invalid")
        return None


def store_cached_wms_response(
    key: str,
    response: CachedWMSResponse,
    *,
    stale_ttl_seconds: int,
) -> None:
    try:
        payload = serialize_cached_wms_response(response)
        get_redis_connection().setex(key, stale_ttl_seconds, payload)
    except (RedisError, ValueError):
        logger.warning("Reference WMS cache write failed", exc_info=True)


def serialize_cached_wms_response(response: CachedWMSResponse) -> bytes:
    header = json.dumps(
        {
            "content_type": response.content_type,
            "etag": response.etag,
            "fresh_for_seconds": response.fresh_for_seconds,
            "stored_at": response.stored_at,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(header) > MAX_CACHE_HEADER_BYTES:
        raise ValueError("cache header is too large")
    return (
        CACHE_FORMAT
        + str(len(header)).encode("ascii")
        + b"\n"
        + header
        + response.body
    )


def deserialize_cached_wms_response(raw: bytes) -> CachedWMSResponse:
    if not raw.startswith(CACHE_FORMAT):
        raise ValueError("invalid cache format")
    remaining = raw[len(CACHE_FORMAT) :]
    length_raw, separator, remaining = remaining.partition(b"\n")
    if separator != b"\n" or not length_raw.isdigit():
        raise ValueError("invalid cache header length")
    header_length = int(length_raw)
    if not 1 <= header_length <= MAX_CACHE_HEADER_BYTES:
        raise ValueError("invalid cache header length")
    header_raw = remaining[:header_length]
    body = remaining[header_length:]
    if len(header_raw) != header_length or not body:
        raise ValueError("invalid cache payload")
    try:
        header = json.loads(header_raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid cache header") from error
    if not isinstance(header, dict) or set(header) != {
        "content_type",
        "etag",
        "fresh_for_seconds",
        "stored_at",
    }:
        raise ValueError("invalid cache header")
    content_type = header["content_type"]
    etag = header["etag"]
    fresh_for_seconds = header["fresh_for_seconds"]
    stored_at = header["stored_at"]
    if (
        content_type not in {"application/json", "application/geo+json", "image/png"}
        or not isinstance(etag, str)
        or len(etag) != 66
        or not etag.startswith('"')
        or not etag.endswith('"')
        or any(char not in "0123456789abcdef" for char in etag[1:-1])
        or isinstance(fresh_for_seconds, bool)
        or not isinstance(fresh_for_seconds, int)
        or not 1 <= fresh_for_seconds <= 7 * 24 * 60 * 60
        or isinstance(stored_at, bool)
        or not isinstance(stored_at, int)
        or stored_at < 0
    ):
        raise ValueError("invalid cache header")
    expected_etag = f'"{hashlib.sha256(body).hexdigest()}"'
    if etag != expected_etag:
        raise ValueError("cache body digest mismatch")
    return CachedWMSResponse(
        body=body,
        content_type=content_type,
        etag=etag,
        stored_at=stored_at,
        fresh_for_seconds=fresh_for_seconds,
    )
