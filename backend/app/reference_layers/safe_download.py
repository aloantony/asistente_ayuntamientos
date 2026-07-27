"""Bounded HTTPS streaming for reviewed public reference-data sources.

This module is intentionally independent from the assistant web reader.  It
reuses the same security boundary -- exact URL authorization, public DNS only,
IP pinning, hostname-based TLS verification and bounded redirects -- while
streaming large binary artifacts to a caller-provided sink instead of carrying
the response through process IPC or memory.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import math
import multiprocessing
import re
import socket
import ssl
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Protocol
from urllib.parse import SplitResult, quote, urljoin, urlsplit, urlunsplit


MAX_URL_CHARS = 8192
MAX_REQUEST_TARGET_CHARS = 8192
MAX_HEADER_VALUE_CHARS = 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
BLOCKED_HOSTNAMES = frozenset(
    {
        "instance-data",
        "metadata",
        "metadata.google",
        "metadata.google.internal",
        "localhost",
    }
)
BLOCKED_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost")
NON_PUBLIC_IPV6_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
CONTENT_LENGTH_RE = re.compile(r"^[0-9]{1,20}$", re.ASCII)
CONTENT_RANGE_RE = re.compile(
    r"^bytes (?P<start>[0-9]{1,20})-(?P<end>[0-9]{1,20})/" r"(?P<total>[0-9]{1,20})$",
    re.ASCII,
)
MEDIA_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]{1,127}/[a-z0-9!#$&^_.+-]{1,127}$")


class _StreamingSink(Protocol):
    def write(self, data: bytes) -> int | None: ...


class SafeDownloadError(Exception):
    """A classified failure suitable for durable retry policy."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class UnsafeDownloadURLError(SafeDownloadError, ValueError):
    def __init__(self, message: str, *, code: str = "unsafe_url") -> None:
        super().__init__(message, code=code, retryable=False)


class DownloadUnavailableError(SafeDownloadError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "upstream_unavailable",
        retryable: bool = True,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)


class DownloadLimitError(SafeDownloadError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, retryable=False)


class DownloadIntegrityError(SafeDownloadError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "response_integrity",
        retryable: bool = True,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)


class DownloadHTTPError(SafeDownloadError):
    def __init__(
        self,
        status_code: int,
        *,
        retry_after_seconds: int | None,
    ) -> None:
        self.status_code = status_code
        retryable = status_code in RETRYABLE_HTTP_STATUSES
        super().__init__(
            f"reference source returned HTTP {status_code}",
            code=f"http_{status_code}",
            retryable=retryable,
            retry_after_seconds=retry_after_seconds if retryable else None,
        )


@dataclass(frozen=True)
class HTTPSDownloadPolicy:
    allowed_origins: tuple[str, ...]
    max_response_bytes: int
    timeout_seconds: float = 600.0
    dns_timeout_seconds: float = 3.0
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 60.0
    max_redirects: int = 3
    allowed_content_types: frozenset[str] | None = None
    user_agent: str = "AsistenteAyuntamientos/reference-sync"
    require_nonempty: bool = True

    def __post_init__(self) -> None:
        if not self.allowed_origins:
            raise ValueError("allowed_origins cannot be empty")
        normalized_origins = tuple(
            dict.fromkeys(
                _normalize_allowed_origin(value) for value in self.allowed_origins
            )
        )
        object.__setattr__(self, "allowed_origins", normalized_origins)
        _positive_integer(self.max_response_bytes, "max_response_bytes")
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("dns_timeout_seconds", self.dns_timeout_seconds),
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("idle_timeout_seconds", self.idle_timeout_seconds),
        ):
            _positive_finite(value, name)
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or not 0 <= self.max_redirects <= 10
        ):
            raise ValueError("max_redirects must be between 0 and 10")
        _safe_header_value(self.user_agent, "user_agent", allow_empty=False)
        if self.allowed_content_types is not None:
            normalized_types = frozenset(
                _normalize_media_type(value) for value in self.allowed_content_types
            )
            if not normalized_types:
                raise ValueError("allowed_content_types cannot be empty")
            object.__setattr__(self, "allowed_content_types", normalized_types)
        if not isinstance(self.require_nonempty, bool):
            raise ValueError("require_nonempty must be boolean")


