import fnmatch
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.rate_limit import (
    ACQUIRE_SCRIPT,
    REFUND_SCRIPT,
    RateLimitUnavailable,
    RedisSlidingWindowRateLimiter,
)


class FakeRedis:
    """Small deterministic executor for the two Lua script contracts."""

    def __init__(self) -> None:
        self.now_ms = 0
        self.raise_connection_error = False
        self.entries: dict[str, list[tuple[int, str]]] = {}
        self.expires_at: dict[str, int] = {}
        self._lock = threading.Lock()

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        if self.raise_connection_error:
            raise RedisConnectionError("redis://user:secret@private.example")
        assert numkeys == 1
        key = str(keys_and_args[0])
        with self._lock:
            self._expire(key)
            if script == ACQUIRE_SCRIPT:
                return self._acquire(key, keys_and_args[1:])
            if script == REFUND_SCRIPT:
                return self._refund(key, keys_and_args[1:])
            raise AssertionError("unexpected script")

    def _acquire(self, key: str, arguments: tuple[object, ...]) -> int:
        max_attempts, window_ms, member = arguments
        window_ms = int(window_ms)
        self._prune(key, window_ms)
        entries = self.entries.setdefault(key, [])
        if len(entries) >= int(max_attempts):
            return 0
        entries.append((self.now_ms, str(member)))
        self.expires_at[key] = self.now_ms + window_ms + 1
        return 1

    def _refund(self, key: str, arguments: tuple[object, ...]) -> int:
        window_ms = int(arguments[0])
        self._prune(key, window_ms)
        entries = self.entries.get(key, [])
        if not entries:
            self._delete(key)
            return 0
        entries.sort()
        entries.pop()
        if not entries:
            self._delete(key)
        else:
            newest_score = max(score for score, _member in entries)
            self.expires_at[key] = newest_score + window_ms + 1
        return 1

    def _prune(self, key: str, window_ms: int) -> None:
        cutoff = self.now_ms - window_ms
        remaining = [
            entry for entry in self.entries.get(key, []) if entry[0] >= cutoff
        ]
        if remaining:
            self.entries[key] = remaining
        else:
            self._delete(key)

    def _expire(self, key: str) -> None:
        expires_at = self.expires_at.get(key)
        if expires_at is not None and self.now_ms >= expires_at:
            self._delete(key)

    def _delete(self, key: str) -> None:
        self.entries.pop(key, None)
        self.expires_at.pop(key, None)

    def scan_iter(self, match: str):
        if self.raise_connection_error:
            raise RedisConnectionError("unavailable")
        with self._lock:
            yield from [key for key in self.entries if fnmatch.fnmatch(key, match)]

    def delete(self, *names: object) -> int:
        if self.raise_connection_error:
            raise RedisConnectionError("unavailable")
        with self._lock:
            for name in names:
                self._delete(str(name))
        return len(names)

    def advance(self, milliseconds: int) -> None:
        with self._lock:
            self.now_ms += milliseconds

    def pttl(self, key: str) -> int:
        with self._lock:
            self._expire(key)
            expires_at = self.expires_at.get(key)
            return -2 if expires_at is None else expires_at - self.now_ms


def make_limiter(
    client: FakeRedis,
    *,
    max_attempts: int = 2,
    namespace: str = "login",
) -> RedisSlidingWindowRateLimiter:
    return RedisSlidingWindowRateLimiter(
        max_attempts=max_attempts,
        window_seconds=1,
        namespace=namespace,
        key_prefix="test:rate-limit",
        key_secret="test-key-secret",
        client=client,
    )


def test_storage_key_is_stable_namespaced_hmac_without_pii() -> None:
    limiter = make_limiter(FakeRedis())
    logical_key = "203.0.113.10:private.person@example.com"

    storage_key = limiter.storage_key(logical_key)

    assert storage_key == limiter.storage_key(logical_key)
    assert storage_key.startswith("test:rate-limit:login:")
    assert "203.0.113.10" not in storage_key
    assert "private.person@example.com" not in storage_key


def test_window_boundary_and_ttl_match_existing_sliding_window() -> None:
    redis = FakeRedis()
    limiter = make_limiter(redis)
    key = "client:account"

    assert limiter.try_acquire(key)
    assert limiter.try_acquire(key)
    assert not limiter.try_acquire(key)
    assert redis.pttl(limiter.storage_key(key)) == 1001

    redis.advance(1000)
    assert not limiter.try_acquire(key)
    redis.advance(1)
    assert limiter.try_acquire(key)


def test_refund_returns_one_slot_and_shortens_ttl_to_latest_failure() -> None:
    redis = FakeRedis()
    limiter = make_limiter(redis)
    key = "client:account"

    assert limiter.try_acquire(key)
    redis.advance(500)
    assert limiter.try_acquire(key)

    limiter.refund(key)

    assert redis.pttl(limiter.storage_key(key)) == 501
    assert limiter.try_acquire(key)


def test_two_workers_share_one_atomic_budget_under_concurrency() -> None:
    redis = FakeRedis()
    first_worker = make_limiter(redis, max_attempts=10)
    second_worker = make_limiter(redis, max_attempts=10)
    limiters = [first_worker, second_worker]

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(
            executor.map(
                lambda index: limiters[index % 2].try_acquire("shared-subject"),
                range(64),
            )
        )

    assert results.count(True) == 10
    assert results.count(False) == 54


def test_redis_failures_are_fail_closed_without_leaking_connection_detail() -> None:
    redis = FakeRedis()
    limiter = make_limiter(redis)
    redis.raise_connection_error = True

    with pytest.raises(RateLimitUnavailable) as acquire_error:
        limiter.try_acquire("private.person@example.com:secret-password")
    with pytest.raises(RateLimitUnavailable):
        limiter.refund("private.person@example.com:secret-password")

    message = str(acquire_error.value)
    assert "private.person@example.com" not in message
    assert "secret-password" not in message
    assert "redis://" not in message


def test_reset_only_deletes_the_limiter_namespace() -> None:
    redis = FakeRedis()
    login_limiter = make_limiter(redis, namespace="login")
    password_limiter = make_limiter(redis, namespace="change-password")
    assert login_limiter.try_acquire("subject")
    assert password_limiter.try_acquire("subject")

    login_limiter.reset()

    assert redis.pttl(login_limiter.storage_key("subject")) == -2
    assert redis.pttl(password_limiter.storage_key("subject")) > 0
