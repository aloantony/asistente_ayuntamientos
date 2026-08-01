import threading
import time
from collections import deque

from fastapi import HTTPException, status

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

# Techo por IP, complementario al de (IP, cuenta). Sin él, una sola IP puede
# probar una contraseña contra cuentas distintas sin límite alguno: la clave
# incluye el correo, así que cada cuenta nueva estrena su propio cupo. Es más
# holgado que el otro para no bloquear una organización tras un NAT compartido.
login_ip_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.login_ip_rate_limit_attempts,
    window_seconds=settings.login_ip_rate_limit_window_seconds,
)

# Changing a password verifies the current one: without a limit, a hijacked
# session could brute-force it. Shares the login settings, keyed by user id.
change_password_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.login_rate_limit_attempts,
    window_seconds=settings.login_rate_limit_window_seconds,
)

# El endpoint de bootstrap es anónimo: solo lo protegen el token y que no exista
# ningún usuario. Un límite estrecho quita el margen para adivinar el token.
bootstrap_admin_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.bootstrap_admin_rate_limit_attempts,
    window_seconds=settings.bootstrap_admin_rate_limit_window_seconds,
)

# Los siguientes protegen endpoints que cuestan dinero o CPU en cada llamada
# (turno de LLM, TTS, STT con subproceso ffmpeg, subidas, importación de
# ordenanzas). Sin ellos, una sesión válida puede convertirse en una factura o
# en una denegación de servicio. Ver ADR-036.
assistant_turn_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.assistant_rate_limit_attempts,
    window_seconds=settings.assistant_rate_limit_window_seconds,
)

speech_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.speech_rate_limit_attempts,
    window_seconds=settings.speech_rate_limit_window_seconds,
)

upload_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.upload_rate_limit_attempts,
    window_seconds=settings.upload_rate_limit_window_seconds,
)

ordinance_import_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=settings.ordinance_import_rate_limit_attempts,
    window_seconds=settings.ordinance_import_rate_limit_window_seconds,
)

# Registro único para que los tests puedan limpiar el estado sin que la lista se
# desincronice cada vez que se añade un limitador.
ALL_RATE_LIMITERS = (
    login_rate_limiter,
    login_ip_rate_limiter,
    change_password_rate_limiter,
    bootstrap_admin_rate_limiter,
    assistant_turn_rate_limiter,
    speech_rate_limiter,
    upload_rate_limiter,
    ordinance_import_rate_limiter,
)


def reset_all_rate_limiters() -> None:
    for limiter in ALL_RATE_LIMITERS:
        limiter.reset()


def require_rate_limit_slot(
    limiter: SlidingWindowRateLimiter,
    key: str,
    *,
    detail: str,
) -> None:
    """Reserva un hueco o rechaza con 429."""
    if not limiter.try_acquire(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
        )
