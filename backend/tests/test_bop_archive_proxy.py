import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.ordinances import archive_proxy, archive_proxy_service, import_service
from app.ordinances.archive_proxy import ArchiveProxyError
from app.ordinances.bop_burgos import BOP_BURGOS_DOMAIN


@pytest.fixture
def proxy_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(
        settings,
        "bop_archive_proxy_base_url",
        "http://127.0.0.1:8650",
    )
    monkeypatch.setattr(settings, "bop_archive_proxy_api_key", "archive-api-key")
    monkeypatch.setattr(
        settings,
        "bop_archive_proxy_signing_key",
        "archive-signing-key",
    )
    monkeypatch.setattr(settings, "bop_archive_proxy_storage_root", str(tmp_path))
    return TestClient(archive_proxy_service.app)


def test_archive_proxy_authenticates_and_reuses_immutable_object(
    proxy_client,
    tmp_path,
    monkeypatch,
):
    fetch_count = 0

    def fake_fetch(*_args, **_kwargs):
        nonlocal fetch_count
        fetch_count += 1
        return import_service.FetchedSource(
            content=b"official-pdf-content",
            content_type="application/pdf",
        )

    monkeypatch.setattr(archive_proxy_service, "_fetch_direct_source", fake_fetch)
    source_url = f"http://{BOP_BURGOS_DOMAIN}/bulletin.pdf"

    unauthorized = proxy_client.post(
        "/v1/archive",
        json={"source_url": source_url},
    )
    first = proxy_client.post(
        "/v1/archive",
        headers={"Authorization": "Bearer archive-api-key"},
        json={"source_url": source_url},
    )
    second = proxy_client.post(
        "/v1/archive",
        headers={"Authorization": "Bearer archive-api-key"},
        json={"source_url": source_url},
    )

    assert unauthorized.status_code == 401
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == b"official-pdf-content"
    assert second.headers["x-archive-sha256"] == first.headers[
        "x-archive-sha256"
    ]
    assert first.headers["x-archive-signature"]
    assert fetch_count == 1
    assert len(list((tmp_path / "objects").glob("*.bin"))) == 1
    assert len(list((tmp_path / "records").glob("*/*.json"))) == 1

    latest_path = next((tmp_path / "latest").glob("*.json"))
    tampered_manifest = json.loads(latest_path.read_text(encoding="utf-8"))
    tampered_manifest["content_type"] = "text/plain"
    latest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
    tampered = proxy_client.post(
        "/v1/archive",
        headers={"Authorization": "Bearer archive-api-key"},
        json={"source_url": source_url},
    )
    assert tampered.status_code == 500
    assert "signature" in tampered.json()["detail"].lower()


def test_archive_proxy_refresh_preserves_each_content_version(
    proxy_client,
    tmp_path,
    monkeypatch,
):
    contents = iter((b"search-result-v1", b"search-result-v2"))
    monkeypatch.setattr(
        archive_proxy_service,
        "_fetch_direct_source",
        lambda *_args, **_kwargs: import_service.FetchedSource(
            content=next(contents),
            content_type="text/html; charset=utf-8",
        ),
    )
    source_url = f"http://{BOP_BURGOS_DOMAIN}/busqueda?keys=agua"
    request = {
        "source_url": source_url,
    }
    headers = {"Authorization": "Bearer archive-api-key"}

    first = proxy_client.post("/v1/archive", headers=headers, json=request)
    second = proxy_client.post("/v1/archive", headers=headers, json=request)

    assert first.content == b"search-result-v1"
    assert second.content == b"search-result-v2"
    assert first.headers["x-archive-sha256"] != second.headers[
        "x-archive-sha256"
    ]
    assert len(list((tmp_path / "objects").glob("*.bin"))) == 2
    assert len(list((tmp_path / "records").glob("*/*.json"))) == 2


def test_archive_proxy_rejects_non_bop_hosts(proxy_client, monkeypatch):
    monkeypatch.setattr(
        archive_proxy_service,
        "_fetch_direct_source",
        lambda *_args, **_kwargs: pytest.fail("foreign URL reached the network"),
    )

    response = proxy_client.post(
        "/v1/archive",
        headers={"Authorization": "Bearer archive-api-key"},
        json={"source_url": "http://example.com/private.pdf"},
    )

    assert response.status_code == 422


