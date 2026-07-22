import hashlib
import io
import json
import os
import ssl
import struct
import zlib
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import headers_for
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

import app.reference_layers.wms_cache as wms_cache
import app.reference_layers.wms_proxy as wms_proxy
import app.reference_layers.wms_routes as wms_routes
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.models import (
    ReferenceDeliveryAttestation,
    ReferenceLayer,
    ReferenceLicenseReview,
)
from app.reference_layers.wms_cache import (
    CACHE_INDEX_KEY,
    CACHE_SIZES_KEY,
    CACHE_TOTAL_KEY,
    WMS_CACHE_ENTRY_OVERHEAD_BYTES,
    CachedWMSResponse,
    build_wms_cache_key,
    deserialize_cached_wms_response,
    get_cached_wms_response,
    serialize_cached_wms_response,
    store_cached_wms_response,
)
from app.reference_layers.wms_proxy import (
    PNG_SIGNATURE,
    UnsafeWMSEndpointError,
    WMSResponse,
    WMSUpstreamUnavailableError,
    _require_public_addresses,
    _validate_response_body,
    build_identify_request,
    build_tile_request,
    tile_bbox,
    validate_siur_wms_endpoint,
)
from app.reference_layers.wms_schemas import (
    InvalidFeatureInfoError,
    parse_feature_collection,
)
from wms_evidence_fixtures import (
    apply_synthetic_delivery_evidence,
    make_capabilities_xml,
    make_license_review_document,
)


def make_wms_definition(
    *,
    license_status: str = "approved",
    queryable: bool = True,
    supported_crs: tuple[str, ...] = ("EPSG:25830", "EPSG:3857"),
    min_zoom: int | None = None,
    max_zoom: int | None = None,
    bounds: dict[str, float] | None = None,
) -> ReferenceCatalogDefinition:
    return ReferenceCatalogDefinition(
        provider_key="siur",
        source_url="https://idecyl.jcyl.es/siur/assets/settings/settings.json",
        raw_catalog={"fixture": "reference-wms"},
        services=(
            ReferenceServiceDefinition(
                source_key="service:wms:idecyl:urbanismo",
                title="Urbanismo de Castilla y León",
                upstream_protocol="wms",
                base_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
                version="1.3.0",
                default_crs="EPSG:25830",
                default_format="image/png",
                attribution="Junta de Castilla y León",
                license_name="Datos abiertos de Castilla y León",
                license_status=license_status,
                cache_policy="on_demand",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="group:planning",
                node_type="group",
                title="Planeamiento urbanístico",
                sort_order=10,
            ),
            ReferenceLayerDefinition(
                source_key="layer:classification",
                node_type="layer",
                title="Clasificación del suelo",
                parent_key="group:planning",
                service_key="service:wms:idecyl:urbanismo",
                remote_name="plau_cyl_clasificacion",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="proxy",
                style_name="plau_cyl_clasificacion_color",
                image_format="image/png",
                supported_crs=supported_crs,
                bounds=bounds,
                default_visible=True,
                default_opacity=Decimal("0.750"),
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                queryable=queryable,
                styles=(
                    ReferenceLayerStyleDefinition(
                        source_key="plau_cyl_clasificacion_color",
                        title="Clasificación por color",
                        is_default=True,
                    ),
                    ReferenceLayerStyleDefinition(
                        source_key="plau_cyl_clasificacion_trama",
                        title="Clasificación por trama",
                        sort_order=20,
                    ),
                ),
            ),
        ),
        retrieved_at=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
    )


def seed_wms_layer(
    db,
    *,
    with_evidence: bool = True,
    evidence_options: dict | None = None,
    license_document: bytes | None = None,
    **definition_options,
) -> ReferenceLayer:
    apply_catalog_definition(db, make_wms_definition(**definition_options))
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.source_key == "layer:classification"
        )
    )
    if with_evidence:
        apply_synthetic_delivery_evidence(
            db,
            layer,
            license_document=license_document,
            **(evidence_options or {}),
        )
    return layer


def prepare_viewer(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization()
    viewer = make_user()
    grant_permissions(viewer, organization, ["map.view"])
    return organization, viewer


def fake_png_response() -> WMSResponse:
    body = make_png(256, 256)
    return WMSResponse(
        body=body,
        content_type="image/png",
        etag=f'"{hashlib.sha256(body).hexdigest()}"',
    )


def make_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (width * 4) for _ in range(height))
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def make_cached_response(body_size: int, *, marker: int = 1) -> CachedWMSResponse:
    body = bytes([marker]) * body_size
    return CachedWMSResponse(
        body=body,
        content_type="image/png",
        etag=f'"{hashlib.sha256(body).hexdigest()}"',
        stored_at=100,
        fresh_for_seconds=900,
    )


def assert_cache_namespace(redis: Redis, expected_keys: set[str]) -> None:
    encoded_keys = {key.encode() for key in expected_keys}
    assert set(redis.zrange(CACHE_INDEX_KEY, 0, -1)) == encoded_keys
    assert set(redis.hkeys(CACHE_SIZES_KEY)) == encoded_keys
    assert redis.zcard(CACHE_INDEX_KEY) == len(expected_keys)
    assert redis.hlen(CACHE_SIZES_KEY) == len(expected_keys)
    tracked_total = sum(int(value) for value in redis.hvals(CACHE_SIZES_KEY))
    assert int(redis.get(CACHE_TOTAL_KEY) or b"0") == tracked_total
    assert all(redis.exists(key) == 1 for key in expected_keys)


def assert_private_auth_vary(response) -> None:
    tokens = {
        item.strip().casefold()
        for item in response.headers["vary"].split(",")
        if item.strip()
    }
    assert {"authorization", "cookie"} <= tokens


