from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from fastapi.testclient import TestClient
import pytest

from app.reference_layers import gwc_gateway as gateway_module
from app.reference_layers.gwc_gateway import (
    GeoWebCacheGatewayUpstreamError,
    create_app,
)


@dataclass
class FakeUpstream:
    status: int = 200
    headers: tuple[tuple[str, str], ...] = (
        ("Content-Type", "text/plain"),
        ("Content-Length", "2"),
    )
    chunks: tuple[bytes, ...] = (b"ok",)

    def iter_body(self) -> Iterator[bytes]:
        yield from self.chunks


class SequencedVerifier:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> Mapping[str, object]:
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("unexpected contract verification")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, Mapping)
        return outcome


class RecordingOpener:
    def __init__(self, response: FakeUpstream | Exception) -> None:
        self.response = response
        self.calls: list[
            tuple[str, str, dict[str, str], bytes]
        ] = []

    def __call__(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeUpstream:
        self.calls.append((method, target, dict(headers), body))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def verified() -> dict[str, object]:
    return {"verified": True}


def gateway_client(
    verifier: SequencedVerifier,
    opener: RecordingOpener,
) -> TestClient:
    return TestClient(
        create_app(verifier=verifier, upstream_opener=opener)
    )


def test_gateway_health_is_fail_closed_on_false_or_failed_contract() -> None:
    verifier = SequencedVerifier(
        [
            verified(),
            {"verified": False},
            RuntimeError("GeoServer restarted"),
        ]
    )
    opener = RecordingOpener(FakeUpstream())

    with gateway_client(verifier, opener) as client:
        assert client.get("/_gwc_gateway/health").status_code == 200
        assert client.get("/_gwc_gateway/health").status_code == 503
        assert client.get("/_gwc_gateway/health").status_code == 503

    assert verifier.calls == 3
    assert opener.calls == []


def test_gateway_revalidates_every_request_and_recovers_after_restart() -> None:
    verifier = SequencedVerifier(
        [
            verified(),
            RuntimeError("GeoServer is restarting"),
            verified(),
        ]
    )
    opener = RecordingOpener(FakeUpstream())

    with gateway_client(verifier, opener) as client:
        first = client.get(
            "/geoserver/siur/wms?service=WMS",
            headers={"Host": "127.0.0.1:8081"},
        )
        blocked = client.get(
            "/geoserver/siur/wms?service=WMS",
            headers={"Host": "127.0.0.1:8081"},
        )
        recovered = client.get(
            "/geoserver/siur/wms?service=WMS",
            headers={"Host": "127.0.0.1:8081"},
        )

    assert (first.status_code, first.content) == (200, b"ok")
    assert blocked.status_code == 503
    assert (recovered.status_code, recovered.content) == (200, b"ok")
    assert verifier.calls == 3
    assert len(opener.calls) == 2
    assert opener.calls[0][1] == (
        "/geoserver/siur/wms?service=WMS"
    )


def test_gateway_proxies_admin_body_and_rewrites_internal_location() -> None:
    verifier = SequencedVerifier([verified()])
    opener = RecordingOpener(
        FakeUpstream(
            status=201,
            headers=(
                (
                    "Location",
                    "http://127.0.0.1:8080/geoserver/rest/workspaces/siur",
                ),
                ("Content-Type", "application/json"),
                ("Content-Length", "2"),
                ("Connection", "keep-alive"),
            ),
            chunks=(b"{}",),
        )
    )

    with gateway_client(verifier, opener) as client:
        response = client.post(
            "/geoserver/rest/workspaces",
            headers={
                "Host": "localhost:8081",
                "Authorization": "Basic safe-test-value",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )

    assert response.status_code == 201
    assert response.content == b"{}"
    assert response.headers["location"] == (
        "http://127.0.0.1:8081/geoserver/rest/workspaces/siur"
    )
    method, target, headers, body = opener.calls[0]
    assert (method, target, body) == (
        "POST",
        "/geoserver/rest/workspaces",
        b"{}",
    )
    assert headers["Host"] == "127.0.0.1:8081"
    assert headers["authorization"] == "Basic safe-test-value"
    assert headers["Content-Length"] == "2"


@pytest.mark.parametrize(
    "path",
    [
        "/geoserver/gwc/rest/diskquota.json",
        "/geoserver/gwc/rest/blobstores/other.xml",
        "/geoserver/gwc/rest/blobstores.xml",
    ],
)
def test_gateway_blocks_public_mutation_of_its_own_contract(
    path: str,
) -> None:
    verifier = SequencedVerifier([verified()])
    opener = RecordingOpener(FakeUpstream())

    with gateway_client(verifier, opener) as client:
        response = client.put(
            path,
            headers={"Host": "127.0.0.1:8081"},
            content=b"unsafe",
        )

    assert response.status_code == 403
    assert verifier.calls == 1
    assert opener.calls == []


def test_gateway_rejects_non_loopback_host_and_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = SequencedVerifier([verified()])
    opener = RecordingOpener(FakeUpstream())

    with gateway_client(verifier, opener) as client:
        bad_host = client.get(
            "/geoserver/siur/wms",
            headers={"Host": "maps.example.test"},
        )
        monkeypatch.setattr(
            gateway_module,
            "MAX_REQUEST_BODY_BYTES",
            3,
        )
        too_large = client.post(
            "/geoserver/rest/workspaces",
            headers={"Host": "127.0.0.1:8081"},
            content=b"four",
        )

    assert bad_host.status_code == 400
    assert too_large.status_code == 413
    assert verifier.calls == 1
    assert opener.calls == []


def test_gateway_maps_fixed_upstream_failure_without_leaking_detail() -> None:
    verifier = SequencedVerifier([verified()])
    opener = RecordingOpener(
        GeoWebCacheGatewayUpstreamError("sensitive upstream detail")
    )

    with gateway_client(verifier, opener) as client:
        response = client.get(
            "/geoserver/siur/wms",
            headers={"Host": "127.0.0.1:8081"},
        )

    assert response.status_code == 502
    assert "sensitive" not in response.text
