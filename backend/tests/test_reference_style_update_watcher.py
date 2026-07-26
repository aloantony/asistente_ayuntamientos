from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.resources import files
import json
from pathlib import Path
from threading import Barrier, Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import app.reference_layers.style_update_admin as style_update_admin
import app.reference_layers.style_update_watcher as style_update_watcher
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
)
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.mirror_authorization import url_origin
from app.reference_layers.mirror_orchestrator import classify_worker_failure
from app.reference_layers.models import (
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceStyleObservedVersion,
    ReferenceStyleUpdateCheck,
    ReferenceStyleUpdateReview,
)
from app.reference_layers.reviewed_style_evidence import (
    reviewed_miteco_mvt_style_reference,
)
from app.reference_layers.safe_download import (
    DownloadHTTPError,
    DownloadLimitError,
    DownloadUnavailableError,
    HTTPSDownloadResult,
)
from app.reference_layers.source_discovery import acquisition_candidates
from app.reference_layers.style_update_watcher import (
    OfficialStyleReviewRequiredError,
    apply_style_update_review,
    check_official_style_update,
    parse_style_update_review,
    plan_style_update_review,
    require_official_style_promotion_allowed,
    style_watch_target_for_source,
)
from support_reference_mirror_authorization import authorize_mirror_source


NOW = datetime(2026, 7, 26, 9, tzinfo=timezone.utc)
PROFILE = "miteco-flood-q10-ogc-api-features-v1"
CATALOG_ENDPOINT = (
    "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ10/wms.aspx"
)
CATALOG_LAYER = "Z.I. con alta probabilidad"


class FakeDownloader:
    def __init__(
        self,
        *,
        body: bytes = b"",
        result: HTTPSDownloadResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.body = body
        self.result = result
        self.error = error
        self.calls: list[dict[str, str | None]] = []

    def download(
        self,
        url,
        sink,
        *,
        etag=None,
        last_modified=None,
        accept=None,
    ):
        self.calls.append(
            {
                "url": url,
                "etag": etag,
                "last_modified": last_modified,
                "accept": accept,
            }
        )
        if self.error is not None:
            raise self.error
        sink.write(self.body)
        assert self.result is not None
        return self.result


def _catalog_definition(provider_key: str):
    service = ReferenceServiceDefinition(
        source_key="service",
        title="MITECO flood service",
        upstream_protocol="wms",
        base_url=CATALOG_ENDPOINT,
        default_format="image/png",
    )
    layer = ReferenceLayerDefinition(
        source_key="layer:siur:" + "1" * 64,
        node_type="layer",
        title=CATALOG_LAYER,
        service_key="service",
        remote_name=CATALOG_LAYER,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={"west": -7.6, "south": 39.9, "east": -1.3, "north": 43.4},
        min_zoom=6,
        max_zoom=18,
        style_name="default",
        styles=(
            ReferenceLayerStyleDefinition(
                source_key="default",
                title="Default",
                remote_name="default",
                is_default=True,
            ),
        ),
    )
    definition = ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://catalog.example.test/settings.json",
        raw_catalog={"revision": 1},
        services=(service,),
        layers=(layer,),
        retrieved_at=NOW - timedelta(days=1),
    )
    return definition, service, layer


def _seed_source(
    db,
    *,
    provider_key: str,
    authorize: bool = True,
    permission_overrides: dict[str, bool] | None = None,
):
    definition, service_definition, layer_definition = _catalog_definition(
        provider_key
    )
    apply_catalog_definition(db, definition)
    candidate = acquisition_candidates(service_definition, layer_definition)[0]
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.source_key == layer_definition.source_key,
        )
    )
    source = ReferenceLayerSource(
        provider_key=provider_key,
        layer_id=layer.id,
        source_key=candidate.source_key,
        protocol=candidate.protocol,
        target_kind=candidate.target_kind,
        endpoint_url=candidate.endpoint_url,
        remote_name=candidate.remote_name,
        sync_strategy=candidate.sync_strategy,
        config_json=candidate.config,
        definition_sha256=candidate.definition_sha256,
        enabled=True,
        is_primary=True,
        priority=candidate.priority,
        next_check_at=NOW,
    )
    db.add(source)
    db.commit()
    if authorize:
        authorize_mirror_source(
            db,
            source,
            allowed_origins=sorted(_source_origins(source)),
            permission_overrides=permission_overrides,
        )
    return source


@pytest.fixture
def committed_style_source(engine):
    """Seed rows visible to independent sessions used by race tests."""

    provider_key = f"style-watch-race-{uuid4().hex[:12]}"
    with Session(engine, expire_on_commit=False) as setup:
        source = _seed_source(setup, provider_key=provider_key)
        source_id = source.id
    yield source_id
    with Session(engine) as cleanup:
        source = cleanup.get(ReferenceLayerSource, source_id)
        if source is not None:
            source.enabled = False
            source.is_primary = False
            cleanup.commit()


