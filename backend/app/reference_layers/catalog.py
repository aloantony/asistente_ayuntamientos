from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerStyle,
    ReferenceService,
)

PROTOCOLS = {"wms", "wfs", "wmts", "xyz", "arcgis_rest", "local"}
SERVICE_STATUSES = {"active", "degraded", "disabled"}
LAYER_STATUSES = {"active", "degraded", "disabled"}
LICENSE_STATUSES = {"pending", "approved", "restricted"}
CACHE_POLICIES = {"none", "on_demand", "mirror"}
NODE_TYPES = {"group", "layer"}
ROLES = {"base", "overlay"}
RENDERERS = {"raster_tile", "vector_tile"}
DELIVERY_MODES = {"proxy", "mirror"}
PROVIDER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$")
SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,254}$")
REMOTE_STYLE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:]{1,255}$")
_CATALOG_PROVIDER_LOCK_DOMAIN = b"asistente/reference-catalog-apply/v1\0"


class ReferenceCatalogValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceServiceDefinition:
    source_key: str
    title: str
    upstream_protocol: str
    base_url: str
    capabilities_url: str | None = None
    version: str | None = None
    default_crs: str | None = None
    default_format: str | None = None
    attribution: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    license_status: str = "pending"
    cache_policy: str = "none"
    capabilities_sha256: str | None = None
    status: str = "active"
    last_error: str | None = None


@dataclass(frozen=True)
class ReferenceLayerStyleDefinition:
    source_key: str
    title: str
    remote_name: str | None = None
    description: str | None = None
    legend_url: str | None = None
    sort_order: int = 0
    is_default: bool = False
    status: str = "active"


@dataclass(frozen=True)
class ReferenceLayerDefinition:
    source_key: str
    node_type: str
    title: str
    parent_key: str | None = None
    service_key: str | None = None
    description: str | None = None
    remote_name: str | None = None
    role: str | None = None
    renderer: str | None = None
    delivery_mode: str | None = None
    style_name: str | None = None
    image_format: str | None = None
    supported_crs: tuple[str, ...] = ()
    bounds: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    sort_order: int = 0
    default_visible: bool = False
    default_opacity: Decimal = Decimal("1")
    min_zoom: int | None = None
    max_zoom: int | None = None
    min_scale_denominator: Decimal | None = None
    max_scale_denominator: Decimal | None = None
    queryable: bool = False
    downloadable: bool = False
    legend_url: str | None = None
    metadata_url: str | None = None
    status: str = "active"
    styles: tuple[ReferenceLayerStyleDefinition, ...] = ()


@dataclass(frozen=True)
class ReferenceCatalogDefinition:
    provider_key: str
    source_url: str
    raw_catalog: dict[str, Any]
    services: tuple[ReferenceServiceDefinition, ...]
    layers: tuple[ReferenceLayerDefinition, ...]
    unresolved_count: int = 0
    retrieved_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass(frozen=True)
class ReferenceCatalogSyncPlan:
    definition: ReferenceCatalogDefinition
    content_sha256: str
    definition_sha256: str
    base_state_sha256: str
    new_services: tuple[str, ...]
    updated_services: tuple[str, ...]
    missing_services: tuple[str, ...]
    new_layers: tuple[str, ...]
    updated_layers: tuple[str, ...]
    missing_layers: tuple[str, ...]
    new_styles: tuple[str, ...]
    updated_styles: tuple[str, ...]
    missing_styles: tuple[str, ...]
    unchanged_count: int
    blocking_issues: tuple[str, ...]

    @property
    def service_count(self) -> int:
        return len(self.definition.services)

    @property
    def group_count(self) -> int:
        return sum(layer.node_type == "group" for layer in self.definition.layers)

    @property
    def layer_count(self) -> int:
        return sum(layer.node_type == "layer" for layer in self.definition.layers)

    @property
    def style_count(self) -> int:
        return sum(len(layer.styles) for layer in self.definition.layers)


