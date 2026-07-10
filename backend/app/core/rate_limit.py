import hashlib
import hmac
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from typing import Protocol, cast

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import settings

# How many acquisitions between sweeps of keys whose window has expired, so
# the dict cannot grow unbounded with keys never touched again.
SWEEP_EVERY_OPERATIONS = 256

ACQUIRE_SCRIPT = """
local key = KEYS[1]
local max_attempts = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local member = ARGV[3]
local redis_time = redis.call('TIME')
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local cutoff_ms = now_ms - window_ms

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff_ms - 1)
if redis.call('ZCARD', key) >= max_attempts then
    return 0
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms + 1)
return 1
"""

REFUND_SCRIPT = """
local key = KEYS[1]
local window_ms = tonumber(ARGV[1])
local redis_time = redis.call('TIME')
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local cutoff_ms = now_ms - window_ms

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff_ms - 1)
local reservation_id = ARGV[2]
local removed = redis.call('ZREM', key, reservation_id)

local newest = redis.call('ZREVRANGE', key, 0, 0, 'WITHSCORES')
if #newest == 0 then
    redis.call('DEL', key)
else
    local remaining_ttl_ms = math.floor(tonumber(newest[2]) + window_ms - now_ms + 1)
    if remaining_ttl_ms > 0 then
        redis.call('PEXPIRE', key, remaining_ttl_ms)
    else
        redis.call('DEL', key)
    end
end
return removed
"""


class RateLimitUnavailable(RuntimeError):
    """The configured shared rate-limit store cannot make a safe decision."""


class RedisClient(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...

    def scan_iter(self, match: str) -> Iterator[object]: ...

    def delete(self, *names: object) -> object: ...


class RateLimiter(Protocol):
    def try_acquire(self, key: str) -> str | None: ...

    def refund(self, key: str, reservation_id: str) -> None: ...

    def reset(self) -> None: ...


class InMemorySlidingWindowRateLimiter:
    """Thread-safe fallback for explicit development and test configuration.

    try_acquire() reserves one attempt atomically — concurrent requests
    cannot exceed the budget while a slow credential check runs — and
    refund() returns the slot when the guarded operation succeeds, so only
    failures end up consuming quota.
    """

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        *,
        namespace: str = "memory",
        key_secret: str = "development-only",
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.namespace = namespace
        self._key_secret = key_secret.encode()
        self._attempts: dict[str, deque[tuple[float, str]]] = {}
        self._operations = 0
        self._lock = threading.Lock()

    def _prune(
        self,
        key: str,
        now: float,
    ) -> deque[tuple[float, str]] | None:
        attempts = self._attempts.get(key)
        if attempts is None:
            return None
        while attempts and now - attempts[0][0] > self.window_seconds:
            attempts.popleft()
        if not attempts:
            del self._attempts[key]
            return None
        return attempts

    def try_acquire(self, key: str) -> str | None:
        key = self.storage_key(key)
        now = time.monotonic()
        reservation_id = uuid.uuid4().hex
        with self._lock:
            attempts = self._prune(key, now)
            if attempts is not None and len(attempts) >= self.max_attempts:
                return None
            if attempts is None:
                attempts = deque()
                self._attempts[key] = attempts
            attempts.append((now, reservation_id))

            self._operations += 1
            if self._operations % SWEEP_EVERY_OPERATIONS == 0:
                for stale_key in list(self._attempts):
                    self._prune(stale_key, now)
            return reservation_id

    def refund(self, key: str, reservation_id: str) -> None:
        key = self.storage_key(key)
        with self._lock:
            attempts = self._attempts.get(key)
            if attempts:
                for attempt in attempts:
                    if attempt[1] == reservation_id:
                        attempts.remove(attempt)
                        break
                if not attempts:
                    del self._attempts[key]

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()

    def storage_key(self, key: str) -> str:
        digest = hmac.new(self._key_secret, key.encode(), hashlib.sha256).hexdigest()
        return f"{self.namespace}:{digest}"


class RedisSlidingWindowRateLimiter:
    """Distributed sliding window implemented as atomic Redis Lua scripts."""

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        *,
        namespace: str,
        key_prefix: str,
        key_secret: str,
        client: RedisClient,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.window_ms = max(1, int(window_seconds * 1000))
        self.namespace = namespace
        self.key_prefix = key_prefix
        self._key_secret = key_secret.encode()
        self._client = client

    def storage_key(self, key: str) -> str:
        digest = hmac.new(self._key_secret, key.encode(), hashlib.sha256).hexdigest()
        return f"{self.key_prefix}:{self.namespace}:{digest}"

    def try_acquire(self, key: str) -> str | None:
        reservation_id = uuid.uuid4().hex
        try:
            result = self._client.eval(
                ACQUIRE_SCRIPT,
                1,
                self.storage_key(key),
                self.max_attempts,
                self.window_ms,
                reservation_id,
            )
        except RedisError:
            # Authentication must stop when the distributed budget cannot be
            # checked. Do not leak the URL or logical key through logs/errors.
            raise RateLimitUnavailable(
                "The shared authentication rate limiter is unavailable"
            ) from None
        return reservation_id if result else None

    def refund(self, key: str, reservation_id: str) -> None:
        try:
            self._client.eval(
                REFUND_SCRIPT,
                1,
                self.storage_key(key),
                self.window_ms,
                reservation_id,
            )
        except RedisError:
            raise RateLimitUnavailable(
                "The shared authentication rate limiter is unavailable"
            ) from None

    def reset(self) -> None:
        try:
            keys = list(
                self._client.scan_iter(
                    match=f"{self.key_prefix}:{self.namespace}:*"
                )
            )
            if keys:
                self._client.delete(*keys)
        except RedisError:
            raise RateLimitUnavailable(
                "The shared authentication rate limiter is unavailable"
            ) from None


def _redis_client() -> RedisClient:
    return cast(
        RedisClient,
        Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.rate_limit_redis_timeout_seconds,
            socket_timeout=settings.rate_limit_redis_timeout_seconds,
        ),
    )


def create_rate_limiter(
    namespace: str,
    *,
    client: RedisClient | None = None,
) -> RateLimiter:
    common = {
        "max_attempts": settings.login_rate_limit_attempts,
        "window_seconds": settings.login_rate_limit_window_seconds,
        "namespace": namespace,
        "key_secret": settings.secret_key,
    }
    if settings.rate_limit_backend == "memory":
        return InMemorySlidingWindowRateLimiter(**common)
    return RedisSlidingWindowRateLimiter(
        **common,
        key_prefix=settings.rate_limit_key_prefix,
        client=client if client is not None else _redis_client(),
    )


login_rate_limiter = create_rate_limiter("login")

# Changing a password verifies the current one: without a limit, a hijacked
# session could brute-force it. Shares the login settings, keyed by user id.
change_password_rate_limiter = create_rate_limiter("change-password")
