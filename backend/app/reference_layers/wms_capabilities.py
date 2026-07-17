"""Bounded, local-file parsing for immutable SIUR WMS capabilities evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from app.reference_layers.wms_proxy import (
    WMS_TOKEN,
    UnsafeWMSEndpointError,
    validate_siur_wms_endpoint,
)

MAX_WMS_CAPABILITIES_BYTES = 4 * 1024 * 1024
MAX_WMS_CAPABILITIES_ELEMENTS = 50_000
MAX_WMS_CAPABILITIES_TEXT_BYTES = 2 * 1024 * 1024
MAX_WMS_CAPABILITY_LAYERS = 10_000
MAX_WMS_LAYER_DEPTH = 64
MAX_WMS_STYLES_PER_LAYER = 256
MAX_WMS_CRS_PER_LAYER = 256
MAX_WMS_FORMATS = 100
NORMALIZATION_VERSION = "siur-wms-capabilities-v1"
SUPPORTED_ROOTS = {
    "1.1.1": "WMT_MS_Capabilities",
    "1.3.0": "WMS_Capabilities",
}
SUPPORTED_NAMESPACES = {
    "1.1.1": {""},
    "1.3.0": {"http://www.opengis.net/wms"},
}
CORE_ELEMENTS = {
    "Capability",
    "Request",
    "GetMap",
    "GetFeatureInfo",
    "GetLegendGraphic",
    "Format",
    "DCPType",
    "HTTP",
    "Get",
    "OnlineResource",
    "Layer",
    "CRS",
    "SRS",
    "Style",
    "Name",
}
XML_ENCODING = re.compile(
    br"<\?xml[^>]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


class WMSCapabilitiesError(ValueError):
    """The supplied capabilities document is unsafe or internally invalid."""


@dataclass(frozen=True)
class WMSLayerCapability:
    name: str
    crs: tuple[str, ...]
    queryable: bool
    styles: tuple[str, ...]

    def normalized(self) -> dict[str, object]:
        return {
            "name": self.name,
            "crs": list(self.crs),
            "queryable": self.queryable,
            "styles": list(self.styles),
        }


@dataclass(frozen=True)
class WMSCapabilitiesEvidence:
    raw_xml: bytes
    raw_sha256: str
    normalized_sha256: str
    version: str
    get_map_endpoint: str
    get_legend_endpoint: str | None
    get_feature_info_endpoint: str | None
    get_map_formats: tuple[str, ...]
    get_legend_formats: tuple[str, ...]
    get_feature_info_formats: tuple[str, ...]
    layers: tuple[WMSLayerCapability, ...]

    @property
    def normalization_version(self) -> str:
        return NORMALIZATION_VERSION

    @property
    def layer_manifest(self) -> list[dict[str, object]]:
        return [layer.normalized() for layer in self.layers]

    def normalized(self) -> dict[str, object]:
        return {
            "normalization_version": NORMALIZATION_VERSION,
            "wms_version": self.version,
            "get_map_endpoint": self.get_map_endpoint,
            "get_legend_endpoint": self.get_legend_endpoint,
            "get_feature_info_endpoint": self.get_feature_info_endpoint,
            "get_map_formats": list(self.get_map_formats),
            "get_legend_formats": list(self.get_legend_formats),
            "get_feature_info_formats": list(
                self.get_feature_info_formats
            ),
            "layers": self.layer_manifest,
        }


def parse_wms_capabilities(document: bytes) -> WMSCapabilitiesEvidence:
    if not isinstance(document, bytes):
        raise WMSCapabilitiesError("Capabilities must be supplied as bytes")
    if not 0 < len(document) <= MAX_WMS_CAPABILITIES_BYTES:
        raise WMSCapabilitiesError(
            "Capabilities size is outside the allowed range"
        )
    _require_safe_xml_encoding(document)
    lowered = document.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise WMSCapabilitiesError("DTD and entity declarations are forbidden")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise WMSCapabilitiesError("Capabilities XML is malformed") from exc

    elements = list(root.iter())
    if len(elements) > MAX_WMS_CAPABILITIES_ELEMENTS:
        raise WMSCapabilitiesError("Capabilities contains too many elements")
    text_size = sum(
        len(value.encode("utf-8"))
        for element in elements
        for value in (element.text or "", element.tail or "")
    )
    if text_size > MAX_WMS_CAPABILITIES_TEXT_BYTES:
        raise WMSCapabilitiesError("Capabilities text is too large")

    version = (root.get("version") or "").strip()
    if SUPPORTED_ROOTS.get(version) != _local_name(root.tag):
        raise WMSCapabilitiesError("Only WMS 1.1.1 and 1.3.0 are supported")
    root_namespace = _namespace(root.tag)
    if root_namespace not in SUPPORTED_NAMESPACES[version]:
        raise WMSCapabilitiesError("Capabilities namespace is unsupported")
    if any(
        _local_name(element.tag) in CORE_ELEMENTS
        and _namespace(element.tag) != root_namespace
        for element in elements
    ):
        raise WMSCapabilitiesError("Capabilities mixes WMS namespaces")
    capability = _required_child(root, "Capability")
    request = _required_child(capability, "Request")
    get_map = _required_child(request, "GetMap")
    get_map_formats = _operation_formats(get_map, "GetMap")
    get_map_endpoint = _operation_endpoint(get_map, "GetMap")

    get_legend = _optional_child(request, "GetLegendGraphic")
    if get_legend is None:
        get_legend_formats: tuple[str, ...] = ()
        get_legend_endpoint = None
    else:
        get_legend_formats = _operation_formats(
            get_legend,
            "GetLegendGraphic",
        )
        get_legend_endpoint = _operation_endpoint(
            get_legend,
            "GetLegendGraphic",
        )

    get_feature_info = _optional_child(request, "GetFeatureInfo")
    if get_feature_info is None:
        get_feature_info_formats: tuple[str, ...] = ()
        get_feature_info_endpoint = None
    else:
        get_feature_info_formats = _operation_formats(
            get_feature_info,
            "GetFeatureInfo",
        )
        get_feature_info_endpoint = _operation_endpoint(
            get_feature_info,
            "GetFeatureInfo",
        )

    manifest: dict[str, WMSLayerCapability] = {}
    root_layers = _children(capability, "Layer")
    if not root_layers:
        raise WMSCapabilitiesError("Capabilities has no layer tree")
    for layer in root_layers:
        _walk_layer(
            layer,
            depth=0,
            inherited_crs=(),
            inherited_queryable=False,
            inherited_styles=("",),
            manifest=manifest,
        )
    if not manifest:
        raise WMSCapabilitiesError("Capabilities has no named layers")

    layers = tuple(manifest[name] for name in sorted(manifest))
    normalized = {
        "normalization_version": NORMALIZATION_VERSION,
        "wms_version": version,
        "get_map_endpoint": get_map_endpoint,
        "get_legend_endpoint": get_legend_endpoint,
        "get_feature_info_endpoint": get_feature_info_endpoint,
        "get_map_formats": list(get_map_formats),
        "get_legend_formats": list(get_legend_formats),
        "get_feature_info_formats": list(get_feature_info_formats),
        "layers": [layer.normalized() for layer in layers],
    }
    normalized_sha256 = canonical_capabilities_sha256(normalized)
    return WMSCapabilitiesEvidence(
        raw_xml=document,
        raw_sha256=hashlib.sha256(document).hexdigest(),
        normalized_sha256=normalized_sha256,
        version=version,
        get_map_endpoint=get_map_endpoint,
        get_legend_endpoint=get_legend_endpoint,
        get_feature_info_endpoint=get_feature_info_endpoint,
        get_map_formats=get_map_formats,
        get_legend_formats=get_legend_formats,
        get_feature_info_formats=get_feature_info_formats,
        layers=layers,
    )


def canonical_capabilities_sha256(normalized: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_safe_xml_encoding(document: bytes) -> None:
    if document.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in document:
        raise WMSCapabilitiesError("Capabilities must use UTF-8 encoding")
    declaration = XML_ENCODING.search(document[:512])
    if declaration is not None and declaration.group(1).lower() not in {
        b"utf-8",
        b"utf8",
    }:
        raise WMSCapabilitiesError("Capabilities must use UTF-8 encoding")


def _walk_layer(
    element: ElementTree.Element,
    *,
    depth: int,
    inherited_crs: tuple[str, ...],
    inherited_queryable: bool,
    inherited_styles: tuple[str, ...],
    manifest: dict[str, WMSLayerCapability],
) -> None:
    if depth > MAX_WMS_LAYER_DEPTH:
        raise WMSCapabilitiesError("Capabilities layer tree is too deep")
    queryable = _queryable(element, inherited_queryable)
    crs = set(inherited_crs)
    for child in element:
        if _local_name(child.tag) not in {"CRS", "SRS"}:
            continue
        for value in (child.text or "").split():
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
                raise WMSCapabilitiesError("Layer contains an invalid CRS")
            crs.add(value)
    if len(crs) > MAX_WMS_CRS_PER_LAYER:
        raise WMSCapabilitiesError("Layer contains too many CRS values")

    styles = set(inherited_styles)
    styles.add("")
    for style in _children(element, "Style"):
        name = _optional_child_text(style, "Name")
        if name is None:
            continue
        if not WMS_TOKEN.fullmatch(name):
            raise WMSCapabilitiesError("Layer contains an invalid style name")
        styles.add(name)
    if len(styles) > MAX_WMS_STYLES_PER_LAYER:
        raise WMSCapabilitiesError("Layer contains too many styles")

    name = _optional_child_text(element, "Name")
    if name is not None:
        if not WMS_TOKEN.fullmatch(name):
            raise WMSCapabilitiesError("Layer contains an invalid name")
        candidate = WMSLayerCapability(
            name=name,
            crs=tuple(sorted(crs)),
            queryable=queryable,
            styles=tuple(sorted(styles)),
        )
        if name in manifest:
            raise WMSCapabilitiesError(
                "Capabilities contains duplicate named layers"
            )
        manifest[name] = candidate
        if len(manifest) > MAX_WMS_CAPABILITY_LAYERS:
            raise WMSCapabilitiesError("Capabilities contains too many layers")

    inherited = tuple(sorted(styles))
    inherited_layer_crs = tuple(sorted(crs))
    for child in _children(element, "Layer"):
        _walk_layer(
            child,
            depth=depth + 1,
            inherited_crs=inherited_layer_crs,
            inherited_queryable=queryable,
            inherited_styles=inherited,
            manifest=manifest,
        )


def _queryable(element: ElementTree.Element, inherited: bool) -> bool:
    value = element.get("queryable")
    if value is None:
        return inherited
    if value == "0":
        return False
    if value == "1":
        return True
    raise WMSCapabilitiesError("Layer contains an invalid queryable flag")


def _operation_formats(
    operation: ElementTree.Element,
    label: str,
) -> tuple[str, ...]:
    formats: set[str] = set()
    for element in _children(operation, "Format"):
        value = (element.text or "").strip().casefold()
        if (
            not value
            or len(value) > 255
            or any(ord(character) < 32 for character in value)
        ):
            raise WMSCapabilitiesError(f"{label} contains an invalid format")
        formats.add(value)
    if not formats or len(formats) > MAX_WMS_FORMATS:
        raise WMSCapabilitiesError(f"{label} formats are not usable")
    return tuple(sorted(formats))


def _operation_endpoint(operation: ElementTree.Element, label: str) -> str:
    endpoints: set[str] = set()
    dcp_types = _children(operation, "DCPType")
    if not dcp_types:
        raise WMSCapabilitiesError(f"{label} has no DCPType binding")
    for dcp_type in dcp_types:
        http = _required_child(dcp_type, "HTTP")
        for get_binding in _children(http, "Get"):
            resources = _children(get_binding, "OnlineResource")
            if len(resources) != 1:
                raise WMSCapabilitiesError(
                    f"{label} GET binding must declare one OnlineResource"
                )
            href = resources[0].get(XLINK_HREF)
            if href is None:
                raise WMSCapabilitiesError(
                    f"{label} endpoint has no xlink:href"
                )
            endpoints.add(_normalize_endpoint(href))
    if len(endpoints) != 1:
        raise WMSCapabilitiesError(
            f"{label} must declare one verified GET endpoint"
        )
    return endpoints.pop()


def _normalize_endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise WMSCapabilitiesError("WMS endpoint is invalid")
    candidate = value.strip()
    try:
        parsed = validate_siur_wms_endpoint(candidate)
    except UnsafeWMSEndpointError as exc:
        raise WMSCapabilitiesError(
            "WMS endpoint is outside the SIUR allowlist"
        ) from exc
    split = urlsplit(candidate)
    if split.query:
        raise WMSCapabilitiesError("WMS endpoint contains parameters")
    return urlunsplit(
        (parsed.scheme, parsed.hostname or "", parsed.path, "", "")
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _children(
    parent: ElementTree.Element,
    name: str,
) -> list[ElementTree.Element]:
    return [child for child in parent if _local_name(child.tag) == name]


def _optional_child(
    parent: ElementTree.Element,
    name: str,
) -> ElementTree.Element | None:
    matches = _children(parent, name)
    if len(matches) > 1:
        raise WMSCapabilitiesError(f"Capabilities contains duplicate {name}")
    return matches[0] if matches else None


def _required_child(
    parent: ElementTree.Element,
    name: str,
) -> ElementTree.Element:
    child = _optional_child(parent, name)
    if child is None:
        raise WMSCapabilitiesError(f"Capabilities is missing {name}")
    return child


def _optional_child_text(
    parent: ElementTree.Element,
    name: str,
) -> str | None:
    child = _optional_child(parent, name)
    if child is None:
        return None
    value = (child.text or "").strip()
    if not value:
        raise WMSCapabilitiesError(f"Capabilities contains an empty {name}")
    return value