def canonical_catalog_sha256(raw_catalog: dict[str, Any]) -> str:
    encoded = json.dumps(
        raw_catalog,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _catalog_provider_lock_key(provider_key: str) -> int:
    digest = hashlib.sha256(
        _CATALOG_PROVIDER_LOCK_DOMAIN + provider_key.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _lock_catalog_provider(db: Session, provider_key: str) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _catalog_provider_lock_key(provider_key)},
    )


def lock_catalog_provider(db: Session, provider_key: str) -> None:
    """Serialize an operator transition with catalog apply for one provider."""

    _lock_catalog_provider(db, provider_key)


def canonical_definition_sha256(
    definition: ReferenceCatalogDefinition,
) -> str:
    return canonical_normalized_definition_sha256(
        normalized_catalog_definition(definition)
    )


def canonical_normalized_definition_sha256(
    payload: dict[str, Any],
) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalized_catalog_definition(
    definition: ReferenceCatalogDefinition,
) -> dict[str, Any]:
    payload = {
        "provider_key": definition.provider_key,
        "source_url": definition.source_url,
        "unresolved_count": definition.unresolved_count,
        "services": sorted(
            (asdict(service) for service in definition.services),
            key=lambda service: service["source_key"],
        ),
        "layers": sorted(
            (_normalized_layer_definition(layer) for layer in definition.layers),
            key=lambda layer: layer["source_key"],
        ),
    }
    return _canonical_json_value(payload)


def build_catalog_sync_plan(
    db: Session,
    definition: ReferenceCatalogDefinition,
) -> ReferenceCatalogSyncPlan:
    issues = validate_catalog_definition(definition)
    try:
        content_sha256 = canonical_catalog_sha256(definition.raw_catalog)
    except (TypeError, ValueError):
        content_sha256 = "0" * 64
        issues.append("raw_catalog must contain only JSON-compatible values")
    try:
        definition_sha256 = canonical_definition_sha256(definition)
    except (TypeError, ValueError):
        definition_sha256 = "0" * 64
        issues.append("catalog definition must contain only JSON-compatible values")
    if issues:
        return ReferenceCatalogSyncPlan(
            definition=definition,
            content_sha256=content_sha256,
            definition_sha256=definition_sha256,
            base_state_sha256="0" * 64,
            new_services=(),
            updated_services=(),
            missing_services=(),
            new_layers=(),
            updated_layers=(),
            missing_layers=(),
            new_styles=(),
            updated_styles=(),
            missing_styles=(),
            unchanged_count=0,
            blocking_issues=tuple(issues),
        )

    existing_services = {
        service.source_key: service
        for service in db.scalars(
            select(ReferenceService).where(
                ReferenceService.provider_key == definition.provider_key
            )
        )
    }
    existing_layers = {
        layer.source_key: layer
        for layer in db.scalars(
            select(ReferenceLayer)
            .options(
                selectinload(ReferenceLayer.parent),
                selectinload(ReferenceLayer.service),
            )
            .where(ReferenceLayer.provider_key == definition.provider_key)
        )
    }
    existing_styles = {
        f"{style.layer.source_key}|{style.source_key}": style
        for style in db.scalars(
            select(ReferenceLayerStyle)
            .options(selectinload(ReferenceLayerStyle.layer))
            .where(
                ReferenceLayerStyle.provider_key == definition.provider_key
            )
        )
    }
    current_snapshots = tuple(
        db.scalars(
            select(ReferenceCatalogSnapshot).where(
                ReferenceCatalogSnapshot.provider_key == definition.provider_key,
                ReferenceCatalogSnapshot.is_current.is_(True),
            )
        )
    )
    base_state_sha256 = _catalog_base_state_sha256(
        existing_services,
        existing_layers,
        existing_styles,
        current_snapshots,
    )

    new_services: list[str] = []
    updated_services: list[str] = []
    missing_services: list[str] = []
    new_layers: list[str] = []
    updated_layers: list[str] = []
    missing_layers: list[str] = []
    new_styles: list[str] = []
    updated_styles: list[str] = []
    missing_styles: list[str] = []
    unchanged_count = 0

    service_definitions = {
        service.source_key: service for service in definition.services
    }
    for key, service_definition in service_definitions.items():
        existing = existing_services.get(key)
        if existing is None:
            new_services.append(key)
        elif _service_signature(existing) != _service_definition_signature(
            service_definition
        ):
            updated_services.append(key)
        else:
            unchanged_count += 1
    for key, existing in existing_services.items():
        if key not in service_definitions:
            if existing.status != "missing":
                missing_services.append(key)
            else:
                unchanged_count += 1

    layer_definitions = {layer.source_key: layer for layer in definition.layers}
    for key, layer_definition in layer_definitions.items():
        existing = existing_layers.get(key)
        if existing is None:
            new_layers.append(key)
        elif _layer_signature(existing) != _layer_definition_signature(
            layer_definition
        ):
            updated_layers.append(key)
        else:
            unchanged_count += 1
    for key, existing in existing_layers.items():
        if key not in layer_definitions:
            if existing.status != "missing":
                missing_layers.append(key)
            else:
                unchanged_count += 1

    style_definitions = {
        f"{layer.source_key}|{style.source_key}": style
        for layer in definition.layers
        for style in layer.styles
    }
    for key, style_definition in style_definitions.items():
        existing = existing_styles.get(key)
        if existing is None:
            new_styles.append(key)
        elif _style_signature(existing) != _style_definition_signature(
            style_definition
        ):
            updated_styles.append(key)
        else:
            unchanged_count += 1
    for key, existing in existing_styles.items():
        if key not in style_definitions:
            if existing.status != "missing":
                missing_styles.append(key)
            else:
                unchanged_count += 1

    return ReferenceCatalogSyncPlan(
        definition=definition,
        content_sha256=content_sha256,
        definition_sha256=definition_sha256,
        base_state_sha256=base_state_sha256,
        new_services=tuple(sorted(new_services)),
        updated_services=tuple(sorted(updated_services)),
        missing_services=tuple(sorted(missing_services)),
        new_layers=tuple(sorted(new_layers)),
        updated_layers=tuple(sorted(updated_layers)),
        missing_layers=tuple(sorted(missing_layers)),
        new_styles=tuple(sorted(new_styles)),
        updated_styles=tuple(sorted(updated_styles)),
        missing_styles=tuple(sorted(missing_styles)),
        unchanged_count=unchanged_count,
        blocking_issues=(),
    )