@dataclass(frozen=True)
class HTTPSDownloadResult:
    source_url: str
    final_url: str
    status_code: int
    not_modified: bool
    content_type: str | None
    size_bytes: int
    sha256: str | None
    etag: str | None
    last_modified: str | None
    redirects: int
    redirect_chain: tuple[str, ...]


@dataclass(frozen=True)
class HTTPSHeadResult:
    source_url: str
    final_url: str
    status_code: int
    content_type: str
    content_length: int | None
    accept_ranges: str | None
    etag: str | None
    last_modified: str | None
    redirects: int
    redirect_chain: tuple[str, ...]


@dataclass(frozen=True)
class HTTPSRangeResult:
    source_url: str
    final_url: str
    status_code: int
    content_type: str
    content_length: int
    object_size: int
    range_start: int
    range_end: int
    size_bytes: int
    sha256: str
    etag: str | None
    last_modified: str | None
    redirects: int
    redirect_chain: tuple[str, ...]


@dataclass
class _DNSLaunchState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    complete: bool = False
    started: bool = False
    cancel_requested: bool = False
    error: BaseException | None = None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        address: str,
        port: int,
        *,
        timeout: float,
    ) -> None:
        super().__init__(
            hostname,
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


class SafeHTTPSDownloader:
    def __init__(self, policy: HTTPSDownloadPolicy) -> None:
        self.policy = policy
        self._allowed_origins = frozenset(
            _url_origin(urlsplit(origin)) for origin in policy.allowed_origins
        )

    def download(
        self,
        url: str,
        sink: _StreamingSink,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
    ) -> HTTPSDownloadResult:
        source_url = self._authorize_url(url)
        conditional_etag = _optional_request_header(etag, "etag")
        conditional_modified = _optional_request_header(
            last_modified,
            "last_modified",
        )
        accepted = _optional_request_header(accept, "accept") or "*/*"
        deadline = monotonic() + self.policy.timeout_seconds
        current_url = source_url
        redirect_chain = [source_url]
        seen = {source_url}

        for redirect_count in range(self.policy.max_redirects + 1):
            parsed = urlsplit(current_url)
            connection, response = self._request_once(
                parsed,
                deadline=deadline,
                etag=conditional_etag,
                last_modified=conditional_modified,
                accept=accepted,
            )
            try:
                status_code = int(response.status)
                if status_code in REDIRECT_STATUSES:
                    if redirect_count >= self.policy.max_redirects:
                        raise DownloadLimitError(
                            "reference source exceeded redirect limit",
                            code="redirect_limit",
                        )
                    location = _bounded_response_header(
                        response.getheader("Location"),
                        "Location",
                        required=True,
                    )
                    redirected = self._authorize_url(urljoin(current_url, location))
                    if redirected in seen:
                        raise UnsafeDownloadURLError(
                            "reference source contains a redirect loop",
                            code="redirect_loop",
                        )
                    seen.add(redirected)
                    redirect_chain.append(redirected)
                    current_url = redirected
                    continue

                response_etag = _bounded_response_header(
                    response.getheader("ETag"),
                    "ETag",
                )
                response_modified = _bounded_response_header(
                    response.getheader("Last-Modified"),
                    "Last-Modified",
                )
                if status_code == 304:
                    if conditional_etag is None and conditional_modified is None:
                        raise DownloadIntegrityError(
                            "unexpected HTTP 304 without a conditional request",
                            code="unexpected_not_modified",
                            retryable=False,
                        )
                    return HTTPSDownloadResult(
                        source_url=source_url,
                        final_url=current_url,
                        status_code=304,
                        not_modified=True,
                        content_type=None,
                        size_bytes=0,
                        sha256=None,
                        etag=response_etag or conditional_etag,
                        last_modified=response_modified or conditional_modified,
                        redirects=redirect_count,
                        redirect_chain=tuple(redirect_chain),
                    )
                if status_code != 200:
                    raise DownloadHTTPError(
                        status_code,
                        retry_after_seconds=_retry_after_seconds(
                            response.getheader("Retry-After")
                        ),
                    )

                encoding = _bounded_response_header(
                    response.getheader("Content-Encoding"),
                    "Content-Encoding",
                )
                if encoding is not None and encoding.casefold() != "identity":
                    raise DownloadIntegrityError(
                        "compressed transfer encodings are not accepted",
                        code="content_encoding",
                        retryable=False,
                    )
                content_type = _response_content_type(
                    response.getheader("Content-Type")
                )
                if (
                    self.policy.allowed_content_types is not None
                    and content_type not in self.policy.allowed_content_types
                ):
                    raise DownloadIntegrityError(
                        "reference source returned an unsupported content type",
                        code="content_type",
                        retryable=False,
                    )
                declared_size = _content_length(response.getheader("Content-Length"))
                if (
                    declared_size is not None
                    and declared_size > self.policy.max_response_bytes
                ):
                    raise DownloadLimitError(
                        "reference source exceeds its byte limit",
                        code="response_too_large",
                    )

                size_bytes, digest = self._stream_body(
                    response,
                    connection,
                    sink,
                    deadline=deadline,
                )
                if declared_size is not None and declared_size != size_bytes:
                    raise DownloadIntegrityError(
                        "reference source returned an incomplete body",
                        code="content_length_mismatch",
                    )
                if self.policy.require_nonempty and size_bytes == 0:
                    raise DownloadIntegrityError(
                        "reference source returned an empty body",
                        code="empty_response",
                        retryable=False,
                    )
                return HTTPSDownloadResult(
                    source_url=source_url,
                    final_url=current_url,
                    status_code=200,
                    not_modified=False,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    sha256=digest,
                    etag=response_etag,
                    last_modified=response_modified,
                    redirects=redirect_count,
                    redirect_chain=tuple(redirect_chain),
                )
            finally:
                _close_connection(connection)

        raise DownloadLimitError(
            "reference source exceeded redirect limit",
            code="redirect_limit",
        )

    def head(
        self,
        url: str,
        *,
        accept: str | None = None,
    ) -> HTTPSHeadResult:
        """Read bounded response metadata without reading an object body."""

        source_url = self._authorize_url(url)
        accepted = _optional_request_header(accept, "accept") or "*/*"
        deadline = monotonic() + self.policy.timeout_seconds
        current_url = source_url
        redirect_chain = [source_url]
        seen = {source_url}
        headers = {
            "Accept": accepted,
            "Accept-Encoding": "identity",
            "User-Agent": self.policy.user_agent,
        }

        for redirect_count in range(self.policy.max_redirects + 1):
            parsed = urlsplit(current_url)
            connection, response = self._request_once_with_headers(
                parsed,
                deadline=deadline,
                method="HEAD",
                headers=headers,
            )
            try:
                status_code = int(response.status)
                if status_code in REDIRECT_STATUSES:
                    current_url = self._redirect_url(
                        current_url,
                        response,
                        redirect_count=redirect_count,
                        redirect_chain=redirect_chain,
                        seen=seen,
                    )
                    continue
                if status_code != 200:
                    raise DownloadHTTPError(
                        status_code,
                        retry_after_seconds=_retry_after_seconds(
                            response.getheader("Retry-After")
                        ),
                    )
                _require_identity_encoding(response)
                content_type = _response_content_type(
                    response.getheader("Content-Type")
                )
                if (
                    self.policy.allowed_content_types is not None
                    and content_type not in self.policy.allowed_content_types
                ):
                    raise DownloadIntegrityError(
                        "reference source returned an unsupported content type",
                        code="content_type",
                        retryable=False,
                    )
                return HTTPSHeadResult(
                    source_url=source_url,
                    final_url=current_url,
                    status_code=status_code,
                    content_type=content_type,
                    content_length=_content_length(
                        response.getheader("Content-Length")
                    ),
                    accept_ranges=_bounded_response_header(
                        response.getheader("Accept-Ranges"),
                        "Accept-Ranges",
                    ),
                    etag=_bounded_response_header(
                        response.getheader("ETag"),
                        "ETag",
                    ),
                    last_modified=_bounded_response_header(
                        response.getheader("Last-Modified"),
                        "Last-Modified",
                    ),
                    redirects=redirect_count,
                    redirect_chain=tuple(redirect_chain),
                )
            finally:
                _close_connection(connection)

        raise DownloadLimitError(
            "reference source exceeded redirect limit",
            code="redirect_limit",
        )

    def download_range(
        self,
        url: str,
        sink: _StreamingSink,
        *,
        start: int,
        end: int,
        if_match: str,
        accept: str | None = None,
    ) -> HTTPSRangeResult:
        """Download one exact byte range under a strong object validator."""

        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 0
            or isinstance(end, bool)
            or not isinstance(end, int)
            or end < start
        ):
            raise ValueError("byte range is invalid")
        expected_size = end - start + 1
        if expected_size > self.policy.max_response_bytes:
            raise DownloadLimitError(
                "requested byte range exceeds its byte limit",
                code="response_too_large",
            )
        source_url = self._authorize_url(url)
        accepted = _optional_request_header(accept, "accept") or "*/*"
        validator = _optional_request_header(if_match, "if_match")
        if validator is None or validator.startswith("W/"):
            raise ValueError("if_match must be a strong ETag")
        deadline = monotonic() + self.policy.timeout_seconds
        current_url = source_url
        redirect_chain = [source_url]
        seen = {source_url}
        headers = {
            "Accept": accepted,
            "Accept-Encoding": "identity",
            "If-Match": validator,
            "Range": f"bytes={start}-{end}",
            "User-Agent": self.policy.user_agent,
        }

        for redirect_count in range(self.policy.max_redirects + 1):
            parsed = urlsplit(current_url)
            connection, response = self._request_once_with_headers(
                parsed,
                deadline=deadline,
                method="GET",
                headers=headers,
            )
            try:
                status_code = int(response.status)
                if status_code in REDIRECT_STATUSES:
                    current_url = self._redirect_url(
                        current_url,
                        response,
                        redirect_count=redirect_count,
                        redirect_chain=redirect_chain,
                        seen=seen,
                    )
                    continue
                if status_code == 200:
                    raise DownloadIntegrityError(
                        "reference source ignored the requested byte range",
                        code="range_not_honored",
                        retryable=False,
                    )
                if status_code != 206:
                    raise DownloadHTTPError(
                        status_code,
                        retry_after_seconds=_retry_after_seconds(
                            response.getheader("Retry-After")
                        ),
                    )
                _require_identity_encoding(response)
                content_type = _response_content_type(
                    response.getheader("Content-Type")
                )
                if (
                    self.policy.allowed_content_types is not None
                    and content_type not in self.policy.allowed_content_types
                ):
                    raise DownloadIntegrityError(
                        "reference source returned an unsupported content type",
                        code="content_type",
                        retryable=False,
                    )
                declared_size = _content_length(response.getheader("Content-Length"))
                if declared_size != expected_size:
                    raise DownloadIntegrityError(
                        "reference source returned an invalid range length",
                        code="content_range_mismatch",
                        retryable=True,
                    )
                range_value = _bounded_response_header(
                    response.getheader("Content-Range"),
                    "Content-Range",
                    required=True,
                )
                match = (
                    CONTENT_RANGE_RE.fullmatch(range_value)
                    if range_value is not None
                    else None
                )
                if match is None:
                    raise DownloadIntegrityError(
                        "reference source returned an invalid Content-Range",
                        code="content_range_mismatch",
                        retryable=False,
                    )
                observed_start = int(match.group("start"))
                observed_end = int(match.group("end"))
                object_size = int(match.group("total"))
                if observed_start != start or observed_end != end or object_size <= end:
                    raise DownloadIntegrityError(
                        "reference source returned a different byte range",
                        code="content_range_mismatch",
                        retryable=True,
                    )
                response_etag = _bounded_response_header(
                    response.getheader("ETag"),
                    "ETag",
                )
                if response_etag != validator:
                    raise DownloadIntegrityError(
                        "reference source changed during its ranged read",
                        code="validator_mismatch",
                        retryable=True,
                    )
                size_bytes, digest = self._stream_body(
                    response,
                    connection,
                    sink,
                    deadline=deadline,
                )
                if size_bytes != expected_size:
                    raise DownloadIntegrityError(
                        "reference source returned a truncated byte range",
                        code="content_range_mismatch",
                        retryable=True,
                    )
                return HTTPSRangeResult(
                    source_url=source_url,
                    final_url=current_url,
                    status_code=status_code,
                    content_type=content_type,
                    content_length=declared_size,
                    object_size=object_size,
                    range_start=start,
                    range_end=end,
                    size_bytes=size_bytes,
                    sha256=digest,
                    etag=response_etag,
                    last_modified=_bounded_response_header(
                        response.getheader("Last-Modified"),
                        "Last-Modified",
                    ),
                    redirects=redirect_count,
                    redirect_chain=tuple(redirect_chain),
                )
            finally:
                _close_connection(connection)

        raise DownloadLimitError(
            "reference source exceeded redirect limit",
            code="redirect_limit",
        )

    def _authorize_url(self, value: object) -> str:
        normalized = normalize_https_url(value)
        if _url_origin(urlsplit(normalized)) not in self._allowed_origins:
            raise UnsafeDownloadURLError(
                "reference source URL is outside its exact allowlist",
                code="origin_not_allowed",
            )
        return normalized

    def _request_once(
        self,
        parsed: SplitResult,
        *,
        deadline: float,
        etag: str | None,
        last_modified: str | None,
        accept: str,
    ):
        headers = {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "User-Agent": self.policy.user_agent,
        }
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified
        return self._request_once_with_headers(
            parsed,
            deadline=deadline,
            method="GET",
            headers=headers,
        )

    def _request_once_with_headers(
        self,
        parsed: SplitResult,
        *,
        deadline: float,
        method: str,
        headers: dict[str, str],
    ):
        if method not in {"GET", "HEAD"}:
            raise ValueError("HTTPS request method is unsupported")
        remaining = _remaining(deadline)
        addresses = _resolve_public_addresses(
            parsed,
            timeout=min(self.policy.dns_timeout_seconds, remaining),
        )
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        if len(target) > MAX_REQUEST_TARGET_CHARS:
            raise UnsafeDownloadURLError(
                "reference source request target is too long",
                code="request_target_too_long",
            )
        last_error: BaseException | None = None
        for address in addresses:
            connection = None
            try:
                connection = _open_pinned_connection(
                    parsed,
                    address,
                    timeout=min(
                        self.policy.connect_timeout_seconds,
                        _remaining(deadline),
                    ),
                )
                connection.request(method, target, headers=headers)
                _set_connection_timeout(
                    connection,
                    deadline,
                    idle_timeout=self.policy.idle_timeout_seconds,
                )
                return connection, connection.getresponse()
            except ssl.SSLCertVerificationError as error:
                _close_connection(connection)
                raise DownloadUnavailableError(
                    "reference source TLS certificate is invalid",
                    code="tls_certificate",
                    retryable=False,
                ) from error
            except (socket.timeout, TimeoutError) as error:
                _close_connection(connection)
                last_error = error
            except (OSError, http.client.HTTPException, ssl.SSLError) as error:
                _close_connection(connection)
                last_error = error
        if monotonic() >= deadline:
            raise DownloadUnavailableError(
                "reference source exceeded its total deadline",
                code="total_timeout",
            ) from last_error
        raise DownloadUnavailableError(
            "reference source is unavailable",
            code="connection_failed",
        ) from last_error

    def _redirect_url(
        self,
        current_url: str,
        response,
        *,
        redirect_count: int,
        redirect_chain: list[str],
        seen: set[str],
    ) -> str:
        if redirect_count >= self.policy.max_redirects:
            raise DownloadLimitError(
                "reference source exceeded redirect limit",
                code="redirect_limit",
            )
        location = _bounded_response_header(
            response.getheader("Location"),
            "Location",
            required=True,
        )
        redirected = self._authorize_url(urljoin(current_url, location))
        if redirected in seen:
            raise UnsafeDownloadURLError(
                "reference source contains a redirect loop",
                code="redirect_loop",
            )
        seen.add(redirected)
        redirect_chain.append(redirected)
        return redirected

    def _stream_body(
        self,
        response,
        connection,
        sink: _StreamingSink,
        *,
        deadline: float,
    ) -> tuple[int, str]:
        total = 0
        digest = hashlib.sha256()
        read_chunk = getattr(response, "read1", None) or response.read
        while True:
            _set_connection_timeout(
                connection,
                deadline,
                idle_timeout=self.policy.idle_timeout_seconds,
            )
            try:
                chunk = read_chunk(
                    min(
                        DOWNLOAD_CHUNK_BYTES,
                        self.policy.max_response_bytes + 1 - total,
                    )
                )
            except (socket.timeout, TimeoutError) as error:
                raise DownloadUnavailableError(
                    "reference source stalled while streaming",
                    code="idle_timeout",
                ) from error
            except (OSError, http.client.HTTPException) as error:
                raise DownloadUnavailableError(
                    "reference source failed while streaming",
                    code="body_read_failed",
                ) from error
            if not isinstance(chunk, bytes):
                raise DownloadIntegrityError(
                    "reference source returned a non-bytes body",
                    code="invalid_body",
                    retryable=False,
                )
            if not chunk:
                break
            total += len(chunk)
            if total > self.policy.max_response_bytes:
                raise DownloadLimitError(
                    "reference source exceeds its byte limit",
                    code="response_too_large",
                )
            _write_all(sink, chunk)
            digest.update(chunk)
            _remaining(deadline)
        return total, digest.hexdigest()


