"""Read public pages without exposing an unrestricted server-side fetcher.

The reader is intentionally narrower than a browser: it performs anonymous
GET requests, never executes JavaScript, and only accepts bounded HTML or
plain-text responses. Every operation influenced by the remote endpoint -- DNS,
validation, connect/TLS, headers, redirects, body reads and text extraction --
runs in one disposable process. The parent supervises that process with one
absolute deadline and accepts only a bounded, validated IPC result.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import multiprocessing
import re
import selectors
import signal
import socket
import ssl
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import SplitResult, quote, urljoin, urlsplit, urlunsplit

from app.core.config import settings

MAX_WEB_PAGE_URL_CHARS = 2000
MAX_WEB_PAGE_TITLE_CHARS = 300
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/xhtml+xml",
        "text/html",
        "text/plain",
    }
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
BLOCKED_HOSTNAMES = frozenset(
    {
        "instance-data",
        "metadata",
        "metadata.google.internal",
        "metadata.google",
        "localhost",
    }
)
BLOCKED_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost")
CHARSET_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# The process startup has no extra grace period: it consumes the configured
# fetch deadline. Once the deadline expires, cleanup is additionally bounded by
# these joins (at most 0.55 seconds under normal OS process semantics).
WEB_READER_NORMAL_JOIN_SECONDS = 0.05
WEB_READER_TERMINATE_JOIN_SECONDS = 0.15
WEB_READER_KILL_JOIN_SECONDS = 0.35
WEB_READER_MAX_CLEANUP_OVERHEAD_SECONDS = (
    WEB_READER_NORMAL_JOIN_SECONDS
    + WEB_READER_TERMINATE_JOIN_SECONDS
    + WEB_READER_KILL_JOIN_SECONDS
)
MAX_WEB_PAGE_IPC_BYTES = 512 * 1024
MAX_WEB_PAGE_WORKER_ERROR_CHARS = 300
WEB_READER_ADMISSION_WAIT_SECONDS = 0.05
WEB_READER_IPC_CHUNK_BYTES = 64 * 1024

logger = logging.getLogger(__name__)
_WEB_READER_ADMISSION = threading.BoundedSemaphore(
    settings.web_page_max_concurrent_readers
)


class UnsafeWebPageURLError(ValueError):
    """A URL or redirect is not safe for a server-side public fetch."""


class WebPageUnavailableError(Exception):
    """A safe public page could not be downloaded or extracted."""


@dataclass(frozen=True)
class WebPage:
    source_url: str
    final_url: str
    title: str
    content_type: str
    text: str
    content_length_bytes: int
    text_char_count: int
    text_sha256: str
    text_truncated: bool
    redirects: int
    redirect_chain: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_url": self.source_url,
            "final_url": self.final_url,
            "title": self.title,
            "content_type": self.content_type,
            "text": self.text,
            "content_length_bytes": self.content_length_bytes,
            "text_char_count": self.text_char_count,
            "text_sha256": self.text_sha256,
            "text_truncated": self.text_truncated,
            "redirects": self.redirects,
            "redirect_chain": list(self.redirect_chain),
            "untrusted_content": True,
        }


@dataclass(frozen=True)
class _DownloadedPage:
    final_url: str
    content_type: str
    charset: str | None
    body: bytes
    redirects: int
    redirect_chain: tuple[str, ...]


@dataclass(frozen=True)
class _WebReaderConfig:
    app_version: str
    timeout_seconds: float
    dns_timeout_seconds: float
    max_response_bytes: int
    max_redirects: int
    max_text_chars: int


class _AdmissionLease:
    """Release one global reader slot exactly once across racing owners."""

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
    """Coordinate ownership when ``Process.start`` outlives the request."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.complete = False
        self.started = False
        self.error: BaseException | None = None
        self.cancel_requested = False


