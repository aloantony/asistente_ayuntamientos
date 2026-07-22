import hashlib
import json
import struct
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.reference_layers.local_geoserver import (
    FEATURE_INFO_MAX_BYTES,
    LocalGeoServerRenderer,
    LocalGeoServerResponseError,
    LocalGeoServerUnavailableError,
    InvalidLocalGeoServerRequestError,
    UnsafeLocalGeoServerConfigurationError,
    validate_local_geoserver_base_url,
)


def make_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", checksum)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "image/png",
        content_encoding: str | None = None,
        content_length: str | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": content_length or str(len(body)),
        }
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding
        self.read_sizes: list[int] = []

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body[:size]


class FakeConnection:
    def __init__(
        self,
        response: FakeResponse,
        *,
        request_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.request_error = request_error
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, target, headers))
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class RecordingFactory:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, host: str, port: int, timeout: float) -> FakeConnection:
        self.calls.append((host, port, timeout))
        return self.connection


def renderer_with_response(
    response: FakeResponse,
    *,
    request_error: Exception | None = None,
) -> tuple[LocalGeoServerRenderer, FakeConnection, RecordingFactory]:
    connection = FakeConnection(response, request_error=request_error)
    factory = RecordingFactory(connection)
    renderer = LocalGeoServerRenderer(
        base_url="http://127.0.0.1:8081/geoserver",
        workspace="siur",
        timeout_seconds=3.5,
        connection_factory=factory,
    )
    return renderer, connection, factory


def test_settings_accept_and_canonicalize_local_geoserver_configuration() -> None:
    configured = Settings(
        _env_file=None,
        local_geoserver_base_url=" http://127.0.0.1:8081/geoserver/ ",
        local_geoserver_workspace=" siur_local ",
        local_geoserver_timeout_seconds=2.5,
    )

    assert configured.local_geoserver_base_url == (
        "http://127.0.0.1:8081/geoserver"
    )
    assert configured.local_geoserver_workspace == "siur_local"
    assert configured.local_geoserver_timeout_seconds == 2.5


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8081/geoserver",
        "http://localhost:8081/geoserver",
        "http://127.0.0.1.evil.example:8081/geoserver",
        "http://2130706433:8081/geoserver",
        "http://user:password@127.0.0.1:8081/geoserver",
        "http://127.0.0.1:8081/geoserver/rest",
        "http://127.0.0.1:8081/geoserver?url=http://169.254.169.254",
        "http://127.0.0.1:8081/geoserver#fragment",
        "http://127.0.0.1/geoserver",
        "http://127.0.0.1:0/geoserver",
    ],
)
def test_configuration_rejects_every_noncanonical_or_nonloopback_url(
    base_url: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            local_geoserver_base_url=base_url,
        )
    with pytest.raises(UnsafeLocalGeoServerConfigurationError):
        validate_local_geoserver_base_url(base_url)


@pytest.mark.parametrize("timeout", [0, 0.09, 30.1, float("inf"), float("nan")])
def test_configuration_rejects_invalid_renderer_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            local_geoserver_timeout_seconds=timeout,
        )
    with pytest.raises(UnsafeLocalGeoServerConfigurationError):
        LocalGeoServerRenderer(timeout_seconds=timeout)


@pytest.mark.parametrize(
    "workspace",
    ["", "../siur", "siur/path", "other:siur", "siur\nother", "x" * 65],
)
def test_configuration_rejects_workspace_path_or_namespace_injection(
    workspace: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, local_geoserver_workspace=workspace)
    with pytest.raises(UnsafeLocalGeoServerConfigurationError):
        LocalGeoServerRenderer(workspace=workspace)


def test_tile_request_is_workspace_scoped_and_uses_fixed_wms_parameters() -> None:
    renderer = LocalGeoServerRenderer(
        base_url="http://127.0.0.1:8081/geoserver",
        workspace="siur",
    )

    request = renderer.build_tile_request(
        layer_name="planning_v_012345",
        style_name="planning_color_v_012345",
        z=0,
        x=0,
        y=0,
    )
    target = urlsplit(request.target)
    parameters = parse_qs(target.query, keep_blank_values=True)

    assert target.path == "/geoserver/siur/wms"
    assert parameters == {
        "SERVICE": ["WMS"],
        "VERSION": ["1.3.0"],
        "REQUEST": ["GetMap"],
        "LAYERS": ["siur:planning_v_012345"],
        "STYLES": ["siur:planning_color_v_012345"],
        "FORMAT": ["image/png"],
        "TRANSPARENT": ["TRUE"],
        "WIDTH": ["256"],
        "HEIGHT": ["256"],
        "BBOX": [
            "-20037508.34278924,-20037508.34278924,"
            "20037508.34278924,20037508.34278924"
        ],
        "CRS": ["EPSG:3857"],
        "TILED": ["true"],
    }