def _source_origins(source: ReferenceLayerSource) -> set[str]:
    values: list[str] = [source.endpoint_url]
    stack = [source.config_json]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str) and value.startswith("https://"):
            values.append(value)
    return {url_origin(value) for value in values}


def _baseline_body() -> bytes:
    reference = reviewed_miteco_mvt_style_reference(PROFILE)
    assert reference is not None
    return (
        files("app.reference_layers")
        .joinpath(reference["local_evidence_resource"])
        .read_bytes()
    )


def _changed_body(*, semantic_change: bool = True) -> bytes:
    document = json.loads(_baseline_body())
    if semantic_change:
        document["layers"][0]["paint"]["fill-color"] = "#010203"
    return json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode()


def _result(
    source_url: str,
    body: bytes,
    *,
    etag: str = '"style-v1"',
    final_url: str | None = None,
    redirect_chain: tuple[str, ...] | None = None,
) -> HTTPSDownloadResult:
    final = final_url or source_url
    chain = redirect_chain or (source_url,)
    return HTTPSDownloadResult(
        source_url=source_url,
        final_url=final,
        status_code=200,
        not_modified=False,
        content_type="application/json",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        etag=etag,
        last_modified="Sun, 26 Jul 2026 08:00:00 GMT",
        redirects=len(chain) - 1,
        redirect_chain=chain,
    )


def _not_modified(source_url: str) -> HTTPSDownloadResult:
    return HTTPSDownloadResult(
        source_url=source_url,
        final_url=source_url,
        status_code=304,
        not_modified=True,
        content_type=None,
        size_bytes=0,
        sha256=None,
        etag='"style-v1"',
        last_modified="Sun, 26 Jul 2026 08:00:00 GMT",
        redirects=0,
        redirect_chain=(source_url,),
    )


def _review_document(
    source: ReferenceLayerSource,
    observed: ReferenceStyleObservedVersion,
    *,
    decision: str = "retain_vendored",
) -> bytes:
    target = style_watch_target_for_source(source)
    value = {
        "schema_version": "siur-style-update-review-v1",
        "provider_key": source.provider_key,
        "layer_id": source.layer_id,
        "source_id": source.id,
        "source_definition_sha256": source.definition_sha256,
        "profile": target.profile,
        "official_style_url": target.official_url,
        "baseline_raw_sha256": target.baseline_raw_sha256,
        "baseline_semantic_sha256": target.baseline_semantic_sha256,
        "observed_version_id": observed.id,
        "observed_raw_sha256": observed.raw_sha256,
        "observed_semantic_sha256": observed.semantic_sha256,
        "decision": decision,
        "reviewer": "Style reviewer",
        "reviewed_at": "2026-07-28T10:00:00Z",
        "rationale": "Reviewed exact candidate; keep committed local recipe.",
    }
    return json.dumps(value, indent=2, sort_keys=True).encode()


def _damage_candidate_blob(
    store: ReferenceBlobStore,
    observed: ReferenceStyleObservedVersion,
    mode: str,
) -> None:
    path = store.root / observed.storage_key
    if mode == "missing":
        path.unlink()
    else:
        path.write_bytes(b"x" * observed.size_bytes)


def test_exact_200_then_daily_304_records_no_candidate(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(db, provider_key="style-watch-unchanged")
    target = style_watch_target_for_source(source)
    body = _baseline_body()
    first_downloader = FakeDownloader(
        body=body,
        result=_result(target.official_url, body),
    )
    store = ReferenceBlobStore(tmp_path / "store")
    try:
        first = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=first_downloader,
        )
        unused = FakeDownloader(
            error=AssertionError("not-due check must not use the network")
        )
        not_due = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(hours=12),
            idempotency_key="scheduled:not-due",
            downloader=unused,
        )
        conditional = FakeDownloader(result=_not_modified(target.official_url))
        second = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=1),
            downloader=conditional,
        )
    finally:
        store.close()

    assert first.status == "unchanged"
    assert first.observed_version_id is None
    assert not_due.disposition == "not_due"
    assert unused.calls == []
    assert second.status == "unchanged"
    assert conditional.calls[0]["etag"] == '"style-v1"'
    assert conditional.calls[0]["last_modified"] is not None
    assert db.scalar(select(func.count(ReferenceStyleObservedVersion.id))) == 0


def test_orphan_304_is_persisted_as_error_evidence(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(db, provider_key="style-watch-orphan-304")
    target = style_watch_target_for_source(source)
    store = ReferenceBlobStore(tmp_path / "orphan-304")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                result=_not_modified(target.official_url)
            ),
        )
    finally:
        store.close()

    check = db.get(ReferenceStyleUpdateCheck, outcome.check_id)
    assert outcome.status == "error"
    assert check.http_status == 304
    assert check.error_code == "style_download_result_mismatch"
    assert check.not_modified is False


