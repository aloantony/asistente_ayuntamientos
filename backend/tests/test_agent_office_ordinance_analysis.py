import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.agent_office import (
    ordinance_corpus_analysis as ordinance_analysis,
)
from app.agent_office import service as agent_office_service
from app.agent_office.models import (
    AgentOfficeOrdinanceAnalysisItem,
    AgentOfficeTask,
    AgentOfficeTaskEvent,
)
from app.agent_office.routes import run_agent_office_task_inline
from app.agent_office.service import (
    create_task,
    get_task_for_user,
    list_ordinance_analysis_items_for_user,
    list_tasks_for_user,
    mark_task_queue_failed,
    mark_task_queued,
    run_agent_office_task,
    run_ordinance_corpus_analysis_batch,
)
from app.core.config import settings
from app.municipalities.models import Municipality
from app.ordinances.models import (
    Ordinance,
    OrdinanceLegalChunk,
)


def _seed_analysis_task(
    db,
    make_user,
    make_organization,
    grant_permissions,
    *,
    ordinance_count: int,
):
    suffix = uuid.uuid4().hex
    organization = make_organization(f"Corpus analysis {suffix}")
    user = make_user(full_name=f"Corpus analyst {suffix}")
    grant_permissions(
        user,
        organization,
        [
            "agent_office.create",
            "agent_office.view",
            "agent_office.execute",
            "ordinances.compare",
        ],
    )
    municipality = Municipality(
        name=f"Municipio análisis {suffix}",
        province="Burgos",
        autonomous_community="Castilla y León",
        municipality_type="municipality",
        status="active",
    )
    db.add(municipality)
    db.flush()

    ordinances = []
    for index in range(ordinance_count):
        ordinance = Ordinance(
            municipality_id=municipality.id,
            title=f"Ordenanza inventario {index} {suffix}",
            topic=("movilidad" if index % 2 == 0 else "servicios públicos"),
            subtopic=f"subtema-{index % 3}",
            ordinance_type="ordinance",
            curation_status="approved",
            status="active",
            source_url=("https://example.test/ordinance/" f"{suffix}/{index}"),
            source_hash=f"{index:064x}",
        )
        db.add(ordinance)
        db.flush()
        ordinances.append(ordinance)
        db.add(
            OrdinanceLegalChunk(
                ordinance_id=ordinance.id,
                chunk_index=0,
                citation="art. 1",
                text=(
                    "TEXTO_COMPLETO_NO_DEBE_PERSISTIR_EN_EL_WORKFLOW "
                    f"{suffix} {index}"
                ),
                review_status="approved",
                embedding_model=settings.embeddings_model,
                embedding="[1,0]",
                embedding_status="ready",
            )
        )
    db.commit()
    task = create_task(
        db,
        user,
        organization_id=organization.id,
        title="Inventariar corpus interno",
        description=(
            "Clasificar de forma durable el inventario interno "
            "sin conclusiones jurídicas."
        ),
        requested_action="analyze_ordinance_corpus",
        priority="medium",
        approval_policy="never",
        requires_human_approval=False,
        input_payload={
            "municipality_id": municipality.id,
        },
    )
    return user, task, ordinances


def _analysis_items(db, task_id: int):
    return list(
        db.scalars(
            select(AgentOfficeOrdinanceAnalysisItem)
            .where(AgentOfficeOrdinanceAnalysisItem.task_id == task_id)
            .order_by(AgentOfficeOrdinanceAnalysisItem.source_ordinance_id)
        )
    )


def test_rq_failure_callback_waits_for_configured_retries(monkeypatch):
    class RetryingJob:
        retries_left = 1

    def forbidden_session():
        raise AssertionError("retrying job must not be marked terminal")

    monkeypatch.setattr(
        agent_office_service,
        "SessionLocal",
        forbidden_session,
    )
    agent_office_service.mark_ordinance_analysis_rq_failed(
        RetryingJob(),
        None,
        ValueError,
        ValueError("transient"),
        None,
    )


