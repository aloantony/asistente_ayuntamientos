from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.reference_layers.catalog import canonical_catalog_sha256
from app.reference_layers.catalog_watcher import (
    _catalog_watcher_lock_key,
    build_siur_catalog_downloader,
    check_siur_catalog_update,
    scheduled_siur_catalog_check_key,
)
from app.reference_layers.models import (
    ReferenceCatalogObservedVersion,
    ReferenceCatalogSnapshot,
    ReferenceCatalogUpdateCheck,
)
from app.reference_layers.safe_download import (
    DownloadUnavailableError,
    HTTPSDownloadResult,
)
from app.reference_layers.siur_settings import DEFAULT_SOURCE_URL


NOW = datetime(2026, 7, 26, 8, 30, tzinfo=timezone.utc)
LAST_MODIFIED = "Sun, 26 Jul 2026 08:00:00 GMT"


def catalog_document(version: int = 1, *, pretty: bool = False) -> bytes:
    value = {
        "version": version,
        "services": {},
        "layerGroups": [],
    }
    if pretty:
        return json.dumps(
            {
                "layerGroups": value["layerGroups"],
                "services": value["services"],
                "version": value["version"],
            },
            indent=2,
        ).encode()
    return json.dumps(value, separators=(",", ":")).encode()


def download_result(
    document: bytes,
    *,
    etag: str = '"catalog-v1"',
    last_modified: str = LAST_MODIFIED,
) -> HTTPSDownloadResult:
    return HTTPSDownloadResult(
        source_url=DEFAULT_SOURCE_URL,
        final_url=DEFAULT_SOURCE_URL,
        status_code=200,
        not_modified=False,
        content_type="application/json",
        size_bytes=len(document),
        sha256=hashlib.sha256(document).hexdigest(),
        etag=etag,
        last_modified=last_modified,
        redirects=0,
        redirect_chain=(DEFAULT_SOURCE_URL,),
    )


def not_modified_result(
    *,
    etag: str = '"catalog-v2"',
    last_modified: str = LAST_MODIFIED,
) -> HTTPSDownloadResult:
    return HTTPSDownloadResult(
        source_url=DEFAULT_SOURCE_URL,
        final_url=DEFAULT_SOURCE_URL,
        status_code=304,
        not_modified=True,
        content_type=None,
        size_bytes=0,
        sha256=None,
        etag=etag,
        last_modified=last_modified,
        redirects=0,
        redirect_chain=(DEFAULT_SOURCE_URL,),
    )


class FakeDownloader:
    def __init__(
        self,
        *,
        document: bytes = b"",
        result: HTTPSDownloadResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.document = document
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
        sink.write(self.document)
        assert self.result is not None
        return self.result


class TransactionInspectingDownloader(FakeDownloader):
    def __init__(
        self,
        *,
        db: Session,
        engine: Engine,
        document: bytes,
        result: HTTPSDownloadResult,
    ) -> None:
        super().__init__(document=document, result=result)
        self.db = db
        self.engine = engine
        self.saw_transaction_free_download = False

    def download(self, *args, **kwargs):
        assert self.db.in_transaction() is False
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
                {"lock_key": _catalog_watcher_lock_key("siur")},
            ).scalar_one()
            assert acquired is True
        finally:
            transaction.rollback()
            connection.close()
        self.saw_transaction_free_download = True
        return super().download(*args, **kwargs)


def create_current_snapshot(
    db: Session,
    document: bytes,
) -> ReferenceCatalogSnapshot:
    raw = json.loads(document)
    snapshot = ReferenceCatalogSnapshot(
        provider_key="siur",
        source_url=DEFAULT_SOURCE_URL,
        content_sha256=canonical_catalog_sha256(raw),
        definition_sha256="d" * 64,
        raw_catalog_json=raw,
        normalized_definition_json={},
        retrieved_at=NOW - timedelta(days=1),
        service_count=0,
        group_count=0,
        layer_count=0,
        unresolved_count=0,
        status="applied",
        is_current=True,
    )
    db.add(snapshot)
    db.commit()
    return snapshot