@pytest.mark.parametrize(
    ("failure_mode", "expected_code"),
    [
        ("parse", "style_check_error"),
        ("storage", "style_candidate_storage"),
    ],
)
def test_conclusive_changed_200_failure_blocks_through_later_404(
    db,
    tmp_path: Path,
    monkeypatch,
    failure_mode: str,
    expected_code: str,
) -> None:
    source = _seed_source(
        db,
        provider_key=f"style-watch-conclusive-{failure_mode}",
    )
    target = style_watch_target_for_source(source)
    baseline = _baseline_body()
    changed = (
        b'{"version":8,"version":9}'
        if failure_mode == "parse"
        else _changed_body()
    )
    store = ReferenceBlobStore(tmp_path / f"conclusive-{failure_mode}")
    try:
        check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=baseline,
                result=_result(target.official_url, baseline),
            ),
        )
        if failure_mode == "storage":
            def fail_storage(*args, **kwargs):
                del args, kwargs
                raise ReferenceBlobStoreError("injected CAS failure")

            monkeypatch.setattr(store, "put_stream", fail_storage)
        changed_outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=1),
            downloader=FakeDownloader(
                body=changed,
                result=_result(target.official_url, changed),
            ),
        )
        changed_check = db.get(
            ReferenceStyleUpdateCheck,
            changed_outcome.check_id,
        )
        assert changed_check.error_code == expected_code
        assert changed_check.http_status == 200
        assert changed_check.response_raw_sha256 != (
            changed_check.baseline_raw_sha256
        )
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )

        unexpected_304_downloader = FakeDownloader(
            result=_not_modified(target.official_url)
        )
        unexpected_304 = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=2),
            downloader=unexpected_304_downloader,
        )
        unexpected_304_check = db.get(
            ReferenceStyleUpdateCheck,
            unexpected_304.check_id,
        )
        assert unexpected_304.status == "error"
        assert unexpected_304_check.error_code == (
            "style_download_result_mismatch"
        )
        assert unexpected_304_downloader.calls[0]["etag"] is None
        assert (
            unexpected_304_downloader.calls[0]["last_modified"] is None
        )
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )

        transient = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=3),
            downloader=FakeDownloader(
                error=DownloadHTTPError(404, retry_after_seconds=None)
            ),
        )
        assert transient.status == "error"
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )
        recovered = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=4),
            downloader=FakeDownloader(
                body=baseline,
                result=_result(target.official_url, baseline),
            ),
        )
        assert recovered.status == "unchanged"
        require_official_style_promotion_allowed(
            db,
            source=source,
            store=store,
        )
    finally:
        store.close()


def test_304_validator_is_revalidated_after_concurrent_changed_error(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(db, provider_key="style-watch-stale-304")
    target = style_watch_target_for_source(source)
    baseline = _baseline_body()
    changed = b'{"version":8,"version":9}'
    store = ReferenceBlobStore(tmp_path / "stale-304")

    class InterleavingDownloader(FakeDownloader):
        def download(self, *args, **kwargs):
            concurrent = check_official_style_update(
                db,
                source_id=source.id,
                store=store,
                checked_at=NOW + timedelta(days=2),
                idempotency_key="manual:concurrent-changed-error",
                trigger_kind="manual",
                force=True,
                downloader=FakeDownloader(
                    body=changed,
                    result=_result(target.official_url, changed),
                ),
            )
            assert concurrent.status == "error"
            return super().download(*args, **kwargs)

    try:
        check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=baseline,
                result=_result(target.official_url, baseline),
            ),
        )
        stale_downloader = InterleavingDownloader(
            result=_not_modified(target.official_url)
        )
        stale = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=1),
            idempotency_key="manual:stale-304",
            trigger_kind="manual",
            force=True,
            downloader=stale_downloader,
        )
        stale_check = db.get(ReferenceStyleUpdateCheck, stale.check_id)
        assert stale.status == "error"
        assert stale_check.error_code == "stale_not_modified"
        assert stale_downloader.calls[0]["etag"] == '"style-v1"'
        assert stale_downloader.calls[0]["last_modified"] is not None
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )

        recovered = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=3),
            idempotency_key="manual:baseline-recovery",
            trigger_kind="manual",
            force=True,
            downloader=FakeDownloader(
                body=baseline,
                result=_result(target.official_url, baseline),
            ),
        )
        assert recovered.status == "unchanged"
        require_official_style_promotion_allowed(
            db,
            source=source,
            store=store,
        )
    finally:
        store.close()


