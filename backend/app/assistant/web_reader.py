"""Read public web pages without exposing an unrestricted server-side fetcher.

The reader is intentionally narrower than a browser: it performs anonymous
GET requests, never executes JavaScript, and only accepts bounded textual
HTML, plain text, or PDF responses. DNS answers are validated before the
connection and the socket is pinned to the validated address so a DNS
rebinding cannot redirect the request into a private network.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from time import monotonic
from urllib.parse import SplitResult, quote, urljoin, urlsplit, urlunsplit

from pypdf import PdfReader

from app.core.config import settings

MAX_WEB_PAGE_URL_CHARS = 2000
MAX_WEB_PAGE_TITLE_CHARS = 300
MAX_PDF_PAGES = 40
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
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
    text_truncated: bool
    redirects: int

    def as_dict(self) -> dict[str, object]:
        return {
            "source_url": self.source_url,
            "final_url": self.final_url,
            "title": self.title,
            "content_type": self.content_type,
            "text": self.text,
            "text_truncated": self.text_truncated,
            "redirects": self.redirects,
            "untrusted_content": True,
        }


@dataclass(frozen=True)
class _DownloadedPage:
    final_url: str
    content_type: str
    charset: str | None
    body: bytes
    redirects: int


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
    if not 1 <= effective_port <= 65535:
        raise UnsafeWebPageURLError("url contiene un puerto no válido")
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
    """Download and extract a public page under strict SSRF and size controls."""
    source_url = normalize_web_page_url(value)
    try:
        downloaded = _download(source_url)
        title, extracted = _extract_text(
            downloaded.body,
            content_type=downloaded.content_type,
            charset=downloaded.charset,
        )
    except (UnsafeWebPageURLError, WebPageUnavailableError):
        raise
    except (TimeoutError, socket.timeout) as error:
        raise WebPageUnavailableError("La lectura de la página agotó el tiempo") from error
    except (OSError, http.client.HTTPException, ssl.SSLError) as error:
        raise WebPageUnavailableError("No se pudo conectar con la página") from error

    text, truncated = _limit_text(extracted, settings.web_page_max_text_chars)
    if not text:
        raise WebPageUnavailableError("La página no contiene texto extraíble")
    return WebPage(
        source_url=source_url,
        final_url=downloaded.final_url,
        title=(title or downloaded.final_url)[:MAX_WEB_PAGE_TITLE_CHARS],
        content_type=downloaded.content_type,
        text=text,
        text_truncated=truncated,
        redirects=downloaded.redirects,
    )


def _download(source_url: str) -> _DownloadedPage:
    current_url = source_url
    deadline = monotonic() + settings.web_page_timeout_seconds
    for redirect_count in range(settings.web_page_max_redirects + 1):
        parsed = urlsplit(current_url)
        addresses = _resolve_public_addresses(parsed)
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
                    "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.9",
                    "Accept-Encoding": "identity",
                    "User-Agent": f"AsistenteAyuntamientos/{settings.app_version}",
                },
            )
            response = connection.getresponse()
            status = int(response.status)
            if status in REDIRECT_STATUSES:
                location = response.getheader("Location")
                if redirect_count >= settings.web_page_max_redirects:
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
                if (
                    parsed.scheme == "https"
                    and urlsplit(redirected_url).scheme != "https"
                ):
                    raise UnsafeWebPageURLError(
                        "No se permiten redirecciones de https a http"
                    )
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
                and content_length > settings.web_page_max_response_bytes
            ):
                raise WebPageUnavailableError(
                    "La página supera el límite máximo de bytes"
                )
            body = _read_bounded_body(
                response,
                connection,
                deadline=deadline,
                max_bytes=settings.web_page_max_response_bytes,
            )
            if len(body) > settings.web_page_max_response_bytes:
                raise WebPageUnavailableError(
                    "La página supera el límite máximo de bytes"
                )
            return _DownloadedPage(
                final_url=current_url,
                content_type=content_type,
                charset=charset,
                body=body,
                redirects=redirect_count,
            )
        finally:
            connection.close()

    raise WebPageUnavailableError("La página supera el límite de redirecciones")


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


def _resolve_public_addresses(parsed: SplitResult) -> tuple[str, ...]:
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeWebPageURLError("url debe incluir un host")
    _reject_blocked_hostname(hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    literal = _parse_ip_literal(hostname)
    if literal is not None:
        _require_public_ip(literal)
        return (str(literal),)

    try:
        records = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise WebPageUnavailableError("No se pudo resolver el host de la página") from error

    addresses: list[str] = []
    for record in records:
        raw_address = record[4][0]
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
    if content_type == "application/pdf":
        return _extract_pdf_text(body)

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


def _extract_pdf_text(body: bytes) -> tuple[str, str]:
    try:
        reader = PdfReader(BytesIO(body), strict=False)
        if reader.is_encrypted:
            raise WebPageUnavailableError("No se admiten documentos PDF cifrados")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise WebPageUnavailableError(
                f"El PDF supera el límite de {MAX_PDF_PAGES} páginas"
            )
        text_parts: list[str] = []
        remaining = settings.web_page_max_text_chars + 1
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text[:remaining])
            remaining -= len(text_parts[-1])
            if remaining <= 0:
                break
        text = "\n\n".join(text_parts)
        metadata_title = reader.metadata.title if reader.metadata else None
    except WebPageUnavailableError:
        raise
    except Exception as error:
        raise WebPageUnavailableError("No se pudo extraer el PDF") from error
    return _normalize_inline_text(metadata_title or ""), _normalize_extracted_text(text)


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