def normalize_https_url(value: object) -> str:
    if not isinstance(value, str):
        raise UnsafeDownloadURLError("reference source URL must be text")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > MAX_URL_CHARS:
        raise UnsafeDownloadURLError("reference source URL has an invalid length")
    if (
        any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized)
        or any(character.isspace() for character in normalized)
        or "\\" in normalized
    ):
        raise UnsafeDownloadURLError("reference source URL contains unsafe characters")
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as error:
        raise UnsafeDownloadURLError("reference source URL is invalid") from error
    if parsed.scheme.casefold() != "https":
        raise UnsafeDownloadURLError("reference source URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeDownloadURLError("reference source URL cannot contain credentials")
    if parsed.fragment:
        raise UnsafeDownloadURLError("reference source URL cannot contain a fragment")
    if not parsed.hostname:
        raise UnsafeDownloadURLError("reference source URL must contain a hostname")
    if port not in {None, 443}:
        raise UnsafeDownloadURLError("reference source URL must use port 443")

    hostname = _canonical_hostname(parsed.hostname)
    literal = _parse_ip_literal(hostname)
    if literal is not None:
        _require_public_ip(literal)
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="%=&?/:@!$'()*+,;-._~")
    return urlunsplit(("https", rendered_host, path, query, ""))


