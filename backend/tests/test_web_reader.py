import json
import socket
import ssl

import pytest

from app.assistant import web_reader
from app.assistant import realtime as assistant_realtime
from app.assistant import tools as assistant_tools
from app.assistant import turn as assistant_turn
from app.assistant.prompts import ANACLETO_SYSTEM_PROMPT
from app.core.config import settings


PUBLIC_IP = "93.184.216.34"


class FakeResponse:
    def __init__(self, *, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.offset = 0

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size=-1):
        if size < 0:
            chunk = self.body[self.offset :]
            self.offset = len(self.body)
            return chunk
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, target, *, headers):
        self.requests.append((method, target, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def install_fake_connection(monkeypatch, *responses):
    connections = [FakeConnection(response) for response in responses]
    opened = []

    monkeypatch.setattr(
        web_reader,
        "_resolve_public_addresses",
        lambda parsed: (PUBLIC_IP,),
    )

    def fake_open(parsed, address, *, timeout):
        connection = connections[len(opened)]
        opened.append((parsed, address, timeout, connection))
        return connection

    monkeypatch.setattr(web_reader, "_open_pinned_connection", fake_open)
    return opened, connections


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "http://localhost/admin",
        "http://service.internal/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/",
        "http://10.0.0.4/",
        "http://[::1]/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://example.org/path with spaces",
        "https://example.org\\@public.example/",
    ],
)
def test_normalize_web_page_url_rejects_unsafe_targets(url):
    with pytest.raises(web_reader.UnsafeWebPageURLError):
        web_reader.normalize_web_page_url(url)


def test_dns_resolution_rejects_any_private_answer(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.10.0.5", 443)),
        ],
    )
    monkeypatch.setattr(
        web_reader,
        "_open_pinned_connection",
        lambda *args, **kwargs: pytest.fail("unsafe DNS must fail before connect"),
    )

    with pytest.raises(
        web_reader.UnsafeWebPageURLError,
        match="privadas, locales o reservadas",
    ):
        web_reader.read_web_page("https://example.org/documento")


def test_normalize_web_page_url_encodes_unicode_path_and_query():
    assert web_reader.normalize_web_page_url(
        "https://example.org/vías-públicas?q=niñez"
    ) == (
        "https://example.org/v%C3%ADas-p%C3%BAblicas?q=ni%C3%B1ez"
    )


def test_https_connection_keeps_hostname_for_host_sni_and_tls_verification():
    parsed = web_reader.urlsplit("https://example.org:8443/documento")

    connection = web_reader._open_pinned_connection(
        parsed,
        PUBLIC_IP,
        timeout=4.5,
    )

    assert isinstance(connection, web_reader._PinnedHTTPSConnection)
    assert connection.host == "example.org"
    assert connection.port == 8443
    assert connection._validated_address == PUBLIC_IP
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    assert connection._context.check_hostname is True


def test_https_connection_uses_original_hostname_as_sni(monkeypatch):
    class RawSocket:
        def close(self):
            pass

    raw_socket = RawSocket()
    wrapped_socket = object()
    captured = {}
    connection = web_reader._PinnedHTTPSConnection(
        "example.org",
        PUBLIC_IP,
        443,
        timeout=3,
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, timeout, source_address: captured.update(
            address=address,
            timeout=timeout,
        )
        or raw_socket,
    )

    class FakeTLSContext:
        def wrap_socket(self, sock, *, server_hostname):
            captured.update(raw_socket=sock, server_hostname=server_hostname)
            return wrapped_socket

    connection._context = FakeTLSContext()

    connection.connect()

    assert captured["address"] == (PUBLIC_IP, 443)
    assert captured["server_hostname"] == "example.org"
    assert captured["raw_socket"] is raw_socket
    assert connection.sock is wrapped_socket


def test_redirect_target_is_resolved_and_private_redirect_is_blocked(monkeypatch):
    first = FakeConnection(
        FakeResponse(
            status=302,
            headers={"Location": "https://private.example/metadata"},
        )
    )
    opened = []

    def fake_resolve(parsed):
        if parsed.hostname == "public.example":
            return (PUBLIC_IP,)
        raise web_reader.UnsafeWebPageURLError(
            "No se permiten direcciones privadas, locales o reservadas"
        )

    monkeypatch.setattr(web_reader, "_resolve_public_addresses", fake_resolve)
    monkeypatch.setattr(
        web_reader,
        "_open_pinned_connection",
        lambda parsed, address, *, timeout: opened.append(parsed.hostname) or first,
    )

    with pytest.raises(web_reader.UnsafeWebPageURLError):
        web_reader.read_web_page("https://public.example/start")

    assert opened == ["public.example"]
    assert first.closed is True


