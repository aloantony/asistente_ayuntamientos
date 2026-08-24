from __future__ import annotations

import hashlib
import http.client
import re
import socket
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Literal
from urllib.parse import SplitResult, urlencode, urlsplit

from app.core.config import settings
from app.reference_layers.wms_proxy import TILE_SIZE, tile_bbox
from app.reference_layers.wms_schemas import (
    InvalidFeatureInfoError,
    parse_feature_collection,
)

LOCAL_GEOSERVER_VERSION = "1.3.0"
LOCAL_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$", re.ASCII)
PNG_CONTENT_TYPE = "image/png"
JSON_CONTENT_TYPES = frozenset({"application/geo+json", "application/json"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TILE_MAX_BYTES = 1024 * 1024
LEGEND_MAX_BYTES = 1024 * 1024
FEATURE_INFO_MAX_BYTES = 2 * 1024 * 1024
MAX_CONTENT_LENGTH_DIGITS = 20


class UnsafeLocalGeoServerConfigurationError(ValueError):
    """The configured renderer endpoint is not the dedicated loopback service."""


class InvalidLocalGeoServerRequestError(ValueError):
    """A caller supplied an invalid local layer, style or map coordinate."""


class LocalGeoServerError(Exception):
    """Base class for failures talking to the local renderer."""


class LocalGeoServerUnavailableError(LocalGeoServerError):
    """The local renderer could not complete a valid request."""


class LocalGeoServerResponseError(LocalGeoServerError):
    """The local renderer returned an unsafe or malformed response."""


@dataclass(frozen=True)
class LocalGeoServerRequest:
    target: str
    operation: Literal["tile", "legend", "identify"]
    max_response_bytes: int
    allowed_content_types: frozenset[str]
    feature_count: int | None = None


@dataclass(frozen=True)
class LocalGeoServerResponse:
    body: bytes
    content_type: str
    etag: str


ConnectionFactory = Callable[[str, int, float], http.client.HTTPConnection]


class LocalGeoServerRenderer:
    """Closed client for a GeoServer instance published only on loopback.

    The base URL, workspace, layer and style names are all validated and the
    request path is constructed internally. No caller can provide a host,
    arbitrary path or raw query string.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        workspace: str | None = None,
        timeout_seconds: float | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._endpoint = validate_local_geoserver_base_url(
            settings.local_geoserver_base_url if base_url is None else base_url
        )
        self._workspace = _validate_workspace(
            settings.local_geoserver_workspace if workspace is None else workspace
        )
        timeout = (
            settings.local_geoserver_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise UnsafeLocalGeoServerConfigurationError(
                "invalid local GeoServer timeout"
            )
        self._timeout_seconds = float(timeout)
        if (
            not isfinite(self._timeout_seconds)
            or not 0.1 <= self._timeout_seconds <= 30.0
        ):
            raise UnsafeLocalGeoServerConfigurationError(
                "invalid local GeoServer timeout"
            )
        self._connection_factory = connection_factory or _http_connection
        self._wms_path = (
            f"{self._endpoint.path}/{self._workspace}/wms"
        )

    @property
    def endpoint_url(self) -> str:
        return (
            f"http://127.0.0.1:{self._endpoint.port}"
            f"{self._endpoint.path}"
        )

    def build_tile_request(
        self,
        *,
        layer_name: str,
        style_name: str | None,
        z: int,
        x: int,
        y: int,
    ) -> LocalGeoServerRequest:
        parameters = self._map_parameters(
            layer_name=layer_name,
            style_name=style_name,
            bbox=self._tile_bounds(z, x, y),
        )
        parameters.append(("TILED", "true"))
        return self._build_request(
            parameters=parameters,
            operation="tile",
            max_response_bytes=TILE_MAX_BYTES,
            allowed_content_types=frozenset({PNG_CONTENT_TYPE}),
        )

    def render_tile(
        self,
        *,
        layer_name: str,
        style_name: str | None,
        z: int,
        x: int,
        y: int,
    ) -> LocalGeoServerResponse:
        return self._execute(
            self.build_tile_request(
                layer_name=layer_name,
                style_name=style_name,
                z=z,
                x=x,
                y=y,
            )
        )

    def build_legend_request(
        self,
        *,
        layer_name: str,
        style_name: str | None,
    ) -> LocalGeoServerRequest:
        qualified_layer = self._qualified_resource(layer_name, "layer")
        qualified_style = self._qualified_optional_style(style_name)
        return self._build_request(
            parameters=[
                ("SERVICE", "WMS"),
                ("VERSION", LOCAL_GEOSERVER_VERSION),
                ("REQUEST", "GetLegendGraphic"),
                ("FORMAT", PNG_CONTENT_TYPE),
                ("LAYER", qualified_layer),
                ("STYLE", qualified_style),
                ("WIDTH", "20"),
                ("HEIGHT", "20"),
            ],
            operation="legend",
            max_response_bytes=LEGEND_MAX_BYTES,
            allowed_content_types=frozenset({PNG_CONTENT_TYPE}),
        )

    def render_legend(
        self,
        *,
        layer_name: str,
        style_name: str | None,
    ) -> LocalGeoServerResponse:
        return self._execute(
            self.build_legend_request(
                layer_name=layer_name,
                style_name=style_name,
            )
        )

    def build_feature_info_request(
        self,
        *,
        layer_name: str,
        style_name: str | None,
        z: int,
        x: int,
        y: int,
        pixel_x: int,
        pixel_y: int,
        feature_count: int,
    ) -> LocalGeoServerRequest:
        if (
            isinstance(pixel_x, bool)
            or isinstance(pixel_y, bool)
            or not isinstance(pixel_x, int)
            or not isinstance(pixel_y, int)
            or not 0 <= pixel_x < TILE_SIZE
            or not 0 <= pixel_y < TILE_SIZE
        ):
            raise InvalidLocalGeoServerRequestError(
                "invalid local GeoServer identify pixel"
            )
        if (
            isinstance(feature_count, bool)
            or not isinstance(feature_count, int)
            or not 1 <= feature_count <= 10
        ):
            raise InvalidLocalGeoServerRequestError(
                "invalid local GeoServer feature count"
            )
        qualified_layer = self._qualified_resource(layer_name, "layer")
        parameters = self._map_parameters(
            layer_name=layer_name,
            style_name=style_name,
            bbox=self._tile_bounds(z, x, y),
        )
        parameters[2] = ("REQUEST", "GetFeatureInfo")
        parameters.extend(
            [
                ("QUERY_LAYERS", qualified_layer),
                ("INFO_FORMAT", "application/json"),
                ("FEATURE_COUNT", str(feature_count)),
                ("I", str(pixel_x)),
                ("J", str(pixel_y)),
            ]
        )
        return self._build_request(
            parameters=parameters,
            operation="identify",
            max_response_bytes=FEATURE_INFO_MAX_BYTES,
            allowed_content_types=JSON_CONTENT_TYPES,
            feature_count=feature_count,
        )

    def get_feature_info(
        self,
        *,
        layer_name: str,
        style_name: str | None,
        z: int,
        x: int,
        y: int,
        pixel_x: int,
        pixel_y: int,
        feature_count: int,
    ) -> LocalGeoServerResponse:
        return self._execute(
            self.build_feature_info_request(
                layer_name=layer_name,
                style_name=style_name,
                z=z,
                x=x,
                y=y,
                pixel_x=pixel_x,
                pixel_y=pixel_y,
                feature_count=feature_count,
            )
        )

    def _map_parameters(
        self,
        *,
        layer_name: str,
        style_name: str | None,
        bbox: tuple[float, float, float, float],
    ) -> list[tuple[str, str]]:
        if (
            not all(isfinite(value) for value in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            raise InvalidLocalGeoServerRequestError(
                "invalid local GeoServer map bounds"
            )
        return [
            ("SERVICE", "WMS"),
            ("VERSION", LOCAL_GEOSERVER_VERSION),
            ("REQUEST", "GetMap"),
            ("LAYERS", self._qualified_resource(layer_name, "layer")),
            ("STYLES", self._qualified_optional_style(style_name)),
            ("FORMAT", PNG_CONTENT_TYPE),
            ("TRANSPARENT", "TRUE"),
            ("WIDTH", str(TILE_SIZE)),
            ("HEIGHT", str(TILE_SIZE)),
            ("BBOX", ",".join(_format_coordinate(value) for value in bbox)),
            ("CRS", "EPSG:3857"),
        ]

    @staticmethod
    def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
        try:
            return tile_bbox(z, x, y)
        except ValueError as error:
            raise InvalidLocalGeoServerRequestError(
                "invalid local GeoServer tile coordinates"
            ) from error

    def _qualified_resource(self, value: str, label: str) -> str:
        if not isinstance(value, str) or LOCAL_RESOURCE_NAME.fullmatch(value) is None:
            raise InvalidLocalGeoServerRequestError(
                f"invalid local GeoServer {label}"
            )
        return f"{self._workspace}:{value}"

    def _qualified_optional_style(self, value: str | None) -> str:
        if value is None:
            return ""
        return self._qualified_resource(value, "style")

    def _build_request(
        self,
        *,
        parameters: list[tuple[str, str]],
        operation: Literal["tile", "legend", "identify"],
        max_response_bytes: int,
        allowed_content_types: frozenset[str],
        feature_count: int | None = None,
    ) -> LocalGeoServerRequest:
        target = f"{self._wms_path}?{urlencode(parameters)}"
        if len(target) > 4096:
            raise InvalidLocalGeoServerRequestError(
                "local GeoServer request is too large"
            )
        return LocalGeoServerRequest(
            target=target,
            operation=operation,
            max_response_bytes=max_response_bytes,
            allowed_content_types=allowed_content_types,
            feature_count=feature_count,
        )

    def _execute(self, request: LocalGeoServerRequest) -> LocalGeoServerResponse:
        connection: http.client.HTTPConnection | None = None
        try:
            connection = self._connection_factory(
                "127.0.0.1",
                self._endpoint.port or 0,
                self._timeout_seconds,
            )
            connection.request(
                "GET",
                request.target,
                headers={
                    "Accept": ",".join(sorted(request.allowed_content_types)),
                    "Accept-Encoding": "identity",
                    "User-Agent": f"AsistenteAyuntamientos/{settings.app_version}",
                },
            )
            response = connection.getresponse()
            if int(response.status) != 200:
                raise LocalGeoServerUnavailableError(
                    "local GeoServer request failed"
                )
            content_encoding = response.getheader("Content-Encoding") or "identity"
            if content_encoding.strip().casefold() != "identity":
                raise LocalGeoServerResponseError(
                    "unsupported local GeoServer content encoding"
                )
            content_type = _content_type(response.getheader("Content-Type"))
            if content_type not in request.allowed_content_types:
                raise LocalGeoServerResponseError(
                    "unexpected local GeoServer content type"
                )
            content_length = _content_length(response.getheader("Content-Length"))
            if (
                content_length is not None
                and content_length > request.max_response_bytes
            ):
                raise LocalGeoServerResponseError(
                    "local GeoServer response is too large"
                )
            body = response.read(request.max_response_bytes + 1)
            if not body:
                raise LocalGeoServerResponseError(
                    "empty local GeoServer response"
                )
            if len(body) > request.max_response_bytes:
                raise LocalGeoServerResponseError(
                    "local GeoServer response is too large"
                )
        except (LocalGeoServerUnavailableError, LocalGeoServerResponseError):
            raise
        except (
            OSError,
            TimeoutError,
            http.client.HTTPException,
            socket.timeout,
        ) as error:
            raise LocalGeoServerUnavailableError(
                "local GeoServer is unavailable"
            ) from error
        finally:
            if connection is not None:
                connection.close()
        _validate_response_body(
            body,
            operation=request.operation,
            content_type=content_type,
            feature_count=request.feature_count,
        )
        return LocalGeoServerResponse(
            body=body,
            content_type=content_type,
            etag=f'"{hashlib.sha256(body).hexdigest()}"',
        )


def validate_local_geoserver_base_url(value: str) -> SplitResult:
    if not isinstance(value, str) or len(value) > 2000:
        raise UnsafeLocalGeoServerConfigurationError(
            "invalid local GeoServer endpoint"
        )
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as error:
        raise UnsafeLocalGeoServerConfigurationError(
            "invalid local GeoServer endpoint"
        ) from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or parsed.netloc != f"127.0.0.1:{port}"
        or parsed.path != "/geoserver"
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeLocalGeoServerConfigurationError(
            "invalid local GeoServer endpoint"
        )
    return parsed


def _validate_workspace(value: str) -> str:
    if not isinstance(value, str):
        raise UnsafeLocalGeoServerConfigurationError(
            "invalid local GeoServer workspace"
        )
    normalized = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", normalized) is None:
        raise UnsafeLocalGeoServerConfigurationError(
            "invalid local GeoServer workspace"
        )
    return normalized


def _http_connection(
    host: str,
    port: int,
    timeout: float,
) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port=port, timeout=timeout)


def _content_type(value: str | None) -> str:
    if not isinstance(value, str):
        raise LocalGeoServerResponseError(
            "missing local GeoServer content type"
        )
    return value.partition(";")[0].strip().casefold()


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_CONTENT_LENGTH_DIGITS
        or not normalized.isascii()
        or not normalized.isdecimal()
    ):
        raise LocalGeoServerResponseError(
            "invalid local GeoServer content length"
        )
    return int(normalized)


def _validate_response_body(
    body: bytes,
    *,
    operation: str,
    content_type: str,
    feature_count: int | None,
) -> None:
    if operation in {"tile", "legend"}:
        if content_type != PNG_CONTENT_TYPE or not body.startswith(PNG_SIGNATURE):
            raise LocalGeoServerResponseError(
                "invalid local GeoServer image"
            )
        width, height = _png_dimensions(body)
        if operation == "tile" and (width, height) != (TILE_SIZE, TILE_SIZE):
            raise LocalGeoServerResponseError(
                "invalid local GeoServer tile dimensions"
            )
        if operation == "legend" and (
            width > 2048 or height > 8192 or width * height > 4_000_000
        ):
            raise LocalGeoServerResponseError(
                "invalid local GeoServer legend dimensions"
            )
        return
    if operation == "identify" and feature_count is not None:
        try:
            parse_feature_collection(body, max_features=feature_count)
        except InvalidFeatureInfoError as error:
            raise LocalGeoServerResponseError(
                "invalid local GeoServer feature information"
            ) from error
        return
    raise LocalGeoServerResponseError(
        "unsupported local GeoServer operation"
    )


def _png_dimensions(body: bytes) -> tuple[int, int]:
    if (
        len(body) < 33
        or body[8:12] != b"\x00\x00\x00\r"
        or body[12:16] != b"IHDR"
    ):
        raise LocalGeoServerResponseError(
            "invalid local GeoServer PNG header"
        )
    ihdr = body[12:29]
    expected_crc = struct.unpack(">I", body[29:33])[0]
    if zlib.crc32(ihdr) & 0xFFFFFFFF != expected_crc:
        raise LocalGeoServerResponseError(
            "invalid local GeoServer PNG header"
        )
    width, height = struct.unpack(">II", body[16:24])
    if width == 0 or height == 0:
        raise LocalGeoServerResponseError(
            "invalid local GeoServer PNG dimensions"
        )
    return width, height


def _format_coordinate(value: float) -> str:
    rendered = f"{value:.8f}".rstrip("0").rstrip(".")
    return "0" if rendered == "-0" else rendered