class _TextHTMLParser(HTMLParser):
    """Small, dependency-free visible-text extractor for bounded HTML."""

    _SKIPPED_TAGS = frozenset(
        {"canvas", "iframe", "noscript", "script", "style", "svg", "template"}
    )
    _BREAK_TAGS = frozenset(
        {
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        normalized = tag.casefold()
        if self._skip_depth:
            if normalized in self._SKIPPED_TAGS:
                self._skip_depth += 1
            return
        if normalized in self._SKIPPED_TAGS:
            self._skip_depth = 1
            return
        if normalized == "title":
            self._in_title = True
        if normalized in self._BREAK_TAGS:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if self._skip_depth:
            if normalized in self._SKIPPED_TAGS:
                self._skip_depth -= 1
            return
        if normalized == "title":
            self._in_title = False
        if normalized in self._BREAK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)

    @property
    def title(self) -> str:
        return _normalize_inline_text(" ".join(self._title_parts))

    @property
    def text(self) -> str:
        return _normalize_extracted_text("".join(self._text_parts))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, *, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._validated_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, *, timeout: float):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._validated_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


def normalize_web_page_url(value: object) -> str:
    """Return a canonical, syntactically safe HTTP(S) URL without a fragment."""
    if not isinstance(value, str):
        raise UnsafeWebPageURLError("url debe ser texto")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > MAX_WEB_PAGE_URL_CHARS:
        raise UnsafeWebPageURLError(
            f"url debe tener entre 1 y {MAX_WEB_PAGE_URL_CHARS} caracteres"
        )
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in normalized):
        raise UnsafeWebPageURLError("url no puede contener caracteres de control")
    if any(char.isspace() for char in normalized) or "\\" in normalized:
        raise UnsafeWebPageURLError("url contiene caracteres no permitidos")

    try:
        parsed = urlsplit(normalized)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as error:
        raise UnsafeWebPageURLError("url no es válida") from error

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise UnsafeWebPageURLError("url debe usar http o https")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeWebPageURLError("url no puede incluir credenciales")
    if not hostname:
        raise UnsafeWebPageURLError("url debe incluir un host")
    hostname = hostname.rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise UnsafeWebPageURLError("url contiene un host no válido") from error
    _reject_blocked_hostname(hostname)
    literal = _parse_ip_literal(hostname)
    if literal is not None:
        _require_public_ip(literal)

    default_port = 443 if scheme == "https" else 80
    effective_port = port or default_port
    if effective_port not in {80, 443}:
        raise UnsafeWebPageURLError("url solo puede usar los puertos 80 o 443")
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = rendered_host
    if port is not None and port != default_port:
        netloc = f"{rendered_host}:{port}"
    path = quote(
        parsed.path or "/",
        safe="/%:@!$&'()*+,;=-._~",
    )
    query = quote(
        parsed.query,
        safe="%=&?/:@!$'()*+,;-._~",
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def read_web_page(value: object) -> WebPage:
    """Read a page in one supervised process under a real total deadline.

    Process creation is charged to ``web_page_timeout_seconds``. Normal process
    reaping, terminate and kill joins may add at most
    ``WEB_READER_MAX_CLEANUP_OVERHEAD_SECONDS`` after that deadline.
    """
    source_url = normalize_web_page_url(value)
    config = _web_reader_config()
    return _run_supervised_web_reader(source_url, config)


def _web_reader_config() -> _WebReaderConfig:
    return _WebReaderConfig(
        app_version=settings.app_version,
        timeout_seconds=settings.web_page_timeout_seconds,
        dns_timeout_seconds=settings.web_page_dns_timeout_seconds,
        max_response_bytes=settings.web_page_max_response_bytes,
        max_redirects=settings.web_page_max_redirects,
        max_text_chars=settings.web_page_max_text_chars,
    )


def _run_supervised_web_reader(
    source_url: str,
    config: _WebReaderConfig,
) -> WebPage:
    deadline = monotonic() + config.timeout_seconds
    lease = _acquire_reader_admission(deadline)
    lease_owned_by_main = True
    receive_socket = None
    send_socket = None
    process = None
    process_started = False
    received_message = False
    try:
        try:
            process_context = multiprocessing.get_context("spawn")
            _require_reader_deadline(deadline)
            receive_socket, send_socket = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )
            _require_reader_deadline(deadline)
            process = process_context.Process(
                target=_web_reader_worker,
                args=(send_socket, source_url, config, deadline),
                daemon=True,
            )
            _require_reader_deadline(deadline)
        except (OSError, RuntimeError, ValueError) as error:
            raise WebPageUnavailableError(
                "No se pudieron reservar recursos para la lectura web aislada"
            ) from error

        launch_state = _LaunchState()
        launch_done = threading.Event()
        launcher = threading.Thread(
            target=_launch_web_reader_process,
            args=(
                process,
                send_socket,
                launch_state,
                launch_done,
                lease,
            ),
            name="web-reader-launcher",
            daemon=True,
        )
        try:
            launcher.start()
        except (OSError, RuntimeError) as error:
            raise WebPageUnavailableError(
                "No se pudo iniciar el supervisor de lectura web"
            ) from error

        remaining = deadline - monotonic()
        launched_in_time = remaining > 0 and launch_done.wait(remaining)
        if not launched_in_time:
            with launch_state.lock:
                launch_state.cancel_requested = True
                launch_completed_during_race = launch_state.complete
                process_started = launch_state.started
            if not launch_completed_during_race:
                # The launcher owns the still-blocked ``Process.start`` call.
                # When it returns, it will kill/reap any child and free the slot.
                lease_owned_by_main = False
                process = None
                send_socket = None
                raise WebPageUnavailableError(
                    "La lectura de la página agotó el tiempo"
                )

        with launch_state.lock:
            process_started = launch_state.started
            launch_error = launch_state.error
        if launch_error is not None or not process_started:
            raise WebPageUnavailableError(
                "No se pudo iniciar el proceso aislado de lectura web"
            ) from launch_error

        # The launcher closed the parent's sending endpoint after start.
        send_socket = None
        raw_message = _wait_for_worker_message(
            receive_socket,
            deadline=deadline,
        )
        received_message = True
        return _decode_worker_message(raw_message, source_url, config)
    finally:
        _safe_socket_close(receive_socket)
        _safe_socket_close(send_socket)
        if lease_owned_by_main:
            try:
                if process is not None:
                    if process_started:
                        _cleanup_worker_process(
                            process,
                            allow_normal_exit=received_message,
                        )
                    else:
                        _close_unstarted_process(process)
            finally:
                lease.release()


