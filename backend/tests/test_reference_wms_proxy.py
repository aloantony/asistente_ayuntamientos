import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import headers_for
from sqlalchemy import select

import app.reference_layers.wms_routes as wms_routes
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.models import ReferenceLayer
from app.reference_layers.wms_cache import (
    CACHE_INDEX_KEY,
    CACHE_SIZES_KEY,
    CACHE_TOTAL_KEY,
    WMS_CACHE_BUDGET_BYTES,
    CachedWMSResponse,
    build_wms_cache_key,
    deserialize_cached_wms_response,
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


def seed_wms_layer(db, **definition_options) -> ReferenceLayer:
    apply_catalog_definition(db, make_wms_definition(**definition_options))
    return db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.source_key == "layer:classification"
        )
    )


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
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-reference-cache"] == "MISS"
    assert response.headers["x-content-type-options"] == "nosniff"
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


def test_auth_scope_license_and_layer_capabilities_block_before_network(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
) -> None:
    layer = seed_wms_layer(db, license_status="pending")
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


def test_tile_route_requires_web_mercator_support(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    layer = seed_wms_layer(db, supported_crs=("EPSG:25830",))
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
    assert not parameter_names.intersection(
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
    assert _require_public_addresses(["8.8.8.8", "1.1.1.1"]) == (
        "8.8.8.8",
        "1.1.1.1",
    )
    with pytest.raises(UnsafeWMSEndpointError):
        _require_public_addresses(["127.0.0.1"])
    with pytest.raises(UnsafeWMSEndpointError):
        _require_public_addresses(["8.8.8.8", "10.0.0.1"])
    with pytest.raises(WMSUpstreamUnavailableError):
        _require_public_addresses([])


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


def test_cache_store_uses_an_atomic_namespace_budget(monkeypatch) -> None:
    calls = []

    class FakeRedis:
        def eval(self, *args):
            calls.append(args)
            return len(args[-5])

    monkeypatch.setattr(
        "app.reference_layers.wms_cache.get_redis_connection",
        lambda: FakeRedis(),
    )
    upstream = fake_png_response()
    cached = CachedWMSResponse(
        body=upstream.body,
        content_type=upstream.content_type,
        etag=upstream.etag,
        stored_at=100,
        fresh_for_seconds=900,
    )

    store_cached_wms_response("reference-wms:v1:test", cached, stale_ttl_seconds=3600)

    assert len(calls) == 1
    call = calls[0]
    assert call[1:6] == (
        4,
        "reference-wms:v1:test",
        CACHE_INDEX_KEY,
        CACHE_SIZES_KEY,
        CACHE_TOTAL_KEY,
    )
    assert call[-2] == WMS_CACHE_BUDGET_BYTES


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
