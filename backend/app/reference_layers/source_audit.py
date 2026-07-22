"""Read-only coverage audit for local acquisition of a reference catalog."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceService,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
)


class SourceCoverageAuditError(RuntimeError):
    """A current catalog is unavailable or has inconsistent references."""


@dataclass(frozen=True)
class SourceCoverageIssue:
    layer_id: int
    source_key: str
    detail: str


@dataclass(frozen=True)
class SourceCoverageReport:
    provider_key: str
    snapshot_id: int
    catalog_definition_sha256: str
    layer_count: int
    candidate_count: int
    data_candidate_layer_count: int
    tile_fallback_layer_count: int
    protocol_counts: dict[str, int]
    issues: tuple[SourceCoverageIssue, ...]

    @property
    def candidate_coverage_complete(self) -> bool:
        return self.layer_count > 0 and not self.issues

    def as_dict(self) -> dict:
        return {
            "candidate_coverage_complete": self.candidate_coverage_complete,
            "provider_key": self.provider_key,
            "snapshot_id": self.snapshot_id,
            "catalog_definition_sha256": self.catalog_definition_sha256,
            "layer_count": self.layer_count,
            "candidate_count": self.candidate_count,
            "data_candidate_layer_count": self.data_candidate_layer_count,
            "tile_fallback_layer_count": self.tile_fallback_layer_count,
            "protocol_counts": self.protocol_counts,
            "issues": [
                {
                    "layer_id": item.layer_id,
                    "source_key": item.source_key,
                    "detail": item.detail,
                }
                for item in self.issues
            ],
        }


def audit_current_catalog_sources(
    db: Session,
    *,
    provider_key: str,
) -> SourceCoverageReport:
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    if snapshot is None:
        raise SourceCoverageAuditError("current applied catalog is unavailable")

    services = {
        item.id: item
        for item in db.scalars(
            select(ReferenceService).where(
                ReferenceService.provider_key == provider_key,
                ReferenceService.last_seen_snapshot_id == snapshot.id,
            )
        )
    }
    layers = list(
        db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == provider_key,
                ReferenceLayer.last_seen_snapshot_id == snapshot.id,
                ReferenceLayer.node_type == "layer",
            )
            .order_by(ReferenceLayer.id)
        )
    )
    if not layers:
        raise SourceCoverageAuditError("current catalog has no leaf layers")

    issues: list[SourceCoverageIssue] = []
    protocols: Counter[str] = Counter()
    candidate_count = 0
    data_layers = 0
    fallback_layers = 0
    for record in layers:
        service = services.get(record.service_id or -1)
        if service is None:
            issues.append(
                SourceCoverageIssue(
                    record.id,
                    record.source_key,
                    "layer service is absent from the current snapshot",
                )
            )
            continue
        try:
            candidates = acquisition_candidates(
                _service_definition(service),
                _layer_definition(record),
            )
        except SourceDiscoveryError as exc:
            issues.append(
                SourceCoverageIssue(record.id, record.source_key, str(exc))
            )
            continue
        if not candidates:
            issues.append(
                SourceCoverageIssue(
                    record.id,
                    record.source_key,
                    "no acquisition candidate",
                )
            )
            continue
        candidate_count += len(candidates)
        protocols.update(item.protocol for item in candidates)
        if any(item.target_kind in {"vector", "raster"} for item in candidates):
            data_layers += 1
        if any(item.target_kind == "tiles" for item in candidates):
            fallback_layers += 1

    return SourceCoverageReport(
        provider_key=provider_key,
        snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        layer_count=len(layers),
        candidate_count=candidate_count,
        data_candidate_layer_count=data_layers,
        tile_fallback_layer_count=fallback_layers,
        protocol_counts=dict(sorted(protocols.items())),
        issues=tuple(issues),
    )


def _service_definition(record: ReferenceService) -> ReferenceServiceDefinition:
    return ReferenceServiceDefinition(
        source_key=record.source_key,
        title=record.title,
        upstream_protocol=record.upstream_protocol,
        base_url=record.base_url,
        capabilities_url=record.capabilities_url,
        version=record.version,
        default_crs=record.default_crs,
        default_format=record.default_format,
        attribution=record.attribution,
        license_name=record.license_name,
        license_url=record.license_url,
        license_status=record.license_status,
        cache_policy=record.cache_policy,
        capabilities_sha256=record.capabilities_sha256,
        status=record.status,
        last_error=record.last_error,
    )


def _layer_definition(record: ReferenceLayer) -> ReferenceLayerDefinition:
    return ReferenceLayerDefinition(
        source_key=record.source_key,
        node_type=record.node_type,
        title=record.title,
        description=record.description,
        remote_name=record.remote_name,
        role=record.role,
        renderer=record.renderer,
        delivery_mode=record.delivery_mode,
        style_name=record.style_name,
        image_format=record.image_format,
        supported_crs=tuple(record.supported_crs_json or ()),
        bounds=record.bounds_json,
        options=record.options_json,
        sort_order=record.sort_order,
        default_visible=record.default_visible,
        default_opacity=record.default_opacity,
        min_zoom=record.min_zoom,
        max_zoom=record.max_zoom,
        min_scale_denominator=record.min_scale_denominator,
        max_scale_denominator=record.max_scale_denominator,
        queryable=record.queryable,
        downloadable=record.downloadable,
        legend_url=record.legend_url,
        metadata_url=record.metadata_url,
        status=record.status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audita candidatos de adquisición local del catálogo",
    )
    parser.add_argument("--provider-key", default="siur")
    args = parser.parse_args(argv)
    register_all_models()
    try:
        with SessionLocal() as db:
            report = audit_current_catalog_sources(
                db,
                provider_key=args.provider_key,
            )
    except SourceCoverageAuditError as exc:
        print(
            json.dumps(
                {"candidate_coverage_complete": False, "error": str(exc)}
            )
        )
        return 2
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0 if report.candidate_coverage_complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
