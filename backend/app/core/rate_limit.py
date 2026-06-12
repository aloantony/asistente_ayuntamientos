import threading
import time
from collections import defaultdict, deque

from app.core.config import settings


class SlidingWindowRateLimiter:
    """In-memory per-key sliding window. Per-process state: enough for the
    current single-worker deployment; swap for Redis when scaling out."""

    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and now - attempts[0] > self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                return False
            attempts.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()


login_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.login_rate_limit_attempts,
    window_seconds=settings.login_rate_limit_window_seconds,
)