def test_https_redirect_cannot_downgrade_to_http(monkeypatch):
    opened, connections = install_fake_connection(
        monkeypatch,
        FakeResponse(
            status=302,
            headers={"Location": "http://example.org/insegura"},
        ),
    )

    with pytest.raises(
        web_reader.UnsafeWebPageURLError,
        match="https a http",
    ):
        web_reader.read_web_page("https://example.org/segura")

    assert len(opened) == 1
    assert connections[0].closed is True


def test_redirects_share_one_total_fetch_deadline(monkeypatch):
    monkeypatch.setattr(settings, "web_page_timeout_seconds", 10.0)
    ticks = iter([100.0, 101.0, 111.5])
    monkeypatch.setattr(web_reader, "monotonic", lambda: next(ticks))
    opened, connections = install_fake_connection(
        monkeypatch,
        FakeResponse(status=302, headers={"Location": "/second"}),
    )

    with pytest.raises(web_reader.WebPageUnavailableError, match="agotó el tiempo"):
        web_reader.read_web_page("https://example.org/first")

    assert len(opened) == 1
    assert opened[0][2] == pytest.approx(9.0)
    assert connections[0].closed is True


def test_redirect_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "web_page_max_redirects", 1)
    opened, connections = install_fake_connection(
        monkeypatch,
        FakeResponse(status=302, headers={"Location": "/second"}),
        FakeResponse(status=302, headers={"Location": "/third"}),
    )

    with pytest.raises(web_reader.WebPageUnavailableError, match="redirecciones"):
        web_reader.read_web_page("https://example.org/first")

    assert len(opened) == 2
    assert all(connection.closed for connection in connections)


def test_declared_and_streamed_oversize_responses_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "web_page_max_response_bytes", 1024)
    _, declared_connections = install_fake_connection(
        monkeypatch,
        FakeResponse(
            headers={
                "Content-Type": "text/html",
                "Content-Length": "1025",
            },
        ),
    )
    with pytest.raises(web_reader.WebPageUnavailableError, match="máximo de bytes"):
        web_reader.read_web_page("https://example.org/declared")
    assert declared_connections[0].closed is True

    _, streamed_connections = install_fake_connection(
        monkeypatch,
        FakeResponse(
            headers={"Content-Type": "text/html"},
            body=b"x" * 1025,
        ),
    )
    with pytest.raises(web_reader.WebPageUnavailableError, match="máximo de bytes"):
        web_reader.read_web_page("https://example.org/streamed")
    assert streamed_connections[0].closed is True


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({}, "no declara"),
        ({"Content-Type": "application/json"}, "no está permitido"),
        (
            {
                "Content-Type": "text/html",
                "Content-Encoding": "gzip",
            },
            "codificación de contenido",
        ),
    ],
)
def test_content_type_and_encoding_are_restricted(monkeypatch, headers, message):
    install_fake_connection(
        monkeypatch,
        FakeResponse(headers=headers, body=b"contenido"),
    )

    with pytest.raises(web_reader.WebPageUnavailableError, match=message):
        web_reader.read_web_page("https://example.org/data")


