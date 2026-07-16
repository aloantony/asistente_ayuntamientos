import hashlib
import json
import logging
import math
import multiprocessing
import os
import selectors
import signal
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmbeddingsUnavailableError(Exception):
    pass


class EmbeddingWorkerCleanupError(EmbeddingsUnavailableError):
    """The supervisor could not prove that its provider child was reaped."""


EMBEDDING_TIMEOUT_MIN_SECONDS = 0.1
EMBEDDING_TIMEOUT_MAX_SECONDS = 120.0
EMBEDDING_WORKER_NORMAL_JOIN_SECONDS = 0.05
EMBEDDING_WORKER_TERMINATE_JOIN_SECONDS = 0.2
EMBEDDING_WORKER_KILL_JOIN_SECONDS = 0.5
EMBEDDING_WORKER_MAX_CLEANUP_SECONDS = (
    EMBEDDING_WORKER_NORMAL_JOIN_SECONDS
    + EMBEDDING_WORKER_TERMINATE_JOIN_SECONDS
    + EMBEDDING_WORKER_KILL_JOIN_SECONDS
)
EMBEDDING_CLAIM_SAFETY_SECONDS = 30.0
MAX_EMBEDDING_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EMBEDDING_IPC_BYTES = 4 * 1024 * 1024
EMBEDDING_IPC_CHUNK_BYTES = 64 * 1024
MAX_EMBEDDING_VECTOR_DIMENSIONS = 16_384
EMBEDDING_ADMISSION_WAIT_SECONDS = 0.05

_EMBEDDING_ADMISSION = threading.BoundedSemaphore(
    settings.embeddings_max_concurrent_workers
)


@dataclass(frozen=True)
class _ExternalEmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float


class _AdmissionLease:
    """Release one global worker slot exactly once across racing owners."""

    def __init__(self, semaphore) -> None:
        self._semaphore = semaphore
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._semaphore.release()


