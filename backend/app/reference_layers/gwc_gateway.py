"""Fail-closed loopback gateway for every local GeoServer request.

GeoServer itself has no published host port.  This process shares its private
network namespace, listens on the only published loopback port, and actively
revalidates the exact GeoWebCache FileBlobStore and disk quota before every
proxied serving or administrative request.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import http.client
import os
from pathlib import Path
import re
import socket
from typing import Protocol
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.reference_layers.geoserver_admin import GeoServerAdminClient
from app.reference_layers.gwc_quota import runtime_contract_status

INTERNAL_GEOSERVER_HOST = "127.0.0.1"
INTERNAL_GEOSERVER_PORT = 8080
INTERNAL_GEOSERVER_BASE_URL = (
    f"http://{INTERNAL_GEOSERVER_HOST}:{INTERNAL_GEOSERVER_PORT}/geoserver"
)
GATEWAY_HEALTH_PATH = "/_gwc_gateway/health"
TILE_CACHE_PATH = Path("/var/lib/geowebcache")
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
MAX_REQUEST_TARGET_BYTES = 16 * 1024
UPSTREAM_CHUNK_BYTES = 64 * 1024
ALLOWED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
PROTECTED_GWC_MUTATION_PATHS = (
    "/geoserver/gwc/rest/blobstores",
    "/geoserver/gwc/rest/diskquota",
)
LOOPBACK_HOST = re.compile(r"^(?:127\.0\.0\.1|localhost):([0-9]{1,5})$")


class GeoWebCacheGatewayError(Exception):
    """The gateway cannot safely forward a local GeoServer request."""


class GeoWebCacheGatewayRequestError(GeoWebCacheGatewayError):
    """The incoming loopback proxy request is invalid."""


class GeoWebCacheGatewayUpstreamError(GeoWebCacheGatewayError):
    """The fixed internal GeoServer proxy target is unavailable."""


class _Upstream(Protocol):
    status: int
    headers: tuple[tuple[str, str], ...]

    def iter_body(self) -> Iterator[bytes]: ...


ContractVerifier = Callable[[], Mapping[str, object]]
UpstreamOpener = Callable[
    [str, str, Mapping[str, str], bytes],
    _Upstream,
]


@dataclass
class _HTTPUpstream:
    connection: http.client.HTTPConnection
    response: http.client.HTTPResponse
    status: int
    headers: tuple[tuple[str, str], ...]

    def iter_body(self) -> Iterator[bytes]:
        try:
            while True:
                chunk = self.response.read(UPSTREAM_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        finally:
            self.connection.close()


def create_app(
    *,
    verifier: ContractVerifier | None = None,
    upstream_opener: UpstreamOpener | None = None,
) -> FastAPI:
    contract_verifier = verifier or _verify_current_contract
    open_upstream = upstream_opener or _open_upstream
    application = FastAPI(
        title="GeoWebCache fail-closed gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get(GATEWAY_HEALTH_PATH)
    async def health() -> JSONResponse:
        try:
            report = await run_in_threadpool(contract_verifier)
            if report.get("verified") is not True:
                raise GeoWebCacheGatewayError(
                    "active GeoWebCache contract is not verified"
                )
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"verified": False},
            )
        return JSONResponse(
            status_code=200,
            content={
                "verified": report.get("verified") is True,
                "schema_version": 1,
            },
        )

    async def proxy(request: Request) -> Response:
        try:
            target = _validated_target(request)
            public_host = _validated_public_host(
                request.headers.get("host")
            )
        except GeoWebCacheGatewayRequestError:
            return JSONResponse(
                status_code=400,
                content={"detail": "invalid local GeoServer gateway request"},
            )
        try:
            report = await run_in_threadpool(contract_verifier)
            if report.get("verified") is not True:
                raise GeoWebCacheGatewayError(
                    "active GeoWebCache contract is not verified"
                )
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "local GeoServer contract is not verified"
                },
            )
        if _is_protected_gwc_mutation(request.method, request.url.path):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "GeoWebCache contract mutations require the private "
                        "bootstrap path"
                    )
                },
            )
        try:
            body = await _bounded_request_body(request)
            headers = _upstream_request_headers(
                request,
                public_host=public_host,
                body=body,
            )
            upstream = await run_in_threadpool(
                open_upstream,
                request.method,
                target,
                headers,
                body,
            )
        except GeoWebCacheGatewayRequestError:
            return JSONResponse(
                status_code=413,
                content={"detail": "local GeoServer request body is too large"},
            )
        except Exception:
            return JSONResponse(
                status_code=502,
                content={"detail": "local GeoServer is unavailable"},
            )
        response = StreamingResponse(
            upstream.iter_body(),
            status_code=upstream.status,
        )
        response.raw_headers = _upstream_response_headers(
            upstream.headers,
            public_host=public_host,
        )
        return response

    application.add_api_route(
        "/geoserver",
        proxy,
        methods=list(ALLOWED_METHODS),
    )
    application.add_api_route(
        "/geoserver/{path:path}",
        proxy,
        methods=list(ALLOWED_METHODS),
    )
    return application


def _verify_current_contract() -> Mapping[str, object]:
    return runtime_contract_status(
        cache_path=TILE_CACHE_PATH,
        configured=settings,
        client=GeoServerAdminClient(
            base_url=INTERNAL_GEOSERVER_BASE_URL,
        ),
    )


def _validated_target(request: Request) -> str:
    raw_path = request.scope.get("raw_path")
    query = request.scope.get("query_string", b"")
    if (
        not isinstance(raw_path, bytes)
        or not isinstance(query, bytes)
        or not raw_path.startswith(b"/geoserver")
        or (
            raw_path != b"/geoserver"
            and not raw_path.startswith(b"/geoserver/")
        )
        or b"\x00" in raw_path
        or b"\\" in raw_path
    ):
        raise GeoWebCacheGatewayRequestError("invalid gateway target")
    try:
        decoded_path = unquote(raw_path.decode("ascii"), errors="strict")
        query.decode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError):
        raise GeoWebCacheGatewayRequestError(
            "invalid gateway target encoding"
        ) from None
    if (
        "//" in decoded_path
        or "\\" in decoded_path
        or any(part == ".." for part in decoded_path.split("/"))
    ):
        raise GeoWebCacheGatewayRequestError("unsafe gateway target")
    target = raw_path + (b"?" + query if query else b"")
    if len(target) > MAX_REQUEST_TARGET_BYTES:
        raise GeoWebCacheGatewayRequestError("gateway target is too large")
    return target.decode("ascii")


def _validated_public_host(value: str | None) -> str:
    if not isinstance(value, str):
        raise GeoWebCacheGatewayRequestError("gateway Host is missing")
    normalized = value.strip().casefold()
    match = LOOPBACK_HOST.fullmatch(normalized)
    if match is None:
        raise GeoWebCacheGatewayRequestError("gateway Host is not loopback")
    port = int(match.group(1))
    if not 1 <= port <= 65535:
        raise GeoWebCacheGatewayRequestError("gateway Host port is invalid")
    configured_port = _public_port()
    if port != configured_port:
        raise GeoWebCacheGatewayRequestError("gateway Host port differs")
    return f"127.0.0.1:{configured_port}"


def _public_port() -> int:
    raw = os.environ.get("GWC_GATEWAY_PUBLIC_PORT", "8081").strip()
    if (
        not raw
        or len(raw) > 5
        or not raw.isascii()
        or not raw.isdecimal()
    ):
        raise GeoWebCacheGatewayRequestError(
            "gateway public port is invalid"
        )
    port = int(raw)
    if not 1 <= port <= 65535:
        raise GeoWebCacheGatewayRequestError(
            "gateway public port is invalid"
        )
    return port


async def _bounded_request_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        normalized = declared.strip()
        if (
            not normalized
            or len(normalized) > 20
            or not normalized.isascii()
            or not normalized.isdecimal()
            or int(normalized) > MAX_REQUEST_BODY_BYTES
        ):
            raise GeoWebCacheGatewayRequestError(
                "gateway request body is too large"
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_REQUEST_BODY_BYTES:
            raise GeoWebCacheGatewayRequestError(
                "gateway request body is too large"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _upstream_request_headers(
    request: Request,
    *,
    public_host: str,
    body: bytes,
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if (
            name.casefold() not in HOP_BY_HOP_HEADERS
            and name.casefold()
            not in {
                "content-length",
                "host",
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-port",
                "x-forwarded-proto",
            }
        )
    }
    headers["Host"] = public_host
    headers["Connection"] = "close"
    headers["X-Forwarded-Host"] = public_host
    headers["X-Forwarded-Port"] = public_host.rsplit(":", 1)[1]
    headers["X-Forwarded-Proto"] = "http"
    if body or request.headers.get("content-length") is not None:
        headers["Content-Length"] = str(len(body))
    return headers


def _is_protected_gwc_mutation(method: str, path: str) -> bool:
    return method not in {"GET", "HEAD", "OPTIONS"} and any(
        path == prefix
        or path.startswith(f"{prefix}/")
        or path.startswith(f"{prefix}.")
        for prefix in PROTECTED_GWC_MUTATION_PATHS
    )


def _open_upstream(
    method: str,
    target: str,
    headers: Mapping[str, str],
    body: bytes,
) -> _HTTPUpstream:
    connection = http.client.HTTPConnection(
        INTERNAL_GEOSERVER_HOST,
        port=INTERNAL_GEOSERVER_PORT,
        timeout=settings.local_geoserver_timeout_seconds,
    )
    try:
        connection.request(
            method,
            target,
            body=body or None,
            headers=dict(headers),
        )
        response = connection.getresponse()
        return _HTTPUpstream(
            connection=connection,
            response=response,
            status=int(response.status),
            headers=tuple(response.getheaders()),
        )
    except (
        OSError,
        TimeoutError,
        ValueError,
        http.client.HTTPException,
        socket.timeout,
    ) as error:
        connection.close()
        raise GeoWebCacheGatewayUpstreamError(
            "fixed internal GeoServer is unavailable"
        ) from error


def _upstream_response_headers(
    headers: tuple[tuple[str, str], ...],
    *,
    public_host: str,
) -> list[tuple[bytes, bytes]]:
    result: list[tuple[bytes, bytes]] = []
    internal_root = INTERNAL_GEOSERVER_BASE_URL
    public_root = f"http://{public_host}/geoserver"
    for name, value in headers:
        normalized_name = name.strip().casefold()
        if (
            not normalized_name
            or normalized_name in HOP_BY_HOP_HEADERS
            or "\r" in value
            or "\n" in value
        ):
            continue
        if normalized_name == "location":
            value = value.replace(internal_root, public_root, 1)
        try:
            result.append(
                (
                    normalized_name.encode("ascii"),
                    value.encode("latin-1"),
                )
            )
        except UnicodeEncodeError:
            continue
    return result


def healthcheck(
    *,
    gateway_port: int = 8081,
    public_port: int | None = None,
) -> int:
    advertised_port = (
        _public_port() if public_port is None else public_port
    )
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port=gateway_port,
        timeout=3.0,
    )
    try:
        connection.request(
            "GET",
            GATEWAY_HEALTH_PATH,
            headers={
                "Accept": "application/json",
                "Connection": "close",
                "Host": f"127.0.0.1:{advertised_port}",
            },
        )
        response = connection.getresponse()
        response.read(4096)
        return 0 if int(response.status) == 200 else 1
    except (OSError, http.client.HTTPException, socket.timeout):
        return 1
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Healthcheck for the fail-closed GeoWebCache gateway."
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="verify that the loopback gateway currently admits requests",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.healthcheck:
        raise SystemExit("--healthcheck is required")
    return healthcheck()


app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
