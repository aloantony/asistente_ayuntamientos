from datetime import datetime, timedelta, timezone
import json
from sqlalchemy import select

from app.reference_layers.mirror_authorization import (
    apply_mirror_authorization_review,
    bind_sync_run_authorization,
    parse_mirror_authorization,
    url_origin,
)
from app.reference_layers.models import (
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
    ReferenceSyncRun,
)


def mirror_authorization_document(
    db,
    source: ReferenceLayerSource,
    *,
    decision: str = "approved",
    reviewed_at: datetime | None = None,
    supersedes_review_sha256: str | None = None,
    attribution: str | None = "Origen de prueba",
    allowed_origins: list[str] | None = None,
    permission_overrides: dict[str, bool] | None = None,
) -> bytes:
    layer = db.get(ReferenceLayer, source.layer_id)
    assert layer is not None and layer.service_id is not None
    granted = decision == "approved"
    permissions = {
        "metadata_probe": granted,
        "dataset_download": granted,
        "local_storage": granted,
        "local_service": granted,
        "bulk_tile_seed": granted and source.target_kind == "tiles",
    }
    permissions.update(permission_overrides or {})
    origin = url_origin(source.endpoint_url)
    value = {
        "schema_version": "siur-mirror-authorization-v1",
        "provider_key": source.provider_key,
        "service_id": layer.service_id,
        "layer_id": source.layer_id,
        "source_id": source.id,
        "source_definition_sha256": source.definition_sha256,
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "canonical_origin": origin,
        "allowed_origins": allowed_origins or [origin],
        "decision": decision,
        "reviewer": "Automated test reviewer",
        "reviewed_at": (
            reviewed_at
            or datetime(2026, 7, 26, 12, tzinfo=timezone.utc)
        ).isoformat().replace("+00:00", "Z"),
        "license": {
            "name": "Test open-data license",
            "url": "https://licenses.example.test/open-data",
            "terms": "Test-only permission evidence.",
            "attribution": attribution if granted else None,
        },
        "permissions": permissions,
        "supersedes_review_sha256": supersedes_review_sha256,
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode()


def authorize_mirror_source(
    db,
    source: ReferenceLayerSource,
    *,
    decision: str = "approved",
    reviewed_at: datetime | None = None,
    supersedes_review_sha256: str | None = None,
    attribution: str | None = "Origen de prueba",
    allowed_origins: list[str] | None = None,
    permission_overrides: dict[str, bool] | None = None,
):
    document = mirror_authorization_document(
        db,
        source,
        decision=decision,
        reviewed_at=reviewed_at,
        supersedes_review_sha256=supersedes_review_sha256,
        attribution=attribution,
        allowed_origins=allowed_origins,
        permission_overrides=permission_overrides,
    )
    evidence = parse_mirror_authorization(document)
    return apply_mirror_authorization_review(
        db,
        document,
        expected_review_sha256=evidence.review_sha256,
        expected_document_sha256=evidence.document_sha256,
    )


def ensure_authorized_mirror_source(
    db,
    source: ReferenceLayerSource,
    *,
    reviewed_at: datetime | None = None,
):
    latest = db.scalar(
        select(ReferenceMirrorAuthorizationReview)
        .where(
            ReferenceMirrorAuthorizationReview.source_id == source.id
        )
        .order_by(
            ReferenceMirrorAuthorizationReview.reviewed_at.desc(),
            ReferenceMirrorAuthorizationReview.id.desc(),
        )
        .limit(1)
    )
    if latest is None:
        return authorize_mirror_source(
            db,
            source,
            reviewed_at=reviewed_at,
        )
    if (
        latest.source_definition_sha256 == source.definition_sha256
        and latest.protocol == source.protocol
        and latest.target_kind == source.target_kind
        and latest.decision == "approved"
        and latest.allow_metadata_probe
        and latest.allow_dataset_download
        and latest.allow_local_storage
        and latest.allow_local_service
        and (
            source.target_kind != "tiles"
            or latest.allow_bulk_tile_seed
        )
    ):
        return latest
    moment = reviewed_at or latest.reviewed_at + timedelta(seconds=1)
    if moment <= latest.reviewed_at:
        moment = latest.reviewed_at + timedelta(seconds=1)
    return authorize_mirror_source(
        db,
        source,
        reviewed_at=moment,
        supersedes_review_sha256=latest.review_sha256,
    )


def supersede_mirror_authorization(
    db,
    source: ReferenceLayerSource,
    predecessor,
    *,
    decision: str,
    permission_overrides: dict[str, bool] | None = None,
):
    return authorize_mirror_source(
        db,
        source,
        decision=decision,
        reviewed_at=predecessor.reviewed_at + timedelta(seconds=1),
        allowed_origins=list(predecessor.allowed_origins_json),
        supersedes_review_sha256=predecessor.review_sha256,
        permission_overrides=permission_overrides,
    )


def bind_run_authorization(
    db,
    source: ReferenceLayerSource,
    run_or_id: ReferenceSyncRun | int,
):
    run = (
        db.get(ReferenceSyncRun, run_or_id)
        if isinstance(run_or_id, int)
        else run_or_id
    )
    assert run is not None
    review = bind_sync_run_authorization(db, run=run, source=source)
    db.commit()
    return review