def test_ordinance_analysis_requires_corpus_permission_when_created(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization("Unauthorized corpus analysis")
    user = make_user(full_name="Agent office user without corpus access")
    grant_permissions(
        user,
        organization,
        ["agent_office.create", "agent_office.view"],
    )

    with pytest.raises(HTTPException) as caught:
        create_task(
            db,
            user,
            organization_id=organization.id,
            title="Unauthorized inventory",
            description="Must not persist a task without corpus access.",
            requested_action="analyze_ordinance_corpus",
            approval_policy="never",
            requires_human_approval=False,
        )

    assert caught.value.status_code == 403
    assert db.scalar(select(AgentOfficeTask.id)) is None


def test_pending_analysis_results_require_review_permission(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    suffix = uuid.uuid4().hex
    organization = make_organization(f"Pending corpus {suffix}")
    owner = make_user(full_name=f"Pending corpus owner {suffix}")
    viewer = make_user(full_name=f"Pending corpus viewer {suffix}")
    grant_permissions(
        owner,
        organization,
        [
            "agent_office.create",
            "agent_office.view",
            "ordinances.compare",
            "ordinances.review",
        ],
    )
    grant_permissions(
        viewer,
        organization,
        ["agent_office.view", "ordinances.compare"],
    )
    task = create_task(
        db,
        owner,
        organization_id=organization.id,
        title="Pending inventory",
        description="Inventory including the review pipeline.",
        requested_action="analyze_ordinance_corpus",
        approval_policy="never",
        requires_human_approval=False,
        input_payload={"include_pending": True},
    )

    with pytest.raises(HTTPException) as task_error:
        get_task_for_user(db, viewer, task.id)
    assert task_error.value.status_code == 403
    assert task.id not in {
        visible.id for visible in list_tasks_for_user(db, viewer)
    }
    with pytest.raises(HTTPException) as items_error:
        list_ordinance_analysis_items_for_user(
            db,
            viewer,
            task_id=task.id,
        )
    assert items_error.value.status_code == 403


def test_ordinance_analysis_rejects_unbounded_inline_execution(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user, task, _ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=1,
    )

    with pytest.raises(HTTPException) as caught:
        run_agent_office_task_inline(task.id, db, user)

    assert caught.value.status_code == 409
    assert task.status == "approved"


def test_running_analysis_requires_expired_lease_before_manual_requeue(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    user, task, _ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=2,
    )
    monkeypatch.setattr(
        ordinance_analysis,
        "ORDINANCE_ANALYSIS_CATALOG_PAGE_SIZE",
        1,
    )
    running = run_ordinance_corpus_analysis_batch(
        task.id,
        db,
        page_budget=1,
    )
    assert running.status == "running"

    with pytest.raises(HTTPException) as active_error:
        mark_task_queued(db, user, running)
    assert active_error.value.status_code == 409

    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=(
            agent_office_service.ORDINANCE_ANALYSIS_ACTIVE_LEASE_SECONDS
            + 1
        )
    )
    events = db.scalars(
        select(AgentOfficeTaskEvent).where(
            AgentOfficeTaskEvent.task_id == task.id
        )
    ).all()
    for event in events:
        event.created_at = stale_at
    db.commit()

    recovery_job_id = "analysis-recovery-job"
    requeued = mark_task_queued(
        db,
        user,
        running,
        queue_job_id=recovery_job_id,
    )
    assert requeued.status == "running"
    assert any(
        event.event_type == "analysis_requeued"
        for event in requeued.events
    )

    queue_failed = mark_task_queue_failed(
        db,
        user,
        task.id,
        RuntimeError("redis unavailable"),
        queue_job_id=recovery_job_id,
    )
    assert queue_failed.status == "failed"
    assert "redis unavailable" in queue_failed.error_message


def test_stale_rq_callback_cannot_fail_newer_recovery_job(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    user, task, _ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=2,
    )
    old_job_id = "analysis-old-job"
    task = mark_task_queued(
        db,
        user,
        task,
        queue_job_id=old_job_id,
    )
    monkeypatch.setattr(
        ordinance_analysis,
        "ORDINANCE_ANALYSIS_CATALOG_PAGE_SIZE",
        1,
    )
    running = run_ordinance_corpus_analysis_batch(
        task.id,
        db,
        page_budget=1,
    )
    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=(
            agent_office_service.ORDINANCE_ANALYSIS_ACTIVE_LEASE_SECONDS
            + 1
        )
    )
    events = db.scalars(
        select(AgentOfficeTaskEvent).where(
            AgentOfficeTaskEvent.task_id == task.id
        )
    ).all()
    for event in events:
        event.created_at = stale_at
    db.commit()

    newer_job_id = "analysis-newer-job"
    recovered = mark_task_queued(
        db,
        user,
        running,
        queue_job_id=newer_job_id,
    )
    assert recovered.status == "running"

    class BorrowedSession:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    class StaleJob:
        id = old_job_id
        args = (task.id,)
        retries_left = 0

    monkeypatch.setattr(
        agent_office_service,
        "SessionLocal",
        lambda: BorrowedSession(),
    )
    agent_office_service.mark_ordinance_analysis_rq_failed(
        StaleJob(),
        None,
        RuntimeError,
        RuntimeError("old terminal failure"),
        None,
    )

    db.expire_all()
    still_running = db.get(AgentOfficeTask, task.id)
    assert still_running.status == "running"
    assert "old terminal failure" not in (
        still_running.error_message or ""
    )