def _normalize_allowed_origin(value: object) -> str:
    normalized = normalize_https_url(value)
    parsed = urlsplit(normalized)
    if parsed.path not in {"", "/"} or parsed.query:
        raise ValueError("allowed origins cannot include a path or query")
    hostname = parsed.hostname or ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{rendered_host}"


def _canonical_hostname(value: str) -> str:
    hostname = value.rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise UnsafeDownloadURLError("reference source hostname is invalid") from error
    if not hostname or len(hostname) > 253:
        raise UnsafeDownloadURLError("reference source hostname is invalid")
    if _parse_ip_literal(hostname) is None:
        if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_HOST_SUFFIXES):
            raise UnsafeDownloadURLError("internal and metadata hosts are forbidden")
        labels = hostname.split(".")
        if len(labels) < 2 or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        ):
            raise UnsafeDownloadURLError("reference source hostname is invalid")
    return hostname


def _url_origin(parsed: SplitResult) -> tuple[str, str, int]:
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeDownloadURLError("reference source URL has no hostname")
    return (
        parsed.scheme.casefold(),
        hostname.rstrip(".").casefold(),
        parsed.port or 443,
    )


def _resolve_public_addresses(
    parsed: SplitResult,
    *,
    timeout: float,
) -> tuple[str, ...]:
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeDownloadURLError("reference source URL has no hostname")
    literal = _parse_ip_literal(hostname)
    if literal is not None:
        _require_public_ip(literal)
        return (str(literal),)
    if timeout <= 0:
        raise DownloadUnavailableError(
            "reference source DNS timed out", code="dns_timeout"
        )
    deadline = monotonic() + timeout

    receive = None
    send = None
    process = None
    process_owned_by_main = True
    process_started = False
    try:
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=_dns_worker,
            args=(send, hostname, parsed.port or 443),
            daemon=True,
        )
        if deadline - monotonic() <= 0:
            raise DownloadUnavailableError(
                "reference source DNS timed out",
                code="dns_timeout",
            )
        launch_state = _DNSLaunchState()
        launch_done = threading.Event()
        launcher = threading.Thread(
            target=_launch_dns_process,
            args=(process, send, launch_state, launch_done),
            name="reference-dns-launcher",
            daemon=True,
        )
        launcher.start()
        send = None
        remaining = deadline - monotonic()
        launched_in_time = remaining > 0 and launch_done.wait(remaining)
        if not launched_in_time:
            with launch_state.lock:
                launch_state.cancel_requested = True
                launch_completed_during_race = launch_state.complete
            if not launch_completed_during_race:
                process_owned_by_main = False
                raise DownloadUnavailableError(
                    "reference source DNS timed out",
                    code="dns_timeout",
                )

        with launch_state.lock:
            process_started = launch_state.started
            launch_error = launch_state.error
        if launch_error is not None or not process_started:
            raise DownloadUnavailableError(
                "reference source DNS failed",
                code="dns_failed",
            ) from launch_error

        remaining = deadline - monotonic()
        if remaining <= 0 or not receive.poll(remaining):
            raise DownloadUnavailableError(
                "reference source DNS timed out",
                code="dns_timeout",
            )
        try:
            message = receive.recv()
        except (EOFError, OSError) as error:
            raise DownloadUnavailableError(
                "reference source DNS failed",
                code="dns_failed",
            ) from error
        if (
            not isinstance(message, tuple)
            or len(message) != 2
            or message[0] != "ok"
            or not isinstance(message[1], list)
        ):
            raise DownloadUnavailableError(
                "reference source DNS failed",
                code="dns_failed",
            )
        return _require_public_addresses(message[1])
    except SafeDownloadError:
        raise
    except Exception as error:
        raise DownloadUnavailableError(
            "reference source DNS failed",
            code="dns_failed",
        ) from error
    finally:
        if receive is not None:
            try:
                receive.close()
            except OSError:
                pass
        if send is not None:
            try:
                send.close()
            except OSError:
                pass
        if process is not None and process_owned_by_main:
            _cleanup_dns_process(process, started=process_started)


