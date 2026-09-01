"""Parse a SIUR Web Map Context as partial, non-authoritative evidence.

The exported WMC describes one map state.  It must never be passed directly to
``apply_catalog_definition`` because it is not the complete SIUR inventory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

if TYPE_CHECKING:
    from app.reference_layers.catalog import (
        ReferenceCatalogDefinition,
        ReferenceLayerStyleDefinition,
    )

CONTEXT_NS = "http://www.opengis.net/context"
SLD_NS = "http://www.opengis.net/sld"
XLINK_NS = "http://www.w3.org/1999/xlink"
MAX_WMC_BYTES = 512 * 1024
MAX_WMC_ELEMENTS = 4_000
MAX_TEXT_CHARS = 4_000
ALLOWED_SIUR_HOSTS = {"idecyl.jcyl.es"}
TECHNICAL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,500}$")
CRS_RE = re.compile(r"^EPSG:[0-9]{3,6}$")
SERVICE_PATH_RE = re.compile(
    r"^/geoserver/(?P<workspace>[A-Za-z0-9_.-]+)/(?P<operation>wms|ows)$",
    re.IGNORECASE,
)
METADATA_FRAGMENT_RE = re.compile(r"^/metadata/(?P<record>[A-Za-z0-9_.:-]+)$")


class SiurWmcError(ValueError):
    """The WMC is malformed, unsafe or outside the supported SIUR profile."""


@dataclass(frozen=True)
class WmcBounds:
    crs: str
    minx: Decimal
    miny: Decimal
    maxx: Decimal
    maxy: Decimal


@dataclass(frozen=True)
class WmcStyleEvidence:
    source_key: str
    remote_name: str
    title: str
    selected: bool
    legend_url: str | None


@dataclass(frozen=True)
class WmcLayerEvidence:
    ordinal: int
    source_key: str
    service_key: str
    service_url: str
    service_version: str
    remote_name: str
    title: str
    crs: str
    visible: bool
    queryable: bool
    opacity: Decimal
    extent: WmcBounds
    styles: tuple[WmcStyleEvidence, ...]
    metadata_record_id: str | None
    min_scale_denominator: Decimal | None
    max_scale_denominator: Decimal | None

    @property
    def selected_style_key(self) -> str | None:
        return next(
            (style.source_key for style in self.styles if style.selected),
            None,
        )


@dataclass(frozen=True)
class WmcEvidence:
    content_sha256: str
    title: str
    bounds: WmcBounds
    window_width: int
    window_height: int
    layers: tuple[WmcLayerEvidence, ...]

    @property
    def service_count(self) -> int:
        return len({layer.service_key for layer in self.layers})

    @property
    def style_count(self) -> int:
        return sum(len(layer.styles) for layer in self.layers)

    @property
    def selected_style_count(self) -> int:
        return sum(layer.selected_style_key is not None for layer in self.layers)

    @property
    def metadata_count(self) -> int:
        return sum(layer.metadata_record_id is not None for layer in self.layers)

    def normalized(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class WmcParityReport:
    provider_mismatch: bool
    service_mismatches: tuple[str, ...]
    declared_layers: int
    matched_layers: tuple[str, ...]
    unmatched_layers: tuple[str, ...]
    ambiguous_layers: tuple[str, ...]
    declared_styles: int
    matched_styles: tuple[str, ...]
    unmatched_styles: tuple[str, ...]
    selected_style_mismatches: tuple[str, ...]

    @property
    def blocking_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        if self.provider_mismatch:
            issues.append("WMC evidence requires the siur provider")
        issues.extend(
            f"WMC service mismatch: {key}" for key in self.service_mismatches
        )
        issues.extend(f"Unmatched WMC layer: {key}" for key in self.unmatched_layers)
        issues.extend(f"Ambiguous WMC layer: {key}" for key in self.ambiguous_layers)
        issues.extend(f"Unmatched WMC style: {key}" for key in self.unmatched_styles)
        issues.extend(
            f"Selected WMC style mismatch: {key}"
            for key in self.selected_style_mismatches
        )
        return tuple(issues)


def parse_wmc_evidence(document: bytes) -> WmcEvidence:
    if not isinstance(document, bytes):
        raise SiurWmcError("WMC input must be bytes")
    if not document or len(document) > MAX_WMC_BYTES:
        raise SiurWmcError("WMC size is outside the allowed range")
    try:
        decoded = document.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SiurWmcError("WMC must use UTF-8 encoding") from exc
    if "\x00" in decoded:
        raise SiurWmcError("WMC must use UTF-8 encoding")
    lowered = decoded.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise SiurWmcError("DTD and entity declarations are forbidden")

    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise SiurWmcError("WMC is not well-formed XML") from exc

    elements = list(root.iter())
    if len(elements) > MAX_WMC_ELEMENTS:
        raise SiurWmcError("WMC contains too many XML elements")
    if any(len(element.text or "") > MAX_TEXT_CHARS for element in elements):
        raise SiurWmcError("WMC contains an oversized text value")
    if root.tag != _tag("ViewContext") or root.get("version") != "1.1.0":
        raise SiurWmcError("Only OGC WMC 1.1.0 is supported")

    general = _required_child(root, "General")
    title = _required_text(general, "Title")
    bounds = _parse_bounds(_required_child(general, "BoundingBox"))
    window = _required_child(general, "Window")
    window_width = _positive_int(window.get("width"), "Window width")
    window_height = _positive_int(window.get("height"), "Window height")

    layer_list = _required_child(root, "LayerList")
    layer_elements = layer_list.findall(_tag("Layer"))
    if not layer_elements:
        raise SiurWmcError("WMC does not contain any layers")
    layers = tuple(
        _parse_layer(element, ordinal)
        for ordinal, element in enumerate(layer_elements, start=1)
    )
    layer_keys = [layer.source_key for layer in layers]
    if len(layer_keys) != len(set(layer_keys)):
        raise SiurWmcError("WMC contains duplicate layer identities")
    service_signatures: dict[str, tuple[str, str]] = {}
    for layer in layers:
        signature = (layer.service_url, layer.service_version)
        previous = service_signatures.setdefault(layer.service_key, signature)
        if previous != signature:
            raise SiurWmcError("WMC contains conflicting service definitions")
    return WmcEvidence(
        content_sha256=hashlib.sha256(document).hexdigest(),
        title=title,
        bounds=bounds,
        window_width=window_width,
        window_height=window_height,
        layers=layers,
    )


def compare_wmc_to_catalog(
    evidence: WmcEvidence,
    definition: "ReferenceCatalogDefinition",
) -> WmcParityReport:
    matched_layers: list[str] = []
    unmatched_layers: list[str] = []
    ambiguous_layers: list[str] = []
    matched_styles: list[str] = []
    unmatched_styles: list[str] = []
    selected_style_mismatches: list[str] = []

    catalog_services = {
        service.source_key: service for service in definition.services
    }
    service_mismatches: list[str] = []
    observed_services = {
        layer.service_key: layer for layer in evidence.layers
    }
    for key, observed in observed_services.items():
        service = catalog_services.get(key)
        try:
            service_url = (
                _parse_service_url(service.base_url)[0]
                if service is not None
                else None
            )
        except SiurWmcError:
            service_url = None
        if (
            service is None
            or service.upstream_protocol != "wms"
            or service_url != observed.service_url
            or service.version != observed.service_version
        ):
            service_mismatches.append(key)

    catalog_layers = [
        layer for layer in definition.layers if layer.node_type == "layer"
    ]
    for observed in evidence.layers:
        candidates = [
            layer
            for layer in catalog_layers
            if layer.service_key == observed.service_key
            and _remote_names_match(layer.remote_name, observed.remote_name)
        ]
        if not candidates:
            unmatched_layers.append(observed.source_key)
            continue
        if len(candidates) != 1:
            ambiguous_layers.append(observed.source_key)
            continue

        layer = candidates[0]
        matched_layers.append(observed.source_key)
        default_styles = {
            style.source_key for style in layer.styles if style.is_default
        }
        _, unique_style_matches = _catalog_style_matches(
            layer.styles,
            observed.styles,
        )
        selected_style = None
        for style in observed.styles:
            identity = f"{observed.source_key}|{style.source_key}"
            catalog_key = unique_style_matches.get(style.source_key)
            if catalog_key is not None:
                matched_styles.append(identity)
                if style.selected:
                    selected_style = catalog_key
            else:
                unmatched_styles.append(identity)
        if (
            selected_style != layer.style_name
            or default_styles
            != ({selected_style} if selected_style is not None else set())
        ):
            selected_style_mismatches.append(observed.source_key)

    return WmcParityReport(
        provider_mismatch=definition.provider_key != "siur",
        service_mismatches=tuple(service_mismatches),
        declared_layers=len(evidence.layers),
        matched_layers=tuple(matched_layers),
        unmatched_layers=tuple(unmatched_layers),
        ambiguous_layers=tuple(ambiguous_layers),
        declared_styles=evidence.style_count,
        matched_styles=tuple(matched_styles),
        unmatched_styles=tuple(unmatched_styles),
        selected_style_mismatches=tuple(selected_style_mismatches),
    )


def augment_catalog_with_wmc_evidence(
    definition: "ReferenceCatalogDefinition",
    evidence: WmcEvidence,
) -> "ReferenceCatalogDefinition":
    """Enrich matching catalog entries without ever adding WMC-only layers."""

    from app.reference_layers.catalog import ReferenceLayerStyleDefinition

    observed_services = {
        layer.service_key: layer for layer in evidence.layers
    }
    services = []
    for service in definition.services:
        observed = observed_services.get(service.source_key)
        if observed is not None and service.version is None:
            try:
                matches_endpoint = (
                    _parse_service_url(service.base_url)[0]
                    == observed.service_url
                )
            except SiurWmcError:
                matches_endpoint = False
            if matches_endpoint and service.upstream_protocol == "wms":
                service = replace(service, version=observed.service_version)
        services.append(service)

    layers = list(definition.layers)
    for observed in evidence.layers:
        indexes = [
            index
            for index, layer in enumerate(layers)
            if layer.node_type == "layer"
            and layer.service_key == observed.service_key
            and _remote_names_match(layer.remote_name, observed.remote_name)
        ]
        if len(indexes) != 1:
            continue
        index = indexes[0]
        layer = layers[index]
        candidate_keys, uniquely_matched = _catalog_style_matches(
            layer.styles,
            observed.styles,
        )
        observed_by_catalog_key = {
            catalog_key: next(
                style
                for style in observed.styles
                if style.source_key == observed_key
            )
            for observed_key, catalog_key in uniquely_matched.items()
        }
        observed_order = {
            style.source_key: sort_order
            for sort_order, style in enumerate(observed.styles)
        }
        selected_style = next(
            (style for style in observed.styles if style.selected),
            None,
        )
        selected_style_key = (
            uniquely_matched.get(selected_style.source_key)
            if selected_style is not None
            else None
        )
        merged_styles: dict[str, ReferenceLayerStyleDefinition] = {}
        for style in layer.styles:
            observed_style = observed_by_catalog_key.get(style.source_key)
            merged_styles[style.source_key] = replace(
                style,
                title=(
                    observed_style.title
                    if observed_style is not None
                    else style.title
                ),
                legend_url=(
                    style.legend_url
                    or (
                        observed_style.legend_url
                        if observed_style is not None
                        else None
                    )
                ),
                remote_name=(
                    observed_style.remote_name
                    if observed_style is not None
                    else style.remote_name
                ),
                sort_order=observed_order.get(
                    observed_style.source_key
                    if observed_style is not None
                    else "",
                    style.sort_order,
                ),
                is_default=style.source_key == selected_style_key,
            )
        for sort_order, style in enumerate(observed.styles):
            if not candidate_keys[style.source_key]:
                merged_styles[style.source_key] = ReferenceLayerStyleDefinition(
                    source_key=style.source_key,
                    title=style.title,
                    remote_name=style.remote_name,
                    legend_url=style.legend_url,
                    sort_order=sort_order,
                    is_default=style.selected,
                )
                if style.selected:
                    selected_style_key = style.source_key
        layers[index] = replace(
            layer,
            style_name=selected_style_key,
            queryable=observed.queryable,
            styles=tuple(
                sorted(
                    merged_styles.values(),
                    key=lambda style: (style.sort_order, style.source_key),
                )
            ),
        )

    return replace(
        definition,
        services=tuple(services),
        layers=tuple(layers),
    )


def _parse_layer(element: ElementTree.Element, ordinal: int) -> WmcLayerEvidence:
    visible = not _binary_flag(element.get("hidden"), "Layer hidden")
    queryable = _binary_flag(element.get("queryable"), "Layer queryable")
    server = _required_child(element, "Server")
    if server.get("service") != "OGC:WMS":
        raise SiurWmcError(f"Layer {ordinal}: only OGC:WMS is supported")
    service_version = server.get("version") or ""
    if service_version not in {"1.1.1", "1.3.0"}:
        raise SiurWmcError(f"Layer {ordinal}: unsupported WMS version")
    service_url, workspace = _parse_service_url(
        _online_resource(_required_child(server, "OnlineResource"))
    )
    service_key = f"service:wms:idecyl:{workspace.lower()}"

    remote_name = _required_text(element, "Name")
    if not TECHNICAL_NAME_RE.fullmatch(remote_name):
        raise SiurWmcError(f"Layer {ordinal}: invalid technical name")
    layer_component = remote_name.rsplit(":", 1)[-1].lower()
    source_key = f"layer:wms:idecyl:{workspace.lower()}:{layer_component}"
    title = _required_text(element, "Title")
    crs = _valid_crs(_required_text(element, "SRS"), f"Layer {ordinal} SRS")

    extension = _required_child(element, "Extension")
    opacity_percent = _integer_text(
        _required_text(extension, "Opacity"),
        f"Layer {ordinal} opacity",
    )
    if not 0 <= opacity_percent <= 100:
        raise SiurWmcError(f"Layer {ordinal}: opacity is outside 0-100")
    extent = _parse_bounds(_required_child(extension, "Extent"))

    styles = _parse_styles(element, ordinal)
    metadata_record_id = _parse_metadata_record_id(element, ordinal)
    min_scale = _optional_positive_decimal(
        element.findtext(f"{{{SLD_NS}}}MinScaleDenominator"),
        f"Layer {ordinal} minimum scale",
    )
    max_scale = _optional_positive_decimal(
        element.findtext(f"{{{SLD_NS}}}MaxScaleDenominator"),
        f"Layer {ordinal} maximum scale",
    )
    if min_scale is not None and max_scale is not None and min_scale > max_scale:
        raise SiurWmcError(f"Layer {ordinal}: invalid scale range")

    return WmcLayerEvidence(
        ordinal=ordinal,
        source_key=source_key,
        service_key=service_key,
        service_url=service_url,
        service_version=service_version,
        remote_name=remote_name,
        title=title,
        crs=crs,
        visible=visible,
        queryable=queryable,
        opacity=Decimal(opacity_percent) / Decimal(100),
        extent=extent,
        styles=styles,
        metadata_record_id=metadata_record_id,
        min_scale_denominator=min_scale,
        max_scale_denominator=max_scale,
    )


def _parse_styles(
    layer: ElementTree.Element,
    ordinal: int,
) -> tuple[WmcStyleEvidence, ...]:
    style_list = layer.find(_tag("StyleList"))
    if style_list is None:
        return ()
    styles: list[WmcStyleEvidence] = []
    style_keys: set[str] = set()
    selected_count = 0
    for style_element in style_list.findall(_tag("Style")):
        remote_name = _required_text(style_element, "Name")
        if not TECHNICAL_NAME_RE.fullmatch(remote_name):
            raise SiurWmcError(f"Layer {ordinal}: invalid style name")
        source_key = remote_name.casefold()
        if source_key in style_keys:
            raise SiurWmcError(f"Layer {ordinal}: duplicate style name")
        style_keys.add(source_key)
        selected = style_element.get("current") == "1"
        selected_count += selected
        legend = style_element.find(f"{_tag('LegendURL')}/{_tag('OnlineResource')}")
        legend_url = None if legend is None else _parse_evidence_url(
            _online_resource(legend),
            allow_fragment=False,
            label=f"Layer {ordinal} legend",
        )
        styles.append(
            WmcStyleEvidence(
                source_key=source_key,
                remote_name=remote_name,
                title=_required_text(style_element, "Title"),
                selected=selected,
                legend_url=legend_url,
            )
        )
    if selected_count > 1:
        # SIUR publishes a WMC where plau_cyl_planes_parciales marks two styles
        # as current. The document stays valid evidence, but its selection for
        # that layer is ambiguous, so it selects nothing rather than guessing:
        # the layer keeps whatever default settings.json already derives, and
        # the probe simply stops confirming a default here. See ADR-055.
        return tuple(
            WmcStyleEvidence(
                source_key=style.source_key,
                remote_name=style.remote_name,
                title=style.title,
                selected=False,
                legend_url=style.legend_url,
            )
            for style in styles
        )
    return tuple(styles)


def _parse_metadata_record_id(
    layer: ElementTree.Element,
    ordinal: int,
) -> str | None:
    resource = layer.find(f"{_tag('MetadataURL')}/{_tag('OnlineResource')}")
    if resource is None:
        return None
    value = _parse_evidence_url(
        _online_resource(resource),
        allow_fragment=True,
        label=f"Layer {ordinal} metadata",
    )
    parsed = urlsplit(value)
    match = METADATA_FRAGMENT_RE.fullmatch(parsed.fragment)
    if match is None:
        raise SiurWmcError(f"Layer {ordinal}: unsupported metadata URL")
    return match.group("record")


def _parse_service_url(value: str) -> tuple[str, str]:
    normalized = _parse_evidence_url(
        value,
        allow_fragment=False,
        label="WMS service",
    )
    parsed = urlsplit(normalized)
    if parsed.query:
        raise SiurWmcError("WMS service URL contains unexpected parameters")
    match = SERVICE_PATH_RE.fullmatch(parsed.path.rstrip("/"))
    if match is None:
        raise SiurWmcError("WMS service URL has an unsupported path")
    workspace = match.group("workspace")
    canonical_path = f"/geoserver/{workspace}/wms"
    return (
        urlunsplit(("https", parsed.hostname or "", canonical_path, "", "")),
        workspace,
    )


def _parse_evidence_url(
    value: str,
    *,
    allow_fragment: bool,
    label: str,
) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SiurWmcError(f"{label}: invalid URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_SIUR_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (parsed.fragment and not allow_fragment)
    ):
        raise SiurWmcError(f"{label}: URL is outside the SIUR allowlist")
    return value.rstrip("?")


def _parse_bounds(element: ElementTree.Element) -> WmcBounds:
    crs = _valid_crs(element.get("SRS") or "", "Bounds SRS")
    minx = _decimal_attribute(element, "minx")
    miny = _decimal_attribute(element, "miny")
    maxx = _decimal_attribute(element, "maxx")
    maxy = _decimal_attribute(element, "maxy")
    if minx >= maxx or miny >= maxy:
        raise SiurWmcError("Bounds are empty or inverted")
    return WmcBounds(crs=crs, minx=minx, miny=miny, maxx=maxx, maxy=maxy)


def _required_child(
    parent: ElementTree.Element,
    local_name: str,
) -> ElementTree.Element:
    child = parent.find(_tag(local_name))
    if child is None:
        raise SiurWmcError(f"Missing WMC element: {local_name}")
    return child


def _required_text(parent: ElementTree.Element, local_name: str) -> str:
    element = _required_child(parent, local_name)
    value = (element.text or "").strip()
    if not value or len(value) > MAX_TEXT_CHARS:
        raise SiurWmcError(f"Invalid WMC text: {local_name}")
    return value


def _online_resource(element: ElementTree.Element) -> str:
    value = (element.get(f"{{{XLINK_NS}}}href") or "").strip()
    if not value:
        raise SiurWmcError("OnlineResource does not contain an href")
    return value


def _decimal_attribute(element: ElementTree.Element, name: str) -> Decimal:
    return _required_decimal(element.get(name), f"Bounds {name}")


def _required_decimal(value: str | None, label: str) -> Decimal:
    try:
        parsed = Decimal(value or "")
    except InvalidOperation as exc:
        raise SiurWmcError(f"{label}: invalid decimal") from exc
    if not parsed.is_finite():
        raise SiurWmcError(f"{label}: decimal must be finite")
    return parsed


def _optional_positive_decimal(
    value: str | None,
    label: str,
) -> Decimal | None:
    if value is None or not value.strip():
        return None
    parsed = _required_decimal(value.strip(), label)
    if parsed <= 0:
        raise SiurWmcError(f"{label}: value must be positive")
    return parsed


def _integer_text(value: str, label: str) -> int:
    if not value.isdigit():
        raise SiurWmcError(f"{label}: invalid integer")
    return int(value)


def _positive_int(value: str | None, label: str) -> int:
    parsed = _integer_text(value or "", label)
    if not 0 < parsed <= 100_000:
        raise SiurWmcError(f"{label}: value is outside the allowed range")
    return parsed


def _binary_flag(value: str | None, label: str) -> bool:
    if value not in {"0", "1"}:
        raise SiurWmcError(f"{label}: expected 0 or 1")
    return value == "1"


def _valid_crs(value: str, label: str) -> str:
    if not CRS_RE.fullmatch(value):
        raise SiurWmcError(f"{label}: unsupported CRS identifier")
    return value


def _remote_names_match(candidate: str | None, observed: str) -> bool:
    if not candidate:
        return False
    return candidate == observed or candidate.rsplit(":", 1)[-1] == observed.rsplit(
        ":", 1
    )[-1]


def _matching_catalog_styles(styles: tuple[Any, ...], observed: str) -> list[Any]:
    return [
        style
        for style in styles
        if _remote_names_match(style.remote_name or style.source_key, observed)
    ]


def _catalog_style_matches(
    catalog_styles: tuple[Any, ...],
    observed_styles: tuple[Any, ...],
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    candidates = {
        observed.source_key: tuple(
            style.source_key
            for style in _matching_catalog_styles(
                catalog_styles,
                observed.remote_name,
            )
        )
        for observed in observed_styles
    }
    unique = {
        observed_key: keys[0]
        for observed_key, keys in candidates.items()
        if len(keys) == 1
        and sum(keys[0] in other for other in candidates.values()) == 1
    }
    return candidates, unique


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _tag(local_name: str) -> str:
    return f"{{{CONTEXT_NS}}}{local_name}"