def apply_catalog_definition(
    db: Session,
    definition: ReferenceCatalogDefinition,
    *,
    expected_plan: ReferenceCatalogSyncPlan | None = None,
) -> tuple[ReferenceCatalogSnapshot, ReferenceCatalogSyncPlan]:
    try:
        _lock_catalog_provider(db, definition.provider_key)
        # A caller can build the reviewed plan with this same Session before
        # waiting for the provider lock. Reload ORM state so the revalidation
        # observes any catalog transaction that committed while we waited.
        db.expire_all()
        plan = build_catalog_sync_plan(db, definition)
        if plan.blocking_issues:
            raise ReferenceCatalogValidationError("; ".join(plan.blocking_issues))
        if (
            expected_plan is not None
            and _sync_plan_signature(plan)
            != _sync_plan_signature(expected_plan)
        ):
            raise ReferenceCatalogValidationError(
                "Catalog state changed after the reviewed dry-run"
            )

        snapshot = db.scalar(
            select(ReferenceCatalogSnapshot).where(
                ReferenceCatalogSnapshot.provider_key == definition.provider_key,
                ReferenceCatalogSnapshot.content_sha256 == plan.content_sha256,
                ReferenceCatalogSnapshot.definition_sha256
                == plan.definition_sha256,
            )
        )
        if snapshot is None:
            snapshot = ReferenceCatalogSnapshot(
                provider_key=definition.provider_key,
                source_url=definition.source_url,
                content_sha256=plan.content_sha256,
                definition_sha256=plan.definition_sha256,
                raw_catalog_json=definition.raw_catalog,
                normalized_definition_json=normalized_catalog_definition(
                    definition
                ),
                retrieved_at=definition.retrieved_at,
                service_count=plan.service_count,
                group_count=plan.group_count,
                layer_count=plan.layer_count,
                unresolved_count=definition.unresolved_count,
                status="validated",
                is_current=False,
            )
            db.add(snapshot)
            db.flush()

        services = {
            service.source_key: service
            for service in db.scalars(
                select(ReferenceService)
                .where(ReferenceService.provider_key == definition.provider_key)
                .with_for_update()
            )
        }
        incoming_service_keys = {item.source_key for item in definition.services}
        for key, service in services.items():
            if key not in incoming_service_keys:
                service.status = "missing"
        for item in definition.services:
            service = services.get(item.source_key)
            if service is None:
                service = ReferenceService(
                    provider_key=definition.provider_key,
                    source_key=item.source_key,
                    last_seen_snapshot_id=snapshot.id,
                )
                db.add(service)
                services[item.source_key] = service
            _apply_service_definition(service, item, snapshot.id)
        db.flush()

        layers = {
            layer.source_key: layer
            for layer in db.scalars(
                select(ReferenceLayer)
                .where(ReferenceLayer.provider_key == definition.provider_key)
                .with_for_update()
            )
        }
        incoming_layer_keys = {item.source_key for item in definition.layers}
        for key, layer in layers.items():
            if key not in incoming_layer_keys:
                layer.status = "missing"

        for item in _topological_layers(definition.layers):
            layer = layers.get(item.source_key)
            if layer is None:
                layer = ReferenceLayer(
                    provider_key=definition.provider_key,
                    source_key=item.source_key,
                    node_type=item.node_type,
                    title=item.title,
                    last_seen_snapshot_id=snapshot.id,
                )
                db.add(layer)
                layers[item.source_key] = layer
            parent = layers.get(item.parent_key) if item.parent_key else None
            service = services.get(item.service_key) if item.service_key else None
            _apply_layer_definition(layer, item, snapshot.id, parent, service)
            db.flush()

        styles = {
            f"{style.layer.source_key}|{style.source_key}": style
            for style in db.scalars(
                select(ReferenceLayerStyle)
                .options(selectinload(ReferenceLayerStyle.layer))
                .where(
                    ReferenceLayerStyle.provider_key == definition.provider_key
                )
                .with_for_update()
            )
        }
        incoming_style_keys = {
            f"{layer.source_key}|{style.source_key}"
            for layer in definition.layers
            for style in layer.styles
        }
        incoming_default_style_keys = {
            f"{layer.source_key}|{style.source_key}"
            for layer in definition.layers
            for style in layer.styles
            if style.is_default
        }
        for key, style in styles.items():
            if key not in incoming_style_keys:
                style.status = "missing"
                style.is_default = False
            elif style.is_default and key not in incoming_default_style_keys:
                style.is_default = False
        db.flush()

        for layer_definition in definition.layers:
            layer = layers[layer_definition.source_key]
            for item in layer_definition.styles:
                key = f"{layer_definition.source_key}|{item.source_key}"
                style = styles.get(key)
                if style is None:
                    style = ReferenceLayerStyle(
                        provider_key=definition.provider_key,
                        layer_id=layer.id,
                        source_key=item.source_key,
                        remote_name=item.remote_name or item.source_key,
                        title=item.title,
                        last_seen_snapshot_id=snapshot.id,
                    )
                    db.add(style)
                    styles[key] = style
                _apply_style_definition(style, item, snapshot.id, layer)
                db.flush()

        current_snapshots = list(
            db.scalars(
                select(ReferenceCatalogSnapshot)
                .where(
                    ReferenceCatalogSnapshot.provider_key == definition.provider_key,
                    ReferenceCatalogSnapshot.is_current.is_(True),
                    ReferenceCatalogSnapshot.id != snapshot.id,
                )
                .with_for_update()
            )
        )
        for current in current_snapshots:
            current.is_current = False
        db.flush()
        snapshot.status = "applied"
        snapshot.is_current = True
        db.commit()
        db.refresh(snapshot)
        return snapshot, plan
    except Exception:
        db.rollback()
        raise