def _launch_dns_process(process, send, state: _DNSLaunchState, done) -> None:
    started = False
    error: BaseException | None = None
    try:
        process.start()
        started = True
    except BaseException as launch_error:
        error = launch_error
    finally:
        try:
            send.close()
        except OSError:
            pass

    with state.lock:
        state.started = started
        state.error = error
        state.complete = True
        cleanup_late_launch = state.cancel_requested
        done.set()

    if cleanup_late_launch:
        _cleanup_dns_process(process, started=started)


def _cleanup_dns_process(process, *, started: bool) -> None:
    try:
        if started:
            process.join(timeout=0.05)
        if started and process.is_alive():
            process.terminate()
            process.join(timeout=0.15)
        if started and process.is_alive():
            process.kill()
            process.join(timeout=0.35)
    except (AssertionError, OSError, RuntimeError, ValueError):
        pass
    try:
        process.close()
    except (AssertionError, OSError, RuntimeError, ValueError):
        pass


def _dns_worker(send, hostname: str, port: int) -> None:
    try:
        records = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        addresses = list(dict.fromkeys(str(record[4][0]) for record in records[:64]))
        send.send(("ok", addresses))
    except (OSError, UnicodeError):
        try:
            send.send(("error", []))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send.close()


def _require_public_addresses(values: list[object]) -> tuple[str, ...]:
    addresses: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise DownloadUnavailableError(
                "reference source DNS returned invalid data",
                code="dns_failed",
            )
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise DownloadUnavailableError(
                "reference source DNS returned invalid data",
                code="dns_failed",
            ) from error
        _require_public_ip(address)
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    if not addresses:
        raise DownloadUnavailableError(
            "reference source DNS returned no addresses",
            code="dns_failed",
        )
    return tuple(addresses)


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
        or getattr(candidate, "is_site_local", False)
        or getattr(candidate, "sixtofour", None) is not None
        or getattr(candidate, "teredo", None) is not None
        or (
            isinstance(candidate, ipaddress.IPv6Address)
            and any(candidate in network for network in NON_PUBLIC_IPV6_NETWORKS)
        )
    ):
        raise UnsafeDownloadURLError(
            "reference source resolved to a non-public address",
            code="non_public_address",
        )