def test_ordinance_analysis_is_explicit_resumable_and_idempotent(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    user, task, ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=5,
    )
    del user
    monkeypatch.setattr(
        ordinance_analysis,
        "ORDINANCE_ANALYSIS_CATALOG_PAGE_SIZE",
        2,
    )

    def forbidden_chat_tool(*args, **kwargs):
        raise AssertionError("durable analysis must not execute a chat tool or LLM")

    monkeypatch.setattr(
        agent_office_service,
        "execute_tool",
        forbidden_chat_tool,
    )

    assert task.department == "ordinances"
    assert task.requested_action == "analyze_ordinance_corpus"
    assert "analyze_ordinance_corpus" in agent_office_service.WORKFLOW_ACTIONS
    assert "analyze_ordinance_corpus" not in agent_office_service.TOOL_ACTIONS

    first = run_ordinance_corpus_analysis_batch(
        task.id,
        db,
        page_budget=1,
    )
    assert first.status == "running"
    assert first.result["snapshot_id"]
    assert first.result["expected_ordinances"] == 5
    assert first.result["catalog_consumed"] == 2
    assert len(_analysis_items(db, task.id)) == 2

    second = run_ordinance_corpus_analysis_batch(
        task.id,
        db,
        page_budget=1,
    )
    assert second.status == "running"
    assert second.result["catalog_consumed"] == 4
    assert len(_analysis_items(db, task.id)) == 4

    final = run_ordinance_corpus_analysis_batch(
        task.id,
        db,
        page_budget=1,
    )
    assert final.status == "completed"
    assert final.result["reconciliation"]["balanced"] is True
    assert final.result["reconciliation"]["completed_items"] == len(ordinances)
    assert sum(final.result["summary"]["functional_topics"].values()) == len(ordinances)

    items = _analysis_items(db, task.id)
    assert len(items) == 5
    assert len({item.source_ordinance_id for item in items}) == 5
    assert {item.status for item in items} == {"completed"}
    assert {item.attempts for item in items} == {1}
    assert all(item.source_digest for item in items)
    assert all(item.source_updated_at for item in items)

    persisted = final.result_json + "".join(item.result_json or "" for item in items)
    events = list(
        db.scalars(
            select(AgentOfficeTaskEvent).where(AgentOfficeTaskEvent.task_id == task.id)
        )
    )
    persisted += "".join(event.message + (event.payload_json or "") for event in events)
    assert "TEXTO_COMPLETO_NO_DEBE_PERSISTIR" not in persisted
    assert "text" not in final.result

    queued_call = {}

    class RecordingQueue:
        def enqueue(self, *args, **kwargs):
            queued_call["args"] = args
            queued_call["kwargs"] = kwargs
            return type("QueuedJob", (), {"id": kwargs["job_id"]})()

    monkeypatch.setattr(
        agent_office_service,
        "get_default_queue",
        lambda: RecordingQueue(),
    )
    agent_office_service.enqueue_agent_office_task_execution(
        final,
        queue_job_id="analysis-payload-test",
    )
    assert queued_call["args"][0] is agent_office_service.run_agent_office_task_job
    assert queued_call["args"][1:] == (task.id,)
    assert queued_call["kwargs"]["result_ttl"] == 0
    assert "TEXTO_COMPLETO_NO_DEBE_PERSISTIR" not in repr(queued_call)

    attempts_before = {item.source_ordinance_id: item.attempts for item in items}
    repeated = run_agent_office_task(task.id, db=db)
    assert repeated.status == "completed"
    attempts_after = {
        item.source_ordinance_id: item.attempts for item in _analysis_items(db, task.id)
    }
    assert attempts_after == attempts_before


def test_deleted_ordinance_keeps_durable_source_identity(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    _user, task, ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=1,
    )
    completed = run_agent_office_task(task.id, db=db)
    assert completed.status == "completed"
    item = _analysis_items(db, task.id)[0]
    source_ordinance_id = item.source_ordinance_id
    source_digest = item.source_digest
    result_json = item.result_json

    db.delete(ordinances[0])
    db.commit()
    db.expire_all()

    retained = db.get(AgentOfficeOrdinanceAnalysisItem, item.id)
    assert retained is not None
    assert retained.ordinance_id is None
    assert retained.source_ordinance_id == source_ordinance_id
    assert retained.source_digest == source_digest
    assert retained.result_json == result_json