def test_legend_and_feature_info_have_closed_operation_specific_parameters() -> None:
    renderer = LocalGeoServerRenderer()

    legend = renderer.build_legend_request(
        layer_name="planning_v1",
        style_name=None,
    )
    legend_parameters = parse_qs(
        urlsplit(legend.target).query,
        keep_blank_values=True,
    )
    assert legend_parameters["REQUEST"] == ["GetLegendGraphic"]
    assert legend_parameters["LAYER"] == ["siur:planning_v1"]
    assert legend_parameters["STYLE"] == [""]
    assert "BBOX" not in legend_parameters

    identify = renderer.build_feature_info_request(
        layer_name="planning_v1",
        style_name="planning_color_v1",
        z=8,
        x=125,
        y=94,
        pixel_x=23,
        pixel_y=42,
        feature_count=5,
    )
    identify_parameters = parse_qs(
        urlsplit(identify.target).query,
        keep_blank_values=True,
    )
    assert identify_parameters["REQUEST"] == ["GetFeatureInfo"]
    assert identify_parameters["QUERY_LAYERS"] == ["siur:planning_v1"]
    assert identify_parameters["INFO_FORMAT"] == ["application/json"]
    assert identify_parameters["FEATURE_COUNT"] == ["5"]
    assert identify_parameters["I"] == ["23"]
    assert identify_parameters["J"] == ["42"]
    assert "X" not in identify_parameters
    assert "Y" not in identify_parameters


@pytest.mark.parametrize(
    "resource_name",
    [
        "",
        "other:layer",
        "../layer",
        "layer/name",
        "https://example.com/layer",
        "layer&REQUEST=GetCapabilities",
        "layer\r\nHost:169.254.169.254",
        "x" * 256,
    ],
)
def test_resource_names_cannot_escape_the_configured_workspace(
    resource_name: str,
) -> None:
    renderer = LocalGeoServerRenderer()
    with pytest.raises(InvalidLocalGeoServerRequestError):
        renderer.build_tile_request(
            layer_name=resource_name,
            style_name=None,
            z=0,
            x=0,
            y=0,
        )
    with pytest.raises(InvalidLocalGeoServerRequestError):
        renderer.build_tile_request(
            layer_name="safe_layer",
            style_name=resource_name,
            z=0,
            x=0,
            y=0,
        )


@pytest.mark.parametrize(
    ("pixel_x", "pixel_y", "feature_count"),
    [(-1, 0, 1), (0, 256, 1), (True, 0, 1), (0, 0, 0), (0, 0, 11)],
)
def test_feature_info_rejects_invalid_pixels_and_limits(
    pixel_x: int,
    pixel_y: int,
    feature_count: int,
) -> None:
    renderer = LocalGeoServerRenderer()
    with pytest.raises(InvalidLocalGeoServerRequestError):
        renderer.build_feature_info_request(
            layer_name="planning",
            style_name=None,
            z=0,
            x=0,
            y=0,
            pixel_x=pixel_x,
            pixel_y=pixel_y,
            feature_count=feature_count,
        )


@pytest.mark.parametrize(
    ("z", "x", "y"),
    [(-1, 0, 0), (25, 0, 0), (1, 2, 0), (1, 0, 2), (True, 0, 0)],
)
def test_tile_request_rejects_invalid_coordinates(z: int, x: int, y: int) -> None:
    renderer = LocalGeoServerRenderer()
    with pytest.raises(InvalidLocalGeoServerRequestError):
        renderer.build_tile_request(
            layer_name="planning",
            style_name=None,
            z=z,
            x=x,
            y=y,
        )


def test_tile_fetch_connects_only_to_numeric_loopback_and_validates_png() -> None:
    body = make_png(256, 256)
    renderer, connection, factory = renderer_with_response(FakeResponse(body))

    rendered = renderer.render_tile(
        layer_name="planning_v1",
        style_name="planning_color_v1",
        z=4,
        x=7,
        y=5,
    )

    assert factory.calls == [("127.0.0.1", 8081, 3.5)]
    assert connection.closed is True
    assert connection.requests[0][0] == "GET"
    assert connection.requests[0][1].startswith("/geoserver/siur/wms?")
    assert connection.requests[0][2]["Accept-Encoding"] == "identity"
    assert rendered.body == body
    assert rendered.content_type == "image/png"
    assert rendered.etag == f'"{hashlib.sha256(body).hexdigest()}"'