def _open_pinned_connection(
    parsed: SplitResult,
    address: str,
    *,
    timeout: float,
):
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeDownloadURLError("reference source URL has no hostname")
    return _PinnedHTTPSConnection(
        hostname,
        address,
        parsed.port or 443,
        timeout=timeout,
    )


def _close_connection(connection) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except (OSError, http.client.HTTPException):
        pass


def _set_connection_timeout(
    connection, deadline: float, *, idle_timeout: float
) -> None:
    timeout = min(idle_timeout, _remaining(deadline))
    connected_socket = getattr(connection, "sock", None)
    if connected_socket is not None:
        connected_socket.settimeout(timeout)


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise DownloadUnavailableError(
            "reference source exceeded its total deadline",
            code="total_timeout",
        )
    return remaining


def _response_content_type(value: str | None) -> str:
    if value is None:
        return "application/octet-stream"
    raw = _bounded_response_header(value, "Content-Type", required=True) or ""
    media_type = raw.partition(";")[0].strip().casefold()
    if MEDIA_TYPE_RE.fullmatch(media_type) is None:
        raise DownloadIntegrityError(
            "reference source returned an invalid content type",
            code="content_type",
            retryable=False,
        )
    return media_type


def _require_identity_encoding(response) -> None:
    encoding = _bounded_response_header(
        response.getheader("Content-Encoding"),
        "Content-Encoding",
    )
    if encoding is not None and encoding.casefold() != "identity":
        raise DownloadIntegrityError(
            "compressed transfer encodings are not accepted",
            code="content_encoding",
            retryable=False,
        )


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = _bounded_response_header(
        value,
        "Content-Length",
        required=True,
    )
    if normalized is None or CONTENT_LENGTH_RE.fullmatch(normalized) is None:
        raise DownloadIntegrityError(
            "reference source returned an invalid content length",
            code="content_length",
            retryable=False,
        )
    return int(normalized)