def validate_catalog_definition(
    definition: ReferenceCatalogDefinition,
) -> list[str]:
    issues: list[str] = []
    if not PROVIDER_KEY_RE.fullmatch(definition.provider_key):
        issues.append("Invalid provider_key")
    if not _valid_declared_http_url(definition.source_url, require_https=True):
        issues.append("Catalog source_url must be an absolute HTTPS URL")
    if definition.unresolved_count < 0:
        issues.append("unresolved_count cannot be negative")
    if definition.retrieved_at.tzinfo is None:
        issues.append("retrieved_at must be timezone-aware")

    service_keys: set[str] = set()
    for service in definition.services:
        if service.source_key in service_keys:
            issues.append(f"Duplicate service key: {service.source_key}")
        service_keys.add(service.source_key)
        if not SOURCE_KEY_RE.fullmatch(service.source_key):
            issues.append(f"Invalid service key: {service.source_key}")
        if not service.title.strip() or len(service.title) > 500:
            issues.append(f"Invalid service title: {service.source_key}")
        if service.upstream_protocol not in PROTOCOLS:
            issues.append(f"Unsupported protocol: {service.source_key}")
        if not _valid_declared_http_url(service.base_url):
            issues.append(f"Invalid service URL: {service.source_key}")
        if service.capabilities_url and not _valid_declared_http_url(
            service.capabilities_url
        ):
            issues.append(f"Invalid capabilities URL: {service.source_key}")
        if service.license_url and not _valid_declared_http_url(
            service.license_url
        ):
            issues.append(f"Invalid license URL: {service.source_key}")
        if service.license_status not in LICENSE_STATUSES:
            issues.append(f"Invalid license status: {service.source_key}")
        if service.cache_policy not in CACHE_POLICIES:
            issues.append(f"Invalid cache policy: {service.source_key}")
        if service.status not in SERVICE_STATUSES:
            issues.append(f"Invalid service status: {service.source_key}")
        if service.capabilities_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", service.capabilities_sha256
        ):
            issues.append(f"Invalid capabilities hash: {service.source_key}")
        for label, value, maximum in (
            ("version", service.version, 30),
            ("default CRS", service.default_crs, 64),
            ("default format", service.default_format, 100),
            ("license name", service.license_name, 255),
        ):
            if value is not None and len(value) > maximum:
                issues.append(f"Invalid service {label}: {service.source_key}")

    layer_definitions: dict[str, ReferenceLayerDefinition] = {}
    for layer in definition.layers:
        if layer.source_key in layer_definitions:
            issues.append(f"Duplicate layer key: {layer.source_key}")
        layer_definitions[layer.source_key] = layer
        if not SOURCE_KEY_RE.fullmatch(layer.source_key):
            issues.append(f"Invalid layer key: {layer.source_key}")
        if not layer.title.strip() or len(layer.title) > 500:
            issues.append(f"Invalid layer title: {layer.source_key}")
        if layer.remote_name is not None and len(layer.remote_name) > 500:
            issues.append(f"Invalid remote layer name: {layer.source_key}")
        if layer.style_name is not None and len(layer.style_name) > 255:
            issues.append(f"Invalid selected style name: {layer.source_key}")
        if layer.image_format is not None and len(layer.image_format) > 100:
            issues.append(f"Invalid layer image format: {layer.source_key}")
        if layer.node_type not in NODE_TYPES:
            issues.append(f"Invalid node type: {layer.source_key}")
        if layer.status not in LAYER_STATUSES:
            issues.append(f"Invalid layer status: {layer.source_key}")
        if layer.sort_order < 0:
            issues.append(f"Negative sort order: {layer.source_key}")
        if not Decimal("0") <= layer.default_opacity <= Decimal("1"):
            issues.append(f"Invalid opacity: {layer.source_key}")
        if not _valid_zoom_range(layer.min_zoom, layer.max_zoom):
            issues.append(f"Invalid zoom range: {layer.source_key}")
        for scale in (
            layer.min_scale_denominator,
            layer.max_scale_denominator,
        ):
            if scale is not None and scale <= 0:
                issues.append(f"Invalid scale denominator: {layer.source_key}")
        if layer.node_type == "group":
            if layer.styles:
                issues.append(f"Group has styles: {layer.source_key}")
            if any(
                value is not None
                for value in (
                    layer.service_key,
                    layer.role,
                    layer.renderer,
                    layer.delivery_mode,
                )
            ):
                issues.append(f"Group has layer-only fields: {layer.source_key}")
        elif layer.node_type == "layer":
            if layer.service_key not in service_keys:
                issues.append(f"Unknown service for layer: {layer.source_key}")
            if layer.role not in ROLES:
                issues.append(f"Invalid role: {layer.source_key}")
            if layer.renderer not in RENDERERS:
                issues.append(f"Invalid renderer: {layer.source_key}")
            if layer.delivery_mode not in DELIVERY_MODES:
                issues.append(f"Invalid delivery mode: {layer.source_key}")
            style_keys: set[str] = set()
            default_style_keys: list[str] = []
            for style in layer.styles:
                if style.source_key in style_keys:
                    issues.append(
                        f"Duplicate style key: {layer.source_key}|{style.source_key}"
                    )
                style_keys.add(style.source_key)
                if not SOURCE_KEY_RE.fullmatch(style.source_key):
                    issues.append(
                        f"Invalid style key: {layer.source_key}|{style.source_key}"
                    )
                remote_style_name = style.remote_name or style.source_key
                if not REMOTE_STYLE_NAME_RE.fullmatch(remote_style_name):
                    issues.append(
                        "Invalid remote style name: "
                        f"{layer.source_key}|{style.source_key}"
                    )
                if not style.title.strip() or len(style.title) > 500:
                    issues.append(
                        f"Invalid style title: {layer.source_key}|{style.source_key}"
                    )
                if style.status not in LAYER_STATUSES:
                    issues.append(
                        f"Invalid style status: {layer.source_key}|{style.source_key}"
                    )
                if style.sort_order < 0:
                    issues.append(
                        "Negative style sort order: "
                        f"{layer.source_key}|{style.source_key}"
                    )
                if style.legend_url and not _valid_declared_http_url(
                    style.legend_url
                ):
                    issues.append(
                        "Invalid style legend URL: "
                        f"{layer.source_key}|{style.source_key}"
                    )
                if style.is_default:
                    default_style_keys.append(style.source_key)
            if len(default_style_keys) > 1:
                issues.append(f"Multiple default styles: {layer.source_key}")
            if layer.style_name and not layer.styles:
                issues.append(
                    f"Selected style has no definition: {layer.source_key}"
                )
            elif layer.style_name and layer.style_name not in style_keys:
                issues.append(f"Unknown selected style: {layer.source_key}")
            if layer.styles and layer.style_name:
                if default_style_keys != [layer.style_name]:
                    issues.append(
                        f"Selected/default style mismatch: {layer.source_key}"
                    )
            elif default_style_keys:
                issues.append(
                    f"Default style has no selected style: {layer.source_key}"
                )
        if layer.legend_url and not _valid_declared_http_url(layer.legend_url):
            issues.append(f"Invalid legend URL: {layer.source_key}")
        if layer.metadata_url and not _valid_declared_http_url(
            layer.metadata_url,
            allow_fragment=True,
        ):
            issues.append(f"Invalid metadata URL: {layer.source_key}")

    for layer in definition.layers:
        if layer.parent_key is None:
            continue
        parent = layer_definitions.get(layer.parent_key)
        if parent is None:
            issues.append(f"Unknown parent for layer: {layer.source_key}")
        elif parent.node_type != "group":
            issues.append(f"Parent is not a group: {layer.source_key}")

    issues.extend(_hierarchy_issues(layer_definitions))
    return sorted(set(issues))