def test_empty_http_200_is_persisted_as_error_evidence(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(db, provider_key="style-watch-empty-200")
    target = style_watch_target_for_source(source)
    body = b""
    store = ReferenceBlobStore(tmp_path / "empty-200")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
    finally:
        store.close()

    check = db.get(ReferenceStyleUpdateCheck, outcome.check_id)
    assert outcome.status == "error"
    assert check.http_status == 200
    assert check.response_size_bytes == 0
    assert check.response_raw_sha256 is None
    assert check.error_code == "style_download_result_mismatch"


@pytest.mark.parametrize("semantic_change", [False, True])
def test_changed_bytes_are_deduplicated_in_cas_and_block_promotion(
    db,
    tmp_path: Path,
    semantic_change: bool,
) -> None:
    source = _seed_source(
        db,
        provider_key=f"style-watch-changed-{str(semantic_change).lower()}",
    )
    target = style_watch_target_for_source(source)
    body = _changed_body(semantic_change=semantic_change)
    store = ReferenceBlobStore(tmp_path / f"store-{semantic_change}")
    try:
        first = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
        second = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=1),
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
        observed = db.get(
            ReferenceStyleObservedVersion,
            first.observed_version_id,
        )
        with store.open_blob(observed.storage_key) as stream:
            assert stream.read() == body
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )
    finally:
        store.close()

    assert first.status == "style_review_required"
    assert second.status == "style_review_required"
    assert second.observed_version_id == first.observed_version_id
    assert db.scalar(select(func.count(ReferenceStyleObservedVersion.id))) == 1
    assert observed.semantic_summary_json[
        "matches_vendored_semantics"
    ] is (not semantic_change)
def test_explicit_review_resolves_candidate_without_replacing_vendored_evidence(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(db, provider_key="style-watch-reviewed")
    target = style_watch_target_for_source(source)
    body = _changed_body()
    store = ReferenceBlobStore(tmp_path / "store")
    try:
        first = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
        observed = db.get(
            ReferenceStyleObservedVersion,
            first.observed_version_id,
        )
        review_document = _review_document(source, observed)
        evidence = parse_style_update_review(review_document)
        review = apply_style_update_review(
            db,
            review_document,
            store=store,
            expected_review_sha256=evidence.review_sha256,
            expected_document_sha256=evidence.document_sha256,
        )
        require_official_style_promotion_allowed(
            db,
            source=source,
            store=store,
        )
        second = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=1),
            downloader=FakeDownloader(result=_not_modified(target.official_url)),
        )
    finally:
        store.close()

    assert review.decision == "retain_vendored"
    assert second.status == "unchanged"
    assert second.observed_version_id == observed.id
    assert (
        reviewed_miteco_mvt_style_reference(PROFILE)[
            "local_evidence_sha256"
        ]
        == target.baseline_raw_sha256
    )


@pytest.mark.parametrize("damage_mode", ["missing", "corrupt"])
def test_review_plan_and_apply_reject_invalid_candidate_cas(
    db,
    tmp_path: Path,
    damage_mode: str,
) -> None:
    source = _seed_source(
        db,
        provider_key=f"style-watch-plan-cas-{damage_mode}",
    )
    target = style_watch_target_for_source(source)
    body = _changed_body()
    store = ReferenceBlobStore(tmp_path / f"plan-cas-{damage_mode}")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
        observed = db.get(
            ReferenceStyleObservedVersion,
            outcome.observed_version_id,
        )
        document = _review_document(source, observed)
        evidence = parse_style_update_review(document)
        _damage_candidate_blob(store, observed, damage_mode)

        with pytest.raises(
            ValueError,
            match="CAS evidence failed integrity",
        ):
            plan_style_update_review(db, document, store=store)
        with pytest.raises(
            ValueError,
            match="CAS evidence failed integrity",
        ):
            apply_style_update_review(
                db,
                document,
                store=store,
                expected_review_sha256=evidence.review_sha256,
                expected_document_sha256=evidence.document_sha256,
            )
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )
    finally:
        store.close()
    assert db.scalar(select(func.count(ReferenceStyleUpdateReview.id))) == 0


@pytest.mark.parametrize("damage_mode", ["missing", "corrupt"])
def test_resolved_review_gate_rejects_later_candidate_cas_loss(
    db,
    tmp_path: Path,
    damage_mode: str,
) -> None:
    source = _seed_source(
        db,
        provider_key=f"style-watch-reviewed-cas-{damage_mode}",
    )
    target = style_watch_target_for_source(source)
    body = _changed_body()
    store = ReferenceBlobStore(tmp_path / f"reviewed-cas-{damage_mode}")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
        observed = db.get(
            ReferenceStyleObservedVersion,
            outcome.observed_version_id,
        )
        document = _review_document(source, observed)
        evidence = parse_style_update_review(document)
        apply_style_update_review(
            db,
            document,
            store=store,
            expected_review_sha256=evidence.review_sha256,
            expected_document_sha256=evidence.document_sha256,
        )
        followup = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=1),
            downloader=FakeDownloader(
                result=_not_modified(target.official_url)
            ),
        )
        assert followup.status == "unchanged"
        assert followup.observed_version_id == observed.id
        require_official_style_promotion_allowed(
            db,
            source=source,
            store=store,
        )
        _damage_candidate_blob(store, observed, damage_mode)
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )
    finally:
        store.close()