def _bounded_response_header(
    value: str | None,
    name: str,
    *,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise DownloadIntegrityError(
                f"reference source omitted {name}",
                code="invalid_header",
                retryable=False,
            )
        return None
    try:
        return _safe_header_value(value.strip(), name, allow_empty=not required)
    except ValueError as error:
        raise DownloadIntegrityError(
            f"reference source returned an invalid {name}",
            code="invalid_header",
            retryable=False,
        ) from error


def _safe_header_value(value: object, name: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    if (
        len(value) > MAX_HEADER_VALUE_CHARS
        or (not allow_empty and not value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} is invalid") from error
    return value


def _optional_request_header(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    return _safe_header_value(value.strip(), name, allow_empty=False)


def _normalize_media_type(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("content types must be text")
    normalized = value.strip().casefold()
    if MEDIA_TYPE_RE.fullmatch(normalized) is None:
        raise ValueError("content type is invalid")
    return normalized


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        normalized = _safe_header_value(value.strip(), "Retry-After", allow_empty=False)
    except ValueError:
        return None
    if normalized.isdecimal():
        return min(int(normalized), 24 * 60 * 60)
    try:
        target = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    seconds = math.ceil((target - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(seconds, 24 * 60 * 60))


def _write_all(sink: _StreamingSink, chunk: bytes) -> None:
    view = memoryview(chunk)
    offset = 0
    while offset < len(view):
        written = sink.write(view[offset:])
        if (
            written is None
            or isinstance(written, bool)
            or not isinstance(written, int)
            or written <= 0
        ):
            raise DownloadIntegrityError(
                "reference artifact sink made no progress",
                code="sink_write_failed",
                retryable=False,
            )
        if written > len(view) - offset:
            raise DownloadIntegrityError(
                "reference artifact sink returned an invalid write count",
                code="sink_write_failed",
                retryable=False,
            )
        offset += written


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")
    return float(value)