def test_failed_item_resumes_from_fixed_snapshot_without_duplicates(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    user, task, ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=3,
    )
    target_id = ordinances[1].id
    original_builder = ordinance_analysis.build_ordinance_inventory_result
    failed_once = False

    def fail_one_item_once(catalog_row):
        nonlocal failed_once
        if int(catalog_row["ordinance_id"]) == target_id and not failed_once:
            failed_once = True
            raise ValueError("controlled metadata classifier failure")
        return original_builder(catalog_row)

    monkeypatch.setattr(
        ordinance_analysis,
        "build_ordinance_inventory_result",
        fail_one_item_once,
    )
    failed_task = run_agent_office_task(task.id, db=db)
    assert failed_task.status == "failed"
    fixed_snapshot = failed_task.result["snapshot_id"]
    first_items = _analysis_items(db, task.id)
    assert len(first_items) == 3
    assert sum(item.status == "failed" for item in first_items) == 1
    assert {item.source_ordinance_id: item.attempts for item in first_items}[
        target_id
    ] == 1

    queued = mark_task_queued(
        db,
        user,
        failed_task,
    )
    assert queued.status == "queued"
    recovered = run_agent_office_task(task.id, db=db)
    assert recovered.status == "completed"
    assert recovered.result["snapshot"]["id"] == fixed_snapshot
    assert recovered.result["progress"]["resume_passes"] == 1
    assert recovered.result["reconciliation"]["balanced"] is True

    recovered_items = _analysis_items(db, task.id)
    assert len(recovered_items) == 3
    assert len({item.source_ordinance_id for item in recovered_items}) == 3
    attempts = {item.source_ordinance_id: item.attempts for item in recovered_items}
    assert attempts[target_id] == 2
    assert all(
        attempts[ordinance.id] == (2 if ordinance.id == target_id else 1)
        for ordinance in ordinances
    )
    assert all(item.status == "completed" for item in recovered_items)
    assert "TEXTO_COMPLETO_NO_DEBE_PERSISTIR" not in json.dumps(
        recovered.result,
        ensure_ascii=False,
    )


def test_ordinance_analysis_caps_resume_passes_and_item_attempts(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    user, task, ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=1,
    )
    target_id = ordinances[0].id

    def always_fail(_catalog_row):
        raise ValueError("persistent classifier failure")

    monkeypatch.setattr(
        ordinance_analysis,
        "build_ordinance_inventory_result",
        always_fail,
    )

    failed_task = run_agent_office_task(task.id, db=db)
    assert failed_task.status == "failed"
    assert _analysis_items(db, task.id)[0].attempts == 1

    for expected_resume_pass in range(
        1,
        ordinance_analysis.ORDINANCE_ANALYSIS_MAX_RESUME_PASSES + 1,
    ):
        queued = mark_task_queued(db, user, failed_task)
        assert queued.status == "queued"
        failed_task = run_agent_office_task(task.id, db=db)
        assert failed_task.status == "failed"
        assert failed_task.result["resume_passes"] == expected_resume_pass

    capped_item = _analysis_items(db, task.id)[0]
    assert capped_item.source_ordinance_id == target_id
    assert (
        capped_item.attempts == ordinance_analysis.ORDINANCE_ANALYSIS_MAX_ITEM_ATTEMPTS
    )

    queued = mark_task_queued(db, user, failed_task)
    assert queued.status == "queued"
    still_failed = run_agent_office_task(task.id, db=db)
    assert still_failed.status == "failed"
    assert "límite de reanudaciones" in still_failed.error_message
    assert (
        _analysis_items(db, task.id)[0].attempts
        == ordinance_analysis.ORDINANCE_ANALYSIS_MAX_ITEM_ATTEMPTS
    )


def test_final_snapshot_verification_rejects_corpus_drift(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user, task, ordinances = _seed_analysis_task(
        db,
        make_user,
        make_organization,
        grant_permissions,
        ordinance_count=1,
    )
    state = ordinance_analysis.initialize_ordinance_analysis_state(
        db,
        task,
        user,
    )
    outcome = ordinance_analysis.process_ordinance_analysis_page(
        db,
        task,
        state,
    )
    assert outcome.complete is True
    db.commit()

    ordinances[0].title = "Título modificado después de enumerar"
    db.commit()

    with pytest.raises(ValueError, match="corpus cambió"):
        ordinance_analysis.verify_ordinance_analysis_snapshot(
            db,
            outcome.state,
        )
