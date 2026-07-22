from __future__ import annotations

import hashlib
import io
import socket
import ssl
import threading
from collections import deque
from time import monotonic
from urllib.parse import urlsplit

import pytest

from app.reference_layers import safe_download
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.safe_download import (
    DownloadHTTPError,
    DownloadIntegrityError,
    DownloadLimitError,
    DownloadUnavailableError,
    HTTPSDownloadPolicy,
    SafeHTTPSDownloader,
    UnsafeDownloadURLError,
    normalize_https_url,
)


PUBLIC_IP = "93.184.216.34"


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        *,
        body: bytes = b"payload",
        headers: dict[str, str] | None = None,
        chunk_size: int = 3,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._offset = 0
        self._headers = {key.casefold(): value for key, value in (headers or {}).items()}
        self._chunk_size = chunk_size
        self._read_error = read_error

    def getheader(self, name: str):
        return self._headers.get(name.casefold())

    def read1(self, size: int) -> bytes:
        if self._read_error is not None:
            error = self._read_error
            self._read_error = None
            raise error
        if self._offset >= len(self._body):
            return b""
        count = min(size, self._chunk_size, len(self._body) - self._offset)
        chunk = self._body[self._offset : self._offset + count]
        self._offset += count
        return chunk


class FakeConnection:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        request_error: BaseException | None = None,
        response_error: BaseException | None = None,
    ) -> None:
        self.response = response or FakeResponse()
        self.request_error = request_error
        self.response_error = response_error
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.sock = FakeSocket()
        self.closed = False

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self):
        if self.response_error is not None:
            raise self.response_error
        return self.response

    def close(self) -> None:
        self.closed = True


def make_policy(**overrides) -> HTTPSDownloadPolicy:
    values = {
        "allowed_origins": ("https://maps.example",),
        "max_response_bytes": 1024,
        "timeout_seconds": 30,
        "dns_timeout_seconds": 2,
        "connect_timeout_seconds": 3,
        "idle_timeout_seconds": 4,
        "allowed_content_types": frozenset(
            {"application/geo+json", "application/octet-stream"}
        ),
    }
    values.update(overrides)
    return HTTPSDownloadPolicy(**values)


def install_connections(monkeypatch, connections, *, addresses=(PUBLIC_IP,)):
    pending = deque(connections)
    opens = []
    monkeypatch.setattr(
        safe_download,
        "_resolve_public_addresses",
        lambda parsed, timeout: tuple(addresses),
    )

    def open_connection(parsed, address, *, timeout):
        opens.append((parsed, address, timeout))
        return pending.popleft()

    monkeypatch.setattr(safe_download, "_open_pinned_connection", open_connection)
    return opens


