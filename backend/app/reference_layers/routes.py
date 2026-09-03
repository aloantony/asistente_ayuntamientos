import logging
from decimal import Decimal
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Response,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.config import settings as app_settings
from app.db.session import get_db
from app.reference_layers.access import (
    require_catalog_manage,
    require_catalog_view,
)
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
)
from app.reference_layers.delivery_builder import (
    DeliveryBuildError,
    canonical_json_sha256,
)
from app.reference_layers.models import (
    OrganizationReferenceLayerSetting,
    ReferenceCatalogSnapshot,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceService,
    ReferenceSyncRun,
)
from app.reference_layers.local_delivery import (
    catalog_local_delivery_availability,
    catalog_served_tile_coverage,
)
from app.reference_layers.local_metadata import (
    LocalMetadataError,
    catalog_local_metadata_availability,
    resolve_local_metadata,
)
from app.reference_layers.mirror_authorization import (
    effective_service_attributions,
)
from app.reference_layers.mirror_status import catalog_mirror_statuses
from app.reference_layers.reviewed_ortho_evidence import (
    ReviewedOrthoEvidenceError,
    require_reviewed_ign_ortho_delivery_allowed,
    reviewed_ign_ortho_catalog_projection,
    reviewed_ign_ortho_source_projection,
)
from app.reference_layers.schemas import (
    ReferenceCatalogRead,
    ReferenceCatalogSnapshotRead,
    ReferenceLayerRead,
    ReferenceLayerSettingRead,
    ReferenceLayerSettingUpdate,
    ReferenceLayerStyleRead,
    ReferenceServiceRead,
)
from app.reference_layers.wms_delivery import (
    attested_proxy_attributions,
    catalog_delivery_availability,
)
from app.reference_layers.wms_routes import router as wms_router
from app.users.models import User

router = APIRouter(tags=["reference-layers"])
router.include_router(wms_router)
logger = logging.getLogger(__name__)


def _active_reviewed_ortho_projection(
    *,
    service: ReferenceService | None,
    layer: ReferenceLayer,
    version: ReferenceDeliveryVersion,
    run: ReferenceSyncRun,
) -> dict[str, object]:
    catalog_layer = layer.remote_name or layer.source_key
    frozen = (
        run.source_definition_json
        if isinstance(run.source_definition_json, dict)
        else {}
    )
    try:
        if (
            canonical_json_sha256(frozen)
            != run.source_definition_sha256
            or canonical_json_sha256(version.validation_json)
            != version.validation_sha256
        ):
            raise ReviewedOrthoEvidenceError(
                "active ortho hashes are invalid"
            )
        projection = require_reviewed_ign_ortho_delivery_allowed(
            catalog_endpoint_url=service.base_url if service is not None else "",
            catalog_layer=catalog_layer,
            source_definition=frozen,
            validation_json=version.validation_json,
            content_sha256=version.content_sha256,
        )
        if projection is None:
            raise ReviewedOrthoEvidenceError(
                "active delivery no longer has its reviewed catalog identity"
            )
    except (ReviewedOrthoEvidenceError, DeliveryBuildError):
        return {
            "equivalence_status": "invalid",
            "public_notice": (
                "La entrega local activa está bloqueada: sus bytes "
                "históricos no tienen la identidad y la paridad "
                "versionadas exigidas."
            ),
            "selected_layer": frozen.get("remote_name"),
            "profile": (
                frozen.get("config", {})
                .get("reviewed_equivalence", {})
                .get("profile")
                if isinstance(frozen.get("config"), dict)
                else None
            ),
            "required_attribution": None,
            "scope": "active_delivery",
            "delivery_content_sha256": version.content_sha256,
        }
    return {
        **projection,
        "scope": "active_delivery",
    }