class _LaunchState:
    """Transfer a late Process.start and its admission slot to the launcher."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.complete = False
        self.started = False
        self.error: BaseException | None = None
        self.cancel_requested = False


def _validated_embedding_timeout(value: object) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as error:
        raise EmbeddingsUnavailableError(
            "Embeddings timeout is invalid"
        ) from error
    if not math.isfinite(timeout) or not (
        EMBEDDING_TIMEOUT_MIN_SECONDS
        <= timeout
        <= EMBEDDING_TIMEOUT_MAX_SECONDS
    ):
        raise EmbeddingsUnavailableError("Embeddings timeout is invalid")
    return timeout


def supervised_embedding_claim_lease_seconds(
    timeout_seconds: object | None = None,
) -> float:
    """Return a lease that cannot expire during a supervised provider call.

    The child has a hard deadline and the parent always terminates and reaps it
    before returning.  Cleanup and safety allowances keep claim takeover after
    the persisted provider deadline without relying on socket inactivity
    timeouts.
    """

    timeout = _validated_embedding_timeout(
        settings.embeddings_timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    return timeout + supervised_embedding_cleanup_margin_seconds()


def supervised_embedding_cleanup_margin_seconds() -> float:
    return (
        EMBEDDING_WORKER_MAX_CLEANUP_SECONDS
        + EMBEDDING_CLAIM_SAFETY_SECONDS
    )


def embed_text(text: str) -> tuple[str | None, str, str]:
    """Return (vector string, model, status) for storage in pgvector/text.

    The local hash runtime is deterministic and network-free. It is good enough
    for tests and development, while production can switch to an EU-hosted
    OpenAI-compatible embeddings endpoint through configuration.
    """
    clean_text = text.strip()
    if settings.embeddings_runtime == "disabled" or not clean_text:
        return None, settings.embeddings_model, "disabled"
    if settings.embeddings_runtime == "openai_compatible":
        return (
            _format_vector(_embed_openai_compatible(clean_text)),
            settings.embeddings_model,
            "ready",
        )
    return (
        _format_vector(_embed_local_hash(clean_text, settings.embeddings_dimensions)),
        settings.embeddings_model,
        "ready",
    )


def embed_text_supervised(
    text: str,
    *,
    provider_deadline_at: datetime | None = None,
) -> tuple[str | None, str, str]:
    """Embed text with a hard total deadline for external HTTP runtimes.

    Local/disabled runtimes remain in-process.  The OpenAI-compatible request
    runs in a disposable child with bounded IPC.  The parent does not return
    until that child has exited or has been killed and reaped.
    """

    clean_text = text.strip()
    provider_deadline_epoch = None
    if provider_deadline_at is not None:
        provider_deadline_epoch = _resolve_provider_deadline_epoch(
            settings.embeddings_timeout_seconds,
            provider_deadline_at=provider_deadline_at,
        )
        _require_embedding_deadline(provider_deadline_epoch)
    if settings.embeddings_runtime != "openai_compatible" or not clean_text:
        return embed_text(clean_text)
    if not settings.embeddings_base_url or not settings.embeddings_api_key:
        raise EmbeddingsUnavailableError("Embeddings provider is not configured")
    config = _ExternalEmbeddingConfig(
        base_url=settings.embeddings_base_url,
        api_key=settings.embeddings_api_key,
        model=settings.embeddings_model,
        timeout_seconds=_validated_embedding_timeout(
            settings.embeddings_timeout_seconds
        ),
    )
    if provider_deadline_epoch is None:
        provider_deadline_epoch = _resolve_provider_deadline_epoch(
            config.timeout_seconds,
            provider_deadline_at=None,
        )
    vector = _run_supervised_external_embedding(
        clean_text,
        config,
        provider_deadline_epoch=provider_deadline_epoch,
    )
    return _format_vector(vector), config.model, "ready"


def _resolve_provider_deadline_epoch(
    timeout_seconds: object,
    *,
    provider_deadline_at: datetime | None,
) -> float:
    timeout = _validated_embedding_timeout(timeout_seconds)
    if provider_deadline_at is None:
        return time() + timeout
    if provider_deadline_at.tzinfo is None:
        raise EmbeddingsUnavailableError(
            "Embeddings provider deadline must be timezone-aware"
        )
    deadline_epoch = provider_deadline_at.astimezone(timezone.utc).timestamp()
    if not math.isfinite(deadline_epoch):
        raise EmbeddingsUnavailableError("Embeddings provider deadline is invalid")
    return deadline_epoch


def _run_supervised_external_embedding(
    text: str,
    config: _ExternalEmbeddingConfig,
    *,
    provider_deadline_epoch: float | None = None,
    worker_entry=None,
    worker_operation=None,
    process_start_method: str = "spawn",
    process_context=None,
) -> list[float]:
    """Run one external embedding in a process under one absolute deadline."""

    timeout = _validated_embedding_timeout(config.timeout_seconds)
    deadline_epoch = (
        time() + timeout
        if provider_deadline_epoch is None
        else float(provider_deadline_epoch)
    )
    if not math.isfinite(deadline_epoch):
        raise EmbeddingsUnavailableError("Embeddings provider deadline is invalid")
    _require_embedding_deadline(deadline_epoch)
    admission = _acquire_embedding_admission(deadline_epoch)
    admission_owned_by_main = True
    receive_socket = None
    send_socket = None
    process = None
    process_started = False
    received_message = False
    entry = worker_entry or _embedding_worker_entry
    operation = worker_operation or _request_external_embedding
    try:
        try:
            context = process_context or multiprocessing.get_context(
                process_start_method
            )
            _require_embedding_deadline(deadline_epoch)
            receive_socket, send_socket = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )
            _require_embedding_deadline(deadline_epoch)
            process = context.Process(
                target=entry,
                args=(send_socket, text, config, deadline_epoch, operation),
                daemon=True,
            )
            _require_embedding_deadline(deadline_epoch)
        except (OSError, RuntimeError, ValueError, AssertionError) as error:
            raise EmbeddingsUnavailableError(
                "Embeddings worker resources could not be reserved"
            ) from error

        launch_state = _LaunchState()
        launch_done = threading.Event()
        launcher = threading.Thread(
            target=_launch_embedding_process,
            args=(
                process,
                send_socket,
                launch_state,
                launch_done,
                admission,
            ),
            name="embedding-worker-launcher",
            daemon=True,
        )
        try:
            launcher.start()
        except (OSError, RuntimeError) as error:
            raise EmbeddingsUnavailableError(
                "Embeddings worker launcher could not be started"
            ) from error

        remaining = deadline_epoch - time()
        launched_in_time = remaining > 0 and launch_done.wait(remaining)
        if not launched_in_time:
            with launch_state.lock:
                launch_state.cancel_requested = True
                launch_completed_during_race = launch_state.complete
                process_started = launch_state.started
            if not launch_completed_during_race:
                # The launcher retains the process, sending socket and global
                # admission slot.  When start eventually returns, the child
                # sees the expired durable deadline before any provider I/O;
                # the launcher then kills/reaps it and releases capacity.
                admission_owned_by_main = False
                process = None
                send_socket = None
            raise EmbeddingWorkerCleanupError(
                "Embeddings worker launch exceeded its durable deadline"
            )

        with launch_state.lock:
            process_started = launch_state.started
            launch_error = launch_state.error
        if launch_error is not None or not process_started:
            raise EmbeddingsUnavailableError(
                "Embeddings worker could not be started"
            ) from launch_error

        # The launcher closed the parent's sending endpoint after Process.start.
        send_socket = None
        _require_embedding_deadline(deadline_epoch)
        raw_message = _wait_for_embedding_worker_message(
            receive_socket,
            deadline_epoch=deadline_epoch,
        )
        received_message = True
        return _decode_embedding_worker_message(raw_message)
    finally:
        _safe_socket_close(receive_socket)
        _safe_socket_close(send_socket)
        if admission_owned_by_main:
            cleanup_confirmed = process is None
            try:
                if process is not None:
                    if process_started:
                        _cleanup_embedding_process(
                            process,
                            allow_normal_exit=received_message,
                        )
                    else:
                        _close_unstarted_embedding_process(process)
                    cleanup_confirmed = True
            finally:
                # An unconfirmed child keeps its slot quarantined.  Releasing
                # it would let repeated cleanup failures exceed the global
                # process/provider admission bound.
                if cleanup_confirmed:
                    admission.release()


def _acquire_embedding_admission(deadline_epoch: float) -> _AdmissionLease:
    remaining = deadline_epoch - time()
    if remaining <= 0:
        raise EmbeddingsUnavailableError(
            "Embeddings request exceeded its deadline"
        )
    try:
        acquired = _EMBEDDING_ADMISSION.acquire(
            timeout=min(remaining, EMBEDDING_ADMISSION_WAIT_SECONDS)
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise EmbeddingsUnavailableError(
            "Embeddings worker capacity could not be reserved"
        ) from error
    if not acquired:
        raise EmbeddingsUnavailableError(
            "Embeddings worker capacity is currently occupied"
        )
    return _AdmissionLease(_EMBEDDING_ADMISSION)


def _launch_embedding_process(
    process,
    send_socket,
    state: _LaunchState,
    done: threading.Event,
    admission: _AdmissionLease,
) -> None:
    started = False
    error: BaseException | None = None
    try:
        process.start()
        started = True
    except BaseException as launch_error:
        error = launch_error
    finally:
        _safe_socket_close(send_socket)

    with state.lock:
        state.started = started
        state.error = error
        state.complete = True
        cleanup_late_launch = state.cancel_requested
        done.set()

    if not cleanup_late_launch:
        return
    cleanup_confirmed = False
    try:
        if started:
            _cleanup_embedding_process(process, allow_normal_exit=False)
        else:
            _close_unstarted_embedding_process(process)
        cleanup_confirmed = True
    except EmbeddingWorkerCleanupError:
        logger.exception("Late embeddings worker cleanup could not be confirmed")
    finally:
        if cleanup_confirmed:
            admission.release()


def _require_embedding_deadline(deadline_epoch: float) -> None:
    if time() >= deadline_epoch:
        raise EmbeddingsUnavailableError("Embeddings request exceeded its deadline")


def _wait_for_embedding_worker_message(
    receive_socket,
    *,
    deadline_epoch: float,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    selector = None
    try:
        receive_socket.setblocking(False)
        selector = selectors.DefaultSelector()
        selector.register(receive_socket, selectors.EVENT_READ)
        while True:
            remaining = deadline_epoch - time()
            if remaining <= 0:
                raise EmbeddingsUnavailableError(
                    "Embeddings request exceeded its deadline"
                )
            events = selector.select(remaining)
            if not events:
                raise EmbeddingsUnavailableError(
                    "Embeddings request exceeded its deadline"
                )
            try:
                chunk = receive_socket.recv(
                    min(
                        EMBEDDING_IPC_CHUNK_BYTES,
                        MAX_EMBEDDING_IPC_BYTES + 1 - total,
                    )
                )
            except BlockingIOError:
                continue
            if not chunk:
                if not chunks:
                    raise EmbeddingsUnavailableError(
                        "Embeddings worker exited without a result"
                    )
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_EMBEDDING_IPC_BYTES:
                raise EmbeddingsUnavailableError(
                    "Embeddings worker response exceeded the IPC limit"
                )
    except EmbeddingsUnavailableError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise EmbeddingsUnavailableError(
            "Embeddings worker IPC failed"
        ) from error
    finally:
        if selector is not None:
            try:
                selector.close()
            except (OSError, RuntimeError, ValueError):
                pass


def _embedding_worker_entry(
    send_socket,
    text: str,
    config: _ExternalEmbeddingConfig,
    provider_deadline_epoch: float,
    operation,
) -> None:
    """Child entry: arm the hard deadline before any provider operation."""

    remaining = provider_deadline_epoch - time()
    if remaining <= 0:
        _safe_socket_close(send_socket)
        return
    try:
        signal.signal(signal.SIGALRM, _embedding_worker_alarm)
        signal.setitimer(signal.ITIMER_REAL, remaining)
    except (AttributeError, OSError, RuntimeError, ValueError):
        # The production image is POSIX. Fail closed rather than performing an
        # external request without an independent child-side deadline.
        _safe_socket_close(send_socket)
        return

    try:
        try:
            vector = operation(text, config, provider_deadline_epoch)
            message: dict[str, object] = {"status": "ok", "vector": vector}
        except EmbeddingsUnavailableError:
            message = {
                "status": "unavailable",
                "message": "Embeddings request failed",
            }
        except BaseException:
            message = {
                "status": "unavailable",
                "message": "Embeddings worker failed",
            }
        _send_embedding_worker_message(send_socket, message)
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except (AttributeError, OSError, RuntimeError, ValueError):
            pass
        _safe_socket_close(send_socket)


def _embedding_worker_alarm(signum, frame) -> None:
    del signum, frame
    os._exit(124)


def _request_external_embedding(
    text: str,
    config: _ExternalEmbeddingConfig,
    provider_deadline_epoch: float,
) -> list[float]:
    remaining = provider_deadline_epoch - time()
    if remaining <= 0:
        raise EmbeddingsUnavailableError("Embeddings request exceeded its deadline")
    payload = {"model": config.model, "input": text}
    request = urlrequest.Request(
        f"{config.base_url.rstrip('/')}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(
            request,
            timeout=min(config.timeout_seconds, remaining),
        ) as response:
            raw_body = response.read(MAX_EMBEDDING_PROVIDER_RESPONSE_BYTES + 1)
    except (urlerror.HTTPError, urlerror.URLError, TimeoutError) as error:
        raise EmbeddingsUnavailableError("Embeddings request failed") from error
    if len(raw_body) > MAX_EMBEDDING_PROVIDER_RESPONSE_BYTES:
        raise EmbeddingsUnavailableError("Embeddings response is too large")
    try:
        data = json.loads(raw_body.decode("utf-8"))
        embedding = data["data"][0]["embedding"]
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ) as error:
        raise EmbeddingsUnavailableError("Embeddings response is invalid") from error
    return _validate_external_embedding_vector(embedding)


def _validate_external_embedding_vector(value: object) -> list[float]:
    if not isinstance(value, list) or not (
        1 <= len(value) <= MAX_EMBEDDING_VECTOR_DIMENSIONS
    ):
        raise EmbeddingsUnavailableError("Embeddings response is invalid")
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise EmbeddingsUnavailableError("Embeddings response is invalid")
        number = float(item)
        if not math.isfinite(number):
            raise EmbeddingsUnavailableError("Embeddings response is invalid")
        vector.append(number)
    return vector


def _send_embedding_worker_message(
    send_socket,
    message: dict[str, object],
) -> None:
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        payload = b'{"status":"unavailable","message":"Invalid worker result"}'
    if len(payload) > MAX_EMBEDDING_IPC_BYTES:
        payload = (
            b'{"status":"unavailable","message":"Worker result too large"}'
        )
    try:
        send_socket.sendall(payload)
        send_socket.shutdown(socket.SHUT_WR)
    except (BrokenPipeError, OSError):
        pass


def _decode_embedding_worker_message(raw_message: bytes) -> list[float]:
    try:
        message = json.loads(raw_message.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmbeddingsUnavailableError(
            "Embeddings worker returned invalid data"
        ) from error
    if not isinstance(message, dict):
        raise EmbeddingsUnavailableError("Embeddings worker returned invalid data")
    if message.get("status") == "ok":
        return _validate_external_embedding_vector(message.get("vector"))
    raise EmbeddingsUnavailableError("Embeddings request failed")


def _cleanup_embedding_process(process, *, allow_normal_exit: bool) -> None:
    """Never return while the supervised child is still alive."""

    try:
        if allow_normal_exit:
            process.join(timeout=EMBEDDING_WORKER_NORMAL_JOIN_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=EMBEDDING_WORKER_TERMINATE_JOIN_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=EMBEDDING_WORKER_KILL_JOIN_SECONDS)
        if process.is_alive():
            raise EmbeddingWorkerCleanupError(
                "Embeddings worker could not be stopped"
            )
        process.join(timeout=0)
        process.close()
    except EmbeddingWorkerCleanupError:
        raise
    except (OSError, RuntimeError, ValueError, AssertionError) as error:
        raise EmbeddingWorkerCleanupError(
            "Embeddings worker cleanup failed"
        ) from error


def _close_unstarted_embedding_process(process) -> None:
    try:
        process.close()
    except (OSError, RuntimeError, ValueError, AssertionError) as error:
        raise EmbeddingWorkerCleanupError(
            "Embeddings worker cleanup failed"
        ) from error


def _safe_socket_close(value) -> None:
    if value is None:
        return
    try:
        value.close()
    except (OSError, RuntimeError, ValueError):
        pass


def vector_similarity(first: str | None, second: str | None) -> float:
    first_vector = _parse_vector(first)
    second_vector = _parse_vector(second)
    if not first_vector or not second_vector or len(first_vector) != len(second_vector):
        return 0.0
    dot = sum(a * b for a, b in zip(first_vector, second_vector))
    first_norm = math.sqrt(sum(value * value for value in first_vector))
    second_norm = math.sqrt(sum(value * value for value in second_vector))
    if first_norm == 0 or second_norm == 0:
        return 0.0
    return dot / (first_norm * second_norm)


def _embed_openai_compatible(text: str) -> list[float]:
    if not settings.embeddings_base_url or not settings.embeddings_api_key:
        raise EmbeddingsUnavailableError("Embeddings provider is not configured")

    payload = {
        "model": settings.embeddings_model,
        "input": text,
    }
    request = urlrequest.Request(
        f"{settings.embeddings_base_url.rstrip('/')}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.embeddings_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(
            request,
            timeout=settings.embeddings_timeout_seconds,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urlerror.HTTPError, urlerror.URLError, TimeoutError) as error:
        raise EmbeddingsUnavailableError("Embeddings request failed") from error
    try:
        embedding = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as error:
        raise EmbeddingsUnavailableError("Embeddings response is invalid") from error
    if not isinstance(embedding, list) or not all(
        isinstance(value, (int, float)) for value in embedding
    ):
        raise EmbeddingsUnavailableError("Embeddings response is invalid")
    return [float(value) for value in embedding]


def _embed_local_hash(text: str, dimensions: int) -> list[float]:
    vector = [0.0 for _ in range(dimensions)]
    for raw_word in text.lower().split():
        word = "".join(character for character in raw_word if character.isalnum())
        if not word:
            continue
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        weight = 1.0 + (digest[5] / 255.0)
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _format_vector(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def _parse_vector(value: str | None) -> list[float]:
    if not value:
        return []
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    vector: list[float] = []
    for item in parsed:
        if not isinstance(item, (int, float)):
            return []
        vector.append(float(item))
    return vector
