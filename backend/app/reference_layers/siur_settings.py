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
        self._catalog(self.root, "$", None, True, root=True)
        if not self.nodes:
            self.issue("$", "catalog_missing", "no recognized groups or layers")

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
        self, url: str, protocol: str | None, path: str
    ) -> str | None:
        value: dict[str, Any] = {"title": "Embedded SIUR service", "url": url}
        if protocol:
            value["protocol"] = protocol
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