def test_valid_feature_info_is_bounded_and_returned_as_json() -> None:
    body = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"class": "urban"},
                    "geometry": None,
                }
            ],
        }
    ).encode()
    renderer, connection, _ = renderer_with_response(
        FakeResponse(body, content_type="application/geo+json; charset=UTF-8")
    )

    rendered = renderer.get_feature_info(
        layer_name="planning_v1",
        style_name=None,
        z=4,
        x=7,
        y=5,
        pixel_x=10,
        pixel_y=20,
        feature_count=1,
    )

    assert rendered.body == body
    assert rendered.content_type == "application/geo+json"
    assert connection.closed is True


def test_valid_legend_uses_the_same_bounded_local_transport() -> None:
    body = make_png(20, 40)
    renderer, connection, factory = renderer_with_response(FakeResponse(body))

    rendered = renderer.render_legend(
        layer_name="planning_v1",
        style_name="planning_color_v1",
    )

    assert rendered.body == body
    assert factory.calls == [("127.0.0.1", 8081, 3.5)]
    parameters = parse_qs(
        urlsplit(connection.requests[0][1]).query,
        keep_blank_values=True,
    )
    assert parameters["REQUEST"] == ["GetLegendGraphic"]
    assert connection.closed is True


def test_http_errors_and_network_failures_are_unavailable_without_redirects() -> None:
    redirect_renderer, redirect_connection, redirect_factory = renderer_with_response(
        FakeResponse(b"redirect", status=302, content_type="text/plain")
    )
    with pytest.raises(LocalGeoServerUnavailableError):
        redirect_renderer.render_tile(
            layer_name="planning",
            style_name=None,
            z=0,
            x=0,
            y=0,
        )
    assert len(redirect_factory.calls) == 1
    assert len(redirect_connection.requests) == 1
    assert redirect_connection.closed is True

    failed_renderer, failed_connection, failed_factory = renderer_with_response(
        FakeResponse(make_png(256, 256)),
        request_error=ConnectionRefusedError(),
    )
    with pytest.raises(LocalGeoServerUnavailableError):
        failed_renderer.render_tile(
            layer_name="planning",
            style_name=None,
            z=0,
            x=0,
            y=0,
        )
    assert failed_factory.calls == [("127.0.0.1", 8081, 3.5)]
    assert failed_connection.closed is True


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(b"<html>not a PNG</html>", content_type="text/html"),
        FakeResponse(make_png(512, 256)),
        FakeResponse(
            make_png(256, 256),
            content_encoding="gzip",
        ),
        FakeResponse(
            make_png(256, 256),
            content_length=str(1024 * 1024 + 1),
        ),
    ],
)
def test_tile_fetch_rejects_malformed_or_oversized_responses(
    response: FakeResponse,
) -> None:
    renderer, connection, _ = renderer_with_response(response)
    with pytest.raises(LocalGeoServerResponseError):
        renderer.render_tile(
            layer_name="planning",
            style_name=None,
            z=0,
            x=0,
            y=0,
        )
    assert connection.closed is True


def test_feature_info_rejects_invalid_or_excessive_feature_collections() -> None:
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": None},
                {"type": "Feature", "properties": {}, "geometry": None},
            ],
        }
    ).encode()
    renderer, connection, _ = renderer_with_response(
        FakeResponse(payload, content_type="application/json")
    )

    with pytest.raises(LocalGeoServerResponseError):
        renderer.get_feature_info(
            layer_name="planning",
            style_name=None,
            z=0,
            x=0,
            y=0,
            pixel_x=0,
            pixel_y=0,
            feature_count=1,
        )
    assert connection.closed is True


def test_body_read_is_capped_even_without_a_content_length() -> None:
    response = FakeResponse(
        b"{" + b"x" * FEATURE_INFO_MAX_BYTES,
        content_type="application/json",
    )
    response.headers.pop("Content-Length")
    renderer, connection, _ = renderer_with_response(response)

    with pytest.raises(LocalGeoServerResponseError):
        renderer.get_feature_info(
            layer_name="planning",
            style_name=None,
            z=0,
            x=0,
            y=0,
            pixel_x=0,
            pixel_y=0,
            feature_count=1,
        )
    assert response.read_sizes == [FEATURE_INFO_MAX_BYTES + 1]
    assert connection.closed is True