def test_matching_catalog_records_immutable_unchanged_evidence(
    db: Session,
) -> None:
    document = catalog_document()
    snapshot = create_current_snapshot(db, document)
    fake = FakeDownloader(document=document, result=download_result(document))

    outcome = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:matching",
        downloader=fake,
    )

    assert outcome.disposition == "recorded"
    assert outcome.status == "unchanged"
    assert outcome.observed_content_sha256 == snapshot.content_sha256
    check = db.get(ReferenceCatalogUpdateCheck, outcome.check_id)
    assert check is not None
    assert check.baseline_snapshot_id == snapshot.id
    assert check.http_status == 200
    assert check.response_etag == '"catalog-v1"'
    assert check.response_last_modified == LAST_MODIFIED
    assert check.response_raw_sha256 == hashlib.sha256(document).hexdigest()
    assert check.not_modified is False
    assert check.next_check_at == NOW + timedelta(days=1)
    assert check.observed_version.raw_catalog_json == json.loads(document)
    assert check.observed_version.analysis_json["parser"] == "siur-settings-v1"
    assert db.scalar(select(func.count(ReferenceCatalogSnapshot.id))) == 1
    assert db.get(ReferenceCatalogSnapshot, snapshot.id).is_current is True


def test_download_runs_without_database_transaction_or_provider_lock(
    db: Session,
    engine: Engine,
) -> None:
    document = catalog_document()
    create_current_snapshot(db, document)
    downloader = TransactionInspectingDownloader(
        db=db,
        engine=engine,
        document=document,
        result=download_result(document),
    )

    outcome = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:transaction-free-download",
        downloader=downloader,
    )

    assert outcome.disposition == "recorded"
    assert downloader.saw_transaction_free_download is True


def test_changed_catalog_is_staged_without_applying_it(db: Session) -> None:
    snapshot = create_current_snapshot(db, catalog_document(version=1))
    changed = catalog_document(version=2)
    fake = FakeDownloader(
        document=changed,
        result=download_result(changed, etag='"catalog-v2"'),
    )

    outcome = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:changed",
        downloader=fake,
    )

    assert outcome.status == "update_available"
    observed = db.scalar(select(ReferenceCatalogObservedVersion))
    assert observed is not None
    assert observed.content_sha256 == canonical_catalog_sha256(
        json.loads(changed)
    )
    assert observed.raw_catalog_json["version"] == 2
    assert db.scalar(select(func.count(ReferenceCatalogSnapshot.id))) == 1
    persisted_snapshot = db.get(ReferenceCatalogSnapshot, snapshot.id)
    assert persisted_snapshot.is_current is True
    assert persisted_snapshot.raw_catalog_json["version"] == 1


def test_conditional_304_reuses_version_and_preserves_update_state(
    db: Session,
) -> None:
    create_current_snapshot(db, catalog_document(version=1))
    changed = catalog_document(version=2)
    initial = FakeDownloader(
        document=changed,
        result=download_result(
            changed,
            etag='"catalog-v2"',
            last_modified=LAST_MODIFIED,
        ),
    )
    first = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:2026-07-26",
        downloader=initial,
    )
    first_check = db.get(ReferenceCatalogUpdateCheck, first.check_id)
    observed_version_id = first_check.observed_version_id
    conditional = FakeDownloader(result=not_modified_result())

    second = check_siur_catalog_update(
        db,
        checked_at=NOW + timedelta(days=1),
        idempotency_key="scheduled:2026-07-27",
        downloader=conditional,
    )

    assert second.status == "update_available"
    assert conditional.calls == [
        {
            "url": DEFAULT_SOURCE_URL,
            "etag": '"catalog-v2"',
            "last_modified": LAST_MODIFIED,
            "accept": "application/json",
        }
    ]
    second_check = db.get(ReferenceCatalogUpdateCheck, second.check_id)
    assert second_check.not_modified is True
    assert second_check.http_status == 304
    assert second_check.response_size_bytes == 0
    assert second_check.response_raw_sha256 is None
    assert second_check.observed_version_id == observed_version_id
    assert (
        db.scalar(select(func.count(ReferenceCatalogObservedVersion.id))) == 1
    )


def test_canonical_hash_ignores_json_formatting_and_key_order(
    db: Session,
) -> None:
    compact = catalog_document(pretty=False)
    reformatted = catalog_document(pretty=True)
    snapshot = create_current_snapshot(db, compact)
    assert hashlib.sha256(compact).hexdigest() != hashlib.sha256(
        reformatted
    ).hexdigest()
    fake = FakeDownloader(
        document=reformatted,
        result=download_result(reformatted),
    )

    outcome = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:reformatted",
        downloader=fake,
    )

    assert outcome.status == "unchanged"
    assert outcome.observed_content_sha256 == snapshot.content_sha256


