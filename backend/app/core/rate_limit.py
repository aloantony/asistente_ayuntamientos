import threading
import time
from collections import deque

from app.core.config import settings

# How many acquisitions between sweeps of keys whose window has expired, so
# the dict cannot grow unbounded with keys never touched again.
SWEEP_EVERY_OPERATIONS = 256


class SlidingWindowRateLimiter:
    """In-memory per-key sliding window. Per-process state: enough for the
    current single-worker deployment; swap for Redis when scaling out.

    try_acquire() reserves one attempt atomically — concurrent requests
    cannot exceed the budget while a slow credential check runs — and
    refund() returns the slot when the guarded operation succeeds, so only
    failures end up consuming quota.
    """

    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = {}
        self._operations = 0
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        attempts = self._attempts.get(key)
        if attempts is None:
            return None
        while attempts and now - attempts[0] > self.window_seconds:
            attempts.popleft()
        if not attempts:
            del self._attempts[key]
            return None
        return attempts

    def try_acquire(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._prune(key, now)
            if attempts is not None and len(attempts) >= self.max_attempts:
                return False
            if attempts is None:
                attempts = deque()
                self._attempts[key] = attempts
            attempts.append(now)

            self._operations += 1
            if self._operations % SWEEP_EVERY_OPERATIONS == 0:
                for stale_key in list(self._attempts):
                    self._prune(stale_key, now)
            return True

    def refund(self, key: str) -> None:
        with self._lock:
            attempts = self._attempts.get(key)
            if attempts:
                # Timestamps are fungible: releasing the newest entry returns
                # exactly one slot to the window.
                attempts.pop()
                if not attempts:
                    del self._attempts[key]

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()


login_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.login_rate_limit_attempts,
    window_seconds=settings.login_rate_limit_window_seconds,
)

# Changing a password verifies the current one: without a limit, a hijacked
# session could brute-force it. Shares the login settings, keyed by user id.
change_password_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.login_rate_limit_attempts,
    window_seconds=settings.login_rate_limit_window_seconds,
)
