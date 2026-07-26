from __future__ import annotations

import logging
import threading

from app.reference_layers import mirror_runtime


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


def test_scheduler_once_checks_catalog_then_reconciles_and_enqueues(
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
        "enqueue_reference_sources_once",
        lambda *, reconcile: calls.append(("enqueue", reconcile)) or [],
    )

    mirror_runtime.run_scheduler(threading.Event(), once=True)

    assert calls == ["catalog", "reconcile", ("enqueue", False)]