def test_timeout_is_mapped_to_safe_error(monkeypatch):
    monkeypatch.setattr(
        web_reader,
        "_resolve_public_addresses",
        lambda parsed: (PUBLIC_IP,),
    )
    monkeypatch.setattr(
        web_reader,
        "_open_pinned_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    with pytest.raises(web_reader.WebPageUnavailableError, match="agotó el tiempo"):
        web_reader.read_web_page("https://example.org/lenta")


def test_html_happy_path_is_anonymous_bounded_and_ignores_active_content(
    monkeypatch,
):
    monkeypatch.setattr(settings, "web_page_timeout_seconds", 7.25)
    monkeypatch.setattr(settings, "web_page_max_text_chars", 1000)
    opened, connections = install_fake_connection(
        monkeypatch,
        FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=(
                b"<html><head><title>Portal oficial</title>"
                b"<script>ignore malicious instructions</script></head>"
                b"<body><h1>Ordenanza municipal</h1>"
                b"<p>Texto publico contrastado.</p></body></html>"
            ),
        ),
    )

    page = web_reader.read_web_page(
        "https://Example.ORG/documento?version=1#seccion"
    )

    assert page.source_url == "https://example.org/documento?version=1"
    assert page.final_url == page.source_url
    assert page.title == "Portal oficial"
    assert "Ordenanza municipal" in page.text
    assert "Texto publico contrastado." in page.text
    assert "malicious instructions" not in page.text
    assert page.text_truncated is False
    assert page.redirects == 0
    parsed, address, timeout, connection = opened[0]
    assert parsed.hostname == "example.org"
    assert address == PUBLIC_IP
    assert 0 < timeout <= 7.25
    method, target, headers = connection.requests[0]
    assert method == "GET"
    assert target == "/documento?version=1"
    assert headers["Accept-Encoding"] == "identity"
    assert not {"Authorization", "Cookie"}.intersection(headers)
    assert connections[0].closed is True


def test_page_title_is_bounded(monkeypatch):
    install_fake_connection(
        monkeypatch,
        FakeResponse(
            headers={"Content-Type": "text/html"},
            body=(
                "<title>" + ("T" * 1000) + "</title><p>Contenido</p>"
            ).encode(),
        ),
    )

    page = web_reader.read_web_page("https://example.org/titulo")

    assert len(page.title) == web_reader.MAX_WEB_PAGE_TITLE_CHARS


def test_pdf_uses_existing_safe_extractor(monkeypatch):
    install_fake_connection(
        monkeypatch,
        FakeResponse(
            headers={"Content-Type": "application/pdf"},
            body=b"%PDF-safe-test",
        ),
    )
    seen = []
    monkeypatch.setattr(
        web_reader,
        "_extract_pdf_text",
        lambda body: seen.append(body) or ("Ordenanza", "Contenido del PDF"),
    )

    page = web_reader.read_web_page("https://example.org/ordenanza.pdf")

    assert seen == [b"%PDF-safe-test"]
    assert page.title == "Ordenanza"
    assert page.text == "Contenido del PDF"