@router.get(
    "/reference-layers/catalog",
    response_model=ReferenceCatalogRead,
)
def get_reference_catalog(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    provider_key: Annotated[
        str,
        Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.:/-]*$"),
    ],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> ReferenceCatalogRead:
    require_catalog_view(db, current_user, organization_id)
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot)
        .where(
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
            ReferenceCatalogSnapshot.provider_key == provider_key,
        )
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reference layer catalog is not available",
        )

    services = list(
        db.scalars(
            select(ReferenceService)
            .where(
                ReferenceService.provider_key == snapshot.provider_key,
                ReferenceService.last_seen_snapshot_id == snapshot.id,
            )
            .order_by(ReferenceService.title, ReferenceService.id)
        )
    )
    layers = list(
        db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == snapshot.provider_key,
                ReferenceLayer.last_seen_snapshot_id == snapshot.id,
            )
            .order_by(
                ReferenceLayer.parent_id.asc().nulls_first(),
                ReferenceLayer.sort_order,
                ReferenceLayer.id,
            )
        )
    )
    styles = list(
        db.scalars(
            select(ReferenceLayerStyle)
            .where(
                ReferenceLayerStyle.provider_key == snapshot.provider_key,
                ReferenceLayerStyle.last_seen_snapshot_id == snapshot.id,
            )
            .order_by(
                ReferenceLayerStyle.layer_id,
                ReferenceLayerStyle.sort_order,
                ReferenceLayerStyle.id,
            )
        )
    )
    services_by_id = {service.id: service for service in services}
    substitutions: dict[int, dict[str, object]] = {}
    for layer in layers:
        service = services_by_id.get(layer.service_id or -1)
        if service is None or layer.remote_name is None:
            continue
        try:
            projection = reviewed_ign_ortho_catalog_projection(
                service.base_url,
                layer.remote_name,
            )
        except (ReviewedOrthoEvidenceError, DeliveryBuildError):
            projection = {
                "equivalence_status": "invalid",
                "public_notice": (
                    "La evidencia versionada de la sustitución no supera "
                    "su comprobación de integridad."
                ),
                "selected_layer": None,
                "profile": None,
                "scope": "candidate",
            }
        if projection is not None:
            projection["scope"] = "candidate"
            substitutions[layer.id] = projection

    active_rows = db.execute(
        select(
            ReferenceLayerDeliveryState,
            ReferenceDeliveryVersion,
            ReferenceSyncRun,
        )
        .join(
            ReferenceDeliveryVersion,
            ReferenceDeliveryVersion.id
            == ReferenceLayerDeliveryState.active_version_id,
        )
        .join(
            ReferenceSyncRun,
            ReferenceSyncRun.id
            == ReferenceDeliveryVersion.sync_run_id,
        )
        .where(
            ReferenceLayerDeliveryState.provider_key
            == snapshot.provider_key,
            ReferenceLayerDeliveryState.layer_id.in_(
                [layer.id for layer in layers]
            ),
            ReferenceLayerDeliveryState.status == "active",
            ReferenceLayerDeliveryState.active_version_id.is_not(None),
        )
    ).all()
    layers_by_id = {layer.id: layer for layer in layers}
    active_substitution_layers: set[int] = set()
    for state, version, run in active_rows:
        layer = layers_by_id.get(state.layer_id)
        if layer is None:
            continue
        service = services_by_id.get(layer.service_id or -1)
        try:
            frozen_projection = reviewed_ign_ortho_source_projection(
                run.source_definition_json
            )
        except ReviewedOrthoEvidenceError:
            frozen_is_reviewed = True
        else:
            frozen_is_reviewed = frozen_projection is not None
        if (
            layer.id not in substitutions
            and not frozen_is_reviewed
        ):
            continue
        active_substitution_layers.add(layer.id)
        substitutions[layer.id] = _active_reviewed_ortho_projection(
            service=service,
            layer=layer,
            version=version,
            run=run,
        )

    primary_sources = list(
        db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key == snapshot.provider_key,
                ReferenceLayerSource.layer_id.in_(
                    [layer.id for layer in layers]
                ),
                ReferenceLayerSource.enabled.is_(True),
                ReferenceLayerSource.is_primary.is_(True),
            )
        )
    )
    for source in primary_sources:
        if source.layer_id in active_substitution_layers:
            continue
        try:
            projection = reviewed_ign_ortho_source_projection(
                {
                    "protocol": source.protocol,
                    "target_kind": source.target_kind,
                    "endpoint_url": source.endpoint_url,
                    "remote_name": source.remote_name,
                    "sync_strategy": source.sync_strategy,
                    "priority": source.priority,
                    "config": source.config_json,
                }
            )
        except ReviewedOrthoEvidenceError:
            substitutions[source.layer_id] = {
                "equivalence_status": "invalid",
                "public_notice": (
                    "La evidencia versionada de la sustitución no coincide "
                    "con la fuente primaria y debe revisarse."
                ),
                "selected_layer": None,
                "profile": None,
                "scope": "candidate",
            }
        else:
            if projection is not None:
                projection["scope"] = "candidate"
                substitutions[source.layer_id] = projection
    settings: dict[int, OrganizationReferenceLayerSetting] = {}
    if organization_id is not None:
        settings = {
            item.layer_id: item
            for item in db.scalars(
                select(OrganizationReferenceLayerSetting).where(
                    OrganizationReferenceLayerSetting.organization_id
                    == organization_id
                )
            )
        }

    proxy_delivery_availability = catalog_delivery_availability(
        db,
        snapshot=snapshot,
        services=services,
        layers=layers,
        styles=styles,
        enabled=app_settings.reference_remote_proxy_enabled,
    )
    local_delivery_availability = catalog_local_delivery_availability(
        db,
        provider_key=snapshot.provider_key,
        layers=layers,
        styles=styles,
    )
    local_metadata_availability = {
        layer.id: False for layer in layers
    }
    if any(
        availability is not None
        and availability.delivery_available
        for availability in local_delivery_availability.values()
    ):
        try:
            with _reference_blob_store() as store:
                local_metadata_availability = (
                    catalog_local_metadata_availability(
                        db,
                        store,
                        provider_key=snapshot.provider_key,
                        layers=layers,
                        local_availability=(
                            local_delivery_availability
                        ),
                    )
                )
        except ReferenceBlobStoreError:
            logger.error(
                "Local reference metadata storage is unavailable",
                exc_info=True,
            )
    delivery_availability = {
        layer.id: (
            local_delivery_availability[layer.id]
            if local_delivery_availability[layer.id] is not None
            else proxy_delivery_availability[layer.id]
        )
        for layer in layers
    }
    mirror_statuses = catalog_mirror_statuses(
        db,
        provider_key=snapshot.provider_key,
        layers=layers,
        styles=styles,
        local_availability=local_delivery_availability,
    )
    service_attributions = effective_service_attributions(
        db,
        services=services,
    )
    # A proxied service still owes its provider the credit its licence demands,
    # and SIUR ships none in the catalog. Mirror authorizations win when they
    # exist, because a reviewer wrote those by hand for an exact source.
    for service_id, credit in attested_proxy_attributions(
        db,
        services=services,
    ).items():
        if not service_attributions.get(service_id):
            service_attributions[service_id] = credit

    # El catálogo de SIUR no publica límites ni zooms para sus fondos, así que
    # se le añaden los que el espejo tiene de verdad. Sin esto el visor se abre
    # donde diga una constante y deja acercarse ocho niveles más allá de la
    # última tesela que existe.
    served_coverage = catalog_served_tile_coverage(
        db,
        provider_key=snapshot.provider_key,
        layers=layers,
        availability=local_delivery_availability,
    )
    layer_reads: list[ReferenceLayerRead] = []
    for layer in layers:
        setting = settings.get(layer.id)
        effective_visible = layer.default_visible
        effective_opacity = Decimal(layer.default_opacity)
        if setting is not None:
            if setting.visible is not None:
                effective_visible = setting.visible
            if setting.opacity is not None:
                effective_opacity = Decimal(setting.opacity)
        availability = delivery_availability[layer.id]
        coverage = served_coverage.get(layer.id)
        mirror_status = mirror_statuses[layer.id]
        substitution = substitutions.get(layer.id)
        layer_reads.append(
            ReferenceLayerRead.model_validate(layer).model_copy(
                update={
                    "effective_visible": effective_visible,
                    "effective_opacity": float(effective_opacity),
                    "bounds_json": (
                        layer.bounds_json
                        or (
                            coverage.bounds
                            if coverage is not None
                            else None
                        )
                    ),
                    "min_zoom": (
                        layer.min_zoom
                        if layer.min_zoom is not None
                        else (
                            coverage.min_zoom
                            if coverage is not None
                            else None
                        )
                    ),
                    "max_zoom": (
                        layer.max_zoom
                        if layer.max_zoom is not None
                        else (
                            coverage.max_zoom
                            if coverage is not None
                            else None
                        )
                    ),
                    "delivery_available": (
                        availability.delivery_available
                    ),
                    "identify_available": (
                        availability.identify_available
                    ),
                    "delivery_blocker": availability.delivery_blocker,
                    "available_style_ids": list(
                        availability.available_style_ids
                    ),
                    "legend_available": (
                        availability.legend_available
                    ),
                    "metadata_available": (
                        local_metadata_availability[layer.id]
                    ),
                    "source_substitution_status": (
                        substitution.get("equivalence_status")
                        if substitution is not None
                        else None
                    ),
                    "source_substitution_notice": (
                        substitution.get("public_notice")
                        if substitution is not None
                        else None
                    ),
                    "source_substitution_selected_layer": (
                        substitution.get("selected_layer")
                        if substitution is not None
                        else None
                    ),
                    "source_substitution_profile": (
                        substitution.get("profile")
                        if substitution is not None
                        else None
                    ),
                    "source_substitution_scope": (
                        substitution.get("scope")
                        if substitution is not None
                        else None
                    ),
                    "source_substitution_attribution": (
                        substitution.get("required_attribution")
                        if substitution is not None
                        else None
                    ),
                    "source_substitution_content_sha256": (
                        substitution.get("delivery_content_sha256")
                        if substitution is not None
                        else None
                    ),
                    "mirror_status": mirror_status.status,
                    "active_version_id": mirror_status.active_version_id,
                    "active_generation": mirror_status.active_generation,
                    "active_source_version": (
                        mirror_status.active_source_version
                    ),
                    "active_reference_at": mirror_status.active_reference_at,
                    "active_created_at": mirror_status.active_created_at,
                    "last_run_status": mirror_status.last_run_status,
                    "last_checked_at": mirror_status.last_checked_at,
                    "last_sync_error_code": mirror_status.last_error_code,
                    "last_sync_error_summary": mirror_status.last_error_summary,
                    "next_check_at": mirror_status.next_check_at,
                }
            )
        )

    return ReferenceCatalogRead(
        snapshot=ReferenceCatalogSnapshotRead.model_validate(snapshot),
        organization_id=organization_id,
        services=[
            ReferenceServiceRead.model_validate(item).model_copy(
                update={"attribution": service_attributions[item.id]}
            )
            for item in services
        ],
        layers=layer_reads,
        styles=[
            ReferenceLayerStyleRead.model_validate(item).model_copy(
                update={
                    "legend_available": (
                        item.id
                        in delivery_availability[
                            item.layer_id
                        ].available_legend_style_ids
                    )
                }
            )
            for item in styles
        ],
    )