def test_vendor_update_required_review_keeps_promotion_blocked(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(
        db,
        provider_key="style-watch-vendor-update-required",
    )
    target = style_watch_target_for_source(source)
    body = _changed_body()
    store = ReferenceBlobStore(tmp_path / "vendor-update-required")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
        observed = db.get(
            ReferenceStyleObservedVersion,
            outcome.observed_version_id,
        )
        document = _review_document(
            source,
            observed,
            decision="vendor_update_required",
        )
        evidence = parse_style_update_review(document)
        apply_style_update_review(
            db,
            document,
            store=store,
            expected_review_sha256=evidence.review_sha256,
            expected_document_sha256=evidence.document_sha256,
        )

        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                db,
                source=source,
                store=store,
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    "error",
    [
        DownloadHTTPError(404, retry_after_seconds=None),
        DownloadUnavailableError("network unavailable", code="connection_failed"),
    ],
)
def test_404_or_network_failure_is_durable_and_keeps_last_success_usable(
    db,
    tmp_path: Path,
    error: BaseException,
) -> None:
    source = _seed_source(
        db,
        provider_key=f"style-watch-error-{getattr(error, 'code', 'http')}",
    )
    target = style_watch_target_for_source(source)
    baseline = _baseline_body()
    store = ReferenceBlobStore(tmp_path / getattr(error, "code", "http"))
    try:
        check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=baseline,
                result=_result(target.official_url, baseline),
            ),
        )
        failed = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW + timedelta(days=1),
            downloader=FakeDownloader(error=error),
        )
        require_official_style_promotion_allowed(
            db,
            source=source,
            store=store,
        )
    finally:
        store.close()

    assert failed.status == "error"
    check = db.get(ReferenceStyleUpdateCheck, failed.check_id)
    assert check.error_code in {"http_404", "connection_failed"}
    assert check.next_check_at == NOW + timedelta(days=2)
    assert db.scalar(select(func.count(ReferenceStyleObservedVersion.id))) == 0


@pytest.mark.parametrize(
    ("body", "result_factory", "error", "expected_code"),
    [
        (
            b'{"version":8,"version":9}',
            lambda target, body: _result(target.official_url, body),
            None,
            "style_check_error",
        ),
        (
            b"",
            None,
            DownloadLimitError(
                "style response exceeds limit",
                code="response_too_large",
            ),
            "response_too_large",
        ),
    ],
)
def test_json_and_byte_limits_fail_without_candidate(
    db,
    tmp_path: Path,
    body: bytes,
    result_factory,
    error,
    expected_code: str,
) -> None:
    source = _seed_source(
        db,
        provider_key=f"style-watch-limit-{expected_code}",
    )
    target = style_watch_target_for_source(source)
    downloader = (
        FakeDownloader(error=error)
        if error is not None
        else FakeDownloader(
            body=body,
            result=result_factory(target, body),
        )
    )
    store = ReferenceBlobStore(tmp_path / expected_code)
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=downloader,
        )
    finally:
        store.close()
    check = db.get(ReferenceStyleUpdateCheck, outcome.check_id)
    assert outcome.status == "error"
    assert check.error_code == expected_code
    assert db.scalar(select(func.count(ReferenceStyleObservedVersion.id))) == 0


def test_cross_origin_redirect_result_is_rejected(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(db, provider_key="style-watch-redirect")
    target = style_watch_target_for_source(source)
    body = _changed_body()
    redirected = "https://attacker.example.test/style.json"
    downloader = FakeDownloader(
        body=body,
        result=_result(
            target.official_url,
            body,
            final_url=redirected,
            redirect_chain=(target.official_url, redirected),
        ),
    )
    store = ReferenceBlobStore(tmp_path / "redirect")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=downloader,
        )
    finally:
        store.close()
    check = db.get(ReferenceStyleUpdateCheck, outcome.check_id)
    assert check.error_code == "style_download_result_mismatch"
    assert check.response_final_url is None
    assert db.scalar(select(func.count(ReferenceStyleObservedVersion.id))) == 0


def test_metadata_probe_permission_is_required_before_network(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(
        db,
        provider_key="style-watch-no-metadata-auth",
        permission_overrides={"metadata_probe": False},
    )
    downloader = FakeDownloader(
        error=AssertionError("unauthorized metadata probe used the network")
    )
    store = ReferenceBlobStore(tmp_path / "no-auth")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=downloader,
        )
    finally:
        store.close()
    check = db.get(ReferenceStyleUpdateCheck, outcome.check_id)
    assert downloader.calls == []
    assert check.error_code == "mirror_authorization_restricted"
    assert check.authorization_review_id is None