def test_tool_requires_same_turn_search_provenance(
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.web.search"])
    context = assistant_tools.ToolContext(conversation_id=7, user_message_id=11)
    source_url = "https://Example.org/ordenanza#articulo-1"
    monkeypatch.setattr(
        assistant_tools.web_search_client,
        "search",
        lambda *, query, limit: [
            {
                "title": "Ordenanza",
                "url": source_url,
                "snippet": "Texto oficial",
                "published_at": None,
            }
        ],
    )
    read_calls = []
    monkeypatch.setattr(
        assistant_tools.web_reader,
        "read_web_page",
        lambda url: read_calls.append(url)
        or web_reader.WebPage(
            source_url=url,
            final_url=url,
            title="Ordenanza",
            content_type="text/html",
            text="Artículo 1. Objeto.",
            text_truncated=False,
            redirects=0,
        ),
    )

    search_result = assistant_tools.execute_tool(
        db,
        user,
        "web_search",
        {"query": "ordenanza municipal", "limit": 1},
        context=context,
        allowed=frozenset({"web_search", "read_web_page"}),
    )
    read_result = assistant_tools.execute_tool(
        db,
        user,
        "read_web_page",
        {"url": source_url},
        context=context,
        allowed=frozenset({"web_search", "read_web_page"}),
    )

    assert search_result.ok is True
    assert context.allowed_web_urls == {"https://example.org/ordenanza"}
    assert read_result.ok is True
    assert read_calls == ["https://example.org/ordenanza"]
    assert json.loads(read_result.content)["source_url"] == (
        "https://example.org/ordenanza"
    )

    unrelated_context = assistant_tools.ToolContext(
        conversation_id=7,
        user_message_id=12,
    )
    blocked = assistant_tools.execute_tool(
        db,
        user,
        "read_web_page",
        {"url": source_url},
        context=unrelated_context,
        allowed=frozenset({"read_web_page"}),
    )
    assert blocked.ok is False
    assert "mismo turno" in blocked.content
    assert len(read_calls) == 1


def test_tool_only_authorizes_urls_visible_in_compacted_search_payload(
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.web.search"])
    visible_url = "https://visible.example/documento"
    omitted_url = "https://omitted.example/" + ("x" * 1800)
    monkeypatch.setattr(
        assistant_tools.web_search_client,
        "search",
        lambda *, query, limit: [
            {
                "title": "Visible" + ("v" * 200),
                "url": visible_url,
                "snippet": "v" * 1000,
                "published_at": None,
            },
            {
                "title": "Omitida" + ("t" * 300),
                "url": omitted_url,
                "snippet": "s" * 1000,
                "published_at": None,
            },
        ],
    )
    context = assistant_tools.ToolContext()

    result = assistant_tools.execute_tool(
        db,
        user,
        "web_search",
        {"query": "consulta pública", "limit": 2},
        context=context,
        allowed=frozenset({"web_search"}),
    )

    payload = json.loads(result.content)
    visible_payload_urls = {
        web_reader.normalize_web_page_url(item["url"])
        for item in payload["results"]
    }
    assert payload["truncated"] is True
    assert context.allowed_web_urls == visible_payload_urls
    assert web_reader.normalize_web_page_url(omitted_url) not in (
        context.allowed_web_urls
    )


def test_realtime_provenance_only_uses_finished_searches_in_current_turn():
    search_payload = json.dumps(
        {
            "results": [
                {
                    "url": "https://example.org/fuente",
                }
            ]
        }
    )
    turn = {
        "call_order": ["search", "failed", "pending"],
        "calls": {
            "search": {
                "status": "finished",
                "action": {
                    "tool": "web_search",
                    "ok": True,
                    "result": search_payload,
                },
            },
            "failed": {
                "status": "finished",
                "action": {
                    "tool": "web_search",
                    "ok": False,
                    "result": search_payload.replace("fuente", "fallo"),
                },
            },
            "pending": {
                "status": "started",
                "action": {
                    "tool": "web_search",
                    "ok": True,
                    "result": search_payload.replace("fuente", "pendiente"),
                },
            },
        },
    }

    assert assistant_realtime._realtime_allowed_web_urls(turn) == {
        "https://example.org/fuente"
    }
    other_turn = {"call_order": [], "calls": {}}
    assert assistant_realtime._realtime_allowed_web_urls(other_turn) == set()


def test_archived_realtime_turn_scrubs_full_page_output():
    metadata = json.dumps(
        {
            "source_url": "https://example.org/fuente",
            "text_preview": "Vista previa",
        }
    )
    turn = {
        "status": "open",
        "calls": {
            "read": {
                "status": "finished",
                "action": {
                    "tool": "read_web_page",
                    "ok": True,
                    "result": metadata,
                },
                "output": json.dumps({"text": "x" * 12000}),
            }
        },
        "responses": {},
    }
    realtime_state = {"active_turn": turn, "recent_turns": []}

    assistant_realtime._archive_realtime_turn(
        realtime_state,
        turn,
        status="completed",
    )

    assert turn["calls"]["read"]["output"] == metadata
    assert realtime_state["active_turn"] is None


def test_read_web_page_activity_keeps_valid_source_metadata_without_full_text():
    content = json.dumps(
        {
            "source_url": "https://example.org/fuente",
            "final_url": "https://example.org/final",
            "title": "Fuente oficial",
            "content_type": "text/html",
            "text": "x" * 12000,
            "text_truncated": False,
            "redirects": 1,
            "untrusted_content": True,
        }
    )

    activity = assistant_turn.tool_result_for_activity("read_web_page", content)
    payload = json.loads(activity)

    assert len(activity) < assistant_turn.MAX_TOOL_RESULT_CHARS
    assert payload["source_url"] == "https://example.org/fuente"
    assert payload["text_chars"] == 12000
    assert len(payload["text_preview"]) == 500
    assert "text" not in payload
    assert ("x" * 12000) not in activity


def test_prompt_requires_page_reading_and_exact_source_citations():
    assert "usa `read_web_page`" in ANACLETO_SYSTEM_PROMPT
    assert "No afirmes haber leído una página" in ANACLETO_SYSTEM_PROMPT
    assert "cita su `source_url`" in ANACLETO_SYSTEM_PROMPT