def test_archive_proxy_rejects_client_controlled_cache_mode(proxy_client):
    response = proxy_client.post(
        "/v1/archive",
        headers={"Authorization": "Bearer archive-api-key"},
        json={
            "source_url": f"http://{BOP_BURGOS_DOMAIN}/file.pdf",
            "cache_mode": "refresh",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "source_url",
    [
        f"http://{BOP_BURGOS_DOMAIN}:8080/file.pdf",
        f"http://{BOP_BURGOS_DOMAIN}/file.pdf#section",
        f"HTTP://{BOP_BURGOS_DOMAIN}/file.pdf",
    ],
)
def test_archive_proxy_rejects_noncanonical_bop_urls(
    proxy_client,
    monkeypatch,
    source_url,
):
    monkeypatch.setattr(
        archive_proxy_service,
        "_fetch_direct_source",
        lambda *_args, **_kwargs: pytest.fail("unsafe URL reached the network"),
    )

    response = proxy_client.post(
        "/v1/archive",
        headers={"Authorization": "Bearer archive-api-key"},
        json={"source_url": source_url},
    )

    assert response.status_code == 422


def test_backend_client_verifies_proxy_digest_and_signature(
    proxy_client,
    monkeypatch,
):
    monkeypatch.setattr(
        archive_proxy_service,
        "_fetch_direct_source",
        lambda *_args, **_kwargs: import_service.FetchedSource(
            content=b"signed-official-content",
            content_type="application/pdf",
        ),
    )
    source_url = f"http://{BOP_BURGOS_DOMAIN}/signed.pdf"
    proxy_response = proxy_client.post(
        "/v1/archive",
        headers={"Authorization": "Bearer archive-api-key"},
        json={"source_url": source_url},
    )
    captured_payload = {}

    class FakeResponse:
        def __init__(self, content: bytes):
            self.content = content
            self.offset = 0
            self.headers = proxy_response.headers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size):
            chunk = self.content[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    class FakeOpener:
        def __init__(self, content: bytes):
            self.content = content

        def open(self, request, timeout):
            captured_payload.update(json.loads(request.data.decode("utf-8")))
            assert request.get_header("Authorization") == (
                "Bearer archive-api-key"
            )
            assert timeout == settings.bop_archive_proxy_timeout_seconds
            return FakeResponse(self.content)

    monkeypatch.setattr(
        archive_proxy,
        "_ARCHIVE_PROXY_OPENER",
        FakeOpener(proxy_response.content),
    )

    archived = archive_proxy.fetch_archived_source(source_url)

    assert archived.content == b"signed-official-content"
    assert captured_payload == {
        "source_url": source_url,
    }

    monkeypatch.setattr(
        archive_proxy,
        "_ARCHIVE_PROXY_OPENER",
        FakeOpener(proxy_response.content + b"tampered"),
    )
    with pytest.raises(ArchiveProxyError, match="SHA-256"):
        archive_proxy.fetch_archived_source(source_url)

    with pytest.raises(ArchiveProxyError, match="firma"):
        archive_proxy._validate_archive_metadata(
            source_url=source_url,
            content=proxy_response.content,
            content_type=proxy_response.headers["x-archive-content-type"],
            digest=proxy_response.headers["x-archive-sha256"],
            fetched_at=proxy_response.headers["x-archive-fetched-at"],
            signature="0" * 64,
            signing_key="archive-signing-key",
        )


def test_legacy_bop_urls_are_allowed_only_with_configured_proxy(monkeypatch):
    source_url = f"http://{BOP_BURGOS_DOMAIN}/official.pdf"
    sources = [SimpleNamespace(domain=BOP_BURGOS_DOMAIN)]
    monkeypatch.setattr(settings, "bop_archive_proxy_base_url", None)
    monkeypatch.setattr(settings, "bop_archive_proxy_api_key", None)
    monkeypatch.setattr(settings, "bop_archive_proxy_signing_key", None)

    assert not import_service.source_url_allowed(source_url, sources)

    monkeypatch.setattr(
        settings,
        "bop_archive_proxy_base_url",
        "http://127.0.0.1:8650",
    )
    monkeypatch.setattr(settings, "bop_archive_proxy_api_key", "api-key")
    monkeypatch.setattr(settings, "bop_archive_proxy_signing_key", "signing-key")

    assert import_service.source_url_allowed(source_url, sources)


def test_bop_archive_requires_exact_redirect_host():
    with pytest.raises(import_service.ImportSourceError, match="host oficial exacto"):
        import_service._ensure_exact_source_domain(
            "http://cdn.bopbur.diputaciondeburgos.es/file.pdf",
            {BOP_BURGOS_DOMAIN},
        )


def test_legacy_bop_fetch_checks_allowlist_before_proxy(monkeypatch):
    source_url = f"http://{BOP_BURGOS_DOMAIN}/file.pdf"
    monkeypatch.setattr(
        settings,
        "bop_archive_proxy_base_url",
        "http://localhost:8650",
    )
    monkeypatch.setattr(settings, "bop_archive_proxy_api_key", "api-key")
    monkeypatch.setattr(settings, "bop_archive_proxy_signing_key", "signing-key")
    monkeypatch.setattr(
        archive_proxy,
        "fetch_archived_source",
        lambda *_args, **_kwargs: pytest.fail("disallowed URL reached the proxy"),
    )

    with pytest.raises(import_service.ImportSourceError, match="fuente oficial"):
        import_service._fetch_source(source_url, allowed_domains=set())