def _acquire_reader_admission(deadline: float) -> _AdmissionLease:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise WebPageUnavailableError("La lectura de la página agotó el tiempo")
    try:
        acquired = _WEB_READER_ADMISSION.acquire(
            timeout=min(remaining, WEB_READER_ADMISSION_WAIT_SECONDS)
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise WebPageUnavailableError(
            "No se pudo reservar capacidad para la lectura web"
        ) from error
    if not acquired:
        raise WebPageUnavailableError(
            "La capacidad de lectura web está ocupada; inténtalo de nuevo"
        )
    return _AdmissionLease(_WEB_READER_ADMISSION)


def _require_reader_deadline(deadline: float) -> None:
    if deadline - monotonic() <= 0:
        raise WebPageUnavailableError("La lectura de la página agotó el tiempo")


def _launch_web_reader_process(
    process,
    send_socket,
    state: _LaunchState,
    done: threading.Event,
    lease: _AdmissionLease,
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
    try:
        if started:
            _cleanup_worker_process(process, allow_normal_exit=False)
        else:
            _close_unstarted_process(process)
    except WebPageUnavailableError:
        logger.exception("Late web-reader process cleanup failed")
    finally:
        lease.release()


def _wait_for_worker_message(
    receive_socket,
    *,
    deadline: float,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    selector = None
    try:
        receive_socket.setblocking(False)
        selector = selectors.DefaultSelector()
        selector.register(receive_socket, selectors.EVENT_READ)
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise WebPageUnavailableError(
                    "La lectura de la página agotó el tiempo"
                )
            try:
                events = selector.select(remaining)
            except (OSError, RuntimeError, ValueError) as error:
                raise WebPageUnavailableError(
                    "No se pudo supervisar el proceso aislado de lectura web"
                ) from error
            if not events:
                raise WebPageUnavailableError(
                    "La lectura de la página agotó el tiempo"
                )
            try:
                chunk = receive_socket.recv(
                    min(
                        WEB_READER_IPC_CHUNK_BYTES,
                        MAX_WEB_PAGE_IPC_BYTES + 1 - total,
                    )
                )
            except BlockingIOError:
                continue
            except OSError as error:
                raise WebPageUnavailableError(
                    "El proceso aislado devolvió una respuesta no válida"
                ) from error
            if not chunk:
                if not chunks:
                    raise WebPageUnavailableError(
                        "El proceso aislado de lectura web terminó sin resultado"
                    )
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_WEB_PAGE_IPC_BYTES:
                raise WebPageUnavailableError(
                    "La respuesta aislada supera el límite de IPC"
                )
    except (OSError, RuntimeError, ValueError) as error:
        raise WebPageUnavailableError(
            "No se pudo supervisar el proceso aislado de lectura web"
        ) from error
    finally:
        if selector is not None:
            try:
                selector.close()
            except (OSError, RuntimeError, ValueError):
                pass


def _cleanup_worker_process(process, *, allow_normal_exit: bool) -> None:
    """Reap a worker with bounded joins and never return a live child."""
    try:
        if allow_normal_exit:
            process.join(timeout=WEB_READER_NORMAL_JOIN_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=WEB_READER_TERMINATE_JOIN_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=WEB_READER_KILL_JOIN_SECONDS)
        if process.is_alive():
            raise WebPageUnavailableError(
                "No se pudo detener el proceso aislado de lectura web"
            )
        process.join(timeout=0)
        process.close()
    except WebPageUnavailableError:
        raise
    except (OSError, RuntimeError, ValueError, AssertionError) as error:
        raise WebPageUnavailableError(
            "No se pudo limpiar el proceso aislado de lectura web"
        ) from error


def _close_unstarted_process(process) -> None:
    try:
        process.close()
    except (OSError, RuntimeError, ValueError, AssertionError) as error:
        raise WebPageUnavailableError(
            "No se pudo liberar el proceso aislado de lectura web"
        ) from error


def _safe_socket_close(value) -> None:
    if value is None:
        return
    try:
        value.close()
    except (OSError, RuntimeError, ValueError):
        pass


def _web_reader_worker(
    send_socket,
    source_url: str,
    config: _WebReaderConfig,
    deadline: float,
) -> None:
    """Child entry point. It sends exactly one bounded JSON message."""
    try:
        try:
            page = _read_web_page_in_worker(
                source_url,
                config=config,
                deadline=deadline,
            )
            message: dict[str, object] = {"status": "ok", "page": page.as_dict()}
        except UnsafeWebPageURLError as error:
            message = {"status": "unsafe", "message": _bounded_error(error)}
        except (TimeoutError, socket.timeout):
            message = {
                "status": "timeout",
                "message": "La lectura de la página agotó el tiempo",
            }
        except WebPageUnavailableError as error:
            message = {"status": "unavailable", "message": _bounded_error(error)}
        except (OSError, http.client.HTTPException, ssl.SSLError):
            message = {
                "status": "unavailable",
                "message": "No se pudo conectar con la página",
            }
        except BaseException:
            message = {
                "status": "unavailable",
                "message": "El proceso aislado no pudo leer la página",
            }
        _send_worker_message(send_socket, message)
    finally:
        _safe_socket_close(send_socket)


def _send_worker_message(send_socket, message: dict[str, object]) -> None:
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        payload = (
            b'{"status":"unavailable","message":"Respuesta aislada no '
            b'v\\u00e1lida"}'
        )
    if len(payload) > MAX_WEB_PAGE_IPC_BYTES:
        payload = json.dumps(
            {
                "status": "unavailable",
                "message": "La respuesta aislada supera el límite de IPC",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    try:
        send_socket.sendall(payload)
    except (BrokenPipeError, EOFError, OSError):
        # The caller timed out or disconnected and will reap this process.
        pass
    finally:
        try:
            send_socket.shutdown(socket.SHUT_WR)
        except (OSError, RuntimeError, ValueError):
            pass


def _decode_worker_message(
    raw_message: bytes,
    source_url: str,
    config: _WebReaderConfig,
) -> WebPage:
    try:
        message = json.loads(raw_message.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WebPageUnavailableError(
            "El proceso aislado devolvió una respuesta no válida"
        ) from error
    if not isinstance(message, dict):
        raise WebPageUnavailableError(
            "El proceso aislado devolvió una respuesta no válida"
        )
    status = message.get("status")
    if status == "ok":
        return _web_page_from_worker_payload(
            message.get("page"),
            expected_source_url=source_url,
            config=config,
        )
    worker_message = message.get("message")
    safe_message = (
        worker_message[:MAX_WEB_PAGE_WORKER_ERROR_CHARS]
        if isinstance(worker_message, str) and worker_message
        else "El proceso aislado no pudo leer la página"
    )
    if status == "unsafe":
        raise UnsafeWebPageURLError(safe_message)
    if isinstance(status, str) and status in {"timeout", "unavailable"}:
        raise WebPageUnavailableError(safe_message)
    raise WebPageUnavailableError(
        "El proceso aislado devolvió una respuesta no válida"
    )


def _web_page_from_worker_payload(
    value: object,
    *,
    expected_source_url: str,
    config: _WebReaderConfig,
) -> WebPage:
    if not isinstance(value, dict):
        raise WebPageUnavailableError("El resultado aislado no es válido")
    try:
        source_url = normalize_web_page_url(value["source_url"])
        final_url = normalize_web_page_url(value["final_url"])
        title = value["title"]
        content_type = value["content_type"]
        text = value["text"]
        content_length_bytes = value["content_length_bytes"]
        text_char_count = value["text_char_count"]
        text_sha256 = value["text_sha256"]
        text_truncated = value["text_truncated"]
        redirects = value["redirects"]
        raw_redirect_chain = value["redirect_chain"]
    except (KeyError, TypeError, UnsafeWebPageURLError) as error:
        raise WebPageUnavailableError("El resultado aislado no es válido") from error

    if source_url != expected_source_url or _url_origin(final_url) != _url_origin(
        source_url
    ):
        raise WebPageUnavailableError("El resultado aislado cambió el origen autorizado")
    if (
        not isinstance(title, str)
        or not title
        or len(title) > MAX_WEB_PAGE_TITLE_CHARS
        or not isinstance(content_type, str)
        or content_type not in ALLOWED_CONTENT_TYPES
        or not isinstance(text, str)
        or not text
        or len(text) > config.max_text_chars
        or isinstance(content_length_bytes, bool)
        or not isinstance(content_length_bytes, int)
        or not 0 <= content_length_bytes <= config.max_response_bytes
        or isinstance(text_char_count, bool)
        or not isinstance(text_char_count, int)
        or text_char_count != len(text)
        or not isinstance(text_sha256, str)
        or not SHA256_PATTERN.fullmatch(text_sha256)
        or text_sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest()
        or not isinstance(text_truncated, bool)
        or isinstance(redirects, bool)
        or not isinstance(redirects, int)
        or not 0 <= redirects <= config.max_redirects
        or value.get("untrusted_content") is not True
        or not isinstance(raw_redirect_chain, list)
        or len(raw_redirect_chain) != redirects + 1
    ):
        raise WebPageUnavailableError("El resultado aislado no es válido")
    try:
        redirect_chain = tuple(
            normalize_web_page_url(item) for item in raw_redirect_chain
        )
    except (TypeError, UnsafeWebPageURLError) as error:
        raise WebPageUnavailableError("El resultado aislado no es válido") from error
    if (
        not redirect_chain
        or redirect_chain[0] != source_url
        or redirect_chain[-1] != final_url
        or any(_url_origin(item) != _url_origin(source_url) for item in redirect_chain)
    ):
        raise WebPageUnavailableError("El resultado aislado no es válido")
    return WebPage(
        source_url=source_url,
        final_url=final_url,
        title=title,
        content_type=content_type,
        text=text,
        content_length_bytes=content_length_bytes,
        text_char_count=text_char_count,
        text_sha256=text_sha256,
        text_truncated=text_truncated,
        redirects=redirects,
        redirect_chain=redirect_chain,
    )


def _bounded_error(error: BaseException) -> str:
    message = str(error).strip()
    return (message or "No se pudo leer la página")[
        :MAX_WEB_PAGE_WORKER_ERROR_CHARS
    ]


def _read_web_page_in_worker(
    value: object,
    *,
    config: _WebReaderConfig,
    deadline: float,
) -> WebPage:
    """Perform the complete untrusted operation inside the disposable child."""
    source_url = normalize_web_page_url(value)
    if deadline - monotonic() <= 0:
        raise TimeoutError("web page total deadline exceeded")
    downloaded = _download(source_url, config=config, deadline=deadline)
    title, extracted = _extract_text(
        downloaded.body,
        content_type=downloaded.content_type,
        charset=downloaded.charset,
    )
    if deadline - monotonic() <= 0:
        raise TimeoutError("web page total deadline exceeded")
    text, truncated = _limit_text(extracted, config.max_text_chars)
    if not text:
        raise WebPageUnavailableError("La página no contiene texto extraíble")
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return WebPage(
        source_url=source_url,
        final_url=downloaded.final_url,
        title=(title or downloaded.final_url)[:MAX_WEB_PAGE_TITLE_CHARS],
        content_type=downloaded.content_type,
        text=text,
        content_length_bytes=len(downloaded.body),
        text_char_count=len(text),
        text_sha256=text_sha256,
        text_truncated=truncated,
        redirects=downloaded.redirects,
        redirect_chain=downloaded.redirect_chain,
    )


def _download(
    source_url: str,
    *,
    config: _WebReaderConfig,
    deadline: float,
) -> _DownloadedPage:
    current_url = source_url
    source_origin = _url_origin(source_url)
    redirect_chain = [source_url]
    for redirect_count in range(config.max_redirects + 1):
        parsed = urlsplit(current_url)
        remaining_timeout = deadline - monotonic()
        if remaining_timeout <= 0:
            raise TimeoutError("web page total deadline exceeded")
        addresses = _resolve_public_addresses(
            parsed,
            timeout=min(config.dns_timeout_seconds, remaining_timeout),
        )
        remaining_timeout = deadline - monotonic()
        if remaining_timeout <= 0:
            raise TimeoutError("web page total deadline exceeded")
        connection = _open_pinned_connection(
            parsed,
            addresses[0],
            timeout=remaining_timeout,
        )
        try:
            target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "text/html,application/xhtml+xml,text/plain",
                    "Accept-Encoding": "identity",
                    "User-Agent": f"AsistenteAyuntamientos/{config.app_version}",
                },
            )
            _set_connection_timeout(connection, deadline)
            response = connection.getresponse()
            status = int(response.status)
            if status in REDIRECT_STATUSES:
                location = response.getheader("Location")
                if redirect_count >= config.max_redirects:
                    raise WebPageUnavailableError(
                        "La página supera el límite de redirecciones"
                    )
                if not isinstance(location, str) or not location.strip():
                    raise WebPageUnavailableError(
                        "La página devolvió una redirección no válida"
                    )
                redirected_url = normalize_web_page_url(
                    urljoin(current_url, location)
                )
                if _url_origin(redirected_url) != source_origin:
                    raise UnsafeWebPageURLError(
                        "La página redirige a otro origen; realiza una nueva "
                        "búsqueda para autorizar esa URL"
                    )
                redirect_chain.append(redirected_url)
                current_url = redirected_url
                continue
            if status != 200:
                raise WebPageUnavailableError(
                    f"La página devolvió el estado HTTP {status}"
                )

            content_encoding = (response.getheader("Content-Encoding") or "identity")
            if content_encoding.strip().casefold() != "identity":
                raise WebPageUnavailableError(
                    "La página usa una codificación de contenido no admitida"
                )
            content_type, charset = _parse_content_type(
                response.getheader("Content-Type")
            )
            content_length = _parse_content_length(
                response.getheader("Content-Length")
            )
            if (
                content_length is not None
                and content_length > config.max_response_bytes
            ):
                raise WebPageUnavailableError(
                    "La página supera el límite máximo de bytes"
                )
            body = _read_bounded_body(
                response,
                connection,
                deadline=deadline,
                max_bytes=config.max_response_bytes,
            )
            if len(body) > config.max_response_bytes:
                raise WebPageUnavailableError(
                    "La página supera el límite máximo de bytes"
                )
            return _DownloadedPage(
                final_url=current_url,
                content_type=content_type,
                charset=charset,
                body=body,
                redirects=redirect_count,
                redirect_chain=tuple(redirect_chain),
            )
        finally:
            connection.close()

    raise WebPageUnavailableError("La página supera el límite de redirecciones")


def _url_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeWebPageURLError("url debe incluir un host")
    return (
        parsed.scheme.casefold(),
        hostname.rstrip(".").casefold(),
        parsed.port or (443 if parsed.scheme.casefold() == "https" else 80),
    )


def _read_bounded_body(response, connection, *, deadline: float, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read_chunk = getattr(response, "read1", None) or response.read
    while total <= max_bytes:
        remaining_timeout = deadline - monotonic()
        if remaining_timeout <= 0:
            raise TimeoutError("web page total deadline exceeded")
        connected_socket = getattr(connection, "sock", None)
        if connected_socket is not None:
            connected_socket.settimeout(remaining_timeout)
        chunk = read_chunk(min(64 * 1024, max_bytes + 1 - total))
        if not isinstance(chunk, bytes):
            raise WebPageUnavailableError(
                "La página devolvió un cuerpo de respuesta no válido"
            )
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _set_connection_timeout(connection, deadline: float) -> None:
    remaining_timeout = deadline - monotonic()
    if remaining_timeout <= 0:
        raise TimeoutError("web page total deadline exceeded")
    connected_socket = getattr(connection, "sock", None)
    if connected_socket is not None:
        connected_socket.settimeout(remaining_timeout)


def _resolve_public_addresses(
    parsed: SplitResult,
    *,
    timeout: float,
) -> tuple[str, ...]:
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeWebPageURLError("url debe incluir un host")
    _reject_blocked_hostname(hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    literal = _parse_ip_literal(hostname)
    if literal is not None:
        _require_public_ip(literal)
        return (str(literal),)

    if timeout <= 0:
        raise TimeoutError("web page DNS deadline exceeded")
    try:
        with _dns_deadline(timeout):
            records = socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
    except socket.gaierror as error:
        raise WebPageUnavailableError(
            "No se pudo resolver el host de la página"
        ) from error
    raw_addresses = tuple(str(record[4][0]) for record in records[:64])

    addresses: list[str] = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as error:
            raise UnsafeWebPageURLError(
                "El host devolvió una dirección no válida"
            ) from error
        _require_public_ip(address)
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    if not addresses:
        raise WebPageUnavailableError("El host de la página no tiene direcciones")
    return tuple(addresses)


@contextmanager
def _dns_deadline(timeout: float):
    """Bound libc DNS inside the already-isolated worker on Linux.

    The parent process remains the authoritative total deadline and will kill
    the whole worker even on platforms without ``setitimer``.
    """
    if timeout <= 0:
        raise TimeoutError("web page DNS deadline exceeded")
    if not all(
        hasattr(signal, name)
        for name in ("SIGALRM", "ITIMER_REAL", "setitimer")
    ):
        yield
        return

    def raise_dns_timeout(signum, frame) -> None:
        raise TimeoutError("web page DNS deadline exceeded")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_dns_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _reject_blocked_hostname(hostname: str) -> None:
    normalized = hostname.rstrip(".").casefold()
    if normalized in BLOCKED_HOSTNAMES or normalized.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeWebPageURLError("No se permiten hosts internos o de metadatos")
    if _parse_ip_literal(normalized) is None and "." not in normalized:
        raise UnsafeWebPageURLError("No se permiten nombres de host internos")


def _parse_ip_literal(hostname: str):
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _require_public_ip(address) -> None:
    mapped = getattr(address, "ipv4_mapped", None)
    candidate = mapped or address
    if (
        not candidate.is_global
        or candidate.is_private
        or candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
        or getattr(candidate, "sixtofour", None) is not None
        or getattr(candidate, "teredo", None) is not None
    ):
        raise UnsafeWebPageURLError(
            "No se permiten direcciones privadas, locales o reservadas"
        )


def _open_pinned_connection(
    parsed: SplitResult,
    address: str,
    *,
    timeout: float,
):
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeWebPageURLError("url debe incluir un host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return _PinnedHTTPSConnection(hostname, address, port, timeout=timeout)
    return _PinnedHTTPConnection(hostname, address, port, timeout=timeout)


def _parse_content_type(raw_value: str | None) -> tuple[str, str | None]:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise WebPageUnavailableError("La página no declara un tipo de contenido")
    parts = [part.strip() for part in raw_value.split(";")]
    content_type = parts[0].casefold()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise WebPageUnavailableError(
            f"El tipo de contenido {content_type or 'desconocido'} no está permitido"
        )
    charset = None
    for parameter in parts[1:]:
        name, separator, value = parameter.partition("=")
        if separator and name.strip().casefold() == "charset":
            charset = value.strip().strip('"').strip("'")
            if not CHARSET_PATTERN.fullmatch(charset):
                raise WebPageUnavailableError(
                    "La página declara una codificación de texto no válida"
                )
            break
    return content_type, charset


def _parse_content_length(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    if not normalized.isdecimal():
        raise WebPageUnavailableError(
            "La página declara un tamaño de respuesta no válido"
        )
    return int(normalized)


def _extract_text(
    body: bytes,
    *,
    content_type: str,
    charset: str | None,
) -> tuple[str, str]:
    decoded = _decode_text(body, charset)
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _TextHTMLParser()
        try:
            parser.feed(decoded)
            parser.close()
        except (AssertionError, ValueError) as error:
            raise WebPageUnavailableError("No se pudo extraer el HTML") from error
        return parser.title, parser.text
    return "", _normalize_extracted_text(decoded)


def _decode_text(body: bytes, charset: str | None) -> str:
    encoding = charset or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError as error:
        raise WebPageUnavailableError(
            "La página declara una codificación de texto desconocida"
        ) from error


def _normalize_inline_text(value: str) -> str:
    return " ".join(_strip_unsafe_controls(value).split())


def _normalize_extracted_text(value: str) -> str:
    normalized = _strip_unsafe_controls(value)
    normalized = re.sub(r"[ \t\f\v]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _strip_unsafe_controls(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        char
        if char in {"\n", "\t"} or unicodedata.category(char) not in {"Cc", "Cf"}
        else " "
        for char in normalized
    )


def _limit_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit].rstrip(), True