class FakeLocalGeoServerRenderer:
    def __init__(self, *, png_body: bytes | None = None) -> None:
        self.png_body = png_body or make_png(256, 256)

    def render_tile(self, **kwargs):
        return wms_routes.LocalGeoServerResponse(
            body=self.png_body,
            content_type="image/png",
            etag='"local-tile"',
        )

    def render_legend(self, **kwargs):
        return wms_routes.LocalGeoServerResponse(
            body=make_png(20, 20),
            content_type="image/png",
            etag='"local-legend"',
        )

    def get_feature_info(self, **kwargs):
        body = json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "local"},
                        "geometry": None,
                    }
                ],
            }
        ).encode()
        return wms_routes.LocalGeoServerResponse(
            body=body,
            content_type="application/json",
            etag='"local-identify"',
        )


@pytest.fixture
def isolated_wms_cache_redis(monkeypatch):
    redis = Redis.from_url(
        os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
    )
    try:
        redis.ping()
    except RedisError as error:
        pytest.fail(f"isolated Redis test database is unavailable: {error}")
    redis.flushdb()
    monkeypatch.setattr(wms_cache, "get_redis_connection", lambda: redis)
    try:
        yield redis
    finally:
        redis.flushdb()


def disable_test_cache(monkeypatch) -> None:
    monkeypatch.setattr(wms_routes, "get_cached_wms_response", lambda key: None)
    monkeypatch.setattr(
        wms_routes,
        "store_cached_wms_response",
        lambda *args, **kwargs: None,
    )


