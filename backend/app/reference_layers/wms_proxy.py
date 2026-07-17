from __future__ import annotations

import hashlib
import http.client
import ipaddress
import multiprocessing
import re
import socket
import ssl
import struct
import threading
import zlib
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from urllib.parse import SplitResult, urlencode, urlsplit

from app.core.config import settings

SIUR_WMS_HOST = "idecyl.jcyl.es"
SIUR_WMS_PATH = re.compile(
    r"^/geoserver/[A-Za-z0-9_.-]+/(?:ows|wms)/?$"
)
WMS_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,500}$")
SUPPORTED_WMS_VERSIONS = frozenset({"1.1.1", "1.3.0"})
WEB_MERCATOR_HALF_WORLD = 20_037_508.342789244
TILE_SIZE = 256
WMS_TIMEOUT_SECONDS = 8.0
WMS_DNS_TIMEOUT_SECONDS = 2.0
WMS_MAX_CONCURRENT_MISSES = 8
WMS_TILE_MAX_BYTES = 1024 * 1024
WMS_LEGEND_MAX_BYTES = 1024 * 1024
WMS_IDENTIFY_MAX_BYTES = 2 * 1024 * 1024
PNG_CONTENT_TYPES = frozenset({"image/png"})
JSON_CONTENT_TYPES = frozenset({"application/geo+json", "application/json"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CONTENT_LENGTH_DIGITS = 20
CONTENT_LENGTH = re.compile(rf"[0-9]{{1,{MAX_CONTENT_LENGTH_DIGITS}}}", re.ASCII)
NON_PUBLIC_IPV6_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)

_WMS_MISS_ADMISSION = threading.BoundedSemaphore(WMS_MAX_CONCURRENT_MISSES)


class UnsafeWMSEndpointError(ValueError):
    pass


class WMSUpstreamUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class WMSRequest:
    endpoint_url: str
    target: str
    operation: str
    max_response_bytes: int
    allowed_content_types: frozenset[str]


@dataclass(frozen=True)
class WMSResponse:
    body: bytes
    content_type: str
    etag: str


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        address: str,
        *,
        timeout: float,
    ) -> None:
        super().__init__(
            host,
            port=443,
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


def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    if (
        isinstance(z, bool)
        or isinstance(x, bool)
        or isinstance(y, bool)
        or not all(isinstance(value, int) for value in (z, x, y))
        or not 0 <= z <= 24
        or not 0 <= x < 2**z
        or not 0 <= y < 2**z
    ):
        raise ValueError("invalid tile coordinates")
    span = (2 * WEB_MERCATOR_HALF_WORLD) / (2**z)
    min_x = -WEB_MERCATOR_HALF_WORLD + x * span
    max_x = min_x + span
    max_y = WEB_MERCATOR_HALF_WORLD - y * span
    min_y = max_y - span
    return min_x, min_y, max_x, max_y


def tile_lonlat_bounds(
    z: int,
    x: int,
    y: int,
) -> tuple[float, float, float, float]:
    from math import atan, degrees, pi, sinh

    tile_bbox(z, x, y)
    count = 2**z
    west = x / count * 360.0 - 180.0
    east = (x + 1) / count * 360.0 - 180.0
    north = degrees(atan(sinh(pi * (1.0 - 2.0 * y / count))))
    south = degrees(atan(sinh(pi * (1.0 - 2.0 * (y + 1) / count))))
    return west, south, east, north


def build_tile_request(
    *,
    endpoint_url: str,
    version: str,
    remote_name: str,
    style_name: str,
    z: int,
    x: int,
    y: int,
) -> WMSRequest:
    bbox = tile_bbox(z, x, y)
    parameters = _map_parameters(
        version=version,
        remote_name=remote_name,
        style_name=style_name,
        bbox=bbox,
    )
    return _build_request(
        endpoint_url=endpoint_url,
        parameters=parameters,
        operation="tile",
        max_response_bytes=WMS_TILE_MAX_BYTES,
        allowed_content_types=PNG_CONTENT_TYPES,
    )


def build_legend_request(
    *,
    endpoint_url: str,
    version: str,
    remote_name: str,
    style_name: str,
) -> WMSRequest:
    _require_version(version)
    _require_token(remote_name, "layer")
    if style_name:
        _require_token(style_name, "style")
    parameters = [
        ("SERVICE", "WMS"),
        ("VERSION", version),
        ("REQUEST", "GetLegendGraphic"),
        ("FORMAT", "image/png"),
        ("LAYER", remote_name),
        ("STYLE", style_name),
        ("WIDTH", "20"),
        ("HEIGHT", "20"),
    ]
    return _build_request(
        endpoint_url=endpoint_url,
        parameters=parameters,
        operation="legend",
        max_response_bytes=WMS_LEGEND_MAX_BYTES,
        allowed_content_types=PNG_CONTENT_TYPES,
    )


def build_identify_request(
    *,
    endpoint_url: str,
    version: str,
    remote_name: str,
    style_name: str,
    z: int,
    x: int,
    y: int,
    pixel_x: int,
    pixel_y: int,
    feature_count: int,
) -> WMSRequest:
    if not 0 <= pixel_x < TILE_SIZE or not 0 <= pixel_y < TILE_SIZE:
        raise ValueError("invalid identify pixel")
    if not 1 <= feature_count <= 10:
        raise ValueError("invalid feature count")
    parameters = _map_parameters(
        version=version,
        remote_name=remote_name,
        style_name=style_name,
        bbox=tile_bbox(z, x, y),
    )
    parameters[2] = ("REQUEST", "GetFeatureInfo")
    parameters.extend(
        [
            ("QUERY_LAYERS", remote_name),
            ("INFO_FORMAT", "application/json"),
            ("FEATURE_COUNT", str(feature_count)),
            ("I" if version == "1.3.0" else "X", str(pixel_x)),
            ("J" if version == "1.3.0" else "Y", str(pixel_y)),
        ]
    )
    return _build_request(
        endpoint_url=endpoint_url,
        parameters=parameters,
        operation="identify",
        max_response_bytes=WMS_IDENTIFY_MAX_BYTES,
        allowed_content_types=JSON_CONTENT_TYPES,
    )


def fetch_wms_response(request: WMSRequest) -> WMSResponse:
    endpoint = validate_siur_wms_endpoint(request.endpoint_url)
    if request.operation not in {"tile", "legend", "identify"}:
        raise WMSUpstreamUnavailableError("unsupported WMS operation")
    if not request.target.startswith(endpoint.path + "?"):
        raise WMSUpstreamUnavailableError("invalid WMS request target")
    if len(request.target) > 4096:
        raise WMSUpstreamUnavailableError("WMS request is too large")
    if not _WMS_MISS_ADMISSION.acquire(blocking=False):
        raise WMSUpstreamUnavailableError("WMS capacity is busy")
    try:
        return _fetch_wms_response(request, endpoint)
    finally:
        _WMS_MISS_ADMISSION.release()


def validate_siur_wms_endpoint(value: str) -> SplitResult:
    if not isinstance(value, str) or len(value) > 2000:
        raise UnsafeWMSEndpointError("invalid WMS endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise UnsafeWMSEndpointError("invalid WMS endpoint") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != SIUR_WMS_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not SIUR_WMS_PATH.fullmatch(parsed.path)
    ):
        raise UnsafeWMSEndpointError("invalid WMS endpoint")
    return parsed


def _map_parameters(
    *,
    version: str,
    remote_name: str,
    style_name: str,
    bbox: tuple[float, float, float, float],
) -> list[tuple[str, str]]:
    _require_version(version)
    _require_token(remote_name, "layer")
    if style_name:
        _require_token(style_name, "style")
    if (
        not all(isfinite(value) for value in bbox)
        or bbox[0] >= bbox[2]
        or bbox[1] >= bbox[3]
    ):
        raise ValueError("invalid map bounds")
    crs_parameter = "CRS" if version == "1.3.0" else "SRS"
    return [
        ("SERVICE", "WMS"),
        ("VERSION", version),
        ("REQUEST", "GetMap"),
        ("LAYERS", remote_name),
        ("STYLES", style_name),
        ("FORMAT", "image/png"),
        ("TRANSPARENT", "TRUE"),
        ("WIDTH", str(TILE_SIZE)),
        ("HEIGHT", str(TILE_SIZE)),
        ("BBOX", ",".join(_format_coordinate(value) for value in bbox)),
        (crs_parameter, "EPSG:3857"),
    ]


def _build_request(
    *,
    endpoint_url: str,
    parameters: list[tuple[str, str]],
    operation: str,
    max_response_bytes: int,
    allowed_content_types: frozenset[str],
) -> WMSRequest:
    endpoint = validate_siur_wms_endpoint(endpoint_url)
    target = f"{endpoint.path}?{urlencode(parameters)}"
    return WMSRequest(
        endpoint_url=endpoint_url,
        target=target,
        operation=operation,
        max_response_bytes=max_response_bytes,
        allowed_content_types=allowed_content_types,
    )


def _require_version(value: str) -> None:
    if value not in SUPPORTED_WMS_VERSIONS:
        raise ValueError("unsupported WMS version")


def _require_token(value: str, label: str) -> None:
    if not isinstance(value, str) or not WMS_TOKEN.fullmatch(value):
        raise ValueError(f"invalid WMS {label}")


def _format_coordinate(value: float) -> str:
    rendered = f"{value:.8f}".rstrip("0").rstrip(".")
    return "0" if rendered == "-0" else rendered


def _fetch_wms_response(
    request: WMSRequest,
    endpoint: SplitResult,
) -> WMSResponse:
    deadline = monotonic() + WMS_TIMEOUT_SECONDS
    try:
        addresses = _resolve_public_addresses(
            endpoint.hostname or "",
            timeout=min(WMS_DNS_TIMEOUT_SECONDS, WMS_TIMEOUT_SECONDS),
        )
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection = _PinnedHTTPSConnection(
            endpoint.hostname or "",
            addresses[0],
            timeout=remaining,
        )
        try:
            connection.request(
                "GET",
                request.target,
                headers={
                    "Accept": ",".join(sorted(request.allowed_content_types)),
                    "Accept-Encoding": "identity",
                    "User-Agent": f"AsistenteAyuntamientos/{settings.app_version}",
                },
            )
            _set_socket_timeout(connection, deadline)
            response = connection.getresponse()
            if int(response.status) != 200:
                raise WMSUpstreamUnavailableError("WMS request failed")
            content_encoding = response.getheader("Content-Encoding") or "identity"
            if content_encoding.strip().casefold() != "identity":
                raise WMSUpstreamUnavailableError("unsupported WMS encoding")
            content_type = _content_type(response.getheader("Content-Type"))
            if content_type not in request.allowed_content_types:
                raise WMSUpstreamUnavailableError("unexpected WMS content type")
            content_length = _content_length(response.getheader("Content-Length"))
            if (
                content_length is not None
                and content_length > request.max_response_bytes
            ):
                raise WMSUpstreamUnavailableError("WMS response is too large")
            body = _read_bounded_body(
                response,
                connection=connection,
                deadline=deadline,
                max_bytes=request.max_response_bytes,
            )
        finally:
            connection.close()
    except UnsafeWMSEndpointError:
        raise
    except WMSUpstreamUnavailableError:
        raise
    except (
        OSError,
        TimeoutError,
        http.client.HTTPException,
        socket.timeout,
        ssl.SSLError,
    ) as error:
        raise WMSUpstreamUnavailableError("WMS is unavailable") from error
    _validate_response_body(
        body,
        operation=request.operation,
        content_type=content_type,
    )
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    return WMSResponse(body=body, content_type=content_type, etag=etag)


def _resolve_public_addresses(host: str, *, timeout: float) -> tuple[str, ...]:
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_dns_worker,
        args=(send, host),
        daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        send.close()
        if not receive.poll(timeout):
            raise TimeoutError("WMS DNS timeout")
        try:
            message = receive.recv()
        except (EOFError, OSError) as error:
            raise WMSUpstreamUnavailableError("WMS DNS failed") from error
        if (
            not isinstance(message, tuple)
            or len(message) != 2
            or message[0] != "ok"
            or not isinstance(message[1], list)
        ):
            raise WMSUpstreamUnavailableError("WMS DNS failed")
        return _require_public_addresses(message[1])
    except (UnsafeWMSEndpointError, WMSUpstreamUnavailableError, TimeoutError):
        raise
    except (AssertionError, OSError, RuntimeError, ValueError) as error:
        raise WMSUpstreamUnavailableError("WMS DNS failed") from error
    finally:
        try:
            receive.close()
        except OSError:
            pass
        try:
            send.close()
        except OSError:
            pass
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


def _dns_worker(send, host: str) -> None:
    try:
        results = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        addresses = list(dict.fromkeys(item[4][0] for item in results))
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
            raise WMSUpstreamUnavailableError("WMS DNS failed")
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise WMSUpstreamUnavailableError("WMS DNS failed") from error
        if not _is_public_unicast_address(address):
            raise UnsafeWMSEndpointError("WMS resolved to a non-public address")
        addresses.append(address.compressed)
    if not addresses:
        raise WMSUpstreamUnavailableError("WMS DNS returned no addresses")
    return tuple(dict.fromkeys(addresses))


def _is_public_unicast_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        or getattr(address, "is_site_local", False)
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if (
            address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
            or any(address in network for network in NON_PUBLIC_IPV6_NETWORKS)
        ):
            return False
    return True


def _set_socket_timeout(
    connection: _PinnedHTTPSConnection,
    deadline: float,
) -> None:
    remaining = deadline - monotonic()
    if remaining <= 0 or connection.sock is None:
        raise TimeoutError
    connection.sock.settimeout(remaining)


def _content_type(value: str | None) -> str:
    if not isinstance(value, str):
        raise WMSUpstreamUnavailableError("missing WMS content type")
    return value.partition(";")[0].strip().casefold()


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_CONTENT_LENGTH_DIGITS:
        raise WMSUpstreamUnavailableError("invalid WMS content length")
    normalized = value.strip()
    if CONTENT_LENGTH.fullmatch(normalized) is None:
        raise WMSUpstreamUnavailableError("invalid WMS content length")
    return int(normalized)


def _read_bounded_body(
    response: http.client.HTTPResponse,
    *,
    connection: _PinnedHTTPSConnection,
    deadline: float,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if deadline - monotonic() <= 0:
            raise TimeoutError
        _set_socket_timeout(connection, deadline)
        chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise WMSUpstreamUnavailableError("WMS response is too large")
    if total == 0:
        raise WMSUpstreamUnavailableError("empty WMS response")
    return b"".join(chunks)


def _validate_response_body(
    body: bytes,
    *,
    operation: str,
    content_type: str,
) -> None:
    if operation in {"tile", "legend"}:
        if content_type != "image/png" or not body.startswith(PNG_SIGNATURE):
            raise WMSUpstreamUnavailableError("invalid WMS image")
        width, height = _png_dimensions(body)
        if operation == "tile" and (width, height) != (TILE_SIZE, TILE_SIZE):
            raise WMSUpstreamUnavailableError("invalid WMS tile dimensions")
        if operation == "legend" and (
            width > 2048 or height > 8192 or width * height > 4_000_000
        ):
            raise WMSUpstreamUnavailableError("invalid WMS legend dimensions")
    elif operation == "identify":
        if (
            content_type not in JSON_CONTENT_TYPES
            or body.lstrip()[:1] not in {b"{", b"["}
        ):
            raise WMSUpstreamUnavailableError("invalid WMS feature information")
    else:
        raise WMSUpstreamUnavailableError("unsupported WMS operation")


def _png_dimensions(body: bytes) -> tuple[int, int]:
    if len(body) < 33 or body[8:12] != b"\x00\x00\x00\r" or body[12:16] != b"IHDR":
        raise WMSUpstreamUnavailableError("invalid WMS PNG header")
    ihdr = body[12:29]
    expected_crc = struct.unpack(">I", body[29:33])[0]
    if zlib.crc32(ihdr) & 0xFFFFFFFF != expected_crc:
        raise WMSUpstreamUnavailableError("invalid WMS PNG header")
    width, height = struct.unpack(">II", body[16:24])
    if width == 0 or height == 0:
        raise WMSUpstreamUnavailableError("invalid WMS PNG dimensions")
    return width, height