def test_streams_anonymous_identity_encoded_response_with_hash(monkeypatch) -> None:
    body = b'{"type":"FeatureCollection","features":[]}'
    response = FakeResponse(
        body=body,
        chunk_size=5,
        headers={
            "Content-Type": "Application/Geo+JSON; charset=utf-8",
            "Content-Length": str(len(body)),
            "ETag": '"source-v1"',
            "Last-Modified": "Wed, 22 Jul 2026 08:00:00 GMT",
        },
    )
    connection = FakeConnection(response)
    opens = install_connections(monkeypatch, [connection])
    sink = io.BytesIO()

    result = SafeHTTPSDownloader(make_policy()).download(
        "https://maps.example/data?service=WFS&request=GetFeature",
        sink,
        accept="application/geo+json",
    )

    assert sink.getvalue() == body
    assert result.final_url == (
        "https://maps.example/data?service=WFS&request=GetFeature"
    )
    assert result.content_type == "application/geo+json"
    assert result.size_bytes == len(body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert result.etag == '"source-v1"'
    assert result.last_modified == "Wed, 22 Jul 2026 08:00:00 GMT"
    assert result.redirect_chain == (result.source_url,)
    assert opens[0][1] == PUBLIC_IP
    method, target, headers = connection.requests[0]
    assert method == "GET"
    assert target == "/data?service=WFS&request=GetFeature"
    assert headers == {
        "Accept": "application/geo+json",
        "Accept-Encoding": "identity",
        "User-Agent": "AsistenteAyuntamientos/reference-sync",
    }
    assert connection.closed is True
    assert connection.sock.timeouts


def test_conditional_get_returns_304_without_writing(monkeypatch) -> None:
    response = FakeResponse(
        304,
        body=b"must not be read",
        headers={"ETag": '"v2"'},
    )
    connection = FakeConnection(response)
    install_connections(monkeypatch, [connection])
    sink = io.BytesIO()

    result = SafeHTTPSDownloader(make_policy()).download(
        "https://maps.example/data",
        sink,
        etag='"v1"',
        last_modified="Tue, 21 Jul 2026 08:00:00 GMT",
    )

    assert result.not_modified is True
    assert result.status_code == 304
    assert result.sha256 is None
    assert result.size_bytes == 0
    assert result.etag == '"v2"'
    assert sink.getvalue() == b""
    assert connection.requests[0][2]["If-None-Match"] == '"v1"'
    assert connection.requests[0][2]["If-Modified-Since"].startswith("Tue")


def test_unexpected_304_is_terminal_integrity_failure(monkeypatch) -> None:
    connection = FakeConnection(FakeResponse(304))
    install_connections(monkeypatch, [connection])

    with pytest.raises(DownloadIntegrityError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            io.BytesIO(),
        )

    assert captured.value.code == "unexpected_not_modified"
    assert captured.value.retryable is False


def test_redirects_are_reauthorized_against_exact_origin_allowlist(monkeypatch) -> None:
    first = FakeConnection(
        FakeResponse(302, headers={"Location": "https://cdn.example/files/map.gpkg"})
    )
    body = b"gpkg"
    second = FakeConnection(
        FakeResponse(
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
            },
        )
    )
    install_connections(monkeypatch, [first, second])
    policy = make_policy(
        allowed_origins=("https://maps.example", "https://cdn.example")
    )

    result = SafeHTTPSDownloader(policy).download(
        "https://maps.example/start",
        io.BytesIO(),
    )

    assert result.redirects == 1
    assert result.final_url == "https://cdn.example/files/map.gpkg"
    assert result.redirect_chain == (
        "https://maps.example/start",
        "https://cdn.example/files/map.gpkg",
    )
    assert first.closed and second.closed


def test_cross_origin_redirect_is_rejected_before_second_connection(monkeypatch) -> None:
    first = FakeConnection(
        FakeResponse(302, headers={"Location": "https://evil.example/map"})
    )
    opens = install_connections(monkeypatch, [first])

    with pytest.raises(UnsafeDownloadURLError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/start",
            io.BytesIO(),
        )

    assert captured.value.code == "origin_not_allowed"
    assert len(opens) == 1
    assert first.closed


def test_redirect_loop_and_limit_fail_closed(monkeypatch) -> None:
    loop = FakeConnection(FakeResponse(302, headers={"Location": "/start"}))
    install_connections(monkeypatch, [loop])
    with pytest.raises(UnsafeDownloadURLError) as loop_error:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/start",
            io.BytesIO(),
        )
    assert loop_error.value.code == "redirect_loop"

    limit = FakeConnection(FakeResponse(302, headers={"Location": "/next"}))
    install_connections(monkeypatch, [limit])
    with pytest.raises(DownloadLimitError) as limit_error:
        SafeHTTPSDownloader(make_policy(max_redirects=0)).download(
            "https://maps.example/start",
            io.BytesIO(),
        )
    assert limit_error.value.code == "redirect_limit"


@pytest.mark.parametrize(
    "url",
    [
        "http://maps.example/data",
        "https://user@maps.example/data",
        "https://maps.example:8443/data",
        "https://maps.example/data#fragment",
        "https://maps.example/has space",
        "https://maps.example\\evil/data",
        "https://localhost/data",
        "https://metadata.google.internal/data",
        "https://singlelabel/data",
        "https://127.0.0.1/data",
        "https://[::1]/data",
    ],
)
def test_unsafe_urls_are_rejected_without_dns_or_network(monkeypatch, url) -> None:
    monkeypatch.setattr(
        safe_download,
        "_resolve_public_addresses",
        lambda *_args, **_kwargs: pytest.fail("DNS must not run"),
    )
    with pytest.raises(UnsafeDownloadURLError):
        normalize_https_url(url)