def _valid_declared_http_url(
    value: str,
    *,
    require_https: bool = False,
    allow_fragment: bool = False,
) -> bool:
    # Persistence-time validation only. Any outbound client must also apply a
    # provider allowlist and validate DNS answers and every redirect target.
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not (
        parsed.scheme in allowed_schemes
        and hostname
        and parsed.username is None
        and parsed.password is None
        and (allow_fragment or parsed.fragment == "")
    ):
        return False
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        return False
    if hostname in {"localhost", "local"} or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        return True


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Unsupported catalog value: {type(value).__name__}")


def _normalized_layer_definition(
    layer: ReferenceLayerDefinition,
) -> dict[str, Any]:
    payload = asdict(layer)
    payload["styles"] = sorted(
        payload["styles"],
        key=lambda style: style["source_key"],
    )
    for style in payload["styles"]:
        style["remote_name"] = style["remote_name"] or style["source_key"]
    return payload


def _valid_zoom_range(min_zoom: int | None, max_zoom: int | None) -> bool:
    if min_zoom is not None and not 0 <= min_zoom <= 24:
        return False
    if max_zoom is not None and not 0 <= max_zoom <= 24:
        return False
    return min_zoom is None or max_zoom is None or min_zoom <= max_zoom


def _hierarchy_issues(
    definitions: dict[str, ReferenceLayerDefinition],
) -> list[str]:
    issues: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            issues.append(f"Layer hierarchy contains a cycle at: {key}")
            return
        visiting.add(key)
        parent_key = definitions[key].parent_key
        if parent_key in definitions:
            visit(parent_key)
        visiting.remove(key)
        visited.add(key)

    for key in definitions:
        visit(key)
    return issues