def test_operator_cli_lists_template_then_dry_runs_and_applies_exact_hashes(
    db,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = _seed_source(db, provider_key="style-watch-cli")
    target = style_watch_target_for_source(source)
    body = _changed_body()
    store_root = tmp_path / "cli-store"
    with ReferenceBlobStore(store_root) as store:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
    monkeypatch.setattr(
        style_update_admin,
        "register_all_models",
        lambda: None,
    )
    monkeypatch.setattr(
        style_update_admin,
        "SessionLocal",
        lambda: nullcontext(db),
    )
    monkeypatch.setattr(
        style_update_admin,
        "build_style_review_store",
        lambda: ReferenceBlobStore(store_root),
    )

    assert style_update_admin.main(
        ["status", "--source-id", str(source.id)]
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["pending_count"] == 1
    pending = status["pending_candidates"][0]
    assert pending["observed_version_id"] == outcome.observed_version_id
    assert pending["official_style_url"] == target.official_url
    assert pending["candidate_integrity"] == "verified"
    template = pending["review_document_template"]
    assert template["decision"] is None
    template.update(
        {
            "decision": "retain_vendored",
            "reviewer": "Style operator",
            "reviewed_at": "2026-07-28T10:00:00Z",
            "rationale": "Exact candidate reviewed against local adaptation.",
        }
    )
    review_path = tmp_path / "style-review.json"
    review_path.write_bytes(
        json.dumps(template, indent=2, sort_keys=True).encode()
    )

    assert style_update_admin.main(
        ["review", "--file", str(review_path)]
    ) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["applied"] is False
    assert dry_run["observed_version_id"] == outcome.observed_version_id
    assert db.scalar(select(ReferenceStyleUpdateReview.id)) is None

    assert style_update_admin.main(
        ["review", "--file", str(review_path), "--apply"]
    ) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error_code"] == "style_update_review_document_rejected"

    assert style_update_admin.main(
        [
            "review",
            "--file",
            str(review_path),
            "--apply",
            "--expected-review-sha256",
            dry_run["review_sha256"],
            "--expected-document-sha256",
            dry_run["document_sha256"],
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply"
    assert applied["applied"] is True
    assert applied["review_id"] == db.scalar(
        select(ReferenceStyleUpdateReview.id)
    )

    assert style_update_admin.main(["status"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["pending_count"] == 0


def test_operator_cli_rejects_symlink_review_document(
    tmp_path: Path,
    capsys,
) -> None:
    target = tmp_path / "review-target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "review.json"
    link.symlink_to(target)

    assert style_update_admin.main(
        ["review", "--file", str(link)]
    ) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error_code"] == "style_update_review_document_rejected"


def test_pending_style_candidate_has_stable_nonretryable_worker_code() -> None:
    failure = classify_worker_failure(
        OfficialStyleReviewRequiredError(
            "unreviewed official style candidate"
        )
    )
    assert failure.code == "style_review_required"
    assert failure.retryable is False
    assert failure.outcome == "rejected"


def test_observation_check_and_review_rows_are_immutable(
    db,
    tmp_path: Path,
) -> None:
    source = _seed_source(db, provider_key="style-watch-immutable")
    target = style_watch_target_for_source(source)
    body = _changed_body()
    store = ReferenceBlobStore(tmp_path / "immutable")
    try:
        outcome = check_official_style_update(
            db,
            source_id=source.id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=body,
                result=_result(target.official_url, body),
            ),
        )
        observed = db.get(
            ReferenceStyleObservedVersion,
            outcome.observed_version_id,
        )
        document = _review_document(source, observed)
        evidence = parse_style_update_review(document)
        review = apply_style_update_review(
            db,
            document,
            store=store,
            expected_review_sha256=evidence.review_sha256,
            expected_document_sha256=evidence.document_sha256,
        )
    finally:
        store.close()

    for table, row_id in (
        ("reference_style_observed_versions", observed.id),
        ("reference_style_update_checks", outcome.check_id),
        ("reference_style_update_reviews", review.id),
    ):
        with pytest.raises(DBAPIError):
            db.execute(
                text(f"UPDATE {table} SET id = id WHERE id = :id"),
                {"id": row_id},
            )
            db.commit()
        db.rollback()


def test_changed_200_waits_for_304_persistence_and_remains_conclusive(
    engine,
    committed_style_source: int,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_id = committed_style_source
    store = ReferenceBlobStore(tmp_path / "304-before-changed-200")
    baseline = _baseline_body()
    changed = _changed_body()
    with Session(engine, expire_on_commit=False) as setup:
        source = setup.get(ReferenceLayerSource, source_id)
        target = style_watch_target_for_source(source)
        baseline_outcome = check_official_style_update(
            setup,
            source_id=source_id,
            store=store,
            checked_at=NOW,
            downloader=FakeDownloader(
                body=baseline,
                result=_result(target.official_url, baseline),
            ),
        )
        assert baseline_outcome.status == "unchanged"

    stale_downloaded = Event()
    stale_holds_lock = Event()
    release_stale = Event()
    stale_session: list[Session] = []
    original_validator = (
        style_update_watcher._current_conditional_validator
    )

    def pause_stale_check_while_it_holds_the_lock(db, target):
        validator = original_validator(db, target)
        if (
            stale_session
            and db is stale_session[0]
            and stale_downloaded.is_set()
            and not stale_holds_lock.is_set()
        ):
            stale_holds_lock.set()
            assert release_stale.wait(timeout=5)
        return validator

    monkeypatch.setattr(
        style_update_watcher,
        "_current_conditional_validator",
        pause_stale_check_while_it_holds_the_lock,
    )

    class SignalingDownloader(FakeDownloader):
        def __init__(self, signal: Event, **kwargs) -> None:
            super().__init__(**kwargs)
            self.signal = signal

        def download(self, *args, **kwargs):
            result = super().download(*args, **kwargs)
            self.signal.set()
            return result

    def run_stale_304():
        with Session(engine, expire_on_commit=False) as worker:
            stale_session.append(worker)
            return check_official_style_update(
                worker,
                source_id=source_id,
                store=store,
                checked_at=NOW + timedelta(days=2),
                downloader=SignalingDownloader(
                    stale_downloaded,
                    result=_not_modified(target.official_url),
                ),
            )

    def run_changed_200():
        with engine.connect() as connection:
            def before_cursor_execute(
                conn,
                cursor,
                statement,
                parameters,
                context,
                executemany,
            ) -> None:
                del conn, cursor, parameters, context, executemany
                if (
                    "pg_advisory_xact_lock" in statement
                    and "pg_try_advisory_xact_lock" not in statement
                ):
                    release_stale.set()

            def after_cursor_execute(
                conn,
                cursor,
                statement,
                parameters,
                context,
                executemany,
            ) -> None:
                del conn, cursor, parameters, context, executemany
                if "pg_try_advisory_xact_lock" in statement:
                    release_stale.set()

            event.listen(
                connection,
                "before_cursor_execute",
                before_cursor_execute,
            )
            event.listen(
                connection,
                "after_cursor_execute",
                after_cursor_execute,
            )
            try:
                with Session(
                    bind=connection,
                    expire_on_commit=False,
                ) as worker:
                    return check_official_style_update(
                        worker,
                        source_id=source_id,
                        store=store,
                        checked_at=NOW + timedelta(days=1),
                        downloader=FakeDownloader(
                            body=changed,
                            result=_result(
                                target.official_url,
                                changed,
                            ),
                        ),
                    )
            finally:
                event.remove(
                    connection,
                    "before_cursor_execute",
                    before_cursor_execute,
                )
                event.remove(
                    connection,
                    "after_cursor_execute",
                    after_cursor_execute,
                )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            stale_future = pool.submit(run_stale_304)
            assert stale_holds_lock.wait(timeout=5)
            changed_future = pool.submit(run_changed_200)
            stale = stale_future.result(timeout=5)
            changed_outcome = changed_future.result(timeout=5)

        assert stale.status == "unchanged"
        assert changed_outcome.disposition == "recorded"
        assert changed_outcome.status == "style_review_required"
        with Session(engine) as verification:
            source = verification.get(ReferenceLayerSource, source_id)
            stale_check = verification.get(
                ReferenceStyleUpdateCheck,
                stale.check_id,
            )
            changed_check = verification.get(
                ReferenceStyleUpdateCheck,
                changed_outcome.check_id,
            )
            assert changed_check.id > stale_check.id
            assert changed_check.checked_at < stale_check.checked_at
            with pytest.raises(OfficialStyleReviewRequiredError):
                require_official_style_promotion_allowed(
                    verification,
                    source=source,
                    store=store,
                )
    finally:
        release_stale.set()
        store.close()


def test_concurrent_200_idempotency_preserves_distinct_evidence(
    engine,
    committed_style_source: int,
    tmp_path: Path,
) -> None:
    source_id = committed_style_source
    store = ReferenceBlobStore(tmp_path / "concurrent-200-idempotency")
    baseline = _baseline_body()
    changed = _changed_body()
    with Session(engine) as setup:
        source = setup.get(ReferenceLayerSource, source_id)
        target = style_watch_target_for_source(source)

    def run_pair(
        *,
        key: str,
        checked_at: datetime,
        bodies: tuple[bytes, bytes],
    ):
        barrier = Barrier(2)

        class BarrierDownloader(FakeDownloader):
            def download(self, *args, **kwargs):
                result = super().download(*args, **kwargs)
                barrier.wait(timeout=5)
                return result

        def run(body: bytes):
            with Session(engine, expire_on_commit=False) as worker:
                return check_official_style_update(
                    worker,
                    source_id=source_id,
                    store=store,
                    checked_at=checked_at,
                    idempotency_key=key,
                    trigger_kind="manual",
                    force=True,
                    downloader=BarrierDownloader(
                        body=body,
                        result=_result(target.official_url, body),
                    ),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            return tuple(pool.map(run, bodies))

    try:
        identical_at = NOW + timedelta(days=5)
        identical = run_pair(
            key="manual:concurrent-identical-200",
            checked_at=identical_at,
            bodies=(baseline, baseline),
        )
        assert {item.disposition for item in identical} == {
            "recorded",
            "duplicate",
        }
        assert identical[0].check_id == identical[1].check_id

        distinct_at = NOW + timedelta(days=6)
        distinct = run_pair(
            key="manual:concurrent-distinct-200",
            checked_at=distinct_at,
            bodies=(baseline, changed),
        )
        assert all(item.disposition == "recorded" for item in distinct)
        assert distinct[0].check_id != distinct[1].check_id

        unused = FakeDownloader(
            error=AssertionError("idempotent replay must not use the network")
        )
        with Session(engine, expire_on_commit=False) as replay_session:
            replay = check_official_style_update(
                replay_session,
                source_id=source_id,
                store=store,
                checked_at=distinct_at,
                idempotency_key="manual:concurrent-distinct-200",
                trigger_kind="manual",
                force=True,
                downloader=unused,
            )
        assert replay.disposition == "duplicate"
        assert unused.calls == []

        with Session(engine) as verification:
            identical_checks = tuple(
                verification.scalars(
                    select(ReferenceStyleUpdateCheck).where(
                        ReferenceStyleUpdateCheck.source_id == source_id,
                        ReferenceStyleUpdateCheck.checked_at == identical_at,
                    )
                )
            )
            distinct_checks = tuple(
                verification.scalars(
                    select(ReferenceStyleUpdateCheck).where(
                        ReferenceStyleUpdateCheck.source_id == source_id,
                        ReferenceStyleUpdateCheck.checked_at == distinct_at,
                    )
                )
            )
            assert len(identical_checks) == 1
            assert len(distinct_checks) == 2
            assert {
                check.response_raw_sha256 for check in distinct_checks
            } == {
                hashlib.sha256(baseline).hexdigest(),
                hashlib.sha256(changed).hexdigest(),
            }
            distinct_keys = {
                check.idempotency_key for check in distinct_checks
            }
            assert "manual:concurrent-distinct-200" in distinct_keys
            assert len(distinct_keys) == 2
            assert any(
                key.startswith("concurrent:") for key in distinct_keys
            )
    finally:
        store.close()


def test_changed_200_lock_timeout_persists_fail_closed_retry_evidence(
    engine,
    committed_style_source: int,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_id = committed_style_source
    store = ReferenceBlobStore(tmp_path / "changed-200-lock-timeout")
    changed = _changed_body()
    with Session(engine) as setup:
        source = setup.get(ReferenceLayerSource, source_id)
        target = style_watch_target_for_source(source)

    monkeypatch.setattr(
        style_update_watcher,
        "_STYLE_PERSISTENCE_LOCK_TIMEOUT_MS",
        50,
    )
    holder = engine.connect()
    holder_transaction = holder.begin()
    holder.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {
            "lock_key": style_update_watcher._style_watcher_lock_key(
                target
            )
        },
    )
    checked_at = NOW + timedelta(days=7)
    started = monotonic()
    try:
        with Session(engine, expire_on_commit=False) as contender:
            outcome = check_official_style_update(
                contender,
                source_id=source_id,
                store=store,
                checked_at=checked_at,
                idempotency_key="manual:changed-lock-timeout",
                trigger_kind="manual",
                force=True,
                downloader=FakeDownloader(
                    body=changed,
                    result=_result(target.official_url, changed),
                ),
            )
    finally:
        holder_transaction.rollback()
        holder.close()
        store.close()

    assert monotonic() - started < 2
    assert outcome.disposition == "recorded"
    assert outcome.status == "error"
    with Session(engine) as verification:
        check = verification.get(
            ReferenceStyleUpdateCheck,
            outcome.check_id,
        )
        source = verification.get(ReferenceLayerSource, source_id)
        assert check.error_code == "style_persistence_lock_timeout"
        assert check.error_retryable is True
        assert check.idempotency_key.startswith("contention:")
        assert check.response_raw_sha256 == hashlib.sha256(changed).hexdigest()
        assert check.next_check_at == checked_at + timedelta(minutes=5)
        with pytest.raises(OfficialStyleReviewRequiredError):
            require_official_style_promotion_allowed(
                verification,
                source=source,
                store=None,
            )