def test_url_outside_allowlist_is_rejected_before_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        safe_download,
        "_resolve_public_addresses",
        lambda *_args, **_kwargs: pytest.fail("DNS must not run"),
    )
    with pytest.raises(UnsafeDownloadURLError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://sub.maps.example/data",
            io.BytesIO(),
        )
    assert captured.value.code == "origin_not_allowed"


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.1"],
        [PUBLIC_IP, "127.0.0.1"],
        ["::ffff:127.0.0.1"],
        ["2002:7f00:1::"],
        ["64:ff9b::7f00:1"],
    ],
)
def test_every_dns_answer_must_be_public(addresses) -> None:
    with pytest.raises(UnsafeDownloadURLError) as captured:
        safe_download._require_public_addresses(addresses)
    assert captured.value.code == "non_public_address"


def test_dns_process_launch_is_charged_to_dns_deadline(monkeypatch) -> None:
    release_start = threading.Event()
    cleaned = threading.Event()

    class PipeEndpoint:
        def close(self):
            return None

        def poll(self, _timeout):
            pytest.fail("DNS IPC must not be polled before process start")

    class BlockedProcess:
        def __init__(self, **_kwargs):
            self.alive = False

        def start(self):
            release_start.wait(1)
            self.alive = True

        def join(self, timeout):
            return None

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False
            cleaned.set()

        def kill(self):
            self.alive = False
            cleaned.set()

        def close(self):
            return None

    class BlockedContext:
        def Pipe(self, *, duplex):
            assert duplex is False
            return PipeEndpoint(), PipeEndpoint()

        def Process(self, **kwargs):
            return BlockedProcess(**kwargs)

    monkeypatch.setattr(
        safe_download.multiprocessing,
        "get_context",
        lambda method: BlockedContext(),
    )
    started_at = monotonic()
    with pytest.raises(DownloadUnavailableError) as captured:
        safe_download._resolve_public_addresses(
            urlsplit("https://maps.example/data"),
            timeout=0.02,
        )
    elapsed = monotonic() - started_at

    assert captured.value.code == "dns_timeout"
    assert elapsed < 0.5
    release_start.set()
    assert cleaned.wait(1)


def test_dns_resource_allocation_failure_is_classified_and_closes_pipe(
    monkeypatch,
) -> None:
    endpoints = []

    class PipeEndpoint:
        def __init__(self):
            self.closed = False
            endpoints.append(self)

        def close(self):
            self.closed = True

    class BrokenContext:
        def Pipe(self, *, duplex):
            assert duplex is False
            return PipeEndpoint(), PipeEndpoint()

        def Process(self, **_kwargs):
            raise OSError("process resources unavailable")

    monkeypatch.setattr(
        safe_download.multiprocessing,
        "get_context",
        lambda method: BrokenContext(),
    )

    with pytest.raises(DownloadUnavailableError) as captured:
        safe_download._resolve_public_addresses(
            urlsplit("https://maps.example/data"),
            timeout=1,
        )

    assert captured.value.code == "dns_failed"
    assert len(endpoints) == 2
    assert all(endpoint.closed for endpoint in endpoints)


def test_pinned_tls_connection_uses_validated_ip_and_original_sni(monkeypatch) -> None:
    calls = []

    class RawSocket:
        def close(self):
            calls.append("raw-close")

    class TLSContext:
        def wrap_socket(self, raw_socket, *, server_hostname):
            calls.append((raw_socket, server_hostname))
            return "tls-socket"

    raw = RawSocket()
    monkeypatch.setattr(
        safe_download.socket,
        "create_connection",
        lambda address, timeout, source_address: calls.append(
            (address, timeout, source_address)
        )
        or raw,
    )
    connection = safe_download._PinnedHTTPSConnection(
        "maps.example",
        PUBLIC_IP,
        443,
        timeout=7,
    )
    connection._context = TLSContext()

    connection.connect()

    assert connection.host == "maps.example"
    assert calls[0][0] == (PUBLIC_IP, 443)
    assert calls[1] == (raw, "maps.example")
    assert connection.sock == "tls-socket"


def test_declared_and_streamed_size_limits_fail_without_unbounded_reads(
    monkeypatch,
) -> None:
    declared = FakeConnection(
        FakeResponse(
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "5",
            }
        )
    )
    install_connections(monkeypatch, [declared])
    with pytest.raises(DownloadLimitError) as declared_error:
        SafeHTTPSDownloader(make_policy(max_response_bytes=4)).download(
            "https://maps.example/data",
            io.BytesIO(),
        )
    assert declared_error.value.code == "response_too_large"

    streamed = FakeConnection(
        FakeResponse(
            body=b"12345",
            headers={"Content-Type": "application/octet-stream"},
        )
    )
    install_connections(monkeypatch, [streamed])
    with pytest.raises(DownloadLimitError) as streamed_error:
        SafeHTTPSDownloader(make_policy(max_response_bytes=4)).download(
            "https://maps.example/data",
            io.BytesIO(),
        )
    assert streamed_error.value.code == "response_too_large"