def _topological_layers(
    definitions: tuple[ReferenceLayerDefinition, ...],
) -> list[ReferenceLayerDefinition]:
    by_key = {item.source_key: item for item in definitions}
    depth_cache: dict[str, int] = {}

    def depth(key: str) -> int:
        if key in depth_cache:
            return depth_cache[key]
        parent_key = by_key[key].parent_key
        value = 0 if parent_key is None else depth(parent_key) + 1
        depth_cache[key] = value
        return value

    return sorted(
        definitions,
        key=lambda item: (
            depth(item.source_key),
            item.sort_order,
            item.source_key,
        ),
    )


def _sync_plan_signature(plan: ReferenceCatalogSyncPlan) -> tuple:
    return (
        plan.content_sha256,
        plan.definition_sha256,
        plan.base_state_sha256,
        plan.new_services,
        plan.updated_services,
        plan.missing_services,
        plan.new_layers,
        plan.updated_layers,
        plan.missing_layers,
        plan.new_styles,
        plan.updated_styles,
        plan.missing_styles,
    )


def _catalog_base_state_sha256(
    services: dict[str, ReferenceService],
    layers: dict[str, ReferenceLayer],
    styles: dict[str, ReferenceLayerStyle],
    current_snapshots: tuple[ReferenceCatalogSnapshot, ...],
) -> str:
    payload = {
        "current_snapshots": [
            (snapshot.content_sha256, snapshot.definition_sha256)
            for snapshot in sorted(
                current_snapshots,
                key=lambda item: (
                    item.content_sha256,
                    item.definition_sha256,
                ),
            )
        ],
        "services": [
            (key, _service_signature(services[key]))
            for key in sorted(services)
        ],
        "layers": [
            (key, _layer_signature(layers[key])) for key in sorted(layers)
        ],
        "styles": [
            (key, _style_signature(styles[key])) for key in sorted(styles)
        ],
    }
    encoded = json.dumps(
        _canonical_json_value(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _service_definition_signature(item: ReferenceServiceDefinition) -> tuple:
    return (
        item.title,
        item.upstream_protocol,
        item.base_url,
        item.capabilities_url,
        item.version,
        item.default_crs,
        item.default_format,
        item.attribution,
        item.license_name,
        item.license_url,
        item.license_status,
        item.cache_policy,
        item.capabilities_sha256,
        item.status,
        item.last_error,
    )


def _style_definition_signature(item: ReferenceLayerStyleDefinition) -> tuple:
    return (
        item.remote_name or item.source_key,
        item.title,
        item.description,
        item.legend_url,
        item.sort_order,
        item.is_default,
        item.status,
    )


def _style_signature(item: ReferenceLayerStyle) -> tuple:
    return (
        item.remote_name,
        item.title,
        item.description,
        item.legend_url,
        item.sort_order,
        item.is_default,
        item.status,
    )


def _service_signature(item: ReferenceService) -> tuple:
    return (
        item.title,
        item.upstream_protocol,
        item.base_url,
        item.capabilities_url,
        item.version,
        item.default_crs,
        item.default_format,
        item.attribution,
        item.license_name,
        item.license_url,
        item.license_status,
        item.cache_policy,
        item.capabilities_sha256,
        item.status,
        item.last_error,
    )


def _layer_definition_signature(item: ReferenceLayerDefinition) -> tuple:
    return (
        item.parent_key,
        item.service_key,
        item.node_type,
        item.title,
        item.description,
        item.remote_name,
        item.role,
        item.renderer,
        item.delivery_mode,
        item.style_name,
        item.image_format,
        tuple(item.supported_crs),
        item.bounds,
        item.options,
        item.sort_order,
        item.default_visible,
        Decimal(item.default_opacity),
        item.min_zoom,
        item.max_zoom,
        item.min_scale_denominator,
        item.max_scale_denominator,
        item.queryable,
        item.downloadable,
        item.legend_url,
        item.metadata_url,
        item.status,
    )


def _layer_signature(item: ReferenceLayer) -> tuple:
    return (
        item.parent.source_key if item.parent else None,
        item.service.source_key if item.service else None,
        item.node_type,
        item.title,
        item.description,
        item.remote_name,
        item.role,
        item.renderer,
        item.delivery_mode,
        item.style_name,
        item.image_format,
        tuple(item.supported_crs_json or ()),
        item.bounds_json,
        item.options_json,
        item.sort_order,
        item.default_visible,
        Decimal(item.default_opacity),
        item.min_zoom,
        item.max_zoom,
        item.min_scale_denominator,
        item.max_scale_denominator,
        item.queryable,
        item.downloadable,
        item.legend_url,
        item.metadata_url,
        item.status,
    )


def _apply_service_definition(
    service: ReferenceService,
    item: ReferenceServiceDefinition,
    snapshot_id: int,
) -> None:
    service.last_seen_snapshot_id = snapshot_id
    for name, value in (
        ("title", item.title),
        ("upstream_protocol", item.upstream_protocol),
        ("base_url", item.base_url),
        ("capabilities_url", item.capabilities_url),
        ("version", item.version),
        ("default_crs", item.default_crs),
        ("default_format", item.default_format),
        ("attribution", item.attribution),
        ("license_name", item.license_name),
        ("license_url", item.license_url),
        ("license_status", item.license_status),
        ("cache_policy", item.cache_policy),
        ("capabilities_sha256", item.capabilities_sha256),
        ("status", item.status),
        ("last_error", item.last_error),
    ):
        setattr(service, name, value)


def _apply_layer_definition(
    layer: ReferenceLayer,
    item: ReferenceLayerDefinition,
    snapshot_id: int,
    parent: ReferenceLayer | None,
    service: ReferenceService | None,
) -> None:
    layer.last_seen_snapshot_id = snapshot_id
    layer.parent_id = parent.id if parent else None
    layer.service_id = service.id if service else None
    values = {
        "node_type": item.node_type,
        "title": item.title,
        "description": item.description,
        "remote_name": item.remote_name,
        "role": item.role,
        "renderer": item.renderer,
        "delivery_mode": item.delivery_mode,
        "style_name": item.style_name,
        "image_format": item.image_format,
        "supported_crs_json": list(item.supported_crs) or None,
        "bounds_json": item.bounds,
        "options_json": item.options,
        "sort_order": item.sort_order,
        "default_visible": item.default_visible,
        "default_opacity": item.default_opacity,
        "min_zoom": item.min_zoom,
        "max_zoom": item.max_zoom,
        "min_scale_denominator": item.min_scale_denominator,
        "max_scale_denominator": item.max_scale_denominator,
        "queryable": item.queryable,
        "downloadable": item.downloadable,
        "legend_url": item.legend_url,
        "metadata_url": item.metadata_url,
        "status": item.status,
    }
    for name, value in values.items():
        setattr(layer, name, value)


def _apply_style_definition(
    style: ReferenceLayerStyle,
    item: ReferenceLayerStyleDefinition,
    snapshot_id: int,
    layer: ReferenceLayer,
) -> None:
    style.last_seen_snapshot_id = snapshot_id
    style.layer_id = layer.id
    for name, value in (
        ("remote_name", item.remote_name or item.source_key),
        ("title", item.title),
        ("description", item.description),
        ("legend_url", item.legend_url),
        ("sort_order", item.sort_order),
        ("is_default", item.is_default),
        ("status", item.status),
    ):
        setattr(style, name, value)