@router.get(
    "/organizations/{organization_id}/reference-layers/{layer_id}"
    "/metadata.json",
    response_class=Response,
)
def get_reference_layer_metadata(
    organization_id: Annotated[int, Path(ge=1)],
    layer_id: Annotated[int, Path(ge=1)],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    require_catalog_view(db, current_user, organization_id)
    layer = db.get(ReferenceLayer, layer_id)
    if layer is None or layer.node_type != "layer":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reference layer not found",
        )
    try:
        with _reference_blob_store() as store:
            document = resolve_local_metadata(
                db,
                store,
                layer=layer,
            )
    except (LocalMetadataError, ReferenceBlobStoreError):
        logger.warning(
            "Active local reference metadata failed validation",
            exc_info=True,
            extra={"layer_id": layer_id},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Local reference metadata is not available",
        ) from None
    return Response(
        content=document.body,
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "ETag": f'"{document.sha256}"',
            "Vary": "Authorization, Cookie",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Content-Disposition": (
                f'inline; filename="siur-layer-{layer_id}-metadata.json"'
            ),
        },
    )


@router.put(
    "/organizations/{organization_id}/reference-layers/{layer_id}/settings",
    response_model=ReferenceLayerSettingRead,
)
def update_reference_layer_setting(
    organization_id: int,
    layer_id: int,
    payload: ReferenceLayerSettingUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OrganizationReferenceLayerSetting:
    require_catalog_manage(db, current_user, organization_id)
    layer = db.get(ReferenceLayer, layer_id)
    if layer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reference layer not found",
        )
    if layer.node_type != "layer":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Reference layer settings require a layer node",
        )

    setting = db.scalar(
        select(OrganizationReferenceLayerSetting)
        .where(
            OrganizationReferenceLayerSetting.organization_id == organization_id,
            OrganizationReferenceLayerSetting.layer_id == layer_id,
        )
        .with_for_update()
    )
    if setting is None:
        setting = OrganizationReferenceLayerSetting(
            organization_id=organization_id,
            layer_id=layer_id,
        )
        db.add(setting)
    setting.visible = payload.visible
    setting.opacity = payload.opacity
    setting.updated_by_id = current_user.id

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Reference layer setting conflicts with existing data",
        ) from None
    db.refresh(setting)
    return setting


@router.delete(
    "/organizations/{organization_id}/reference-layers/{layer_id}/settings",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_reference_layer_setting(
    organization_id: int,
    layer_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    require_catalog_manage(db, current_user, organization_id)
    setting = db.scalar(
        select(OrganizationReferenceLayerSetting).where(
            OrganizationReferenceLayerSetting.organization_id == organization_id,
            OrganizationReferenceLayerSetting.layer_id == layer_id,
        )
    )
    if setting is not None:
        db.delete(setting)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _reference_blob_store() -> ReferenceBlobStore:
    return ReferenceBlobStore(
        app_settings.reference_storage_root,
        max_blob_bytes=app_settings.reference_blob_max_bytes,
        quota_bytes=app_settings.reference_storage_quota_bytes,
        min_free_bytes=app_settings.reference_storage_min_free_bytes,
        read_only=True,
    )
