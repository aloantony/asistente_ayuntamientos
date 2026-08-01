import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

import pytest

from app.core.http import urlopen_without_redirects


class _RedirectingHandler(BaseHTTPRequestHandler):
    """Redirige siempre y anota si alguien llegó al destino con credencial."""

    forwarded_authorization: list[str | None] = []

    def do_POST(self):  # noqa: N802 - firma de BaseHTTPRequestHandler
        if self.path == "/target":
            self.forwarded_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "/target")
        self.end_headers()

    def log_message(self, *args):  # pragma: no cover - silencia el log de test
        pass


@pytest.fixture
def redirecting_server():
    _RedirectingHandler.forwarded_authorization = []
    server = HTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_redirects_are_refused_instead_of_forwarding_the_credential(
    redirecting_server,
):
    host, port = redirecting_server.server_address[:2]
    request = urlrequest.Request(
        f"http://{host}:{port}/start",
        data=b"{}",
        headers={"Authorization": "Bearer secret-api-key"},
        method="POST",
    )

    with pytest.raises(urlerror.HTTPError) as raised:
        with urlopen_without_redirects(request, timeout=5):
            pass

    assert raised.value.code == 302
    assert _RedirectingHandler.forwarded_authorization == []
