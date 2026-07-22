from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReferenceCatalogSnapshotRead(BaseModel):
    id: int
    provider_key: str
    content_sha256: str
    definition_sha256: str
    retrieved_at: datetime
    service_count: int
    group_count: int
    layer_count: int
    unresolved_count: int
    status: str
    is_current: bool

    model_config = ConfigDict(from_attributes=True)


class ReferenceServiceRead(BaseModel):
    id: int
    title: str
    upstream_protocol: str
    attribution: str | None
    status: str
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReferenceLayerRead(BaseModel):
    id: int
    service_id: int | None
    parent_id: int | None
    source_key: str
    node_type: str
    title: str
    description: str | None
    role: str | None
    renderer: str | None
    delivery_mode: str | None
    bounds_json: dict[str, Any] | None
    sort_order: int
    default_visible: bool
    default_opacity: float
    effective_visible: bool = False
    effective_opacity: float = 1.0
    min_zoom: int | None
    max_zoom: int | None
    min_scale_denominator: Decimal | None
    max_scale_denominator: Decimal | None
    downloadable: bool
    delivery_available: bool = False
    identify_available: bool = False
    delivery_blocker: str | None = None
    available_style_ids: list[int] = Field(default_factory=list)
    legend_available: bool = False
    metadata_available: bool = False
    mirror_status: str = "legacy"
    active_version_id: int | None = None
    active_generation: int | None = None
    active_source_version: str | None = None
    active_reference_at: datetime | None = None
    active_created_at: datetime | None = None
    last_run_status: str | None = None
    last_checked_at: datetime | None = None
    last_sync_error_code: str | None = None
    last_sync_error_summary: str | None = None
    next_check_at: datetime | None = None
    status: str
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReferenceLayerStyleRead(BaseModel):
    id: int
    layer_id: int
    title: str
    description: str | None
    sort_order: int
    is_default: bool
    legend_available: bool = False
    status: str
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReferenceCatalogRead(BaseModel):
    snapshot: ReferenceCatalogSnapshotRead
    organization_id: int | None
    services: list[ReferenceServiceRead]
    layers: list[ReferenceLayerRead]
    styles: list[ReferenceLayerStyleRead]


class ReferenceLayerSettingUpdate(BaseModel):
    visible: bool | None = None
    opacity: Decimal | None = Field(default=None, ge=0, le=1, decimal_places=3)

    @model_validator(mode="after")
    def require_override(self):
        if self.visible is None and self.opacity is None:
            raise ValueError("At least one layer setting override is required")
        return self


class ReferenceLayerSettingRead(BaseModel):
    id: int
    organization_id: int
    layer_id: int
    visible: bool | None
    opacity: float | None
    updated_by_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReferenceCatalogSyncPlanRead(BaseModel):
    provider_key: str
    content_sha256: str
    definition_sha256: str
    service_count: int
    group_count: int
    layer_count: int
    style_count: int
    unresolved_count: int
    new_services: list[str]
    updated_services: list[str]
    missing_services: list[str]
    new_layers: list[str]
    updated_layers: list[str]
    missing_layers: list[str]
    new_styles: list[str]
    updated_styles: list[str]
    missing_styles: list[str]
    unchanged_count: int
    blocking_issues: list[str]
