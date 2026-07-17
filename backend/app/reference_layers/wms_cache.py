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
WMS_CACHE_BUDGET_BYTES = 128 * 1024 * 1024
CACHE_INDEX_KEY = f"{CACHE_PREFIX}:lru"
CACHE_SIZES_KEY = f"{CACHE_PREFIX}:sizes"
CACHE_TOTAL_KEY = f"{CACHE_PREFIX}:total-bytes"
CACHE_METADATA_TTL_SECONDS = 8 * 24 * 60 * 60

_STORE_WITH_BUDGET_SCRIPT = """
if redis.call('EXISTS', KEYS[4]) == 0 then
  redis.call('DEL', KEYS[2])
  redis.call('DEL', KEYS[3])
  redis.call('SET', KEYS[4], 0)
end
local old_size = tonumber(redis.call('HGET', KEYS[3], KEYS[1])) or 0
local new_size = string.len(ARGV[1])
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), KEYS[1])
redis.call('HSET', KEYS[3], KEYS[1], new_size)
local total = tonumber(redis.call('GET', KEYS[4])) or 0
total = total - old_size + new_size
redis.call('SET', KEYS[4], total)
while total > tonumber(ARGV[4]) do
  local oldest = redis.call('ZRANGE', KEYS[2], 0, 0)[1]
  if not oldest then
    break
  end
  redis.call('ZREM', KEYS[2], oldest)
  local oldest_size = tonumber(redis.call('HGET', KEYS[3], oldest)) or 0
  redis.call('HDEL', KEYS[3], oldest)
  redis.call('DEL', oldest)
  total = total - oldest_size
  redis.call('SET', KEYS[4], total)
end
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[5]))
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[5]))
redis.call('EXPIRE', KEYS[4], tonumber(ARGV[5]))
return total
"""


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
        redis = get_redis_connection()
        raw = redis.get(key)
    except RedisError:
        logger.warning("Reference WMS cache read failed", exc_info=True)
        return None
    if not isinstance(raw, bytes):
        return None
    try:
        redis.zadd(CACHE_INDEX_KEY, {key: int(time.time())})
    except RedisError:
        logger.warning("Reference WMS cache LRU update failed", exc_info=True)
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
        if len(payload) > WMS_CACHE_BUDGET_BYTES:
            raise ValueError("cache payload exceeds the WMS cache budget")
        get_redis_connection().eval(
            _STORE_WITH_BUDGET_SCRIPT,
            4,
            key,
            CACHE_INDEX_KEY,
            CACHE_SIZES_KEY,
            CACHE_TOTAL_KEY,
            payload,
            stale_ttl_seconds,
            int(time.time()),
            WMS_CACHE_BUDGET_BYTES,
            CACHE_METADATA_TTL_SECONDS,
        )
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