@pytest.mark.parametrize("length", ["-1", "1,2", "9" * 21, ""])
def test_invalid_content_length_is_terminal(monkeypatch, length) -> None:
    connection = FakeConnection(
        FakeResponse(
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": length,
            }
        )
    )
    install_connections(monkeypatch, [connection])

    with pytest.raises(DownloadIntegrityError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            io.BytesIO(),
        )

    assert captured.value.code in {"content_length", "invalid_header"}
    assert captured.value.retryable is False


def test_truncated_body_is_retryable_integrity_failure(monkeypatch) -> None:
    connection = FakeConnection(
        FakeResponse(
            body=b"123",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "5",
            },
        )
    )
    install_connections(monkeypatch, [connection])

    with pytest.raises(DownloadIntegrityError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            io.BytesIO(),
        )

    assert captured.value.code == "content_length_mismatch"
    assert captured.value.retryable is True


def test_content_encoding_type_empty_and_headers_are_validated(monkeypatch) -> None:
    cases = (
        (
            FakeResponse(
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Encoding": "gzip",
                }
            ),
            "content_encoding",
        ),
        (FakeResponse(headers={"Content-Type": "text/html"}), "content_type"),
        (
            FakeResponse(
                body=b"",
                headers={"Content-Type": "application/octet-stream"},
            ),
            "empty_response",
        ),
        (
            FakeResponse(
                headers={
                    "Content-Type": "application/octet-stream",
                    "ETag": "bad\r\nheader",
                }
            ),
            "invalid_header",
        ),
    )
    for response, expected_code in cases:
        connection = FakeConnection(response)
        install_connections(monkeypatch, [connection])
        with pytest.raises(DownloadIntegrityError) as captured:
            SafeHTTPSDownloader(make_policy()).download(
                "https://maps.example/data",
                io.BytesIO(),
            )
        assert captured.value.code == expected_code
        assert captured.value.retryable is False


def test_missing_content_type_defaults_only_when_no_allowlist(monkeypatch) -> None:
    missing = FakeConnection(FakeResponse(body=b"binary"))
    install_connections(monkeypatch, [missing])
    result = SafeHTTPSDownloader(
        make_policy(allowed_content_types=None)
    ).download("https://maps.example/data", io.BytesIO())
    assert result.content_type == "application/octet-stream"


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_http_errors_are_classified_for_retry(monkeypatch, status_code, retryable) -> None:
    connection = FakeConnection(
        FakeResponse(status_code, headers={"Retry-After": "999999"})
    )
    install_connections(monkeypatch, [connection])

    with pytest.raises(DownloadHTTPError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            io.BytesIO(),
        )

    assert captured.value.status_code == status_code
    assert captured.value.retryable is retryable
    assert captured.value.retry_after_seconds == (86400 if retryable else None)


def test_connection_failure_tries_next_validated_address(monkeypatch) -> None:
    failed = FakeConnection(request_error=OSError("unreachable"))
    body = b"ok"
    successful = FakeConnection(
        FakeResponse(
            body=body,
            headers={"Content-Type": "application/octet-stream"},
        )
    )
    opens = install_connections(
        monkeypatch,
        [failed, successful],
        addresses=("93.184.216.34", "93.184.216.35"),
    )

    result = SafeHTTPSDownloader(make_policy()).download(
        "https://maps.example/data",
        io.BytesIO(),
    )

    assert result.size_bytes == 2
    assert [entry[1] for entry in opens] == ["93.184.216.34", "93.184.216.35"]
    assert failed.closed and successful.closed


def test_certificate_failure_is_terminal_and_does_not_try_another_ip(monkeypatch) -> None:
    certificate_error = ssl.SSLCertVerificationError(1, "certificate invalid")
    failed = FakeConnection(request_error=certificate_error)
    opens = install_connections(
        monkeypatch,
        [failed],
        addresses=("93.184.216.34", "93.184.216.35"),
    )

    with pytest.raises(DownloadUnavailableError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            io.BytesIO(),
        )

    assert captured.value.code == "tls_certificate"
    assert captured.value.retryable is False
    assert len(opens) == 1