def test_idempotency_and_daily_due_gate_do_not_redownload(db: Session) -> None:
    document = catalog_document()
    create_current_snapshot(db, document)
    initial = FakeDownloader(document=document, result=download_result(document))
    first = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:stable",
        downloader=initial,
    )
    unused = FakeDownloader(
        error=AssertionError("duplicate invocation downloaded again")
    )

    duplicate = check_siur_catalog_update(
        db,
        checked_at=NOW + timedelta(minutes=1),
        idempotency_key="scheduled:stable",
        downloader=unused,
    )
    not_due = check_siur_catalog_update(
        db,
        checked_at=NOW + timedelta(hours=1),
        idempotency_key="scheduled:new-key",
        downloader=unused,
    )

    assert first.disposition == "recorded"
    assert duplicate.disposition == "duplicate"
    assert duplicate.check_id == first.check_id
    assert not_due.disposition == "not_due"
    assert not_due.check_id == first.check_id
    assert unused.calls == []
    assert db.scalar(select(func.count(ReferenceCatalogUpdateCheck.id))) == 1


def test_retryable_download_error_is_durable_and_observable(
    db: Session,
) -> None:
    snapshot = create_current_snapshot(db, catalog_document())
    fake = FakeDownloader(
        error=DownloadUnavailableError(
            "upstream timed out",
            code="read_timeout",
        )
    )

    outcome = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:error",
        downloader=fake,
    )

    assert outcome.status == "error"
    check = db.get(ReferenceCatalogUpdateCheck, outcome.check_id)
    assert check.baseline_snapshot_id == snapshot.id
    assert check.observed_version_id is None
    assert check.error_code == "read_timeout"
    assert check.error_message == "upstream timed out"
    assert check.error_retryable is True
    assert check.next_check_at == NOW + timedelta(days=1)


def test_invalid_http_200_body_is_recorded_but_never_staged(
    db: Session,
) -> None:
    create_current_snapshot(db, catalog_document())
    invalid = b"not-json"
    fake = FakeDownloader(document=invalid, result=download_result(invalid))

    outcome = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:invalid",
        downloader=fake,
    )

    assert outcome.status == "error"
    check = db.get(ReferenceCatalogUpdateCheck, outcome.check_id)
    assert check.http_status == 200
    assert check.response_size_bytes == len(invalid)
    assert check.response_raw_sha256 == hashlib.sha256(invalid).hexdigest()
    assert check.error_code == "invalid_catalog_document"
    assert check.error_retryable is False
    assert (
        db.scalar(select(func.count(ReferenceCatalogObservedVersion.id))) == 0
    )


def test_unexpected_redirect_chain_is_rejected_before_staging(
    db: Session,
) -> None:
    create_current_snapshot(db, catalog_document())
    document = catalog_document(version=2)
    unsafe_result = HTTPSDownloadResult(
        source_url=DEFAULT_SOURCE_URL,
        final_url="https://catalog.example.test/settings.json",
        status_code=200,
        not_modified=False,
        content_type="application/json",
        size_bytes=len(document),
        sha256=hashlib.sha256(document).hexdigest(),
        etag='"unsafe"',
        last_modified=None,
        redirects=1,
        redirect_chain=(
            DEFAULT_SOURCE_URL,
            "https://catalog.example.test/settings.json",
        ),
    )
    fake = FakeDownloader(document=document, result=unsafe_result)

    outcome = check_siur_catalog_update(
        db,
        checked_at=NOW,
        idempotency_key="scheduled:unsafe-redirect",
        downloader=fake,
    )

    check = db.get(ReferenceCatalogUpdateCheck, outcome.check_id)
    assert check.status == "error"
    assert check.error_code == "download_result_mismatch"
    assert check.error_retryable is True
    assert (
        db.scalar(select(func.count(ReferenceCatalogObservedVersion.id))) == 0
    )


def test_busy_persistence_lock_is_nonblocking_after_download(
    engine: Engine,
) -> None:
    holder = engine.connect()
    holder_transaction = holder.begin()
    holder.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _catalog_watcher_lock_key("siur")},
    )
    contender_connection = engine.connect()
    contender = Session(bind=contender_connection)
    document = catalog_document()
    fake = FakeDownloader(
        document=document,
        result=download_result(document),
    )
    try:
        outcome = check_siur_catalog_update(
            contender,
            checked_at=NOW,
            idempotency_key="scheduled:lock-busy",
            downloader=fake,
        )
        assert outcome.disposition == "lock_busy"
        assert outcome.check_id is None
        assert len(fake.calls) == 1
    finally:
        contender.close()
        contender_connection.close()
        holder_transaction.rollback()
        holder.close()


def test_downloader_policy_and_daily_key_are_fixed_to_siur() -> None:
    downloader = build_siur_catalog_downloader()

    assert downloader.policy.allowed_origins == (
        "https://idecyl.jcyl.es",
    )
    assert downloader.policy.max_response_bytes == 2 * 1024 * 1024
    assert downloader.policy.allowed_content_types == frozenset(
        {"application/json", "text/json"}
    )
    assert scheduled_siur_catalog_check_key(
        datetime(2026, 7, 27, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    ) == "scheduled:2026-07-26"