def test_tile_route_builds_a_fixed_server_side_wms_request(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    captured = []
    disable_test_cache(monkeypatch)

    def fake_fetch(request):
        captured.append(request)
        return fake_png_response()

    monkeypatch.setattr(wms_routes, "fetch_wms_response", fake_fetch)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        params={
            "URL": "https://attacker.example/wms",
            "BBOX": "0,0,1,1",
            "CRS": "EPSG:4326",
            "FORMAT": "text/xml",
            "LAYERS": "attacker:layer",
            "SERVICE": "WFS",
            "REQUEST": "GetCapabilities",
        },
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-reference-cache"] == "MISS"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert_private_auth_vary(response)
    assert len(captured) == 1
    request = captured[0]
    assert request.endpoint_url == (
        "https://idecyl.jcyl.es/geoserver/urbanismo/wms"
    )
    assert request.target.startswith("/geoserver/urbanismo/wms?")
    parameters = parse_qs(urlsplit(request.target).query, keep_blank_values=True)
    assert parameters == {
        "BBOX": [
            "-20037508.34278924,-20037508.34278924,"
            "20037508.34278924,20037508.34278924"
        ],
        "CRS": ["EPSG:3857"],
        "FORMAT": ["image/png"],
        "HEIGHT": ["256"],
        "LAYERS": ["plau_cyl_clasificacion"],
        "REQUEST": ["GetMap"],
        "SERVICE": ["WMS"],
        "STYLES": ["plau_cyl_clasificacion_color"],
        "TRANSPARENT": ["TRUE"],
        "VERSION": ["1.3.0"],
        "WIDTH": ["256"],
    }


def test_wms_routes_accept_httponly_access_token_cookie_without_bearer_header(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    organization = make_organization()
    viewer = make_user(
        email="wms-cookie@example.com",
        password="password-123",
    )
    grant_permissions(viewer, organization, ["map.view"])
    disable_test_cache(monkeypatch)
    feature_body = b'{"type":"FeatureCollection","features":[]}'
    operations = []

    def fake_fetch(request):
        operations.append(request.operation)
        if request.operation == "identify":
            return WMSResponse(
                body=feature_body,
                content_type="application/json",
                etag=f'"{hashlib.sha256(feature_body).hexdigest()}"',
            )
        return fake_png_response()

    monkeypatch.setattr(wms_routes, "fetch_wms_response", fake_fetch)
    login = client.post(
        "/auth/login",
        json={"email": viewer.email, "password": "password-123"},
    )
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    prefix = f"/organizations/{organization.id}/reference-layers/{layer.id}"

    responses = (
        client.get(f"{prefix}/tiles/0/0/0.png"),
        client.get(f"{prefix}/legend.png"),
        client.get(
            f"{prefix}/identify",
            params={"pixel_x": 1, "pixel_y": 1, "x": 0, "y": 0, "z": 0},
        ),
    )

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert operations == ["tile", "legend", "identify"]
    for response in responses:
        assert_private_auth_vary(response)


def test_wms_vary_headers_cover_authentication_and_scoped_errors(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    path = "/organizations/1/reference-layers/1/tiles/0/0/0.png"
    unauthenticated = client.get(path)
    assert unauthenticated.status_code == 401
    assert_private_auth_vary(unauthenticated)

    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    missing = client.get(
        f"/organizations/{organization.id}/reference-layers/999999/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )
    assert missing.status_code == 404
    assert_private_auth_vary(missing)


def test_auth_scope_license_and_layer_capabilities_block_before_network(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(
        db,
        license_status="approved",
        license_document=make_license_review_document(
            "service:wms:idecyl:urbanismo",
            decision="restricted",
            allow_proxy=False,
            allow_cache=False,
        ),
    )
    allowed_organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    other_organization = make_organization()

    def forbidden_fetch(request):
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden_fetch)
    path = (
        f"/organizations/{allowed_organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png"
    )
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers_for(viewer)).status_code == 451
    cross_scope = client.get(
        f"/organizations/{other_organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )
    assert cross_scope.status_code == 403


def test_favorable_legacy_catalog_fields_cannot_deliver_without_attestation(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(
        db,
        with_evidence=False,
        license_status="approved",
        supported_crs=("EPSG:3857",),
    )
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("unattested delivery must not reach cache or network")

    monkeypatch.setattr(wms_routes, "get_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "store_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Reference layer delivery is not attested"
    }


def test_attested_evidence_overrides_non_authoritative_catalog_claims(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(
        db,
        license_status="pending",
        supported_crs=(),
    )
    assert layer.service.capabilities_sha256 is None
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    disable_test_cache(monkeypatch)
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: fake_png_response(),
    )

    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 200


def test_catalog_change_makes_old_attestation_unusable_before_cache_or_network(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    changed = replace(
        make_wms_definition(),
        raw_catalog={"fixture": "new-current-catalog"},
    )
    apply_catalog_definition(db, changed)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("stale attestation must fail before cache or network")

    monkeypatch.setattr(wms_routes, "get_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "store_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 503


def test_corrupt_current_catalog_definition_hash_fails_closed(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    layer.last_seen_snapshot.normalized_definition_json = {"tampered": True}
    db.commit()
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("corrupt catalog must fail before cache or network")

    monkeypatch.setattr(wms_routes, "get_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "store_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 503


def test_latest_restrictive_human_review_revokes_older_attestation(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    current_attestation = db.scalar(
        select(ReferenceDeliveryAttestation).order_by(
            ReferenceDeliveryAttestation.sequence_number.desc()
        )
    )
    current_review = db.get(
        ReferenceLicenseReview,
        current_attestation.license_review_id,
    )
    restricted = make_license_review_document(
        layer.service.source_key,
        decision="restricted",
        allow_proxy=False,
        allow_cache=False,
        reviewer="Synthetic Revocation Reviewer",
        reviewed_at="2026-07-17T13:30:00Z",
        supersedes_review_sha256=current_review.review_sha256,
    )
    result, plan, _, _ = apply_synthetic_delivery_evidence(
        db,
        layer,
        license_document=restricted,
    )
    assert plan.attestable is True
    assert plan.attestation_kind == "revocation"
    assert result.attestation_id is not None
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("revoked delivery must fail before cache or network")

    monkeypatch.setattr(wms_routes, "get_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 451


def test_superseding_no_cache_review_fails_closed_until_capabilities_match(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    current_attestation = db.scalar(
        select(ReferenceDeliveryAttestation).order_by(
            ReferenceDeliveryAttestation.sequence_number.desc()
        )
    )
    current_review = db.get(
        ReferenceLicenseReview,
        current_attestation.license_review_id,
    )
    no_cache_document = make_license_review_document(
        layer.service.source_key,
        allow_cache=False,
        reviewer="Synthetic No-Cache Reviewer",
        reviewed_at="2026-07-17T13:30:00Z",
        supersedes_review_sha256=current_review.review_sha256,
    )
    revoked, revoked_plan, _, _ = apply_synthetic_delivery_evidence(
        db,
        layer,
        capabilities_xml=make_capabilities_xml(
            endpoint="https://idecyl.jcyl.es/geoserver/otro/wms"
        ),
        license_document=no_cache_document,
    )
    assert revoked_plan.attestation_kind == "revocation"
    assert revoked.attestation_id is not None
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("revoked delivery must not use Redis or the network")

    monkeypatch.setattr(wms_routes, "get_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "store_cached_wms_response", forbidden)
    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden)
    path = (
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png"
    )
    denied = client.get(path, headers=headers_for(viewer))
    assert denied.status_code == 451

    restored, restored_plan, _, _ = apply_synthetic_delivery_evidence(
        db,
        layer,
        license_document=no_cache_document,
    )
    assert restored_plan.attestation_kind == "delivery"
    assert restored.attestation_id != revoked.attestation_id
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: fake_png_response(),
    )
    delivered = client.get(path, headers=headers_for(viewer))

    assert delivered.status_code == 200
    assert delivered.headers["x-reference-cache"] == "BYPASS"


def test_license_cache_permission_is_required_before_any_redis_access(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(
        db,
        license_document=make_license_review_document(
            "service:wms:idecyl:urbanismo",
            allow_proxy=True,
            allow_cache=False,
        ),
    )
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden_cache(*args, **kwargs):
        raise AssertionError("license-disabled cache must not be accessed")

    monkeypatch.setattr(wms_routes, "get_cached_wms_response", forbidden_cache)
    monkeypatch.setattr(wms_routes, "store_cached_wms_response", forbidden_cache)
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: fake_png_response(),
    )
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert response.headers["x-reference-cache"] == "BYPASS"


def test_cache_key_is_bound_to_the_current_attestation_hash(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    attestation = db.scalar(
        select(ReferenceDeliveryAttestation).order_by(
            ReferenceDeliveryAttestation.id.desc()
        )
    )
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    captured = []
    real_builder = build_wms_cache_key

    def capture_key(parts):
        captured.append(parts)
        return real_builder(parts)

    monkeypatch.setattr(wms_routes, "build_wms_cache_key", capture_key)
    monkeypatch.setattr(wms_routes, "get_cached_wms_response", lambda key: None)
    monkeypatch.setattr(
        wms_routes,
        "store_cached_wms_response",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: fake_png_response(),
    )
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert captured == [
        {
            "coordinates": {"x": 0, "y": 0, "z": 0},
            "attestation_sha256": attestation.attestation_sha256,
            "layer_id": layer.id,
            "operation": "tile",
            "provider_key": "siur",
            "style_id": layer.styles[0].id,
        }
    ]


def test_attested_endpoint_version_and_style_must_match_exactly(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    layer.service.version = "1.1.1"
    db.commit()
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: (_ for _ in ()).throw(
            AssertionError("mismatched evidence must block upstream")
        ),
    )
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )
    assert response.status_code == 409


def test_style_id_resolves_to_the_exact_case_sensitive_upstream_style(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    definition = make_wms_definition()
    layer_definition = definition.layers[1]
    exact_styles = (
        "Plau_Cyl_Clasificacion_Color",
        "Plau_Cyl_Clasificacion_Trama",
    )
    styled_layer = replace(
        layer_definition,
        styles=tuple(
            replace(style, remote_name=remote_name)
            for style, remote_name in zip(
                layer_definition.styles,
                exact_styles,
                strict=True,
            )
        ),
    )
    apply_catalog_definition(
        db,
        replace(definition, layers=(definition.layers[0], styled_layer)),
    )
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.source_key == "layer:classification"
        )
    )
    apply_synthetic_delivery_evidence(db, layer, styles=exact_styles)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    disable_test_cache(monkeypatch)
    captured = []

    def capture(request):
        captured.append(request)
        return fake_png_response()

    monkeypatch.setattr(wms_routes, "fetch_wms_response", capture)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        params={"style_id": layer.styles[0].id},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert parse_qs(urlsplit(captured[0].target).query)["STYLES"] == [
        exact_styles[0]
    ]


def test_catalog_exposes_only_attested_effective_delivery_availability(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    layer = seed_wms_layer(db)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    response = client.get(
        "/reference-layers/catalog",
        params={"organization_id": organization.id, "provider_key": "siur"},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    serialized = response.text
    delivered = next(
        item for item in response.json()["layers"] if item["id"] == layer.id
    )
    assert delivered["delivery_available"] is True
    assert delivered["legend_available"] is True
    assert delivered["identify_available"] is True
    assert delivered["available_style_ids"] == [
        style.id for style in layer.styles
    ]
    for secret in (
        "remote_name",
        "attestation_sha256",
        "reviewer",
        "get_map_endpoint",
        "license_status",
    ):
        assert secret not in serialized


def test_catalog_does_not_advertise_delivery_for_a_disabled_service(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    layer = seed_wms_layer(db)
    layer.service.status = "disabled"
    db.commit()
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    response = client.get(
        "/reference-layers/catalog",
        params={"organization_id": organization.id, "provider_key": "siur"},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    delivered = next(
        item for item in response.json()["layers"] if item["id"] == layer.id
    )
    assert delivered["delivery_available"] is False
    assert delivered["delivery_blocker"] == "not_deliverable"


def test_catalog_reports_external_wms_not_deliverable_despite_prior_attestation(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    layer = seed_wms_layer(db)
    layer.service.base_url = (
        "https://www.ign.es/wms-inspire/unidades-administrativas"
    )
    db.commit()
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    response = client.get(
        "/reference-layers/catalog",
        params={"organization_id": organization.id, "provider_key": "siur"},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    delivered = next(
        item for item in response.json()["layers"] if item["id"] == layer.id
    )
    assert delivered["delivery_available"] is False
    assert delivered["delivery_blocker"] == "not_deliverable"


def test_explicit_style_legend_availability_is_independent_of_default_style(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(
        db,
        evidence_options={"styles": ("plau_cyl_clasificacion_trama",)},
    )
    default_style = next(style for style in layer.styles if style.is_default)
    explicit_style = next(style for style in layer.styles if not style.is_default)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    response = client.get(
        "/reference-layers/catalog",
        params={"organization_id": organization.id, "provider_key": "siur"},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    body = response.json()
    delivered = next(item for item in body["layers"] if item["id"] == layer.id)
    styles = {item["id"]: item for item in body["styles"]}
    assert delivered["delivery_available"] is False
    assert delivered["legend_available"] is False
    assert delivered["identify_available"] is True
    assert delivered["available_style_ids"] == [explicit_style.id]
    assert styles[default_style.id]["legend_available"] is False
    assert styles[explicit_style.id]["legend_available"] is True

    disable_test_cache(monkeypatch)

    def fake_fetch(request):
        if request.operation == "identify":
            body = b'{"type":"FeatureCollection","features":[]}'
            return WMSResponse(
                body=body,
                content_type="application/json",
                etag=f'"{hashlib.sha256(body).hexdigest()}"',
            )
        return fake_png_response()

    monkeypatch.setattr(wms_routes, "fetch_wms_response", fake_fetch)
    tile = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        params={"style_id": explicit_style.id},
        headers=headers_for(viewer),
    )
    legend = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/legend.png",
        params={"style_id": explicit_style.id},
        headers=headers_for(viewer),
    )
    identify = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/identify",
        params={
            "pixel_x": 1,
            "pixel_y": 1,
            "style_id": explicit_style.id,
            "x": 0,
            "y": 0,
            "z": 0,
        },
        headers=headers_for(viewer),
    )
    assert tile.status_code == 200
    assert legend.status_code == 200
    assert identify.status_code == 200


@pytest.mark.parametrize(
    "evidence_options",
    [
        {"styles": ()},
        {"map_formats": ("image/jpeg",)},
    ],
)
def test_attested_getmap_requires_png_and_the_exact_catalog_style(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
    evidence_options,
) -> None:
    layer = seed_wms_layer(db, evidence_options=evidence_options)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: (_ for _ in ()).throw(
            AssertionError("unattested capability must block upstream")
        ),
    )

    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 409


def test_legend_requires_its_own_attested_png_operation(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db, evidence_options={"legend_formats": ()})
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    disable_test_cache(monkeypatch)
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: fake_png_response(),
    )
    prefix = f"/organizations/{organization.id}/reference-layers/{layer.id}"

    tile = client.get(f"{prefix}/tiles/0/0/0.png", headers=headers_for(viewer))
    legend = client.get(f"{prefix}/legend.png", headers=headers_for(viewer))

    assert tile.status_code == 200
    assert legend.status_code == 409


def test_attested_getmap_endpoint_must_match_the_current_service_exactly(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    layer.service.base_url = "https://idecyl.jcyl.es/geoserver/otro/wms"
    db.commit()
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: (_ for _ in ()).throw(
            AssertionError("mismatched endpoint must block upstream")
        ),
    )

    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "evidence_options",
    [
        {"queryable": False},
        {"feature_info_formats": ()},
    ],
)
def test_identify_requires_attested_queryability_endpoint_and_json(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
    evidence_options,
) -> None:
    layer = seed_wms_layer(db, evidence_options=evidence_options)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    disable_test_cache(monkeypatch)
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: fake_png_response(),
    )
    prefix = f"/organizations/{organization.id}/reference-layers/{layer.id}"

    tile = client.get(f"{prefix}/tiles/0/0/0.png", headers=headers_for(viewer))
    identify = client.get(
        f"{prefix}/identify",
        params={"pixel_x": 1, "pixel_y": 1, "x": 0, "y": 0, "z": 0},
        headers=headers_for(viewer),
    )

    assert tile.status_code == 200
    assert identify.status_code == 409
    assert identify.json() == {"detail": "Layer is not queryable"}


def test_tile_route_requires_web_mercator_support(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    layer = seed_wms_layer(
        db,
        supported_crs=("EPSG:25830", "EPSG:3857"),
        evidence_options={"crs": ("EPSG:25830",)},
    )
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Layer does not support web map tiles"


def test_tile_route_respects_layer_zoom_and_geographic_bounds(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(
        db,
        min_zoom=5,
        max_zoom=15,
        bounds={"west": -7.1, "south": 39.9, "east": -1.7, "north": 43.3},
    )
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden_fetch(request):
        raise AssertionError("out-of-scope tile must not call upstream")

    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden_fetch)
    prefix = f"/organizations/{organization.id}/reference-layers/{layer.id}/tiles"
    headers = headers_for(viewer)

    wrong_zoom = client.get(f"{prefix}/4/0/0.png", headers=headers)
    outside_bounds = client.get(f"{prefix}/5/0/0.png", headers=headers)

    assert wrong_zoom.status_code == 404
    assert outside_bounds.status_code == 404


def test_tile_route_supports_the_catalog_maximum_zoom(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db, min_zoom=24, max_zoom=24)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    disable_test_cache(monkeypatch)
    captured = []

    def capture(request):
        captured.append(request)
        return fake_png_response()

    monkeypatch.setattr(wms_routes, "fetch_wms_response", capture)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/24/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert len(captured) == 1


@pytest.mark.parametrize(
    "malformed_bounds",
    [
        {},
        {"west": -7.1, "south": 39.9, "east": -1.7},
        {"west": "-7.1", "south": 39.9, "east": -1.7, "north": 43.3},
        {"west": False, "south": 39.9, "east": -1.7, "north": 43.3},
        {"west": -1.7, "south": 39.9, "east": -7.1, "north": 43.3},
        ["-7.1", "39.9", "-1.7", "43.3"],
    ],
)
def test_present_malformed_geographic_bounds_fail_closed_before_network(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
    malformed_bounds,
) -> None:
    layer = seed_wms_layer(db)
    layer.bounds_json = malformed_bounds
    db.commit()
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def forbidden_fetch(request):
        raise AssertionError("malformed bounds must block before upstream")

    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden_fetch)
    prefix = f"/organizations/{organization.id}/reference-layers/{layer.id}"
    headers = headers_for(viewer)

    tile = client.get(f"{prefix}/tiles/0/0/0.png", headers=headers)
    identify = client.get(
        f"{prefix}/identify",
        params={"pixel_x": 1, "pixel_y": 1, "x": 0, "y": 0, "z": 0},
        headers=headers,
    )

    assert tile.status_code == 409
    assert tile.json() == {"detail": "Layer cannot be rendered"}
    assert identify.status_code == 409
    assert identify.json() == {"detail": "Layer cannot be rendered"}


def test_cache_hit_and_conditional_request_do_not_reach_upstream(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    upstream = fake_png_response()
    cached = CachedWMSResponse(
        body=upstream.body,
        content_type=upstream.content_type,
        etag=upstream.etag,
        stored_at=2_000_000_000,
        fresh_for_seconds=900,
    )
    monkeypatch.setattr(
        wms_routes,
        "get_cached_wms_response",
        lambda key: cached,
    )

    def forbidden_fetch(request):
        raise AssertionError("cache hit must not call upstream")

    monkeypatch.setattr(wms_routes, "fetch_wms_response", forbidden_fetch)
    path = (
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png"
    )
    headers = headers_for(viewer)
    first = client.get(path, headers=headers)
    conditional = client.get(
        path,
        headers={**headers, "If-None-Match": upstream.etag},
    )

    assert first.status_code == 200
    assert first.headers["x-reference-cache"] == "HIT"
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert_private_auth_vary(first)
    assert_private_auth_vary(conditional)


def test_identify_is_typed_queryable_and_validates_feature_collection(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    body = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "classification.1",
                    "geometry": None,
                    "properties": {"classification": "urbano"},
                }
            ],
        }
    ).encode()
    captured = []

    def fake_fetch(request):
        captured.append(request)
        return WMSResponse(
            body=body,
            content_type="application/json",
            etag=f'"{hashlib.sha256(body).hexdigest()}"',
        )

    monkeypatch.setattr(wms_routes, "fetch_wms_response", fake_fetch)
    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}/identify",
        params={
            "feature_count": 1,
            "pixel_x": 100,
            "pixel_y": 120,
            "x": 1,
            "y": 1,
            "z": 2,
        },
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert response.json()["features"][0]["id"] == "classification.1"
    parameters = parse_qs(urlsplit(captured[0].target).query)
    assert parameters["REQUEST"] == ["GetFeatureInfo"]
    assert parameters["I"] == ["100"]
    assert parameters["J"] == ["120"]
    assert parameters["FEATURE_COUNT"] == ["1"]
    assert parameters["INFO_FORMAT"] == ["application/json"]


def test_invalid_upstream_identify_is_a_generic_bad_gateway(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    body = b'{"token":"secret"}'
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: WMSResponse(
            body=body,
            content_type="application/json",
            etag=f'"{hashlib.sha256(body).hexdigest()}"',
        ),
    )

    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}/identify",
        params={"pixel_x": 1, "pixel_y": 1, "x": 0, "y": 0, "z": 0},
        headers=headers_for(viewer),
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Reference map service is unavailable"}
    assert "secret" not in response.text


def test_public_contract_has_no_arbitrary_wms_or_url_parameters(client) -> None:
    schema = client.get("/openapi.json").json()
    relevant = {
        path: item
        for path, item in schema["paths"].items()
        if "reference-layers" in path
        and any(token in path for token in ("identify", "legend.png", "tiles"))
    }
    assert len(relevant) == 3
    parameter_names = {
        parameter["name"]
        for item in relevant.values()
        for operation in item.values()
        for parameter in operation.get("parameters", [])
    }
    assert parameter_names <= {
        "access_token",
        "feature_count",
        "layer_id",
        "organization_id",
        "pixel_x",
        "pixel_y",
        "style_id",
        "x",
        "y",
        "z",
    }
    assert not {name.casefold() for name in parameter_names}.intersection(
        {"bbox", "crs", "format", "layers", "request", "service", "url"}
    )


def test_wms_builders_validate_tiles_versions_and_parameter_names() -> None:
    assert tile_bbox(0, 0, 0) == pytest.approx(
        (
            -20_037_508.342789244,
            -20_037_508.342789244,
            20_037_508.342789244,
            20_037_508.342789244,
        )
    )
    with pytest.raises(ValueError, match="tile coordinates"):
        tile_bbox(2, 4, 0)
    west, south, east, north = tile_bbox(24, 2**24 - 1, 2**24 - 1)
    assert east > west
    assert north > south
    request = build_identify_request(
        endpoint_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
        version="1.1.1",
        remote_name="plau_cyl_clasificacion",
        style_name="",
        z=0,
        x=0,
        y=0,
        pixel_x=12,
        pixel_y=34,
        feature_count=2,
    )
    parameters = parse_qs(urlsplit(request.target).query)
    assert "SRS" in parameters and "CRS" not in parameters
    assert parameters["X"] == ["12"] and parameters["Y"] == ["34"]
    with pytest.raises(ValueError, match="version"):
        build_tile_request(
            endpoint_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
            version="1.0.0",
            remote_name="plau_cyl_clasificacion",
            style_name="",
            z=0,
            x=0,
            y=0,
        )


@pytest.mark.parametrize(
    "value",
    [
        "http://idecyl.jcyl.es/geoserver/urbanismo/wms",
        "https://evil.example/geoserver/urbanismo/wms",
        "https://idecyl.jcyl.es:8443/geoserver/urbanismo/wms",
        "https://user@idecyl.jcyl.es/geoserver/urbanismo/wms",
        "https://idecyl.jcyl.es/geoserver/urbanismo/wms?token=secret",
        "https://idecyl.jcyl.es/geonetwork/srv/spa/catalog.search",
    ],
)
def test_siur_wms_allowlist_rejects_unsafe_endpoints(value) -> None:
    with pytest.raises(UnsafeWMSEndpointError):
        validate_siur_wms_endpoint(value)
    assert validate_siur_wms_endpoint(
        "https://idecyl.jcyl.es/geoserver/urbanismo/ows"
    ).hostname == "idecyl.jcyl.es"


def test_dns_policy_rejects_private_and_mixed_answers() -> None:
    assert _require_public_addresses(
        ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]
    ) == (
        "8.8.8.8",
        "1.1.1.1",
        "2606:4700:4700::1111",
    )
    with pytest.raises(UnsafeWMSEndpointError):
        _require_public_addresses(["127.0.0.1"])
    with pytest.raises(UnsafeWMSEndpointError):
        _require_public_addresses(["8.8.8.8", "10.0.0.1"])
    with pytest.raises(WMSUpstreamUnavailableError):
        _require_public_addresses([])


@pytest.mark.parametrize(
    "unsafe_address",
    [
        "224.0.0.1",
        "ff02::1",
        "240.0.0.1",
        "64:ff9b::808:808",
        "64:ff9b:1::1",
        "::ffff:127.0.0.1",
        "2002:0808:0808::1",
    ],
)
def test_dns_policy_rejects_non_public_unicast_and_transition_addresses(
    unsafe_address,
) -> None:
    with pytest.raises(UnsafeWMSEndpointError):
        _require_public_addresses([unsafe_address])
    with pytest.raises(UnsafeWMSEndpointError):
        _require_public_addresses(["8.8.8.8", unsafe_address])


@pytest.mark.parametrize(
    "value",
    ["", "-1", "+1", "1, 1", "１２", "9" * 21, " " * 21],
)
def test_content_length_parser_rejects_malformed_and_overlong_values(value) -> None:
    with pytest.raises(WMSUpstreamUnavailableError):
        wms_proxy._content_length(value)
    assert wms_proxy._content_length(None) is None
    assert wms_proxy._content_length(" 123 ") == 123


@pytest.mark.parametrize("content_length", ["not-a-number", "9" * 100])
def test_malformed_content_length_becomes_generic_bad_gateway(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
    content_length,
) -> None:
    layer = seed_wms_layer(db)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    disable_test_cache(monkeypatch)

    class FakeSocket:
        def settimeout(self, value):
            return None

    class FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.body = io.BytesIO(make_png(256, 256))

        def getheader(self, name):
            return {
                "Content-Encoding": "identity",
                "Content-Type": "image/png",
                "Content-Length": content_length,
            }.get(name)

        def read(self, size):
            return self.body.read(size)

    class FakeConnection:
        def __init__(self, *args, **kwargs) -> None:
            self.sock = FakeSocket()
            self.response = FakeResponse()

        def request(self, *args, **kwargs) -> None:
            return None

        def getresponse(self):
            return self.response

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        wms_proxy,
        "_resolve_public_addresses",
        lambda *args, **kwargs: ("8.8.8.8",),
    )
    monkeypatch.setattr(wms_proxy, "_PinnedHTTPSConnection", FakeConnection)

    response = client.get(
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png",
        headers=headers_for(viewer),
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Reference map service is unavailable"}
    assert content_length not in response.text


def test_pinned_https_connection_uses_validated_ip_and_tls_hostname(
    monkeypatch,
) -> None:
    connection = wms_proxy._PinnedHTTPSConnection(
        "idecyl.jcyl.es",
        "8.8.8.8",
        timeout=1.5,
    )
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    assert connection._context.check_hostname is True
    calls = []
    raw_socket = object()
    wrapped_socket = object()

    class FakeContext:
        def wrap_socket(self, value, *, server_hostname):
            calls.append(("wrap", value, server_hostname))
            return wrapped_socket

    def fake_create_connection(address, timeout, source_address):
        calls.append(("connect", address, timeout, source_address))
        return raw_socket

    connection._context = FakeContext()
    monkeypatch.setattr(wms_proxy.socket, "create_connection", fake_create_connection)

    connection.connect()

    assert connection.sock is wrapped_socket
    assert calls == [
        ("connect", ("8.8.8.8", 443), 1.5, None),
        ("wrap", raw_socket, "idecyl.jcyl.es"),
    ]


def test_cache_keys_are_opaque_deterministic_and_integrity_checked() -> None:
    parts = {
        "definition_sha256": "a" * 64,
        "layer_id": 10,
        "operation": "tile",
        "provider_key": "siur",
        "style_id": 20,
        "x": 1,
        "y": 2,
        "z": 3,
    }
    key = build_wms_cache_key(parts)
    assert key == build_wms_cache_key(dict(reversed(list(parts.items()))))
    assert "siur" not in key and "urbanismo" not in key
    upstream = fake_png_response()
    cached = CachedWMSResponse(
        body=upstream.body,
        content_type=upstream.content_type,
        etag=upstream.etag,
        stored_at=100,
        fresh_for_seconds=900,
    )
    serialized = serialize_cached_wms_response(cached)
    assert deserialize_cached_wms_response(serialized) == cached
    with pytest.raises(ValueError, match="digest"):
        deserialize_cached_wms_response(serialized + b"tampered")


def test_cache_accounts_for_metadata_and_replacement_without_double_counting(
    isolated_wms_cache_redis,
    monkeypatch,
) -> None:
    redis = isolated_wms_cache_redis
    scores = iter((100, 200))
    monkeypatch.setattr(wms_cache, "_current_lru_score", lambda: next(scores))
    key = build_wms_cache_key({"entry": "replacement"})
    first = make_cached_response(32)
    second = make_cached_response(96, marker=2)

    store_cached_wms_response(key, first, stale_ttl_seconds=3600)

    first_size = (
        len(serialize_cached_wms_response(first)) + WMS_CACHE_ENTRY_OVERHEAD_BYTES
    )
    assert int(redis.hget(CACHE_SIZES_KEY, key)) == first_size
    assert int(redis.get(CACHE_TOTAL_KEY)) == first_size
    assert_cache_namespace(redis, {key})

    store_cached_wms_response(key, second, stale_ttl_seconds=3600)

    second_size = (
        len(serialize_cached_wms_response(second)) + WMS_CACHE_ENTRY_OVERHEAD_BYTES
    )
    assert int(redis.hget(CACHE_SIZES_KEY, key)) == second_size
    assert int(redis.get(CACHE_TOTAL_KEY)) == second_size
    assert_cache_namespace(redis, {key})


def test_cache_evicts_oldest_entry_at_exact_byte_budget(
    isolated_wms_cache_redis,
    monkeypatch,
) -> None:
    redis = isolated_wms_cache_redis
    response = make_cached_response(48)
    accounted_size = (
        len(serialize_cached_wms_response(response)) + WMS_CACHE_ENTRY_OVERHEAD_BYTES
    )
    monkeypatch.setattr(wms_cache, "WMS_CACHE_BUDGET_BYTES", accounted_size * 2)
    scores = iter((100, 200, 300))
    monkeypatch.setattr(wms_cache, "_current_lru_score", lambda: next(scores))
    keys = [build_wms_cache_key({"byte-entry": index}) for index in range(3)]

    for key in keys:
        store_cached_wms_response(key, response, stale_ttl_seconds=3600)

    assert redis.exists(keys[0]) == 0
    assert int(redis.get(CACHE_TOTAL_KEY)) == accounted_size * 2
    assert_cache_namespace(redis, set(keys[1:]))


def test_cache_enforces_entry_limit_independently_of_byte_budget(
    isolated_wms_cache_redis,
    monkeypatch,
) -> None:
    redis = isolated_wms_cache_redis
    monkeypatch.setattr(wms_cache, "WMS_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(wms_cache, "WMS_CACHE_BUDGET_BYTES", 10 * 1024 * 1024)
    scores = iter((100, 200, 300))
    monkeypatch.setattr(wms_cache, "_current_lru_score", lambda: next(scores))
    keys = [build_wms_cache_key({"count-entry": index}) for index in range(3)]

    for index, key in enumerate(keys):
        store_cached_wms_response(
            key,
            make_cached_response(16, marker=index + 1),
            stale_ttl_seconds=3600,
        )

    assert redis.exists(keys[0]) == 0
    assert_cache_namespace(redis, set(keys[1:]))


def test_atomic_cache_get_touches_lru_before_entry_limit_eviction(
    isolated_wms_cache_redis,
    monkeypatch,
) -> None:
    redis = isolated_wms_cache_redis
    monkeypatch.setattr(wms_cache, "WMS_CACHE_MAX_ENTRIES", 2)
    scores = iter((100, 200, 300, 400))
    monkeypatch.setattr(wms_cache, "_current_lru_score", lambda: next(scores))
    keys = [build_wms_cache_key({"lru-entry": index}) for index in range(3)]
    response = make_cached_response(24)
    store_cached_wms_response(keys[0], response, stale_ttl_seconds=3600)
    store_cached_wms_response(keys[1], response, stale_ttl_seconds=3600)

    assert get_cached_wms_response(keys[0]) == response
    store_cached_wms_response(keys[2], response, stale_ttl_seconds=3600)

    assert redis.exists(keys[1]) == 0
    assert_cache_namespace(redis, {keys[0], keys[2]})


def test_atomic_cache_get_cleans_expired_value_and_metadata(
    isolated_wms_cache_redis,
    monkeypatch,
) -> None:
    redis = isolated_wms_cache_redis
    scores = iter((100, 200, 300))
    monkeypatch.setattr(wms_cache, "_current_lru_score", lambda: next(scores))
    expired_key = build_wms_cache_key({"entry": "expired"})
    live_key = build_wms_cache_key({"entry": "live"})
    response = make_cached_response(40)
    store_cached_wms_response(expired_key, response, stale_ttl_seconds=3600)
    store_cached_wms_response(live_key, response, stale_ttl_seconds=3600)
    live_size = int(redis.hget(CACHE_SIZES_KEY, live_key))
    redis.delete(expired_key)

    assert get_cached_wms_response(expired_key) is None

    assert redis.zscore(CACHE_INDEX_KEY, expired_key) is None
    assert redis.hget(CACHE_SIZES_KEY, expired_key) is None
    assert int(redis.get(CACHE_TOTAL_KEY)) == live_size
    assert_cache_namespace(redis, {live_key})


def test_cache_redis_failures_are_misses_and_noop_writes(monkeypatch) -> None:
    class BrokenRedis:
        def eval(self, *args):
            raise RedisError("unavailable")

    monkeypatch.setattr(wms_cache, "get_redis_connection", lambda: BrokenRedis())
    key = build_wms_cache_key({"entry": "fail-open"})

    assert get_cached_wms_response(key) is None
    store_cached_wms_response(
        key,
        make_cached_response(16),
        stale_ttl_seconds=3600,
    )


def test_png_validation_rejects_wrong_tile_dimensions_and_invalid_headers() -> None:
    _validate_response_body(
        make_png(256, 256),
        operation="tile",
        content_type="image/png",
    )
    with pytest.raises(WMSUpstreamUnavailableError, match="dimensions"):
        _validate_response_body(
            make_png(512, 512),
            operation="tile",
            content_type="image/png",
        )
    invalid_crc = bytearray(make_png(256, 256))
    invalid_crc[29] ^= 1
    with pytest.raises(WMSUpstreamUnavailableError, match="PNG header"):
        _validate_response_body(
            bytes(invalid_crc),
            operation="tile",
            content_type="image/png",
        )


def test_feature_collection_parser_rejects_duplicates_constants_and_limits() -> None:
    valid = parse_feature_collection(
        b'{"type":"FeatureCollection","features":[]}',
        max_features=1,
    )
    assert valid == {"type": "FeatureCollection", "features": []}
    with pytest.raises(InvalidFeatureInfoError):
        parse_feature_collection(
            b'{"type":"FeatureCollection","type":"FeatureCollection",'
            b'"features":[]}',
            max_features=1,
        )
    deeply_nested = (
        b'{"type":"FeatureCollection","features":[],"nested":'
        + b"[" * 1100
        + b"0"
        + b"]" * 1100
        + b"}"
    )
    with pytest.raises(InvalidFeatureInfoError):
        parse_feature_collection(deeply_nested, max_features=1)
    with pytest.raises(InvalidFeatureInfoError):
        parse_feature_collection(
            b'{"type":"FeatureCollection","features":[],"value":NaN}',
            max_features=1,
        )
    with pytest.raises(InvalidFeatureInfoError):
        parse_feature_collection(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {"type": "Feature", "properties": {}, "geometry": None},
                        {"type": "Feature", "properties": {}, "geometry": None},
                    ],
                }
            ).encode(),
            max_features=1,
        )


def _local_geoserver_selection(layer: ReferenceLayer):
    return wms_routes.LocalDeliverySelection(
        backend="geoserver",
        version_id=77,
        generation=3,
        delivery_kind="vector",
        asset_id=88,
        asset_sha256="a" * 64,
        storage_key="reference_data.layer_v77",
        layer_name="layer_v77",
        style_name="style_v77",
        content_type="application/x-postgis-table",
        identify_available=layer.queryable,
        legend_available=True,
    )


def test_tile_route_prefers_active_local_delivery_without_wms_evidence(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db, with_evidence=False, license_status="pending")
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    monkeypatch.setattr(
        wms_routes,
        "resolve_local_delivery",
        lambda db, layer, style, operation: _local_geoserver_selection(layer),
    )
    monkeypatch.setattr(
        wms_routes,
        "LocalGeoServerRenderer",
        FakeLocalGeoServerRenderer,
    )
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: pytest.fail("the upstream WMS must not be contacted"),
    )

    path = (
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png"
    )
    delivered = client.get(path, headers=headers_for(viewer))
    not_modified = client.get(
        path,
        headers={**headers_for(viewer), "If-None-Match": '"local-tile"'},
    )

    assert delivered.status_code == 200
    assert delivered.headers["x-reference-cache"] == "GWC"
    assert delivered.headers["x-reference-version"] == "77"
    assert delivered.headers["content-type"].startswith("image/png")
    assert not_modified.status_code == 304


def test_local_legend_and_identify_keep_authenticated_public_contract(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db, with_evidence=False, license_status="pending")
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )
    monkeypatch.setattr(
        wms_routes,
        "resolve_local_delivery",
        lambda db, layer, style, operation: _local_geoserver_selection(layer),
    )
    monkeypatch.setattr(
        wms_routes,
        "LocalGeoServerRenderer",
        FakeLocalGeoServerRenderer,
    )
    prefix = f"/organizations/{organization.id}/reference-layers/{layer.id}"

    legend = client.get(f"{prefix}/legend.png", headers=headers_for(viewer))
    identify = client.get(
        f"{prefix}/identify",
        params={
            "z": 0,
            "x": 0,
            "y": 0,
            "pixel_x": 128,
            "pixel_y": 128,
        },
        headers=headers_for(viewer),
    )

    assert legend.status_code == 200
    assert legend.headers["x-reference-version"] == "77"
    assert identify.status_code == 200
    assert identify.json()["features"][0]["properties"] == {"name": "local"}
    assert identify.headers["x-reference-version"] == "77"


def test_invalid_active_local_state_never_falls_back_to_remote_wms(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db, with_evidence=True)
    organization, viewer = prepare_viewer(
        db,
        make_user,
        make_organization,
        grant_permissions,
    )

    def reject_local(*args, **kwargs):
        raise wms_routes.LocalDeliveryError("local_version_invalid")

    monkeypatch.setattr(wms_routes, "resolve_local_delivery", reject_local)
    monkeypatch.setattr(
        wms_routes,
        "fetch_wms_response",
        lambda request: pytest.fail("the upstream WMS must not be contacted"),
    )
    path = (
        f"/organizations/{organization.id}/reference-layers/{layer.id}"
        "/tiles/0/0/0.png"
    )

    response = client.get(path, headers=headers_for(viewer))

    assert response.status_code == 503
    assert response.json()["detail"] == "Local reference layer is unavailable"