def test_body_timeout_is_retryable_and_connection_is_closed(monkeypatch) -> None:
    connection = FakeConnection(
        FakeResponse(
            headers={"Content-Type": "application/octet-stream"},
            read_error=socket.timeout("stalled"),
        )
    )
    install_connections(monkeypatch, [connection])

    with pytest.raises(DownloadUnavailableError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            io.BytesIO(),
        )

    assert captured.value.code == "idle_timeout"
    assert captured.value.retryable is True
    assert connection.closed


def test_total_deadline_is_enforced_while_streaming(monkeypatch) -> None:
    connection = FakeConnection(
        FakeResponse(
            body=b"data",
            headers={"Content-Type": "application/octet-stream"},
        )
    )
    install_connections(monkeypatch, [connection])
    clock = iter((0.0, 0.1, 0.2, 0.3, 31.0))
    monkeypatch.setattr(safe_download, "monotonic", lambda: next(clock))

    with pytest.raises(DownloadUnavailableError) as captured:
        SafeHTTPSDownloader(make_policy(timeout_seconds=30)).download(
            "https://maps.example/data",
            io.BytesIO(),
        )

    assert captured.value.code == "total_timeout"
    assert connection.closed


def test_short_writes_are_completed_and_ambiguous_writes_fail(monkeypatch) -> None:
    body = b"abcdefgh"

    class PartialSink:
        def __init__(self):
            self.body = bytearray()

        def write(self, value):
            count = min(2, len(value))
            self.body.extend(value[:count])
            return count

    connection = FakeConnection(
        FakeResponse(
            body=body,
            headers={"Content-Type": "application/octet-stream"},
            chunk_size=len(body),
        )
    )
    install_connections(monkeypatch, [connection])
    partial = PartialSink()
    SafeHTTPSDownloader(make_policy()).download(
        "https://maps.example/data",
        partial,
    )
    assert bytes(partial.body) == body

    stalled = FakeConnection(
        FakeResponse(
            body=body,
            headers={"Content-Type": "application/octet-stream"},
        )
    )
    install_connections(monkeypatch, [stalled])

    class NoProgressSink:
        def write(self, _value):
            return 0

    with pytest.raises(DownloadIntegrityError) as captured:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            NoProgressSink(),
        )
    assert captured.value.code == "sink_write_failed"

    ambiguous = FakeConnection(
        FakeResponse(
            body=body,
            headers={"Content-Type": "application/octet-stream"},
        )
    )
    install_connections(monkeypatch, [ambiguous])

    class AmbiguousSink:
        def write(self, _value):
            return None

    with pytest.raises(DownloadIntegrityError) as ambiguous_error:
        SafeHTTPSDownloader(make_policy()).download(
            "https://maps.example/data",
            AmbiguousSink(),
        )
    assert ambiguous_error.value.code == "sink_write_failed"


def test_download_stream_commits_as_one_verified_atomic_blob(
    tmp_path,
    monkeypatch,
) -> None:
    body = b"official reference artifact"
    connection = FakeConnection(
        FakeResponse(
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
            },
        )
    )
    install_connections(monkeypatch, [connection])
    store = ReferenceBlobStore(
        tmp_path / "reference-mirror",
        max_blob_bytes=len(body),
    )
    try:
        with store.stage(max_bytes=len(body)) as staging:
            result = SafeHTTPSDownloader(make_policy()).download(
                "https://maps.example/data",
                staging,
            )
            stored = staging.commit(
                expected_sha256=result.sha256,
                expected_size=result.size_bytes,
            )

        assert store.resolve_blob(stored.storage_key).read_bytes() == body
        assert stored.sha256 == hashlib.sha256(body).hexdigest()
        assert list((store.root / "staging").iterdir()) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_origins": ()},
        {"allowed_origins": ("https://maps.example/path",)},
        {"allowed_origins": ("http://maps.example",)},
        {"max_response_bytes": 0},
        {"timeout_seconds": float("inf")},
        {"dns_timeout_seconds": 0},
        {"connect_timeout_seconds": -1},
        {"idle_timeout_seconds": True},
        {"max_redirects": 11},
        {"allowed_content_types": frozenset()},
        {"allowed_content_types": frozenset({"not-a-type"})},
        {"user_agent": "bad\nagent"},
        {"user_agent": "outside-latin-1-\u20ac"},
        {"require_nonempty": 1},
    ],
)
def test_invalid_policy_fails_closed(overrides) -> None:
    with pytest.raises(ValueError):
        make_policy(**overrides)
