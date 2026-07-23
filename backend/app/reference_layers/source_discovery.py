"""Deterministic acquisition candidates for reference map layers.

The catalog records how SIUR displays a layer.  That endpoint is often not the
best source to mirror: a WMS backed by GeoServer can expose the same collection
through WFS or WCS, while an ArcGIS WMS can have a queryable MapServer.  This
module only builds candidates; network probes must prove that the collection is
actually present before a candidate can become a primary source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.mirror_coverage import (
    SIUR_WMS_SUPERTILE_SIZE,
    reviewed_tile_coverage,
    reviewed_tile_format,
)

SourceProtocol = Literal[
    "wfs",
    "wcs",
    "ogc_api_features",
    "arcgis_rest",
    "atom",
    "download",
    "wmts",
    "xyz",
    "wms_tiles",
    "local",
]
TargetKind = Literal["vector", "raster", "tiles"]
SyncStrategy = Literal[
    "conditional_get",
    "full_snapshot",
    "paged_snapshot",
    "tile_seed",
    "manual",
]

_GEOSERVER_SERVICE_RE = re.compile(
    r"^(?P<prefix>/(?:geoserver|geoapps)/[^/]+)/(?:wms|ows)/?$",
    re.IGNORECASE,
)
_ARCGIS_WMS_RE = re.compile(
    r"^(?P<prefix>/.+?/MapServer)/(?:WMSServer)/?$",
    re.IGNORECASE,
)
_WMS_INSPIRE_RE = re.compile(
    r"^(?P<prefix>/)(?:wms-inspire)(?P<suffix>/[^/]+/?$)",
    re.IGNORECASE,
)


class SourceDiscoveryError(ValueError):
    """The catalog entry cannot safely produce acquisition candidates."""


@dataclass(frozen=True)
class SourceCandidate:
    protocol: SourceProtocol
    target_kind: TargetKind
    endpoint_url: str
    remote_name: str
    sync_strategy: SyncStrategy
    priority: int
    config: dict[str, Any]
    source_key: str
    definition_sha256: str


def acquisition_candidates(
    service: ReferenceServiceDefinition,
    layer: ReferenceLayerDefinition,
) -> tuple[SourceCandidate, ...]:
    """Return ordered candidates without asserting remote availability.

    Every renderable WMS/WMTS/XYZ layer receives a finite image fallback.  Data
    endpoints are deliberately preferred, but a later capabilities/metadata
    probe must confirm the exact remote collection and determine whether it is
    vector or raster.
    """

    if layer.node_type != "layer" or not layer.remote_name:
        raise SourceDiscoveryError("acquisition requires a named layer node")
    endpoint = _canonical_endpoint(service.base_url)
    protocol = service.upstream_protocol.lower()
    candidates: list[SourceCandidate] = []

    if protocol == "wms":
        parts = urlsplit(endpoint)
        geoserver = _GEOSERVER_SERVICE_RE.fullmatch(parts.path)
        if geoserver:
            prefix = geoserver.group("prefix")
            style_config = _geoserver_style_config(
                _with_path(parts, parts.path),
                layer,
            )
            candidates.extend(
                (
                    _candidate(
                        protocol="wfs",
                        target_kind="vector",
                        endpoint_url=_with_path(parts, f"{prefix}/wfs"),
                        remote_name=layer.remote_name,
                        sync_strategy="paged_snapshot",
                        priority=20,
                        config={
                            "discovery": "wfs_capabilities",
                            **style_config,
                        },
                    ),
                    _candidate(
                        protocol="wcs",
                        target_kind="raster",
                        endpoint_url=_with_path(parts, f"{prefix}/wcs"),
                        remote_name=layer.remote_name,
                        sync_strategy="full_snapshot",
                        priority=30,
                        config={
                            "discovery": "wcs_capabilities",
                            **style_config,
                        },
                    ),
                )
            )

        inspire = _WMS_INSPIRE_RE.fullmatch(parts.path)
        if inspire:
            candidates.append(
                _candidate(
                    protocol="wfs",
                    target_kind="vector",
                    endpoint_url=_with_path(
                        parts,
                        "/wfs-inspire" + inspire.group("suffix"),
                    ),
                    remote_name=layer.remote_name,
                    sync_strategy="paged_snapshot",
                    priority=25,
                    config={"discovery": "wfs_capabilities"},
                )
            )

        arcgis = _ARCGIS_WMS_RE.fullmatch(parts.path)
        if arcgis:
            candidates.append(
                _candidate(
                    protocol="arcgis_rest",
                    target_kind="vector",
                    endpoint_url=_with_path(parts, arcgis.group("prefix")),
                    remote_name=layer.remote_name,
                    sync_strategy="paged_snapshot",
                    priority=15,
                    config={"discovery": "arcgis_mapserver"},
                )
            )

        candidates.append(
            _candidate(
                protocol="wms_tiles",
                target_kind="tiles",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="tile_seed",
                priority=90,
                config=_tile_config(service, layer),
            )
        )
    elif protocol == "wmts":
        candidates.append(
            _candidate(
                protocol="wmts",
                target_kind="tiles",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="tile_seed",
                priority=50,
                config=_tile_config(service, layer),
            )
        )
    elif protocol == "xyz":
        candidates.append(
            _candidate(
                protocol="xyz",
                target_kind="tiles",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="tile_seed",
                priority=50,
                config=_tile_config(service, layer),
            )
        )
    elif protocol == "wfs":
        candidates.append(
            _candidate(
                protocol="wfs",
                target_kind="vector",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="paged_snapshot",
                priority=10,
                config={"discovery": "wfs_capabilities"},
            )
        )
    elif protocol == "arcgis_rest":
        candidates.append(
            _candidate(
                protocol="arcgis_rest",
                target_kind="vector",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="paged_snapshot",
                priority=10,
                config={"discovery": "arcgis_mapserver"},
            )
        )
    elif protocol == "local":
        candidates.append(
            _candidate(
                protocol="local",
                target_kind=_local_target_kind(layer),
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="manual",
                priority=10,
                config={},
            )
        )
    else:
        raise SourceDiscoveryError(f"unsupported catalog protocol: {protocol}")

    return tuple(sorted(_deduplicate(candidates), key=lambda item: item.priority))


def _candidate(
    *,
    protocol: SourceProtocol,
    target_kind: TargetKind,
    endpoint_url: str,
    remote_name: str,
    sync_strategy: SyncStrategy,
    priority: int,
    config: dict[str, Any],
) -> SourceCandidate:
    identity = {
        "protocol": protocol,
        "target_kind": target_kind,
        "endpoint_url": endpoint_url,
        "remote_name": remote_name,
        "sync_strategy": sync_strategy,
        "priority": priority,
        "config": config,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return SourceCandidate(
        **identity,
        source_key=f"auto:{protocol}:{digest[:32]}",
        definition_sha256=digest,
    )


def candidate_definition(candidate: SourceCandidate) -> dict[str, Any]:
    """Return the stable payload whose hash identifies a source definition."""

    value = asdict(candidate)
    value.pop("source_key")
    value.pop("definition_sha256")
    return value


def _tile_config(
    service: ReferenceServiceDefinition,
    layer: ReferenceLayerDefinition,
) -> dict[str, Any]:
    coverage = reviewed_tile_coverage(
        layer_source_key=layer.source_key,
        bounds=layer.bounds,
        min_zoom=layer.min_zoom,
        max_zoom=layer.max_zoom,
        endpoint_url=service.base_url,
        remote_name=layer.remote_name,
    )
    requested_format = layer.image_format or service.default_format
    image_format = reviewed_tile_format(
        layer_source_key=layer.source_key,
        endpoint_url=service.base_url,
        remote_name=layer.remote_name or "",
        requested_format=requested_format,
    )
    config = {
        "bounds": coverage.bounds,
        "min_zoom": coverage.min_zoom,
        "max_zoom": coverage.max_zoom,
        "format": image_format or _protocol_default_tile_format(service),
        "style_name": layer.style_name or "",
        "coverage_required": True,
    }
    if coverage.max_tile_count is not None:
        config["max_tile_count"] = coverage.max_tile_count
    if coverage.profile is not None:
        config["coverage_profile"] = coverage.profile
        if service.upstream_protocol.casefold() == "wms":
            config["wms_supertile_size"] = SIUR_WMS_SUPERTILE_SIZE
    return config


def _protocol_default_tile_format(
    service: ReferenceServiceDefinition,
) -> str:
    if service.upstream_protocol.casefold() == "xyz":
        path = urlsplit(service.base_url).path.casefold()
        if path.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
    return "image/png"


def _geoserver_style_config(
    style_endpoint_url: str,
    layer: ReferenceLayerDefinition,
) -> dict[str, Any]:
    """Freeze catalog style identities without database-specific IDs."""

    styles = sorted(
        (
            {
                "catalog_style_source_key": style.source_key,
                "remote_name": style.remote_name or style.source_key,
            }
            for style in layer.styles
            if style.status in {"active", "degraded"}
        ),
        key=lambda item: item["catalog_style_source_key"],
    )
    return {
        "style_endpoint_url": style_endpoint_url,
        "style_layer_name": layer.remote_name,
        "styles": styles,
    }


def _local_target_kind(layer: ReferenceLayerDefinition) -> TargetKind:
    return "vector" if layer.renderer == "vector_tile" else "raster"


def _canonical_endpoint(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise SourceDiscoveryError("source endpoint must be credential-free HTTPS")
    port = parts.port
    if port not in {None, 443}:
        raise SourceDiscoveryError("source endpoint uses an unsupported port")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    path = parts.path or "/"
    return urlunsplit(("https", host, path, parts.query, ""))


def _with_path(parts, path: str) -> str:
    host = parts.hostname.encode("idna").decode("ascii").lower()
    return urlunsplit(("https", host, path, "", ""))


def _deduplicate(
    candidates: list[SourceCandidate],
) -> list[SourceCandidate]:
    result: list[SourceCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        key = (
            candidate.protocol,
            candidate.endpoint_url,
            candidate.remote_name,
        )
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result
