from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from importlib.resources import files
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.reference_layers.blob_store import ReferenceBlobStore
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
    finally:
        store.close()

    assert first.status == "style_review_required"
    assert second.status == "style_review_required"
    assert second.observed_version_id == first.observed_version_id
    assert db.scalar(select(func.count(ReferenceStyleObservedVersion.id))) == 1
    assert observed.semantic_summary_json[
        "matches_vendored_semantics"
    ] is (not semantic_change)
    with pytest.raises(OfficialStyleReviewRequiredError):
        require_official_style_promotion_allowed(db, source=source)


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
            expected_review_sha256=evidence.review_sha256,
            expected_document_sha256=evidence.document_sha256,
        )
        require_official_style_promotion_allowed(db, source=source)
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
    finally:
        store.close()
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
        expected_review_sha256=evidence.review_sha256,
        expected_document_sha256=evidence.document_sha256,
    )

    with pytest.raises(OfficialStyleReviewRequiredError):
        require_official_style_promotion_allowed(db, source=source)


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
    finally:
        store.close()

    assert failed.status == "error"
    check = db.get(ReferenceStyleUpdateCheck, failed.check_id)
    assert check.error_code in {"http_404", "connection_failed"}
    assert check.next_check_at == NOW + timedelta(days=2)
    require_official_style_promotion_allowed(db, source=source)
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
    finally:
        store.close()
    observed = db.get(
        ReferenceStyleObservedVersion,
        outcome.observed_version_id,
    )
    document = _review_document(source, observed)
    evidence = parse_style_update_review(document)
    review = apply_style_update_review(
        db,
        document,
        expected_review_sha256=evidence.review_sha256,
        expected_document_sha256=evidence.document_sha256,
    )

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
