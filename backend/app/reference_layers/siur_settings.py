"""Bounded, non-mutating analysis of SIUR ``settings.json`` bytes.

The file has no published versioned schema.  This adapter accepts a small,
explicit alias profile and records every other field as unresolved.  It only
returns a ``ReferenceCatalogDefinition`` when nothing is unresolved and an
operator-supplied baseline matches the complete observed inventory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import math
import re
from typing import Any, Iterable, Mapping
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    validate_catalog_definition,
)

DEFAULT_SOURCE_URL = "https://idecyl.jcyl.es/siur/assets/settings/settings.json"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,254}$")
CRS_RE = re.compile(r"^EPSG:[0-9]{3,6}$", re.I)
REMOTE_STYLE_RE = re.compile(r"^[A-Za-z0-9_.:]{1,255}$")
IDECYL_WMS_PATH_RE = re.compile(
    r"^/geoserver/(?P<workspace>[A-Za-z0-9_.-]+)/(?:wms|ows)$",
    re.I,
)
PROTOCOLS = {"wms", "wfs", "wmts", "xyz", "arcgis_rest"}
RASTER = {"wms", "wmts", "xyz", "arcgis_rest"}
DROP_QUERY = {
    "bbox",
    "crs",
    "format",
    "height",
    "info_format",
    "layers",
    "request",
    "service",
    "srs",
    "styles",
    "transparent",
    "version",
    "width",
}


class SiurSettingsError(ValueError):
    """Malformed or unsafe input."""


class SiurSettingsPromotionError(ValueError):
    """The dry-run is not promotable."""


@dataclass(frozen=True)
class SiurSettingsLimits:
    max_bytes: int = 2 * 1024 * 1024
    max_depth: int = 48
    max_nodes: int = 50_000
    max_string_chars: int = 16_384

    def __post_init__(self) -> None:
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("settings limits must be positive")


@dataclass(frozen=True)
class SiurSettingsBaseline:
    top_level_groups: int
    groups: int
    layers: int
    services: int
    raw_sha256: str | None = None
    layer_keys: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if min(self.top_level_groups, self.groups, self.layers, self.services) < 0:
            raise ValueError("baseline counts cannot be negative")
        if self.raw_sha256 and not SHA_RE.fullmatch(self.raw_sha256):
            raise ValueError("raw_sha256 must be a lowercase SHA-256")
        if self.layer_keys is not None and any(
            not KEY_RE.fullmatch(key) for key in self.layer_keys
        ):
            raise ValueError("baseline contains an invalid layer key")


@dataclass(frozen=True, order=True)
class SiurSettingsUnresolved:
    path: str
    code: str
    detail: str


@dataclass(frozen=True)
class SiurSettingsServiceEvidence:
    path: str
    source_key: str | None
    source_id: str | None
    protocol: str | None
    base_url: str | None


@dataclass(frozen=True)
class SiurSettingsNodeEvidence:
    path: str
    kind: str
    source_key: str | None
    source_id: str | None
    title: str | None
    parent_key: str | None
    service_key: str | None = None
    remote_name: str | None = None
    top_level: bool = False


@dataclass(frozen=True)
class SiurSettingsAnalysis:
    raw_sha256: str
    raw_size_bytes: int
    services: tuple[SiurSettingsServiceEvidence, ...]
    nodes: tuple[SiurSettingsNodeEvidence, ...]
    unresolved: tuple[SiurSettingsUnresolved, ...]
    definition: ReferenceCatalogDefinition | None

    @property
    def group_count(self) -> int:
        return sum(node.kind == "group" for node in self.nodes)

    @property
    def layer_count(self) -> int:
        return sum(node.kind == "layer" for node in self.nodes)

    @property
    def top_level_group_count(self) -> int:
        return sum(node.kind == "group" and node.top_level for node in self.nodes)

    @property
    def can_apply(self) -> bool:
        return self.definition is not None and not self.unresolved

    @property
    def blocking_issues(self) -> tuple[str, ...]:
        return tuple(
            f"{issue.path} [{issue.code}]: {issue.detail}"
            for issue in self.unresolved
        )

    def require_definition(self) -> ReferenceCatalogDefinition:
        if self.can_apply and self.definition is not None:
            return self.definition
        raise SiurSettingsPromotionError(
            "; ".join(self.blocking_issues[:20]) or "analysis is not promotable"
        )


def analyze_siur_settings(
    document: bytes,
    *,
    baseline: SiurSettingsBaseline | None = None,
    limits: SiurSettingsLimits = SiurSettingsLimits(),
    source_url: str = DEFAULT_SOURCE_URL,
    retrieved_at: datetime | None = None,
) -> SiurSettingsAnalysis:
    if not isinstance(document, bytes):
        raise SiurSettingsError("settings input must be bytes")
    if not document or len(document) > limits.max_bytes:
        raise SiurSettingsError("settings size is outside the allowed range")
    raw_sha = hashlib.sha256(document).hexdigest()
    raw = _load_json(document, limits)
    timestamp = retrieved_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")

    parser = _Parser(raw)
    parser.parse()
    parser.check_baseline(baseline, raw_sha)
    definition = None
    if not parser.issues and isinstance(raw, dict):
        candidate = ReferenceCatalogDefinition(
            provider_key="siur",
            source_url=source_url,
            raw_catalog=raw,
            services=tuple(parser.service_definitions.values()),
            layers=tuple(parser.layer_definitions),
            unresolved_count=0,
            retrieved_at=timestamp,
        )
        for issue in validate_catalog_definition(candidate):
            parser.issue("$definition", "invalid_definition", issue)
        if not parser.issues:
            definition = candidate
    return SiurSettingsAnalysis(
        raw_sha256=raw_sha,
        raw_size_bytes=len(document),
        services=tuple(parser.service_evidence.values()),
        nodes=tuple(parser.nodes),
        unresolved=tuple(sorted(set(parser.issues))),
        definition=definition,
    )


class _DuplicateKey(ValueError):
    pass


def _load_json(document: bytes, limits: SiurSettingsLimits) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise _DuplicateKey(key)
            result[key] = value
        return result

    try:
        root = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except _DuplicateKey as exc:
        raise SiurSettingsError(f"duplicate JSON key: {exc}") from exc
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise SiurSettingsError("settings is not strict UTF-8 JSON") from exc

    stack: list[tuple[Any, str, int]] = [(root, "$", 0)]
    count = 0
    while stack:
        value, path, depth = stack.pop()
        count += 1
        if count > limits.max_nodes:
            raise SiurSettingsError("settings contains too many JSON nodes")
        if depth > limits.max_depth:
            raise SiurSettingsError("settings nesting is too deep")
        if isinstance(value, dict):
            stack.extend(
                (child, _path(path, key), depth + 1)
                for key, child in value.items()
            )
        elif isinstance(value, list):
            stack.extend(
                (child, f"{path}[{index}]", depth + 1)
                for index, child in enumerate(value)
            )
        elif isinstance(value, str) and len(value) > limits.max_string_chars:
            raise SiurSettingsError(f"oversized string at {path}")
        elif isinstance(value, float) and not math.isfinite(value):
            raise SiurSettingsError(f"non-finite number at {path}")
    return root


_MISSING = object()
_CONFLICT = object()


class _Reader:
    def __init__(
        self, parser: "_Parser", value: Mapping[str, Any], path: str, kind: str
    ) -> None:
        self.parser, self.value, self.path, self.kind = parser, value, path, kind
        self.used: set[str] = set()

    def take(self, label: str, *aliases: str) -> Any:
        wanted = {_key(alias) for alias in aliases}
        matches = [
            (key, value)
            for key, value in self.value.items()
            if _key(key) in wanted
        ]
        self.used.update(key for key, _ in matches)
        if not matches:
            return _MISSING
        if len({_canonical(value) for _, value in matches}) != 1:
            self.parser.issue(
                self.path,
                "conflicting_aliases",
                f"{label}: {', '.join(k for k, _ in matches)}",
            )
            return _CONFLICT
        return matches[0][1]

    def text(self, label: str, *aliases: str) -> str | None:
        value = self.take(label, *aliases)
        if value is _MISSING or value is _CONFLICT:
            return None
        if not isinstance(value, str) or not value.strip():
            self.parser.issue(self.path, "invalid_text", label)
            return None
        return value.strip()

    def finish(self) -> None:
        for key in self.value:
            if key not in self.used:
                self.parser.issue(
                    _path(self.path, key),
                    "unrecognized_field",
                    f"unrecognized {self.kind} field",
                )


class _Parser:
    SERVICE_CONTAINERS = {"services", "mapservices", "servers", "servicios"}
    NODE_CONTAINERS = {
        "groups",
        "layergroups",
        "categories",
        "grupos",
        "layers",
        "maplayers",
        "overlays",
        "capas",
        "children",
        "items",
        "nodes",
    }
    WRAPPERS = {"catalog", "catalogue", "layertree", "toc"}
    ROOT_META = {"id", "key", "name", "title", "version", "settingsversion"}

    def __init__(self, root: Any) -> None:
        self.root = root
        self.issues: list[SiurSettingsUnresolved] = []
        self.service_definitions: dict[str, ReferenceServiceDefinition] = {}
        self.service_evidence: dict[str, SiurSettingsServiceEvidence] = {}
        self.service_refs: dict[str, str] = {}
        self.layer_definitions: list[ReferenceLayerDefinition] = []
        self.nodes: list[SiurSettingsNodeEvidence] = []
        self.identities: dict[str, str] = {}

    def issue(self, path: str, code: str, detail: str) -> None:
        self.issues.append(SiurSettingsUnresolved(path, code, detail))

    def parse(self) -> None:
        if not isinstance(self.root, dict):
            self.issue("$", "unsupported_root", "root must be an object")
            return
        native_markers = {
            key for key in self.root if _key(key) in {"settings", "selectedsetting"}
        }
        if native_markers:
            self._native_catalog()
        else:
            self._catalog(self.root, "$", None, True, root=True)
        if not self.nodes:
            self.issue("$", "catalog_missing", "no recognized groups or layers")

    def _native_catalog(self) -> None:
        root = self._native_object(
            self.root,
            "$",
            allowed={"settings", "selectedSetting"},
            required={"settings", "selectedSetting"},
            kind="SIUR root",
        )
        if root is None:
            return
        selected = self._native_text(root.get("selectedSetting"), "$.selectedSetting")
        settings = self._native_list(root.get("settings"), "$.settings")
        if settings is None:
            return
        if len(settings) != 1:
            self.issue(
                "$.settings",
                "unsupported_settings_count",
                "exactly one SIUR setting is supported",
            )
        matches: list[tuple[Mapping[str, Any], str]] = []
        for index, value in enumerate(settings):
            path = f"$.settings[{index}]"
            setting = self._native_object(
                value,
                path,
                allowed={
                    "name",
                    "wmcUrl",
                    "suggestedServices",
                    "groupLayers",
                    "favoritesLayers",
                    "tematicSearch",
                    "backMaps",
                    "apps",
                },
                required={
                    "name",
                    "wmcUrl",
                    "suggestedServices",
                    "groupLayers",
                    "favoritesLayers",
                    "tematicSearch",
                    "backMaps",
                    "apps",
                },
                kind="SIUR setting",
            )
            if setting is None:
                continue
            name = self._native_text(setting.get("name"), f"{path}.name")
            if selected is not None and name == selected:
                matches.append((setting, path))
        if selected is None or len(matches) != 1:
            self.issue(
                "$.selectedSetting",
                "selected_setting_unresolved",
                "selectedSetting must identify exactly one setting",
            )
            return
        setting, path = matches[0]
        self._native_setting(setting, path)

    def _native_setting(self, setting: Mapping[str, Any], path: str) -> None:
        wmc_url = self._native_text(setting.get("wmcUrl"), f"{path}.wmcUrl")
        if wmc_url is not None and not _safe_relative_path(wmc_url, suffix=".xml"):
            self.issue(
                f"{path}.wmcUrl",
                "invalid_relative_url",
                "a safe relative XML path is required",
            )

        self._native_suggested_services(
            setting.get("suggestedServices"), f"{path}.suggestedServices"
        )
        self._native_favorites(
            setting.get("favoritesLayers"), f"{path}.favoritesLayers"
        )
        self._native_thematic_search(
            setting.get("tematicSearch"), f"{path}.tematicSearch"
        )
        self._native_apps(setting.get("apps"), f"{path}.apps")

        group_root = self._native_object(
            setting.get("groupLayers"),
            f"{path}.groupLayers",
            allowed={"name", "children"},
            required={"name", "children"},
            kind="groupLayers root",
        )
        top_level_count = 0
        if group_root is not None:
            name = self._native_text(
                group_root.get("name"), f"{path}.groupLayers.name"
            )
            if name is not None and name != "root":
                self.issue(
                    f"{path}.groupLayers.name",
                    "unsupported_group_root",
                    "the non-persistent groupLayers wrapper must be named root",
                )
            children = self._native_list(
                group_root.get("children"), f"{path}.groupLayers.children"
            )
            if children is not None:
                top_level_count = len(children)
                self._native_nodes(
                    children,
                    f"{path}.groupLayers.children",
                    parent=None,
                    top_level=True,
                    breadcrumb=(),
                )
        self._native_back_maps(
            setting.get("backMaps"),
            f"{path}.backMaps",
            sort_offset=top_level_count,
        )

    def _native_nodes(
        self,
        values: list[Any],
        path: str,
        *,
        parent: str | None,
        top_level: bool,
        breadcrumb: tuple[str, ...],
    ) -> None:
        sibling_names: dict[str, str] = {}
        for order, value in enumerate(values):
            item_path = f"{path}[{order}]"
            if not isinstance(value, Mapping):
                self.issue(item_path, "invalid_entry", "object required")
                continue
            name_value = value.get("name")
            if isinstance(name_value, str) and name_value.strip():
                normalized = _native_name(name_value)
                previous = sibling_names.setdefault(normalized, item_path)
                if previous != item_path:
                    self.issue(item_path, "duplicate_sibling_name", previous)
            has_children = "children" in value
            has_endpoint = "endPoint" in value
            if has_children == has_endpoint:
                self.issue(
                    item_path,
                    "invalid_native_node",
                    "exactly one of children or endPoint is required",
                )
                continue
            if has_children:
                self._native_group(
                    value,
                    item_path,
                    parent=parent,
                    top_level=top_level,
                    order=order,
                    breadcrumb=breadcrumb,
                )
            else:
                self._native_layer(
                    value,
                    item_path,
                    parent=parent,
                    order=order,
                    breadcrumb=breadcrumb,
                )

    def _native_group(
        self,
        value: Mapping[str, Any],
        path: str,
        *,
        parent: str | None,
        top_level: bool,
        order: int,
        breadcrumb: tuple[str, ...],
    ) -> None:
        group = self._native_object(
            value,
            path,
            allowed={"name", "children"},
            required={"name", "children"},
            kind="group",
        )
        if group is None:
            return
        title = self._native_text(group.get("name"), f"{path}.name")
        children = self._native_list(group.get("children"), f"{path}.children")
        lineage = breadcrumb + ((title or ""),)
        source_key = (
            self._identity(
                "group",
                "native\0" + "\0".join(_native_name(item) for item in lineage),
                path,
            )
            if title
            else None
        )
        self.nodes.append(
            SiurSettingsNodeEvidence(
                path=path,
                kind="group",
                source_key=source_key,
                source_id=" / ".join(lineage) if title else None,
                title=title,
                parent_key=parent,
                top_level=top_level,
            )
        )
        if source_key and title:
            self.layer_definitions.append(
                ReferenceLayerDefinition(
                    source_key=source_key,
                    node_type="group",
                    title=title,
                    parent_key=parent,
                    sort_order=order,
                )
            )
        if children is not None:
            self._native_nodes(
                children,
                f"{path}.children",
                parent=source_key,
                top_level=False,
                breadcrumb=lineage,
            )

    def _native_layer(
        self,
        value: Mapping[str, Any],
        path: str,
        *,
        parent: str | None,
        order: int,
        breadcrumb: tuple[str, ...],
    ) -> None:
        leaf = self._native_object(
            value,
            path,
            allowed={"name", "endPoint"},
            required={"name", "endPoint"},
            kind="layer placement",
        )
        if leaf is None:
            return
        title = self._native_text(leaf.get("name"), f"{path}.name")
        endpoint = self._native_object(
            leaf.get("endPoint"),
            f"{path}.endPoint",
            allowed={"type", "url", "layer"},
            required={"url", "layer"},
            kind="layer endpoint",
        )
        if endpoint is None:
            return
        raw_type = endpoint.get("type", "wms")
        type_text = self._native_text(raw_type, f"{path}.endPoint.type")
        protocol = _protocol(type_text or "")
        if protocol not in PROTOCOLS:
            self.issue(
                f"{path}.endPoint.type",
                "layer_protocol_missing",
                "unsupported SIUR endpoint type",
            )
        url = self._native_text(endpoint.get("url"), f"{path}.endPoint.url")
        version = (
            self._native_service_version(url, f"{path}.endPoint.url")
            if url and protocol == "wms"
            else None
        )
        service_key = (
            self._embedded_service(
                url,
                protocol,
                f"{path}.endPoint#service",
                version=version,
            )
            if url and protocol
            else None
        )
        layer = self._native_object(
            endpoint.get("layer"),
            f"{path}.endPoint.layer",
            allowed={"name", "extent", "styles", "legend", "metadata"},
            required={"name", "extent", "styles", "metadata"},
            kind="layer definition",
        )
        if layer is None:
            return
        remote = self._native_text(
            layer.get("name"), f"{path}.endPoint.layer.name"
        )
        extent = self._native_extent(
            layer.get("extent"), f"{path}.endPoint.layer.extent"
        )
        styles, selected_style = self._native_styles(
            layer.get("styles"), f"{path}.endPoint.layer.styles"
        )
        metadata_url = self._native_metadata(
            layer.get("metadata"), f"{path}.endPoint.layer.metadata"
        )
        legend_url = None
        image_format = None
        if "legend" in layer:
            legend_url, image_format = self._native_legend(
                layer.get("legend"), f"{path}.endPoint.layer.legend"
            )

        lineage = breadcrumb + ((title or ""),)
        identity = "\0".join(
            (
                "native",
                service_key or "",
                protocol or "",
                remote or "",
                *(_native_name(item) for item in lineage),
            )
        )
        source_key = (
            self._identity("layer", identity, path)
            if service_key and protocol and remote and title
            else None
        )
        self.nodes.append(
            SiurSettingsNodeEvidence(
                path=path,
                kind="layer",
                source_key=source_key,
                source_id=remote,
                title=title,
                parent_key=parent,
                service_key=service_key,
                remote_name=remote,
            )
        )
        renderer = (
            "raster_tile"
            if protocol in RASTER
            else "vector_tile"
            if protocol == "wfs"
            else None
        )
        if source_key and title and service_key and remote and renderer:
            options: dict[str, Any] = {
                "settings_path": path,
                "source_extent": extent,
            }
            if legend_url and image_format:
                options["source_legend"] = {
                    "url": legend_url,
                    "format": image_format,
                }
            self.layer_definitions.append(
                ReferenceLayerDefinition(
                    source_key=source_key,
                    node_type="layer",
                    title=title,
                    parent_key=parent,
                    service_key=service_key,
                    remote_name=remote,
                    role="overlay",
                    renderer=renderer,
                    delivery_mode="proxy",
                    style_name=selected_style,
                    image_format=None,
                    bounds=None,
                    options=options,
                    sort_order=order,
                    default_visible=False,
                    default_opacity=Decimal("1"),
                    queryable=False,
                    downloadable=False,
                    legend_url=legend_url,
                    metadata_url=metadata_url,
                    status="active",
                    styles=styles,
                )
            )

    def _native_back_maps(
        self,
        value: Any,
        path: str,
        *,
        sort_offset: int,
    ) -> None:
        entries = self._native_list(value, path)
        if entries is None:
            return
        sentinel_count = 0
        persisted_order = 0
        for index, value in enumerate(entries):
            item_path = f"{path}[{index}]"
            if isinstance(value, Mapping) and set(value) == {"name"}:
                name = self._native_text(value.get("name"), f"{item_path}.name")
                if name != "SIN FONDO":
                    self.issue(
                        item_path,
                        "unsupported_back_map_marker",
                        "only the exact SIN FONDO marker may omit an endpoint",
                    )
                else:
                    sentinel_count += 1
                continue
            item = self._native_object(
                value,
                item_path,
                allowed={"name", "url", "layer", "type"},
                required={"name", "url", "layer", "type"},
                kind="background map",
            )
            if item is None:
                continue
            title = self._native_text(item.get("name"), f"{item_path}.name")
            url = self._native_text(item.get("url"), f"{item_path}.url")
            remote = self._native_text(item.get("layer"), f"{item_path}.layer")
            raw_type = self._native_text(item.get("type"), f"{item_path}.type")
            protocol = _protocol(raw_type or "")
            if protocol not in {"wmts", "xyz"}:
                self.issue(
                    f"{item_path}.type",
                    "unsupported_back_map_protocol",
                    "only WMTS and XYZ backgrounds are supported",
                )
            service_key = (
                self._embedded_service(url, protocol, f"{item_path}#service")
                if url and protocol
                else None
            )
            source_key = (
                self._identity(
                    "layer",
                    "\0".join(
                        ("native-back-map", service_key or "", remote or "")
                    ),
                    item_path,
                )
                if title and service_key and remote
                else None
            )
            self.nodes.append(
                SiurSettingsNodeEvidence(
                    path=item_path,
                    kind="layer",
                    source_key=source_key,
                    source_id=remote,
                    title=title,
                    parent_key=None,
                    service_key=service_key,
                    remote_name=remote,
                )
            )
            if source_key and title and service_key and remote and protocol:
                self.layer_definitions.append(
                    ReferenceLayerDefinition(
                        source_key=source_key,
                        node_type="layer",
                        title=title,
                        service_key=service_key,
                        remote_name=remote,
                        role="base",
                        renderer="raster_tile",
                        delivery_mode="proxy",
                        options={"settings_path": item_path},
                        sort_order=sort_offset + persisted_order,
                        default_visible=persisted_order == 0,
                        default_opacity=Decimal("1"),
                        queryable=False,
                        downloadable=False,
                        status="active",
                    )
                )
            persisted_order += 1
        if sentinel_count != 1:
            self.issue(
                path,
                "back_map_marker_mismatch",
                "exactly one SIN FONDO marker is required",
            )

    def _native_extent(self, value: Any, path: str) -> dict[str, str] | None:
        extent = self._native_object(
            value,
            path,
            allowed={"srs", "minx", "miny", "maxx", "maxy"},
            required={"srs", "minx", "miny", "maxx", "maxy"},
            kind="extent",
        )
        if extent is None:
            return None
        crs = self._native_text(extent.get("srs"), f"{path}.srs")
        if crs is not None and not CRS_RE.fullmatch(crs):
            self.issue(f"{path}.srs", "invalid_crs", crs)
        raw_values: dict[str, str] = {}
        numbers: dict[str, Decimal] = {}
        for key in ("minx", "miny", "maxx", "maxy"):
            raw = extent.get(key)
            if not isinstance(raw, str) or not raw.strip():
                self.issue(f"{path}.{key}", "invalid_extent", "decimal text required")
                continue
            try:
                number = Decimal(raw)
            except InvalidOperation:
                number = Decimal("NaN")
            if not number.is_finite():
                self.issue(f"{path}.{key}", "invalid_extent", "finite decimal required")
                continue
            raw_values[key] = raw
            numbers[key] = number
        if (
            set(numbers) == {"minx", "miny", "maxx", "maxy"}
            and not (
                numbers["minx"] < numbers["maxx"]
                and numbers["miny"] < numbers["maxy"]
            )
        ):
            self.issue(path, "invalid_extent", "minimums must be below maximums")
        if crs is None or len(raw_values) != 4:
            return None
        return {"srs": crs.upper(), **raw_values}

    def _native_styles(
        self, value: Any, path: str
    ) -> tuple[tuple[ReferenceLayerStyleDefinition, ...], str | None]:
        entries = self._native_list(value, path)
        if entries is None:
            return (), None
        if not entries or len(entries) > 256:
            self.issue(path, "invalid_style_count", "between 1 and 256 styles required")
        definitions: list[ReferenceLayerStyleDefinition] = []
        keys: set[str] = set()
        selected: str | None = None
        for order, value in enumerate(entries):
            item_path = f"{path}[{order}]"
            item = self._native_object(
                value,
                item_path,
                allowed={"name", "title"},
                required={"name", "title"},
                kind="style",
            )
            if item is None:
                continue
            name = item.get("name")
            title = self._native_text(item.get("title"), f"{item_path}.title")
            if not isinstance(name, str):
                self.issue(f"{item_path}.name", "invalid_style", "text required")
                continue
            if name == "":
                if len(entries) != 1:
                    self.issue(
                        f"{item_path}.name",
                        "unsupported_implicit_style",
                        "an implicit WMS style must be the only style",
                    )
                continue
            key = name.casefold()
            if not REMOTE_STYLE_RE.fullmatch(name) or not KEY_RE.fullmatch(key):
                self.issue(f"{item_path}.name", "invalid_style", name)
                continue
            if key in keys:
                self.issue(f"{item_path}.name", "duplicate_style", key)
                continue
            keys.add(key)
            if order == 0:
                selected = key
            if title:
                definitions.append(
                    ReferenceLayerStyleDefinition(
                        source_key=key,
                        title=title,
                        remote_name=name,
                        sort_order=order,
                        is_default=order == 0,
                    )
                )
        return tuple(definitions), selected

    def _native_legend(self, value: Any, path: str) -> tuple[str | None, str | None]:
        legend = self._native_object(
            value,
            path,
            allowed={"url", "format"},
            required={"url", "format"},
            kind="legend",
        )
        if legend is None:
            return None, None
        url = self._native_reference_url(legend.get("url"), f"{path}.url")
        image_format = self._native_text(legend.get("format"), f"{path}.format")
        if image_format is not None and not re.fullmatch(
            r"image/[A-Za-z0-9.+-]{1,64}", image_format
        ):
            self.issue(f"{path}.format", "invalid_image_format", image_format)
            image_format = None
        return url, image_format

    def _native_metadata(self, value: Any, path: str) -> str | None:
        metadata = self._native_object(
            value,
            path,
            allowed={"url"},
            required={"url"},
            kind="metadata",
        )
        if metadata is None:
            return None
        return self._native_reference_url(
            metadata.get("url"), f"{path}.url", allow_fragment=True
        )

    def _native_suggested_services(self, value: Any, path: str) -> None:
        section = self._native_object(
            value,
            path,
            allowed={"wms", "wfs", "wmts"},
            required={"wms", "wfs", "wmts"},
            kind="suggested services",
        )
        if section is None:
            return
        for protocol in ("wms", "wfs", "wmts"):
            entries = self._native_list(section.get(protocol), f"{path}.{protocol}")
            if entries is None:
                continue
            for index, url in enumerate(entries):
                self._native_reference_url(url, f"{path}.{protocol}[{index}]")

    def _native_favorites(self, value: Any, path: str) -> None:
        section = self._native_object(
            value,
            path,
            allowed={"categories"},
            required={"categories"},
            kind="favorites",
        )
        if section is None:
            return
        categories = self._native_list(section.get("categories"), f"{path}.categories")
        if categories is None:
            return
        for category_index, value in enumerate(categories):
            category_path = f"{path}.categories[{category_index}]"
            category = self._native_object(
                value,
                category_path,
                allowed={"name", "servers"},
                required={"name", "servers"},
                kind="favorite category",
            )
            if category is None:
                continue
            self._native_text(category.get("name"), f"{category_path}.name")
            servers = self._native_list(
                category.get("servers"), f"{category_path}.servers"
            )
            if servers is None:
                continue
            for server_index, server_value in enumerate(servers):
                server_path = f"{category_path}.servers[{server_index}]"
                server = self._native_object(
                    server_value,
                    server_path,
                    allowed={"url", "layers"},
                    required={"url", "layers"},
                    kind="favorite server",
                )
                if server is None:
                    continue
                self._native_reference_url(server.get("url"), f"{server_path}.url")
                layers = self._native_list(server.get("layers"), f"{server_path}.layers")
                if layers is not None:
                    for layer_index, layer_name in enumerate(layers):
                        self._native_text(
                            layer_name, f"{server_path}.layers[{layer_index}]"
                        )

    def _native_thematic_search(self, value: Any, path: str) -> None:
        section = self._native_object(
            value,
            path,
            allowed={"themes"},
            required={"themes"},
            kind="thematic search",
        )
        if section is None:
            return
        themes = self._native_list(section.get("themes"), f"{path}.themes")
        if themes is None:
            return
        for theme_index, value in enumerate(themes):
            theme_path = f"{path}.themes[{theme_index}]"
            if not isinstance(value, Mapping):
                self.issue(theme_path, "invalid_entry", "object required")
                continue
            has_layers = "layers" in value
            has_categories = "categories" in value
            allowed = {"name", "layers"} if has_layers else {"name", "categories"}
            theme = self._native_object(
                value,
                theme_path,
                allowed=allowed,
                required=allowed,
                kind="thematic search theme",
            )
            if theme is None:
                continue
            if has_layers == has_categories:
                self.issue(
                    theme_path,
                    "invalid_thematic_theme",
                    "exactly one of layers or categories is required",
                )
                continue
            self._native_text(theme.get("name"), f"{theme_path}.name")
            if has_layers:
                self._native_search_layers(theme.get("layers"), f"{theme_path}.layers")
                continue
            categories = self._native_list(
                theme.get("categories"), f"{theme_path}.categories"
            )
            if categories is None:
                continue
            for category_index, category_value in enumerate(categories):
                category_path = f"{theme_path}.categories[{category_index}]"
                category = self._native_object(
                    category_value,
                    category_path,
                    allowed={"name", "layers"},
                    required={"name", "layers"},
                    kind="thematic search category",
                )
                if category is None:
                    continue
                self._native_text(category.get("name"), f"{category_path}.name")
                self._native_search_layers(
                    category.get("layers"), f"{category_path}.layers"
                )

    def _native_search_layers(self, value: Any, path: str) -> None:
        layers = self._native_list(value, path)
        if layers is None:
            return
        for layer_index, value in enumerate(layers):
            layer_path = f"{path}[{layer_index}]"
            layer = self._native_object(
                value,
                layer_path,
                allowed={"name", "url", "layer", "style", "properties"},
                required={"name", "url", "layer", "style", "properties"},
                kind="thematic search layer",
            )
            if layer is None:
                continue
            for key in ("name", "layer", "style"):
                self._native_text(layer.get(key), f"{layer_path}.{key}")
            self._native_reference_url(layer.get("url"), f"{layer_path}.url")
            properties = self._native_list(
                layer.get("properties"), f"{layer_path}.properties"
            )
            if properties is None:
                continue
            for property_index, value in enumerate(properties):
                property_path = f"{layer_path}.properties[{property_index}]"
                item = self._native_object(
                    value,
                    property_path,
                    allowed={"name", "label", "type", "required"},
                    required={"name", "label", "type", "required"},
                    kind="thematic search property",
                )
                if item is None:
                    continue
                for key in ("name", "label", "type"):
                    self._native_text(item.get(key), f"{property_path}.{key}")
                if not isinstance(item.get("required"), bool):
                    self.issue(
                        f"{property_path}.required",
                        "invalid_boolean",
                        "boolean required",
                    )

    def _native_apps(self, value: Any, path: str) -> None:
        apps = self._native_list(value, path)
        if apps is None:
            return
        for index, value in enumerate(apps):
            item_path = f"{path}[{index}]"
            app = self._native_object(
                value,
                item_path,
                allowed={"name", "image", "external", "url"},
                required={"name", "image", "external", "url"},
                kind="application link",
            )
            if app is None:
                continue
            self._native_text(app.get("name"), f"{item_path}.name")
            image = self._native_text(app.get("image"), f"{item_path}.image")
            if image is not None and not _safe_relative_path(image):
                self.issue(
                    f"{item_path}.image",
                    "invalid_relative_url",
                    "safe relative asset required",
                )
            external = app.get("external")
            if not isinstance(external, bool):
                self.issue(f"{item_path}.external", "invalid_boolean", "boolean required")
                continue
            if external:
                self._native_reference_url(app.get("url"), f"{item_path}.url")
            else:
                route = self._native_text(app.get("url"), f"{item_path}.url")
                if route is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", route):
                    self.issue(
                        f"{item_path}.url",
                        "invalid_internal_application",
                        "component identifier required",
                    )

    def _native_service_version(self, url: str, path: str) -> str | None:
        try:
            versions = [
                value
                for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
                if key.casefold() == "version"
            ]
        except ValueError:
            return None
        if not versions:
            return None
        if len(set(versions)) != 1 or versions[0] not in {"1.1.1", "1.3.0"}:
            self.issue(path, "invalid_service_version", "unsupported WMS version")
            return None
        return versions[0]

    def _native_reference_url(
        self, value: Any, path: str, *, allow_fragment: bool = False
    ) -> str | None:
        url = self._native_text(value, path)
        if url is not None and not _safe_http_reference(
            url, allow_fragment=allow_fragment
        ):
            self.issue(path, "invalid_reference_url", "URL rejected")
            return None
        return url

    def _native_object(
        self,
        value: Any,
        path: str,
        *,
        allowed: set[str],
        required: set[str],
        kind: str,
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            self.issue(path, "invalid_entry", f"{kind} object required")
            return None
        for key in value:
            if key not in allowed:
                self.issue(
                    _path(path, key),
                    "unrecognized_field",
                    f"unrecognized {kind} field",
                )
        for key in sorted(required - set(value)):
            self.issue(_path(path, key), "missing_field", f"required {kind} field")
        return value

    def _native_list(self, value: Any, path: str) -> list[Any] | None:
        if not isinstance(value, list):
            self.issue(path, "invalid_container", "list required")
            return None
        return value

    def _native_text(
        self, value: Any, path: str, *, maximum: int = 500
    ) -> str | None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > maximum
            or any(ord(character) < 32 for character in value)
        ):
            self.issue(path, "invalid_text", "non-empty text required")
            return None
        return value.strip()

    def _catalog(
        self,
        value: Mapping[str, Any],
        path: str,
        parent: str | None,
        top_level: bool,
        *,
        root: bool = False,
    ) -> None:
        used: set[str] = set()
        # Registries must be resolved before layers reference them.
        for key, child in value.items():
            if _key(key) in self.SERVICE_CONTAINERS:
                used.add(key)
                for item, item_path, hint in self._entries(child, _path(path, key)):
                    self._service(item, item_path, hint)
        for key, child in value.items():
            normalized = _key(key)
            if normalized in self.NODE_CONTAINERS:
                used.add(key)
                self._nodes(child, _path(path, key), parent, top_level)
            elif normalized in self.WRAPPERS:
                used.add(key)
                if isinstance(child, Mapping):
                    self._catalog(child, _path(path, key), parent, top_level)
                else:
                    self.issue(_path(path, key), "invalid_wrapper", "object required")
            elif root and normalized in self.ROOT_META:
                used.add(key)
        for key in value:
            if key not in used:
                self.issue(
                    _path(path, key), "unrecognized_field", "unrecognized catalog field"
                )

    def _nodes(self, value: Any, path: str, parent: str | None, top: bool) -> None:
        for order, (item, item_path, hint) in enumerate(self._entries(value, path)):
            kind_value = next(
                (
                    child for key, child in item.items()
                    if _key(key) in {"kind", "nodetype", "type"}
                    and isinstance(child, str)
                ),
                "",
            )
            explicit_kind = _key(kind_value)
            has_children = any(
                _key(key) in self.NODE_CONTAINERS and isinstance(child, (list, dict))
                for key, child in item.items()
            )
            if explicit_kind in {"group", "category"} or has_children:
                self._group(item, item_path, hint, parent, top, order)
            else:
                self._layer(item, item_path, hint, parent, order)

    def _group(
        self,
        value: Mapping[str, Any],
        path: str,
        hint: str | None,
        parent: str | None,
        top: bool,
        order: int,
    ) -> None:
        reader = _Reader(self, value, path, "group")
        source_id = reader.text("id", "id", "key", "code", "uid", "identifier") or hint
        title = reader.text("title", "title", "label", "displayName", "text")
        name = reader.text("name", "name")
        title = title or name
        if source_id is None and name and any(
            _key(key) in {"title", "label", "displayname", "text"}
            for key in value
        ):
            source_id = name
        if not title:
            self.issue(path, "group_title_missing", "title is required")
        source_key = (
            self._identity("group", f"{parent or 'root'}\0{source_id}", path)
            if source_id
            else None
        )
        if not source_id:
            self.issue(path, "group_identity_missing", "explicit id/key/code required")
        node = SiurSettingsNodeEvidence(
            path, "group", source_key, source_id, title, parent, top_level=top
        )
        self.nodes.append(node)
        description = reader.text("description", "description", "abstract", "summary")
        sort_order = _integer(
            self,
            reader.take("order", "order", "sortOrder", "position"),
            path,
            order,
        )
        kind = reader.text("kind", "kind", "nodeType", "type")
        if kind and _key(kind) not in {"group", "category"}:
            self.issue(path, "kind_conflict", kind)
        if source_key and title:
            self.layer_definitions.append(
                ReferenceLayerDefinition(
                    source_key=source_key,
                    node_type="group",
                    title=title,
                    parent_key=parent,
                    description=description,
                    sort_order=sort_order,
                )
            )
        child_mapping = {
            key: child
            for key, child in value.items()
            if _key(key)
            in self.SERVICE_CONTAINERS | self.NODE_CONTAINERS | self.WRAPPERS
        }
        reader.used.update(child_mapping)
        if child_mapping:
            self._catalog(child_mapping, path, source_key, False)
        reader.finish()

    def _layer(
        self,
        value: Mapping[str, Any],
        path: str,
        hint: str | None,
        parent: str | None,
        order: int,
    ) -> None:
        reader = _Reader(self, value, path, "layer")
        source_id = reader.text("id", "id", "key", "code", "uid", "identifier") or hint
        title = reader.text("title", "title", "label", "displayName", "text")
        name = reader.text("name", "name")
        title = title or name
        remote = reader.text(
            "remote name", "layer", "layerName", "remoteName", "typeName", "layers"
        )
        if not title:
            self.issue(path, "layer_title_missing", "title is required")
        protocol = _protocol(
            reader.text("protocol", "protocol", "serviceType", "serverType") or ""
        )
        raw_type = reader.text("type", "type")
        if raw_type:
            if _protocol(raw_type):
                if protocol and protocol != _protocol(raw_type):
                    self.issue(path, "protocol_conflict", raw_type)
                protocol = protocol or _protocol(raw_type)
            elif _key(raw_type) not in {"layer", "overlay"}:
                self.issue(path, "kind_conflict", raw_type)

        service_ref = reader.text(
            "service ref", "serviceId", "serviceKey", "serverId", "serverKey"
        )
        service_value = reader.take("service", "service", "server", "source")
        service_key = None
        if isinstance(service_value, str):
            if _absolute_url(service_value):
                service_key = self._embedded_service(
                    service_value, protocol, f"{path}.service"
                )
            elif _protocol(service_value):
                protocol = protocol or _protocol(service_value)
            else:
                service_ref = service_ref or service_value
        elif service_value is not _MISSING and service_value is not _CONFLICT:
            self.issue(
                path,
                "unsupported_embedded_service",
                "only text references are supported",
            )
        direct_url = reader.text(
            "service URL", "url", "baseUrl", "serviceUrl", "endpoint", "href"
        )
        if direct_url:
            direct_key = self._embedded_service(direct_url, protocol, f"{path}#service")
            if service_key not in {None, direct_key}:
                self.issue(path, "multiple_services", "service and URL differ")
            service_key = direct_key or service_key
        if service_ref:
            resolved = self.service_refs.get(service_ref.casefold())
            if not resolved:
                self.issue(path, "unresolved_service_reference", service_ref)
            elif service_key not in {None, resolved}:
                self.issue(path, "multiple_services", service_ref)
            else:
                service_key = resolved
        if service_key:
            service_protocol = self.service_evidence[service_key].protocol
            if protocol and service_protocol and protocol != service_protocol:
                self.issue(path, "protocol_conflict", "layer and service differ")
            protocol = protocol or service_protocol
        if not service_key:
            self.issue(path, "layer_service_missing", "service is unresolved")
        if protocol not in PROTOCOLS:
            self.issue(path, "layer_protocol_missing", "protocol is unresolved")
        if protocol in {"wms", "wfs", "wmts"} and not remote:
            self.issue(path, "remote_name_missing", "OGC layer name is required")
        remote = remote or (source_id if protocol in {"xyz", "arcgis_rest"} else None)
        identity = f"{service_key}\0{protocol}\0{remote}\0{source_id or ''}"
        source_key = (
            self._identity("layer", identity, path)
            if service_key and protocol and remote
            else None
        )

        visible = _boolean(
            self,
            reader.take("visible", "visible", "defaultVisible", "checked"),
            path,
            False,
        )
        queryable = _boolean(
            self,
            reader.take("queryable", "queryable", "isQueryable", "identify"),
            path,
            False,
        )
        downloadable = _boolean(
            self,
            reader.take("downloadable", "downloadable", "download"),
            path,
            False,
        )
        enabled = _boolean(
            self, reader.take("enabled", "enabled", "active"), path, True
        )
        base = _boolean(
            self,
            reader.take("base", "base", "baseLayer", "isBaseLayer"),
            path,
            False,
        )
        opacity = _opacity(
            self,
            reader.take("opacity", "opacity", "defaultOpacity", "alpha"),
            path,
        )
        role = reader.text("role", "role", "layerRole") or (
            "base" if base else "overlay"
        )
        if role not in {"base", "overlay"}:
            self.issue(path, "invalid_role", role)
        crs = _crs(
            self,
            reader.take("CRS", "crs", "srs", "projection", "supportedCrs"),
            path,
        )
        image_format = reader.text("format", "format", "imageFormat", "mimeType")
        style = reader.text("style", "style", "styleName", "defaultStyle")
        if style and not KEY_RE.fullmatch(style.casefold()):
            self.issue(path, "invalid_style", style)
            style = None
        description = reader.text("description", "description", "abstract", "summary")
        sort_order = _integer(
            self,
            reader.take("order", "order", "sortOrder", "position"),
            path,
            order,
        )
        renderer = (
            "raster_tile"
            if protocol in RASTER
            else "vector_tile"
            if protocol == "wfs"
            else None
        )
        self.nodes.append(
            SiurSettingsNodeEvidence(
                path,
                "layer",
                source_key,
                source_id,
                title,
                parent,
                service_key,
                remote,
                False,
            )
        )
        if (
            source_key
            and title
            and service_key
            and renderer
            and role in {"base", "overlay"}
        ):
            self.layer_definitions.append(
                ReferenceLayerDefinition(
                    source_key=source_key,
                    node_type="layer",
                    title=title,
                    parent_key=parent,
                    service_key=service_key,
                    description=description,
                    remote_name=remote,
                    role=role,
                    renderer=renderer,
                    delivery_mode="proxy",
                    style_name=style.casefold() if style else None,
                    image_format=image_format,
                    supported_crs=crs,
                    options={"settings_path": path},
                    sort_order=sort_order,
                    default_visible=visible,
                    default_opacity=opacity,
                    queryable=queryable,
                    downloadable=downloadable,
                    status="active" if enabled else "disabled",
                    styles=(
                        ReferenceLayerStyleDefinition(
                            source_key=style.casefold(),
                            title=style,
                            remote_name=style,
                            is_default=True,
                        ),
                    )
                    if style
                    else (),
                )
            )
        reader.finish()

    def _service(
        self,
        value: Mapping[str, Any],
        path: str,
        hint: str | None,
    ) -> str | None:
        reader = _Reader(self, value, path, "service")
        source_id = reader.text("id", "id", "key", "code", "identifier") or hint
        title = reader.text("title", "title", "label", "displayName")
        title = title or reader.text("name", "name")
        protocol = _protocol(
            reader.text(
                "protocol", "protocol", "serviceType", "serverType", "type"
            )
            or ""
        )
        raw_url = reader.text(
            "URL",
            "url",
            "baseUrl",
            "serviceUrl",
            "serverUrl",
            "endpoint",
            "href",
        )
        base_url, inferred = self._url(raw_url, path) if raw_url else (None, None)
        protocol = protocol or inferred
        if not raw_url:
            self.issue(path, "service_url_missing", "URL is required")
        if protocol not in PROTOCOLS:
            self.issue(path, "service_protocol_missing", "protocol is unresolved")
        version = reader.text("version", "version", "serviceVersion")
        crs = _crs(self, reader.take("CRS", "crs", "srs", "projection"), path)
        image_format = reader.text("format", "format", "imageFormat", "mimeType")
        attribution = reader.text("attribution", "attribution", "credits", "copyright")
        source_key = (
            _service_key(protocol, base_url, source_id)
            if base_url and protocol
            else None
        )
        if source_key:
            definition = ReferenceServiceDefinition(
                source_key=source_key,
                title=title or source_key,
                upstream_protocol=protocol,
                base_url=base_url,
                version=version,
                default_crs=crs[0] if crs else None,
                default_format=image_format,
                attribution=attribution,
                license_status="pending",
                cache_policy="on_demand",
            )
            previous = self.service_definitions.get(source_key)
            if previous and _service_signature(previous) != _service_signature(
                definition
            ):
                self.issue(
                    path,
                    "conflicting_service",
                    self.service_evidence[source_key].path,
                )
            else:
                self.service_definitions.setdefault(source_key, definition)
                self.service_evidence.setdefault(
                    source_key,
                    SiurSettingsServiceEvidence(
                        path, source_key, source_id, protocol, base_url
                    ),
                )
            for alias in (source_id, hint):
                if alias:
                    old = self.service_refs.get(alias.casefold())
                    if old not in {None, source_key}:
                        self.issue(path, "conflicting_service_reference", alias)
                    self.service_refs[alias.casefold()] = source_key
        reader.finish()
        return source_key

    def _embedded_service(
        self,
        url: str,
        protocol: str | None,
        path: str,
        *,
        version: str | None = None,
    ) -> str | None:
        value: dict[str, Any] = {"title": "Embedded SIUR service", "url": url}
        if protocol:
            value["protocol"] = protocol
        if version:
            value["version"] = version
        return self._service(value, path, None)

    def _url(self, value: str, path: str) -> tuple[str | None, str | None]:
        try:
            parsed, port = urlsplit(value.strip()), urlsplit(value.strip()).port
        except ValueError:
            self.issue(path, "invalid_service_url", "URL rejected")
            return None, None
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        invalid = (
            scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.fragment)
            or (port is not None and port != {"http": 80, "https": 443}[scheme])
        )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if (
            invalid
            or (address and not address.is_global)
            or host in {"localhost", "local"}
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            self.issue(path, "invalid_service_url", "URL rejected")
            return None, None
        query = parse_qsl(parsed.query, keep_blank_values=True)
        hint = next((item for key, item in query if key.lower() == "service"), "")
        retained = sorted(
            (key, item) for key, item in query if key.lower() not in DROP_QUERY
        )
        netloc = f"[{host}]" if ":" in host else host
        normalized = urlunsplit(
            (scheme, netloc, parsed.path or "/", urlencode(retained), "")
        ).rstrip("/")
        inferred, lower_path = _protocol(hint), parsed.path.lower().rstrip("/")
        if not inferred:
            if lower_path.endswith(("/wms", "/ows")):
                inferred = "wms"
            elif lower_path.endswith("/wfs"):
                inferred = "wfs"
            elif lower_path.endswith("/wmts"):
                inferred = "wmts"
            elif "/rest/services/" in lower_path:
                inferred = "arcgis_rest"
            elif all(token in value for token in ("{z}", "{x}", "{y}")):
                inferred = "xyz"
        return normalized, inferred

    def _identity(self, kind: str, material: str, path: str) -> str | None:
        key = _stable(kind, material)
        previous = self.identities.get(key)
        if previous:
            self.issue(path, f"duplicate_{kind}_identity", previous)
            return None
        self.identities[key] = path
        return key

    def _entries(
        self, value: Any, path: str
    ) -> Iterable[tuple[Mapping[str, Any], str, str | None]]:
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    yield item, f"{path}[{index}]", None
                else:
                    self.issue(f"{path}[{index}]", "invalid_entry", "object required")
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(item, Mapping):
                    yield item, _path(path, key), key
                else:
                    self.issue(_path(path, key), "invalid_entry", "object required")
        else:
            self.issue(path, "invalid_container", "list/object required")

    def check_baseline(
        self, baseline: SiurSettingsBaseline | None, raw_sha: str
    ) -> None:
        if baseline is None:
            self.issue("$baseline", "baseline_required", "reviewed counts are required")
            return
        observed = (
            sum(node.kind == "group" and node.top_level for node in self.nodes),
            sum(node.kind == "group" for node in self.nodes),
            sum(node.kind == "layer" for node in self.nodes),
            len(self.service_definitions),
        )
        expected = (
            baseline.top_level_groups,
            baseline.groups,
            baseline.layers,
            baseline.services,
        )
        for label, actual, wanted in zip(
            ("top_groups", "groups", "layers", "services"), observed, expected
        ):
            if actual != wanted:
                self.issue(
                    "$baseline",
                    f"{label}_mismatch",
                    f"expected {wanted}, observed {actual}",
                )
        if baseline.raw_sha256 is None:
            self.issue(
                "$baseline",
                "approved_hash_required",
                "raw_sha256 must pin the reviewed settings bytes",
            )
        elif baseline.raw_sha256 != raw_sha:
            self.issue("$baseline", "raw_sha_mismatch", f"observed {raw_sha}")
        if baseline.layer_keys is None:
            self.issue(
                "$baseline",
                "approved_layer_manifest_required",
                "layer_keys must pin every reviewed layer identity",
            )
        else:
            actual = {
                node.source_key
                for node in self.nodes
                if node.kind == "layer" and node.source_key
            }
            for key in sorted(baseline.layer_keys - actual):
                self.issue("$baseline", "missing_layer", key)
            for key in sorted(actual - baseline.layer_keys):
                self.issue("$baseline", "unexpected_layer", key)


def _boolean(parser: _Parser, value: Any, path: str, default: bool) -> bool:
    if value is _MISSING or value is _CONFLICT:
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1, "0", "1"):
        return str(value) == "1"
    if isinstance(value, str) and value.lower() in {"true", "yes", "si", "sí"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "no"}:
        return False
    parser.issue(path, "invalid_boolean", repr(value))
    return default


def _integer(parser: _Parser, value: Any, path: str, default: int) -> int:
    if value is _MISSING or value is _CONFLICT:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = -1
    if isinstance(value, bool) or result < 0 or str(result) != str(value).strip():
        parser.issue(path, "invalid_integer", repr(value))
        return default
    return result


def _opacity(parser: _Parser, value: Any, path: str) -> Decimal:
    if value is _MISSING or value is _CONFLICT:
        return Decimal("1")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        result = Decimal("-1")
    if Decimal("1") < result <= Decimal("100"):
        result /= 100
    if not result.is_finite() or not Decimal("0") <= result <= Decimal("1"):
        parser.issue(path, "invalid_opacity", repr(value))
        return Decimal("1")
    return result


def _crs(parser: _Parser, value: Any, path: str) -> tuple[str, ...]:
    if value is _MISSING or value is _CONFLICT:
        return ()
    entries = [value] if isinstance(value, str) else value
    if not isinstance(entries, list):
        parser.issue(path, "invalid_crs", repr(value))
        return ()
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not CRS_RE.fullmatch(entry.strip()):
            parser.issue(path, "invalid_crs", repr(entry))
        elif entry.strip().upper() not in result:
            result.append(entry.strip().upper())
    return tuple(result)


def _protocol(value: str) -> str | None:
    return {
        "wms": "wms",
        "ogcwms": "wms",
        "wfs": "wfs",
        "ogcwfs": "wfs",
        "wmts": "wmts",
        "ogcwmts": "wmts",
        "xyz": "xyz",
        "tms": "xyz",
        "tilelayer": "xyz",
        "arcgis": "arcgis_rest",
        "arcgisrest": "arcgis_rest",
        "mapserver": "arcgis_rest",
        "featureserver": "arcgis_rest",
    }.get(_key(value))


def _stable(kind: str, material: str, protocol: str | None = None) -> str:
    middle = f":{protocol}" if protocol else ""
    return f"{kind}:siur{middle}:{hashlib.sha256(material.encode()).hexdigest()}"


def _service_key(
    protocol: str,
    base_url: str,
    source_id: str | None,
) -> str:
    parsed = urlsplit(base_url)
    match = IDECYL_WMS_PATH_RE.fullmatch(parsed.path.rstrip("/"))
    if (
        protocol == "wms"
        and parsed.hostname == "idecyl.jcyl.es"
        and match is not None
    ):
        return f"service:wms:idecyl:{match.group('workspace').lower()}"
    identity = source_id.casefold() if source_id else base_url
    return _stable("service", f"{protocol}\0{identity}", protocol)


def _service_signature(value: ReferenceServiceDefinition) -> tuple[Any, ...]:
    return (
        value.upstream_protocol,
        value.base_url,
        value.version,
        value.default_crs,
        value.default_format,
        value.attribution,
    )


def _absolute_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _safe_http_reference(value: str, *, allow_fragment: bool = False) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.fragment and not allow_fragment)
        or (port is not None and port != {"http": 80, "https": 443}[scheme])
        or host in {"localhost", "local"}
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _safe_relative_path(value: str, *, suffix: str | None = None) -> bool:
    parsed = urlsplit(value)
    parts = parsed.path.split("/")
    return (
        parsed.scheme == ""
        and parsed.netloc == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and not parsed.path.startswith("/")
        and all(part not in {"", ".", ".."} for part in parts)
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", parsed.path))
        and (suffix is None or parsed.path.casefold().endswith(suffix.casefold()))
    )


def _native_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _canonical(value: Any) -> str:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _path(parent: str, key: str) -> str:
    return (
        f"{parent}.{key}"
        if re.fullmatch(r"[A-Za-z_]\w*", key)
        else f"{parent}[{json.dumps(key)}]"
    )
