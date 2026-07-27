from __future__ import annotations

import logging
import threading

import pytest

from app.core.config import Settings
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers import mirror_runtime


def test_cli_registers_all_models_before_running_scheduler(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        mirror_runtime,
        "register_all_models",
        lambda: calls.append("register_models"),
    )
    monkeypatch.setattr(
        mirror_runtime,
        "run_scheduler",
        lambda stop_event, *, once: calls.append(
            f"scheduler:{once}:{stop_event.is_set()}"
        ),
    )

    assert mirror_runtime.main(["scheduler", "--once"]) == 0
    assert calls == ["register_models", "scheduler:True:False"]


def test_worker_prepares_transient_storage_before_processing(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Processor:
        def __init__(self, *, stop_event):
            assert stop_event.is_set() is False
            calls.append("processor")

        def process_next(self):
            calls.append("process")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(
        mirror_runtime,
        "prepare_reference_transient_storage",
        lambda: calls.append("prepare"),
    )
    monkeypatch.setattr(
        mirror_runtime,
        "MirrorRunProcessor",
        Processor,
    )

    mirror_runtime.run_worker(threading.Event(), once=True)

    assert calls == ["prepare", "processor", "process", "close"]


def test_worker_startup_purges_transient_crash_residue(
    tmp_path,
    monkeypatch,
) -> None:
    config = Settings(
        reference_storage_root=str(tmp_path / "persistent"),
        reference_transient_root=str(tmp_path / "transient"),
        reference_blob_max_bytes=2 * 1024 * 1024,
        reference_tile_archive_max_bytes=2 * 1024 * 1024,
        reference_storage_quota_bytes=4 * 1024 * 1024,
        reference_storage_min_free_bytes=0,
        _env_file=None,
    )
    with ReferenceBlobStore(
        config.reference_transient_root,
        max_blob_bytes=config.reference_blob_max_bytes,
        quota_bytes=config.reference_storage_quota_bytes,
    ) as transient:
        orphan_part = (
            transient.root
            / "staging"
            / f"{'a' * 32}.part"
        )
        orphan_part.write_bytes(b"raw source")
        orphan_workspace = (
            transient.root
            / "workspaces"
            / f"masked-geopackage-{'b' * 32}"
        )
        orphan_workspace.mkdir(parents=True)
        (orphan_workspace / "source.gpkg").write_bytes(b"raw source")

    monkeypatch.setattr(mirror_runtime, "settings", config)

    mirror_runtime.prepare_reference_transient_storage()

    assert list(
        (tmp_path / "transient" / "staging").iterdir()
    ) == []
    assert list(
        (tmp_path / "transient" / "workspaces").iterdir()
    ) == []


def test_worker_refuses_incomplete_transient_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    config = Settings(
        reference_storage_root=str(tmp_path / "persistent"),
        reference_transient_root=str(tmp_path / "transient"),
        reference_blob_max_bytes=2 * 1024 * 1024,
        reference_tile_archive_max_bytes=2 * 1024 * 1024,
        reference_storage_quota_bytes=4 * 1024 * 1024,
        reference_storage_min_free_bytes=0,
        _env_file=None,
    )
    with ReferenceBlobStore(
        config.reference_transient_root,
        max_blob_bytes=config.reference_blob_max_bytes,
        quota_bytes=config.reference_storage_quota_bytes,
    ) as transient:
        (transient.root / "staging" / "unexpected").write_bytes(
            b"unclassified residue"
        )

    monkeypatch.setattr(mirror_runtime, "settings", config)

    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        mirror_runtime.prepare_reference_transient_storage()


def test_catalog_watcher_poll_is_throttled_and_reports_evidence(
    monkeypatch,
    caplog,
) -> None:
    calls: list[object] = []

    def run_job() -> dict[str, object]:
        calls.append(object())
        return {
            "disposition": "recorded",
            "status": "unchanged",
            "check_id": 41,
            "next_check_at": "2026-07-27T18:00:00+00:00",
        }

    monkeypatch.setattr(
        mirror_runtime,
        "run_siur_catalog_update_check_job",
        run_job,
    )
    caplog.set_level(logging.INFO)

    assert mirror_runtime._poll_catalog_watcher_if_due(
        1_000.0,
        now=999.0,
        poll_seconds=300.0,
    ) == 1_000.0
    assert calls == []

    assert mirror_runtime._poll_catalog_watcher_if_due(
        1_000.0,
        now=1_000.0,
        poll_seconds=300.0,
    ) == 1_300.0
    assert len(calls) == 1
    assert "SIUR catalog watcher poll completed" in caplog.text


def test_catalog_watcher_failure_is_throttled_without_stopping_scheduler(
    monkeypatch,
    caplog,
) -> None:
    calls: list[str] = []

    def fail_job() -> dict[str, object]:
        calls.append("watcher")
        raise RuntimeError("sensitive upstream detail")

    monkeypatch.setattr(
        mirror_runtime,
        "run_siur_catalog_update_check_job",
        fail_job,
    )
    caplog.set_level(logging.ERROR)

    assert mirror_runtime._poll_catalog_watcher_if_due(
        0.0,
        now=50.0,
        poll_seconds=300.0,
    ) == 350.0
    assert calls == ["watcher"]
    assert "RuntimeError" in caplog.text
    assert "sensitive upstream detail" not in caplog.text


def test_catalog_watcher_invalid_job_result_is_isolated(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(
        mirror_runtime,
        "run_siur_catalog_update_check_job",
        lambda: None,
    )
    caplog.set_level(logging.ERROR)

    assert mirror_runtime._poll_catalog_watcher_if_due(
        0.0,
        now=75.0,
        poll_seconds=300.0,
    ) == 375.0
    assert "AttributeError" in caplog.text


def test_style_watcher_poll_is_throttled_and_reports_review_required(
    monkeypatch,
    caplog,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        mirror_runtime,
        "run_official_style_update_check_job",
        lambda: calls.append("styles")
        or {
            "source_count": 5,
            "recorded_count": 5,
            "review_required_count": 1,
            "error_count": 0,
        },
    )
    caplog.set_level(logging.INFO)

    assert mirror_runtime._poll_style_watcher_if_due(
        100.0,
        now=99.0,
        poll_seconds=300.0,
    ) == 100.0
    assert calls == []
    assert mirror_runtime._poll_style_watcher_if_due(
        100.0,
        now=100.0,
        poll_seconds=300.0,
    ) == 400.0
    assert calls == ["styles"]
    assert "Official style watcher poll completed" in caplog.text


def test_scheduler_once_checks_catalog_reconciles_styles_and_enqueues(
    monkeypatch,
) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        mirror_runtime,
        "run_siur_catalog_update_check_job",
        lambda: calls.append("catalog")
        or {
            "disposition": "not_due",
            "status": "unchanged",
            "check_id": 3,
            "next_check_at": "2026-07-27T18:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        mirror_runtime,
        "reconcile_reference_sources_once",
        lambda: calls.append("reconcile") or (),
    )
    monkeypatch.setattr(
        mirror_runtime,
        "run_official_style_update_check_job",
        lambda: calls.append("styles")
        or {
            "source_count": 5,
            "recorded_count": 0,
            "review_required_count": 0,
            "error_count": 0,
        },
    )
    monkeypatch.setattr(
        mirror_runtime,
        "enqueue_reference_sources_once",
        lambda *, reconcile: calls.append(("enqueue", reconcile)) or [],
    )

    mirror_runtime.run_scheduler(threading.Event(), once=True)

    assert calls == [
        "catalog",
        "reconcile",
        "styles",
        ("enqueue", False),
    ]


def test_scheduler_runs_style_watcher_when_source_reconcile_fails(
    monkeypatch,
    caplog,
) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        mirror_runtime,
        "run_siur_catalog_update_check_job",
        lambda: calls.append("catalog")
        or {
            "disposition": "not_due",
            "status": "unchanged",
            "check_id": 3,
            "next_check_at": "2026-07-27T18:00:00+00:00",
        },
    )

    def fail_reconcile():
        calls.append("reconcile")
        raise RuntimeError("persistent reconcile failure")

    monkeypatch.setattr(
        mirror_runtime,
        "reconcile_reference_sources_once",
        fail_reconcile,
    )
    monkeypatch.setattr(
        mirror_runtime,
        "run_official_style_update_check_job",
        lambda: calls.append("styles")
        or {
            "source_count": 5,
            "recorded_count": 5,
            "review_required_count": 0,
            "error_count": 0,
        },
    )
    monkeypatch.setattr(
        mirror_runtime,
        "enqueue_reference_sources_once",
        lambda *, reconcile: calls.append(("enqueue", reconcile)) or [],
    )
    caplog.set_level(logging.ERROR)

    mirror_runtime.run_scheduler(threading.Event(), once=True)

    assert calls == ["catalog", "reconcile", "styles"]
    assert "RuntimeError" in caplog.text
    assert "persistent reconcile failure" not in caplog.text
