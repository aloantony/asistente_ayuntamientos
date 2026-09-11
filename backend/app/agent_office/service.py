import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status as http_status
from rq import Retry
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.agent_office.ordinance_corpus_analysis import (
    ANALYZE_ORDINANCE_CORPUS_ACTION,
    ORDINANCE_ANALYSIS_PAGES_PER_RQ_JOB,
    initialize_ordinance_analysis_state,
    ordinance_analysis_filters_from_task,
    prepare_ordinance_analysis_resume,
    process_ordinance_analysis_page,
    reconcile_ordinance_analysis,
    require_ordinance_analysis_access,
    verify_ordinance_analysis_snapshot,
)
from app.agent_office.models import (
    AGENT_OFFICE_APPROVAL_POLICIES,
    AGENT_OFFICE_DEPARTMENTS,
    AGENT_OFFICE_ORDINANCE_ANALYSIS_ITEM_STATUSES,
    AGENT_OFFICE_PRIORITIES,
    AGENT_OFFICE_ROUTINE_CADENCES,
    AGENT_OFFICE_ROUTINE_KINDS,
    AGENT_OFFICE_ROUTINE_STATUSES,
    AGENT_OFFICE_TARGET_CHANNELS,
    AgentOfficeRoutine,
    AgentOfficeOrdinanceAnalysisItem,
    AgentOfficeTask,
    AgentOfficeTaskEvent,
)
from app.assistant.tool_authorization import (
    issue_agent_office_tool_authorization,
    tool_input_digest,
)
from app.assistant.tools import (
    PreparedOrdinanceSearchEmbedding,
    ToolContext,
    execute_tool,
    normalize_tool_input,
    prepare_ordinance_search_embedding,
)
from app.core.config import settings
from app.core.jobs import get_default_queue
from app.db.session import SessionLocal
from app.organizations.access import get_user_organization_ids
from app.organizations.models import Organization
from app.ordinances.embeddings import (
    EmbeddingWorkerCleanupError,
    supervised_embedding_cleanup_margin_seconds,
)
from app.ordinances.catalog import (
    OrdinanceCorpusFilters,
    decode_ordinance_catalog_cursor,
)
from app.rbac.permissions import has_permission
from app.requirements.models import Requirement
from app.users.models import User


@dataclass(frozen=True)
class AgentOfficeAgentSpec:
    key: str
    name: str
    department: str
    description: str
    assistant_agent_key: str
    tool_names: frozenset[str]
    mutating_actions: frozenset[str]
    workflow_actions: frozenset[str] = frozenset()
    requires_approval_by_default: bool = True

    @property
    def metadata(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "department": self.department,
            "description": self.description,
            "assistant_agent_key": self.assistant_agent_key,
            "tool_names": sorted(self.tool_names),
            "mutating_actions": sorted(self.mutating_actions),
            "workflow_actions": sorted(self.workflow_actions),
            "requires_approval_by_default": self.requires_approval_by_default,
        }


OFFICE_AGENTS: dict[str, AgentOfficeAgentSpec] = {
    "front_desk": AgentOfficeAgentSpec(
        key="front_desk",
        name="iConcejo Recepción",
        department="front_desk",
        description=(
            "Recibe peticiones, las convierte en tareas supervisadas y las "
            "deriva al agente municipal especialista adecuado."
        ),
        assistant_agent_key="front_desk",
        tool_names=frozenset({"list_organizations", "list_projects", "create_agent_office_task"}),
        mutating_actions=frozenset({"create_agent_office_task"}),
    ),
    "requirements": AgentOfficeAgentSpec(
        key="requirements",
        name="Agente de necesidades",
        department="requirements",
        description="Captura, consulta y mantiene necesidades/requisitos como trabajo supervisado.",
        assistant_agent_key="requirements_intake",
        tool_names=frozenset(
            {
                "list_organizations",
                "list_projects",
                "list_requirements",
                "get_requirement",
                "create_requirement",
                "update_requirement",
                "add_requirement_message",
                "propose_memory_entry",
                "send_admin_feedback",
            }
        ),
        mutating_actions=frozenset(
            {
                "create_requirement",
                "update_requirement",
                "add_requirement_message",
                "propose_memory_entry",
                "send_admin_feedback",
            }
        ),
    ),
    "ordinances": AgentOfficeAgentSpec(
        key="ordinances",
        name="Agente de ordenanzas",
        department="ordinances",
        description=(
            "Inventaría, busca y compara normativa municipal ya importada "
            "y aprobada."
        ),
        assistant_agent_key="consultation",
        tool_names=frozenset(
            {
                "get_ordinance_corpus_manifest",
                "list_ordinance_catalog",
                "semantic_search_ordinances",
            }
        ),
        mutating_actions=frozenset(),
        workflow_actions=frozenset(
            {ANALYZE_ORDINANCE_CORPUS_ACTION}
        ),
        requires_approval_by_default=False,
    ),
    "documents": AgentOfficeAgentSpec(
        key="documents",
        name="Agente documental",
        department="documents",
        description=(
            "Prepara trabajo documental. En v1 crea planes auditables; la lectura "
            "automática de documentos originales queda pendiente del gateway documental."
        ),
        assistant_agent_key="consultation",
        tool_names=frozenset(),
        mutating_actions=frozenset(),
    ),
    "projects": AgentOfficeAgentSpec(
        key="projects",
        name="Agente de proyectos",
        department="projects",
        description="Consulta proyectos/expedientes visibles y prepara resúmenes operativos.",
        assistant_agent_key="consultation",
        tool_names=frozenset({"list_projects"}),
        mutating_actions=frozenset(),
        requires_approval_by_default=False,
    ),
    "map": AgentOfficeAgentSpec(
        key="map",
        name="Agente de mapa",
        department="map",
        description="Consulta ubicaciones visibles de necesidades y proyectos municipales.",
        assistant_agent_key="consultation",
        tool_names=frozenset({"get_map_items"}),
        mutating_actions=frozenset(),
        requires_approval_by_default=False,
    ),
    "admin_feedback": AgentOfficeAgentSpec(
        key="admin_feedback",
        name="Agente de feedback",
        department="admin_feedback",
        description="Convierte fricciones, bugs y propuestas en feedback supervisado para administración.",
        assistant_agent_key="requirements_intake",
        tool_names=frozenset({"send_admin_feedback"}),
        mutating_actions=frozenset({"send_admin_feedback"}),
    ),
    "daily_briefing": AgentOfficeAgentSpec(
        key="daily_briefing",
        name="Agente de informe diario",
        department="daily_briefing",
        description="Prepara un resumen diario de proyectos y necesidades visibles de una organización.",
        assistant_agent_key="consultation",
        tool_names=frozenset({"list_projects", "list_requirements"}),
        mutating_actions=frozenset(),
        requires_approval_by_default=False,
    ),
}

DEFAULT_ACTION_BY_DEPARTMENT = {
    "front_desk": "triage",
    "requirements": "list_requirements",
    "ordinances": "semantic_search_ordinances",
    "documents": "prepare_document_work",
    "projects": "list_projects",
    "map": "get_map_items",
    "admin_feedback": "send_admin_feedback",
    "daily_briefing": "daily_briefing",
}
ACTION_TO_DEPARTMENT = {
    ANALYZE_ORDINANCE_CORPUS_ACTION: "ordinances",
    "get_ordinance_corpus_manifest": "ordinances",
    "list_ordinance_catalog": "ordinances",
    "semantic_search_ordinances": "ordinances",
    "list_projects": "projects",
    "get_map_items": "map",
    "send_admin_feedback": "admin_feedback",
    "daily_briefing": "daily_briefing",
    "list_requirements": "requirements",
    "get_requirement": "requirements",
    "create_requirement": "requirements",
    "update_requirement": "requirements",
    "add_requirement_message": "requirements",
}
WORKFLOW_ACTIONS = frozenset(
    {ANALYZE_ORDINANCE_CORPUS_ACTION}
)
ORDINANCE_TASK_ACTIONS = frozenset(
    {
        ANALYZE_ORDINANCE_CORPUS_ACTION,
        "get_ordinance_corpus_manifest",
        "list_ordinance_catalog",
        "semantic_search_ordinances",
    }
)
ORDINANCE_ANALYSIS_RQ_JOB_TIMEOUT_SECONDS = 15 * 60
ORDINANCE_ANALYSIS_RQ_RETRY_INTERVALS = [10, 30, 120]
ORDINANCE_ANALYSIS_ACTIVE_LEASE_SECONDS = (
    ORDINANCE_ANALYSIS_RQ_JOB_TIMEOUT_SECONDS
    + max(ORDINANCE_ANALYSIS_RQ_RETRY_INTERVALS)
    + 60
)
TOOL_ACTIONS = {
    "list_organizations",
    "list_projects",
    "list_requirements",
    "get_requirement",
    "create_requirement",
    "update_requirement",
    "add_requirement_message",
    "propose_memory_entry",
    "send_admin_feedback",
    "get_ordinance_corpus_manifest",
    "list_ordinance_catalog",
    "semantic_search_ordinances",
    "get_map_items",
}
MUTATING_ACTIONS = {
    "create_requirement",
    "update_requirement",
    "add_requirement_message",
    "propose_memory_entry",
    "send_admin_feedback",
    "create_agent_office_task",
}
EXTERNAL_READ_ACTIONS = frozenset({"semantic_search_ordinances"})
CANCELLABLE_RUNNING_ACTIONS = (
    EXTERNAL_READ_ACTIONS | WORKFLOW_ACTIONS
)
EXTERNAL_READ_CLAIM_EVENT = "external_read_claimed"
EXTERNAL_READ_QUARANTINE_EVENT = "external_read_quarantined"


@dataclass(frozen=True)
class ExternalReadClaim:
    claim_id: str
    execution_attempt_id: int
    tool_input: dict
    provider_deadline_at: datetime


def ensure_organization_exists(db: Session, organization_id: int) -> Organization:
    organization = db.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return organization


def require_agent_office_permission(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
) -> None:
    if has_permission(current_user, permission_code, db, organization_id=organization_id):
        return
    if permission_code != "agent_office.manage" and has_permission(
        current_user,
        "agent_office.manage",
        db,
        organization_id=organization_id,
    ):
        return
    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )


def visible_agent_office_organization_ids(db: Session, current_user: User) -> list[int] | None:
    if current_user.is_superuser:
        return None
    return [
        organization_id
        for organization_id in get_user_organization_ids(db, current_user)
        if has_permission(current_user, "agent_office.view", db, organization_id=organization_id)
        or has_permission(current_user, "agent_office.manage", db, organization_id=organization_id)
    ]


def user_can_view_task(db: Session, current_user: User, task: AgentOfficeTask) -> bool:
    return (
        current_user.is_superuser
        or task.requested_by_id == current_user.id
        or has_permission(current_user, "agent_office.view", db, organization_id=task.organization_id)
        or has_permission(current_user, "agent_office.manage", db, organization_id=task.organization_id)
    )


def require_task_result_access(
    db: Session,
    current_user: User,
    task: AgentOfficeTask,
) -> None:
    """Apply capability-specific checks before returning persisted results."""

    if task.requested_action not in ORDINANCE_TASK_ACTIONS:
        return
    try:
        include_pending = _ordinance_task_includes_pending(task)
    except ValueError as error:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    require_ordinance_analysis_access(
        db,
        current_user,
        OrdinanceCorpusFilters(include_pending=include_pending),
    )


def _ordinance_task_includes_pending(task: AgentOfficeTask) -> bool:
    if task.requested_action == ANALYZE_ORDINANCE_CORPUS_ACTION:
        return ordinance_analysis_filters_from_task(task).include_pending
    task_input = task.input
    include_pending = task_input.get("include_pending", False)
    if not isinstance(include_pending, bool):
        raise ValueError("include_pending debe ser booleano")
    cursor = task_input.get("cursor")
    if cursor is not None:
        if task.requested_action != "list_ordinance_catalog":
            raise ValueError("cursor no está permitido para esta acción")
        if not isinstance(cursor, str):
            raise ValueError("cursor debe ser texto")
        cursor_payload = decode_ordinance_catalog_cursor(cursor)
        cursor_filters = cursor_payload.get("filters")
        if not isinstance(cursor_filters, dict):
            raise ValueError("el cursor no contiene filtros válidos")
        cursor_include_pending = cursor_filters.get(
            "include_pending",
            False,
        )
        if not isinstance(cursor_include_pending, bool):
            raise ValueError("el cursor contiene filtros no válidos")
        include_pending = include_pending or cursor_include_pending
    result_filters = task.result.get("filters")
    if isinstance(result_filters, dict):
        result_include_pending = result_filters.get(
            "include_pending",
            False,
        )
        if not isinstance(result_include_pending, bool):
            raise ValueError("el resultado contiene filtros no válidos")
        include_pending = include_pending or result_include_pending
    return include_pending


def _visible_task_results(
    db: Session,
    current_user: User,
    tasks: list[AgentOfficeTask],
) -> list[AgentOfficeTask]:
    if not any(
        task.requested_action in ORDINANCE_TASK_ACTIONS
        for task in tasks
    ):
        return tasks
    can_manage = current_user.is_superuser or has_permission(
        current_user,
        "ordinances.manage",
        db,
    )
    can_compare = can_manage or has_permission(
        current_user,
        "ordinances.compare",
        db,
    )
    can_review = can_manage or has_permission(
        current_user,
        "ordinances.review",
        db,
    )
    visible: list[AgentOfficeTask] = []
    for task in tasks:
        if task.requested_action not in ORDINANCE_TASK_ACTIONS:
            visible.append(task)
            continue
        try:
            include_pending = _ordinance_task_includes_pending(task)
        except ValueError:
            continue
        if can_compare and (not include_pending or can_review):
            visible.append(task)
    return visible


def get_task_for_user(db: Session, current_user: User, task_id: int) -> AgentOfficeTask:
    task = db.scalar(
        select(AgentOfficeTask)
        .options(selectinload(AgentOfficeTask.events))
        .execution_options(populate_existing=True)
        .where(AgentOfficeTask.id == task_id)
    )
    if task is None or not user_can_view_task(db, current_user, task):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Agent office task not found",
        )
    require_task_result_access(db, current_user, task)
    return task


def lock_task_for_transition(
    db: Session,
    task_id: int,
) -> AgentOfficeTask | None:
    return db.scalar(
        select(AgentOfficeTask)
        .options(
            selectinload(AgentOfficeTask.events),
            selectinload(AgentOfficeTask.requested_by),
            selectinload(AgentOfficeTask.approved_by),
        )
        .where(AgentOfficeTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def list_tasks_for_user(
    db: Session,
    current_user: User,
    *,
    organization_id: int | None = None,
    status_filter: str | None = None,
    department: str | None = None,
) -> list[AgentOfficeTask]:
    query = (
        select(AgentOfficeTask)
        .options(selectinload(AgentOfficeTask.events))
        .order_by(AgentOfficeTask.created_at.desc(), AgentOfficeTask.id.desc())
    )
    if organization_id is not None:
        ensure_organization_exists(db, organization_id)
        query = query.where(AgentOfficeTask.organization_id == organization_id)
    if status_filter is not None:
        query = query.where(AgentOfficeTask.status == status_filter)
    if department is not None:
        if department not in AGENT_OFFICE_DEPARTMENTS:
            raise HTTPException(status_code=400, detail="Invalid agent office department")
        query = query.where(AgentOfficeTask.department == department)

    visible_org_ids = visible_agent_office_organization_ids(db, current_user)
    if visible_org_ids is not None:
        visibility_filters = [AgentOfficeTask.requested_by_id == current_user.id]
        if visible_org_ids:
            visibility_filters.append(AgentOfficeTask.organization_id.in_(visible_org_ids))
        query = query.where(or_(*visibility_filters))

    tasks = list(db.scalars(query.limit(200)))
    return _visible_task_results(db, current_user, tasks)


def list_ordinance_analysis_items_for_user(
    db: Session,
    current_user: User,
    *,
    task_id: int,
    status_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AgentOfficeOrdinanceAnalysisItem]:
    task = get_task_for_user(
        db,
        current_user,
        task_id,
    )
    if task.requested_action != ANALYZE_ORDINANCE_CORPUS_ACTION:
        raise HTTPException(
            status_code=404,
            detail="Ordinance corpus analysis not found",
        )
    if (
        status_filter is not None
        and status_filter
        not in AGENT_OFFICE_ORDINANCE_ANALYSIS_ITEM_STATUSES
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid ordinance analysis item status",
        )
    query = (
        select(AgentOfficeOrdinanceAnalysisItem)
        .where(
            AgentOfficeOrdinanceAnalysisItem.task_id
            == task.id
        )
        .order_by(
            AgentOfficeOrdinanceAnalysisItem.source_ordinance_id
        )
        .limit(limit)
        .offset(offset)
    )
    if status_filter is not None:
        query = query.where(
            AgentOfficeOrdinanceAnalysisItem.status
            == status_filter
        )
    return list(db.scalars(query))


def _json_dumps(value: dict | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False)


def add_task_event(
    db: Session,
    task: AgentOfficeTask,
    event_type: str,
    message: str,
    *,
    payload: dict | None = None,
    created_by_id: int | None = None,
) -> AgentOfficeTaskEvent:
    event = AgentOfficeTaskEvent(
        task_id=task.id,
        event_type=event_type,
        message=message,
        payload_json=_json_dumps(payload),
        created_by_id=created_by_id,
    )
    db.add(event)
    return event


def infer_department(text: str, requested_action: str | None = None) -> str:
    action = (requested_action or "").strip().lower()
    if action in ACTION_TO_DEPARTMENT:
        return ACTION_TO_DEPARTMENT[action]
    return "front_desk"


def normalize_task_request(
    *,
    title: str,
    description: str,
    department: str | None,
    requested_action: str | None,
    approval_policy: str | None,
    requires_human_approval: bool | None,
) -> tuple[str, str, str, bool, str]:
    normalized_department = department or infer_department(f"{title}\n{description}", requested_action)
    if normalized_department not in AGENT_OFFICE_DEPARTMENTS:
        raise HTTPException(status_code=400, detail="Invalid agent office department")

    action = (requested_action or DEFAULT_ACTION_BY_DEPARTMENT[normalized_department]).strip()
    if not action:
        raise HTTPException(status_code=400, detail="requested_action cannot be empty")
    if (
        action in WORKFLOW_ACTIONS
        and action
        not in OFFICE_AGENTS[normalized_department].workflow_actions
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Workflow {action} is not available for department "
                f"{normalized_department}"
            ),
        )

    policy = approval_policy or (
        "before_execution"
        if action in MUTATING_ACTIONS or OFFICE_AGENTS[normalized_department].requires_approval_by_default
        else "never"
    )
    if policy not in AGENT_OFFICE_APPROVAL_POLICIES:
        raise HTTPException(status_code=400, detail="Invalid approval policy")
    if action in MUTATING_ACTIONS and policy == "never":
        policy = "before_execution"

    approval_required = requires_human_approval
    if approval_required is None:
        approval_required = policy != "never" or action in MUTATING_ACTIONS
    if action in MUTATING_ACTIONS:
        approval_required = True

    status = "pending_approval" if approval_required else "approved"
    return normalized_department, action, policy, approval_required, status


def create_task(
    db: Session,
    current_user: User,
    *,
    organization_id: int,
    title: str,
    description: str,
    department: str | None = None,
    requested_action: str | None = None,
    priority: str = "medium",
    approval_policy: str | None = None,
    requires_human_approval: bool | None = None,
    input_payload: dict | None = None,
    due_at: datetime | None = None,
    scheduled_for: datetime | None = None,
    source_conversation_id: int | None = None,
    source_message_id: int | None = None,
    commit: bool = True,
) -> AgentOfficeTask:
    ensure_organization_exists(db, organization_id)
    require_agent_office_permission(db, current_user, organization_id, "agent_office.create")
    if priority not in AGENT_OFFICE_PRIORITIES:
        raise HTTPException(status_code=400, detail="Invalid task priority")

    normalized_department, action, policy, approval_required, initial_status = normalize_task_request(
        title=title,
        description=description,
        department=department,
        requested_action=requested_action,
        approval_policy=approval_policy,
        requires_human_approval=requires_human_approval,
    )
    agent = OFFICE_AGENTS[normalized_department]
    input_payload = dict(input_payload or {})
    input_payload["organization_id"] = organization_id

    task = AgentOfficeTask(
        organization_id=organization_id,
        title=title,
        description=description,
        department=normalized_department,
        requested_action=action,
        priority=priority,
        approval_policy=policy,
        requires_human_approval=approval_required,
        status=initial_status,
        assigned_agent_key=agent.key,
        routing_reason=(
            f"Routed to {agent.key} from requested_action={action!r}; "
            "internal task routing; Obsidian/external knowledge graph excluded by design."
        ),
        input_json=_json_dumps(input_payload),
        due_at=due_at,
        scheduled_for=scheduled_for,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        requested_by_id=current_user.id,
    )
    if action in ORDINANCE_TASK_ACTIONS:
        require_task_result_access(db, current_user, task)
    db.add(task)
    db.flush()
    add_task_event(
        db,
        task,
        "created",
        "Task created and routed to an internal municipal capability.",
        payload={
            "department": normalized_department,
            "requested_action": action,
            "approval_policy": policy,
            "requires_human_approval": approval_required,
        },
        created_by_id=current_user.id,
    )
    if commit:
        db.commit()
        return get_task_for_user(db, current_user, task.id)
    db.flush()
    return task


def approve_or_cancel_task(
    db: Session,
    current_user: User,
    task: AgentOfficeTask,
    *,
    decision: str,
    notes: str | None = None,
) -> AgentOfficeTask:
    locked_task = lock_task_for_transition(db, task.id)
    if locked_task is None or not user_can_view_task(db, current_user, locked_task):
        raise HTTPException(status_code=404, detail="Agent office task not found")
    task = locked_task
    require_agent_office_permission(
        db,
        current_user,
        task.organization_id,
        "agent_office.approve",
    )
    now = datetime.now(timezone.utc)
    if decision == "cancel":
        if task.status == "completed" or (
            task.status == "running"
            and task.requested_action
            not in CANCELLABLE_RUNNING_ACTIONS
        ):
            raise HTTPException(status_code=409, detail="Task cannot be cancelled in its current status")
        task.status = "cancelled"
        task.completed_at = now
        add_task_event(db, task, "cancelled", notes or "Task cancelled by a human reviewer.", created_by_id=current_user.id)
    elif decision == "approve":
        if task.status == "pending_approval":
            task.status = "approved"
            task.approved_by_id = current_user.id
            task.approved_at = now
            add_task_event(db, task, "approved", notes or "Task approved for execution.", created_by_id=current_user.id)
        elif task.status == "waiting_approval":
            task.status = "completed"
            task.approved_by_id = current_user.id
            task.approved_at = now
            task.completed_at = now
            add_task_event(db, task, "final_approved", notes or "Task result approved.", created_by_id=current_user.id)
        else:
            raise HTTPException(status_code=409, detail="Task cannot be approved in its current status")
    else:
        raise HTTPException(status_code=400, detail="Invalid approval decision")
    db.commit()
    return get_task_for_user(db, current_user, task.id)


def mark_task_queued(
    db: Session,
    current_user: User,
    task: AgentOfficeTask,
    *,
    queue_job_id: str | None = None,
) -> AgentOfficeTask:
    locked_task = lock_task_for_transition(db, task.id)
    if locked_task is None or not user_can_view_task(db, current_user, locked_task):
        raise HTTPException(status_code=404, detail="Agent office task not found")
    task = locked_task
    require_agent_office_permission(
        db,
        current_user,
        task.organization_id,
        "agent_office.execute",
    )
    if task.status == "failed" and _latest_attempt_is_quarantined(db, task.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Task cannot be retried because external worker cleanup "
                "could not be confirmed"
            ),
        )
    running_analysis_requeue = (
        task.status == "running"
        and task.requested_action
        == ANALYZE_ORDINANCE_CORPUS_ACTION
        and _running_analysis_is_recoverable(db, task)
    )
    if (
        task.status not in {"approved", "failed"}
        and not running_analysis_requeue
    ):
        raise HTTPException(status_code=409, detail="Only approved or failed tasks can be queued")
    if running_analysis_requeue:
        add_task_event(
            db,
            task,
            "analysis_requeued",
            (
                "Running ordinance analysis was requeued from its "
                "durable checkpoint."
            ),
            payload=(
                {"queue_job_id": queue_job_id}
                if queue_job_id
                else None
            ),
            created_by_id=current_user.id,
        )
        db.commit()
        return get_task_for_user(db, current_user, task.id)
    if (
        task.status == "failed"
        and task.requested_action
        == ANALYZE_ORDINANCE_CORPUS_ACTION
        and task.result.get("mode")
        == "ordinance_corpus_analysis"
    ):
        checkpoint = dict(task.result)
        checkpoint["resume_required"] = (
            checkpoint.get("phase") == "enumeration_complete"
        )
        task.result_json = _json_dumps(checkpoint)
    task.status = "queued"
    task.error_message = None
    add_task_event(
        db,
        task,
        "queued",
        "Task queued for agent office execution.",
        payload=(
            {"queue_job_id": queue_job_id}
            if queue_job_id
            else None
        ),
        created_by_id=current_user.id,
    )
    db.commit()
    return get_task_for_user(db, current_user, task.id)


def mark_task_queue_failed(
    db: Session,
    current_user: User,
    task_id: int,
    error: Exception,
    *,
    queue_job_id: str | None = None,
) -> AgentOfficeTask:
    task = lock_task_for_transition(db, task_id)
    if task is None or not user_can_view_task(db, current_user, task):
        raise HTTPException(status_code=404, detail="Agent office task not found")
    require_agent_office_permission(
        db,
        current_user,
        task.organization_id,
        "agent_office.execute",
    )
    if task.status == "queued":
        task.status = "failed"
        task.error_message = str(error)[:2000]
        task.completed_at = datetime.now(timezone.utc)
        add_task_event(
            db,
            task,
            "failed",
            task.error_message,
            created_by_id=current_user.id,
        )
        db.commit()
        return get_task_for_user(db, current_user, task.id)
    if (
        task.status == "running"
        and task.requested_action == ANALYZE_ORDINANCE_CORPUS_ACTION
        and queue_job_id is not None
        and _latest_ordinance_analysis_queue_job_id(db, task.id)
        == queue_job_id
    ):
        if task.result.get("mode") == "ordinance_corpus_analysis":
            checkpoint = dict(task.result)
            checkpoint["resume_required"] = (
                checkpoint.get("phase") == "enumeration_complete"
            )
            task.result_json = _json_dumps(checkpoint)
        task.status = "failed"
        task.error_message = str(error)[:2000]
        task.completed_at = datetime.now(timezone.utc)
        add_task_event(
            db,
            task,
            "analysis_queue_failed",
            "Recovered ordinance analysis could not be queued.",
            payload={"queue_job_id": queue_job_id},
            created_by_id=current_user.id,
        )
        add_task_event(
            db,
            task,
            "failed",
            task.error_message,
            created_by_id=current_user.id,
        )
        db.commit()
        return get_task_for_user(db, current_user, task.id)
    # A worker or reviewer won the row lock first. Never overwrite its state.
    db.rollback()
    return get_task_for_user(db, current_user, task.id)


def _tool_input_for_task(task: AgentOfficeTask) -> dict:
    tool_input = dict(task.input)
    if task.requested_action in ORDINANCE_TASK_ACTIONS:
        tool_input.pop("organization_id", None)
    else:
        tool_input["organization_id"] = task.organization_id
    if task.requested_action == "semantic_search_ordinances":
        tool_input.setdefault("query", task.description)
    if task.requested_action == "send_admin_feedback":
        tool_input.setdefault("category", "other")
        tool_input.setdefault("title", task.title)
        tool_input.setdefault("description", task.description)
        tool_input.setdefault("priority", task.priority)
    return tool_input


REQUIREMENT_ID_SCOPED_ACTIONS = frozenset(
    {"get_requirement", "update_requirement", "add_requirement_message"}
)


def _validate_task_tool_scope(
    db: Session,
    task: AgentOfficeTask,
    tool_input: dict,
) -> None:
    if task.requested_action not in REQUIREMENT_ID_SCOPED_ACTIONS:
        return

    raw_requirement_id = tool_input.get("requirement_id")
    if raw_requirement_id is None:
        return
    try:
        requirement_id = int(raw_requirement_id)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid requirement_id for scoped agent office task") from error

    requirement_organization_id = db.scalar(
        select(Requirement.organization_id).where(Requirement.id == requirement_id)
    )
    if requirement_organization_id is None:
        return
    if requirement_organization_id != task.organization_id:
        raise ValueError("Referenced requirement is outside task organization")


def _decode_tool_content(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


def _run_tool_action(
    db: Session,
    user: User,
    task: AgentOfficeTask,
    *,
    prepared_ordinance_embedding: PreparedOrdinanceSearchEmbedding | None = None,
) -> dict:
    agent = OFFICE_AGENTS[task.department]
    if task.requested_action not in TOOL_ACTIONS:
        raise ValueError(f"Unsupported tool action: {task.requested_action}")
    if task.requested_action not in agent.tool_names:
        raise ValueError(
            f"Action {task.requested_action} is not available for department {task.department}"
        )
    tool_input = _tool_input_for_task(task)
    _validate_task_tool_scope(db, task, tool_input)
    context = ToolContext(
        conversation_id=task.source_conversation_id,
        user_message_id=task.source_message_id,
        prepared_ordinance_embedding=prepared_ordinance_embedding,
    )
    authorization = None
    if task.requested_action in MUTATING_ACTIONS:
        canonical_input = normalize_tool_input(
            db,
            user,
            task.requested_action,
            tool_input,
            context,
        )
        authorization = issue_agent_office_tool_authorization(
            db,
            task_id=task.id,
            actor_id=user.id,
            tool=task.requested_action,
            input_digest=tool_input_digest(
                task.requested_action,
                canonical_input,
            ),
        )
    result = execute_tool(
        db,
        user,
        task.requested_action,
        tool_input,
        context,
        allowed=agent.tool_names,
        authorization=authorization,
        defer_commit=True,
    )
    return {
        "mode": "tool",
        "tool": task.requested_action,
        "ok": result.ok,
        "content": _decode_tool_content(result.content),
    }


def _run_daily_briefing(db: Session, user: User, task: AgentOfficeTask) -> dict:
    organization_input = {"organization_id": task.organization_id}
    projects = execute_tool(
        db,
        user,
        "list_projects",
        organization_input,
        ToolContext(conversation_id=task.source_conversation_id, user_message_id=task.source_message_id),
        allowed=OFFICE_AGENTS["daily_briefing"].tool_names,
    )
    requirements = execute_tool(
        db,
        user,
        "list_requirements",
        organization_input,
        ToolContext(conversation_id=task.source_conversation_id, user_message_id=task.source_message_id),
        allowed=OFFICE_AGENTS["daily_briefing"].tool_names,
    )
    projects_payload = _decode_tool_content(projects.content) if projects.ok else []
    requirements_payload = _decode_tool_content(requirements.content) if requirements.ok else []
    return {
        "mode": "daily_briefing",
        "ok": projects.ok and requirements.ok,
        "projects": projects_payload if isinstance(projects_payload, list) else [],
        "requirements": requirements_payload if isinstance(requirements_payload, list) else [],
        "summary": {
            "project_count": len(projects_payload) if isinstance(projects_payload, list) else 0,
            "requirement_count": len(requirements_payload) if isinstance(requirements_payload, list) else 0,
            "project_errors": None if projects.ok else projects.content,
            "requirement_errors": None if requirements.ok else requirements.content,
        },
    }


def _run_preparatory_action(task: AgentOfficeTask) -> dict:
    return {
        "mode": "plan",
        "department": task.department,
        "requested_action": task.requested_action,
        "summary": (
            "This v1 office task was routed and audited, but this department "
            "does not yet have a safe backend tool for automatic execution."
        ),
        "next_step": "Assign a concrete backend tool or handle the task manually.",
    }


def execute_task_body(
    db: Session,
    user: User,
    task: AgentOfficeTask,
    *,
    prepared_ordinance_embedding: PreparedOrdinanceSearchEmbedding | None = None,
) -> dict:
    if task.requested_action == "daily_briefing":
        return _run_daily_briefing(db, user, task)
    if task.requested_action in {"triage", "prepare_document_work"}:
        return _run_preparatory_action(task)
    return _run_tool_action(
        db,
        user,
        task,
        prepared_ordinance_embedding=prepared_ordinance_embedding,
    )


def _task_result_error_message(result: dict) -> str:
    for key in ("error", "detail", "message", "content"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:2000]

    summary = result.get("summary")
    if isinstance(summary, dict):
        errors = [
            value.strip()
            for key, value in summary.items()
            if key.endswith("_errors")
            and isinstance(value, str)
            and value.strip()
        ]
        if errors:
            return "; ".join(errors)[:2000]

    action = result.get("tool") or result.get("mode") or "unknown"
    return f"Agent office action {action} returned an unsuccessful result."


def _latest_task_execution_attempt(
    db: Session,
    task_id: int,
) -> AgentOfficeTaskEvent | None:
    return db.scalar(
        select(AgentOfficeTaskEvent)
        .where(
            AgentOfficeTaskEvent.task_id == task_id,
            AgentOfficeTaskEvent.event_type == "started",
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
        .limit(1)
    )


def _running_attempt_is_incomplete(
    db: Session,
    task: AgentOfficeTask,
) -> bool:
    attempt = _latest_task_execution_attempt(db, task.id)
    if attempt is None:
        return False
    terminal_event = db.scalar(
        select(AgentOfficeTaskEvent.id)
        .where(
            AgentOfficeTaskEvent.task_id == task.id,
            AgentOfficeTaskEvent.id > attempt.id,
            AgentOfficeTaskEvent.event_type.in_(
                {"completed", "failed", "draft_ready", "cancelled"}
            ),
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
        .limit(1)
    )
    return terminal_event is None


def _running_analysis_is_recoverable(
    db: Session,
    task: AgentOfficeTask,
) -> bool:
    """Allow manual recovery only after the active worker lease has expired."""

    if not _running_attempt_is_incomplete(db, task):
        return False
    activity_at = db.scalar(
        select(AgentOfficeTaskEvent.created_at)
        .where(
            AgentOfficeTaskEvent.task_id == task.id,
            AgentOfficeTaskEvent.event_type.in_(
                {
                    "started",
                    "analysis_initialized",
                    "analysis_checkpointed",
                    "analysis_continuation_queued",
                    "analysis_retry_pending",
                    "analysis_requeued",
                }
            ),
        )
        .order_by(AgentOfficeTaskEvent.created_at.desc())
        .limit(1)
    )
    if activity_at is None:
        return False
    if activity_at.tzinfo is None:
        activity_at = activity_at.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=ORDINANCE_ANALYSIS_ACTIVE_LEASE_SECONDS
    )
    return activity_at <= cutoff


def _latest_ordinance_analysis_queue_job_id(
    db: Session,
    task_id: int,
) -> str | None:
    events = db.scalars(
        select(AgentOfficeTaskEvent)
        .where(
            AgentOfficeTaskEvent.task_id == task_id,
            AgentOfficeTaskEvent.event_type.in_(
                {
                    "queued",
                    "analysis_requeued",
                    "analysis_continuation_queued",
                }
            ),
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
        .limit(50)
    ).all()
    for event in events:
        queue_job_id = event.payload.get("queue_job_id")
        if isinstance(queue_job_id, str) and queue_job_id:
            return queue_job_id
    return None


def _latest_attempt_is_quarantined(db: Session, task_id: int) -> bool:
    attempt = _latest_task_execution_attempt(db, task_id)
    if attempt is None:
        return False
    quarantine_event = db.scalar(
        select(AgentOfficeTaskEvent.id)
        .where(
            AgentOfficeTaskEvent.task_id == task_id,
            AgentOfficeTaskEvent.id > attempt.id,
            AgentOfficeTaskEvent.event_type
            == EXTERNAL_READ_QUARANTINE_EVENT,
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
        .limit(1)
    )
    return quarantine_event is not None


def _task_is_terminal(task: AgentOfficeTask) -> bool:
    return task.status in {
        "waiting_approval",
        "completed",
        "failed",
        "cancelled",
    }


def _task_execution_user(task: AgentOfficeTask) -> User:
    if (
        task.requires_human_approval
        and task.approval_policy == "before_execution"
        and task.approved_by_id is None
    ):
        raise ValueError("Task requires human approval before execution")
    user = task.requested_by or task.approved_by
    if user is None or not user.is_active:
        raise ValueError("Task has no user context for RBAC execution")
    return user


def _parse_claim_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"External read claim has no valid {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(
            f"External read claim has no valid {field}"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(f"External read claim has no valid {field}")
    return parsed.astimezone(timezone.utc)


def _latest_external_read_claim(
    db: Session,
    *,
    task_id: int,
    execution_attempt_id: int,
) -> AgentOfficeTaskEvent | None:
    claims = db.scalars(
        select(AgentOfficeTaskEvent)
        .where(
            AgentOfficeTaskEvent.task_id == task_id,
            AgentOfficeTaskEvent.event_type == EXTERNAL_READ_CLAIM_EVENT,
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
    ).all()
    return next(
        (
            event
            for event in claims
            if event.payload.get("execution_attempt_id")
            == execution_attempt_id
        ),
        None,
    )


def _claim_external_read_action(
    db: Session,
    user: User,
    task: AgentOfficeTask,
) -> ExternalReadClaim | None:
    """Claim one external read while the caller owns the task row lock.

    The caller must commit immediately after this function returns a claim and
    before invoking the provider.  A live claim makes concurrent workers
    return without issuing another request.  Once the lease expires, retrying
    is safe because this path is strictly read-only.
    """

    attempt = _latest_task_execution_attempt(db, task.id)
    if attempt is None:
        raise ValueError("Agent office task has no execution attempt")
    if _latest_attempt_is_quarantined(db, task.id):
        raise EmbeddingWorkerCleanupError(
            "External read attempt is quarantined after indeterminate cleanup"
        )
    tool_input = _tool_input_for_task(task)
    _validate_task_tool_scope(db, task, tool_input)
    agent = OFFICE_AGENTS[task.department]
    if task.requested_action not in agent.tool_names:
        raise ValueError(
            f"Action {task.requested_action} is not available for department "
            f"{task.department}"
        )
    if task.requested_action == "semantic_search_ordinances" and not has_permission(
        user,
        "ordinances.compare",
        db,
    ):
        raise ValueError("Permission required: ordinances.compare")

    canonical_input = normalize_tool_input(
        db,
        user,
        task.requested_action,
        tool_input,
        ToolContext(
            conversation_id=task.source_conversation_id,
            user_message_id=task.source_message_id,
        ),
    )
    input_digest = tool_input_digest(task.requested_action, canonical_input)
    latest_claim = _latest_external_read_claim(
        db,
        task_id=task.id,
        execution_attempt_id=attempt.id,
    )
    now = datetime.now(timezone.utc)
    if latest_claim is not None:
        payload = latest_claim.payload
        if (
            payload.get("tool") != task.requested_action
            or payload.get("input_digest") != input_digest
        ):
            raise ValueError("External read execution intent changed")
        provider_deadline_at = _parse_claim_datetime(
            payload.get("provider_deadline_at"),
            field="provider deadline",
        )
        lease_expires_at = _parse_claim_datetime(
            payload.get("lease_expires_at"),
            field="lease",
        )
        minimum_lease_expiry = provider_deadline_at + timedelta(
            seconds=supervised_embedding_cleanup_margin_seconds()
        )
        if lease_expires_at < minimum_lease_expiry:
            raise ValueError(
                "External read lease does not cover provider cleanup"
            )
        if lease_expires_at > now:
            return None

    claim_id = uuid.uuid4().hex
    provider_deadline_at = now + timedelta(
        seconds=settings.embeddings_timeout_seconds
    )
    lease_expires_at = provider_deadline_at + timedelta(
        seconds=supervised_embedding_cleanup_margin_seconds()
    )
    add_task_event(
        db,
        task,
        EXTERNAL_READ_CLAIM_EVENT,
        "Read-only external action claimed before provider execution.",
        payload={
            "claim_id": claim_id,
            "execution_attempt_id": attempt.id,
            "tool": task.requested_action,
            "input_digest": input_digest,
            "provider_deadline_at": provider_deadline_at.isoformat(),
            "lease_expires_at": lease_expires_at.isoformat(),
        },
        created_by_id=user.id,
    )
    db.flush()
    return ExternalReadClaim(
        claim_id=claim_id,
        execution_attempt_id=attempt.id,
        tool_input=canonical_input,
        provider_deadline_at=provider_deadline_at,
    )


def _external_read_claim_is_current(
    db: Session,
    task: AgentOfficeTask,
    claim: ExternalReadClaim,
) -> bool:
    latest_claim = _latest_external_read_claim(
        db,
        task_id=task.id,
        execution_attempt_id=claim.execution_attempt_id,
    )
    return (
        latest_claim is not None
        and latest_claim.payload.get("claim_id") == claim.claim_id
    )


def build_agent_office_queue_job_id(
    task_id: int,
    *,
    continuation: bool = False,
) -> str:
    kind = "continue" if continuation else "run"
    return (
        f"agent-office-{task_id}-{kind}-"
        f"{uuid.uuid4().hex}"
    )


def mark_ordinance_analysis_rq_failed(
    job,
    connection,
    exception_type,
    exception_value,
    traceback,
) -> None:
    """Make terminal RQ failure visible without persisting job payloads."""

    del connection, traceback
    retries_left = getattr(job, "retries_left", None)
    if (
        isinstance(retries_left, int)
        and not isinstance(retries_left, bool)
        and retries_left > 0
    ):
        # RQ invokes on_failure before deciding whether to retry.  Keep the
        # task running while another execution attempt is still scheduled.
        return
    if not job.args:
        return
    try:
        task_id = int(job.args[0])
    except (TypeError, ValueError):
        return
    with SessionLocal() as db:
        task = lock_task_for_transition(db, task_id)
        if (
            task is None
            or task.requested_action
            != ANALYZE_ORDINANCE_CORPUS_ACTION
            or task.status
            in {"completed", "failed", "cancelled", "waiting_approval"}
        ):
            db.rollback()
            return
        latest_queue_job_id = _latest_ordinance_analysis_queue_job_id(
            db,
            task.id,
        )
        if (
            latest_queue_job_id is not None
            and latest_queue_job_id != str(job.id)
        ):
            db.rollback()
            return
        error_message = (
            f"{getattr(exception_type, '__name__', 'Error')}: "
            f"{exception_value}"
        )[:2000]
        if (
            task.result.get("mode")
            == "ordinance_corpus_analysis"
        ):
            checkpoint = dict(task.result)
            checkpoint["resume_required"] = (
                checkpoint.get("phase") == "enumeration_complete"
            )
            task.result_json = _json_dumps(checkpoint)
        task.status = "failed"
        task.error_message = error_message
        task.completed_at = datetime.now(timezone.utc)
        add_task_event(
            db,
            task,
            "analysis_failed",
            "Ordinance corpus analysis exhausted its RQ retries.",
            payload={
                "queue_job_id": str(job.id),
                "checkpoint_preserved": True,
            },
        )
        add_task_event(
            db,
            task,
            "failed",
            error_message,
        )
        db.commit()


def run_agent_office_task_job(task_id: int) -> int:
    """RQ entry point that never returns an ORM graph to Redis."""

    run_agent_office_task(task_id)
    return task_id


def enqueue_agent_office_task_execution(
    task: AgentOfficeTask,
    *,
    queue_job_id: str | None = None,
    continuation: bool = False,
):
    job_id = queue_job_id or build_agent_office_queue_job_id(
        task.id,
        continuation=continuation,
    )
    enqueue_options: dict[str, Any] = {
        "job_id": job_id,
        "result_ttl": 0,
    }
    if (
        task.requested_action
        == ANALYZE_ORDINANCE_CORPUS_ACTION
    ):
        enqueue_options.update(
            {
                "job_timeout": (
                    ORDINANCE_ANALYSIS_RQ_JOB_TIMEOUT_SECONDS
                ),
                "retry": Retry(
                    max=len(
                        ORDINANCE_ANALYSIS_RQ_RETRY_INTERVALS
                    ),
                    interval=(
                        ORDINANCE_ANALYSIS_RQ_RETRY_INTERVALS
                    ),
                ),
                "on_failure": mark_ordinance_analysis_rq_failed,
            }
        )
    return get_default_queue().enqueue(
        run_agent_office_task_job,
        task.id,
        **enqueue_options,
    )


def _run_ordinance_corpus_analysis_locked(
    db: Session,
    task: AgentOfficeTask,
    *,
    page_budget: int | None,
    enqueue_continuation: bool,
) -> AgentOfficeTask:
    if page_budget is not None and page_budget < 1:
        raise ValueError("page_budget debe ser mayor o igual que 1")
    if _task_is_terminal(task) or task.status == "pending_approval":
        db.rollback()
        return task

    if task.status == "running":
        if not _running_attempt_is_incomplete(db, task):
            db.rollback()
            return task
    elif task.status in {"approved", "queued"}:
        task.status = "running"
        task.started_at = datetime.now(timezone.utc)
        task.completed_at = None
        task.error_message = None
        add_task_event(
            db,
            task,
            "started",
            "Durable ordinance corpus analysis started.",
            payload={
                "workflow": ANALYZE_ORDINANCE_CORPUS_ACTION,
            },
        )
        db.commit()
        task = lock_task_for_transition(db, task.id)
        if task is None:
            raise ValueError("Agent office task disappeared")
    else:
        db.rollback()
        return task

    pages_processed = 0
    while True:
        task = lock_task_for_transition(db, task.id)
        if task is None:
            raise ValueError("Agent office task disappeared")
        if task.status in {
            "pending_approval",
            "waiting_approval",
            "completed",
            "failed",
            "cancelled",
        }:
            db.rollback()
            return task

        user = _task_execution_user(task)
        filters = ordinance_analysis_filters_from_task(task)
        require_ordinance_analysis_access(
            db,
            user,
            filters,
        )
        state = task.result
        if (
            not state
            or state.get("mode")
            != "ordinance_corpus_analysis"
        ):
            existing_items = int(
                db.scalar(
                    select(
                        func.count(
                            AgentOfficeOrdinanceAnalysisItem.id
                        )
                    ).where(
                        AgentOfficeOrdinanceAnalysisItem.task_id
                        == task.id
                    )
                )
                or 0
            )
            if existing_items:
                raise ValueError(
                    "Hay checkpoints por ordenanza pero falta el "
                    "snapshot fijado de la tarea"
                )
            state = initialize_ordinance_analysis_state(
                db,
                task,
                user,
            )
            task.result_json = _json_dumps(state)
            add_task_event(
                db,
                task,
                "analysis_initialized",
                "Internal ordinance snapshot fixed for durable analysis.",
                payload={
                    "snapshot_id": state["snapshot_id"],
                    "expected_ordinances": state[
                        "expected_ordinances"
                    ],
                    "workflow_version": state[
                        "workflow_version"
                    ],
                },
            )
            db.commit()
            continue

        resumed_state = prepare_ordinance_analysis_resume(
            db,
            task,
            dict(state),
        )
        if resumed_state != state:
            state = resumed_state
            task.result_json = _json_dumps(state)
            add_task_event(
                db,
                task,
                "analysis_resumed",
                "Incomplete ordinance items will be retried.",
                payload={
                    "snapshot_id": state["snapshot_id"],
                    "resume_pass": state["resume_passes"],
                },
            )

        if state["phase"] == "enumeration_complete":
            verify_ordinance_analysis_snapshot(
                db,
                state,
            )
            final_result = reconcile_ordinance_analysis(
                db,
                task,
                state,
            )
            task.result_json = _json_dumps(final_result)
            task.error_message = None
            task.completed_at = datetime.now(timezone.utc)
            add_task_event(
                db,
                task,
                "analysis_completed",
                "Internal ordinance snapshot reconciled.",
                payload={
                    "snapshot_id": state["snapshot_id"],
                    "completed_ordinances": (
                        final_result["reconciliation"][
                            "completed_items"
                        ]
                    ),
                    "balanced": True,
                },
            )
            if (
                task.requires_human_approval
                and task.approval_policy
                in {"after_draft", "always"}
            ):
                task.status = "waiting_approval"
                add_task_event(
                    db,
                    task,
                    "draft_ready",
                    (
                        "Ordinance inventory result is waiting "
                        "for human approval."
                    ),
                )
            else:
                task.status = "completed"
                add_task_event(
                    db,
                    task,
                    "completed",
                    "Task completed by the agent office.",
                )
            db.commit()
            return task

        outcome = process_ordinance_analysis_page(
            db,
            task,
            state,
        )
        state = outcome.state
        task.result_json = _json_dumps(state)
        task.error_message = None
        add_task_event(
            db,
            task,
            "analysis_checkpointed",
            "Ordinance analysis page committed.",
            payload={
                "snapshot_id": state["snapshot_id"],
                "page_candidates": outcome.page_candidates,
                "created_items": outcome.created_items,
                "completed_items": outcome.completed_items,
                "failed_items": outcome.failed_items,
                "skipped_completed_items": (
                    outcome.skipped_completed_items
                ),
                "catalog_consumed": state["catalog_consumed"],
                "expected_ordinances": state[
                    "expected_ordinances"
                ],
                "complete": outcome.complete,
            },
        )
        db.commit()
        pages_processed += 1

        if outcome.complete:
            continue
        if (
            page_budget is None
            or pages_processed < page_budget
        ):
            continue
        if enqueue_continuation:
            queue_job = enqueue_agent_office_task_execution(
                task,
                continuation=True,
            )
            task = lock_task_for_transition(db, task.id)
            if task is None:
                raise ValueError("Agent office task disappeared")
            if task.status == "running":
                add_task_event(
                    db,
                    task,
                    "analysis_continuation_queued",
                    "Next ordinance analysis page queued.",
                    payload={
                        "queue_job_id": str(queue_job.id),
                        "snapshot_id": state["snapshot_id"],
                    },
                )
                db.commit()
            else:
                db.rollback()
        return task


def run_ordinance_corpus_analysis_batch(
    task_id: int,
    db: Session,
    *,
    page_budget: int = 1,
) -> AgentOfficeTask:
    task = lock_task_for_transition(db, task_id)
    if task is None:
        raise ValueError(f"Agent office task not found: {task_id}")
    if (
        task.requested_action
        != ANALYZE_ORDINANCE_CORPUS_ACTION
    ):
        raise ValueError(
            "Task is not an ordinance corpus analysis"
        )
    return _run_ordinance_corpus_analysis_locked(
        db,
        task,
        page_budget=page_budget,
        enqueue_continuation=False,
    )


def run_agent_office_task(task_id: int, db: Session | None = None) -> AgentOfficeTask:
    owns_session = db is None
    session = db or SessionLocal()
    external_claim: ExternalReadClaim | None = None
    try:
        task = lock_task_for_transition(session, task_id)
        if task is None:
            raise ValueError(f"Agent office task not found: {task_id}")
        if (
            task.requested_action
            == ANALYZE_ORDINANCE_CORPUS_ACTION
        ):
            return _run_ordinance_corpus_analysis_locked(
                session,
                task,
                page_budget=(
                    ORDINANCE_ANALYSIS_PAGES_PER_RQ_JOB
                    if owns_session
                    else None
                ),
                enqueue_continuation=owns_session,
            )
        if _task_is_terminal(task) or task.status == "pending_approval":
            session.rollback()
            return task
        started_new_attempt = False
        if task.status == "running":
            if not _running_attempt_is_incomplete(session, task):
                session.rollback()
                return task
        elif task.status in {"approved", "queued"}:
            task.status = "running"
            task.started_at = datetime.now(timezone.utc)
            task.error_message = None
            add_task_event(
                session,
                task,
                "started",
                "Agent office task execution started.",
            )
            session.flush()
            started_new_attempt = True
        else:
            session.rollback()
            return task

        if started_new_attempt:
            # Persist the attempt before invoking execute_tool.  If that call
            # rolls its transaction back, the audit history still records a
            # single durable started -> failed sequence.  Reacquiring the row
            # lock here is mandatory: every DB-local mutator keeps this lock
            # through its effect, authorization ledger and terminal event.
            session.commit()
            task = lock_task_for_transition(session, task_id)
            if task is None:
                raise ValueError(f"Agent office task not found: {task_id}")
            if task.status != "running":
                session.rollback()
                return task
        user = _task_execution_user(task)

        prepared_embedding: PreparedOrdinanceSearchEmbedding | None = None
        if task.requested_action in EXTERNAL_READ_ACTIONS:
            external_claim = _claim_external_read_action(session, user, task)
            if external_claim is None:
                # Another worker owns a live durable claim.  It will finalize
                # the task; this worker must not duplicate the provider call.
                session.rollback()
                return task
            session.commit()

            # No Session operation occurs between this commit and the next
            # FOR UPDATE.  In particular, the embeddings HTTP request cannot
            # retain a transaction, row lock or advisory lock.
            prepared_embedding = prepare_ordinance_search_embedding(
                external_claim.tool_input,
                provider_deadline_at=external_claim.provider_deadline_at,
            )

            task = lock_task_for_transition(session, task_id)
            if task is None:
                raise ValueError(f"Agent office task not found: {task_id}")
            if task.status != "running" or not _external_read_claim_is_current(
                session,
                task,
                external_claim,
            ):
                # Human cancellation or a later recovery claim wins.  The
                # read-only provider result is intentionally discarded.
                session.rollback()
                return task
            user = _task_execution_user(task)

        result = execute_task_body(
            session,
            user,
            task,
            prepared_ordinance_embedding=prepared_embedding,
        )
        task = lock_task_for_transition(session, task_id)
        if task is None:
            raise ValueError(f"Agent office task not found: {task_id}")
        if task.status != "running" or (
            external_claim is not None
            and not _external_read_claim_is_current(
                session,
                task,
                external_claim,
            )
        ):
            session.rollback()
            return task
        task.result_json = json.dumps(result, ensure_ascii=False)
        task.completed_at = datetime.now(timezone.utc)
        if result.get("ok") is False:
            task.status = "failed"
            error_message = _task_result_error_message(result)
            task.error_message = error_message
            add_task_event(
                session,
                task,
                "failed",
                error_message,
                payload=result,
            )
        elif task.requires_human_approval and task.approval_policy in {
            "after_draft",
            "always",
        }:
            task.status = "waiting_approval"
            add_task_event(session, task, "draft_ready", "Task result is waiting for human approval.", payload=result)
        else:
            task.status = "completed"
            add_task_event(session, task, "completed", "Task completed by the agent office.", payload=result)
        session.commit()
        return task
    except Exception as error:
        session.rollback()
        task = lock_task_for_transition(session, task_id)
        if (
            task is not None
            and task.requested_action
            == ANALYZE_ORDINANCE_CORPUS_ACTION
            and owns_session
        ):
            if task.status in {
                "completed",
                "failed",
                "cancelled",
                "waiting_approval",
            }:
                session.rollback()
                return task
            error_detail = getattr(error, "detail", None)
            error_message = str(
                error_detail
                if error_detail is not None
                else error
            )[:2000]
            if (
                task.result.get("mode")
                == "ordinance_corpus_analysis"
            ):
                checkpoint = dict(task.result)
                checkpoint["resume_required"] = (
                    checkpoint.get("phase")
                    == "enumeration_complete"
                )
                task.result_json = _json_dumps(checkpoint)
            task.error_message = error_message
            add_task_event(
                session,
                task,
                "analysis_retry_pending",
                (
                    "Ordinance analysis page failed; the durable "
                    "checkpoint is preserved for RQ retry."
                ),
                payload={
                    "error_type": type(error).__name__,
                    "checkpoint_preserved": True,
                },
            )
            session.commit()
            raise
        if task is not None and isinstance(error, EmbeddingWorkerCleanupError):
            if not _latest_attempt_is_quarantined(session, task.id):
                add_task_event(
                    session,
                    task,
                    EXTERNAL_READ_QUARANTINE_EVENT,
                    (
                        "External provider worker cleanup could not be "
                        "confirmed; automatic retry is quarantined."
                    ),
                )
            if task.status != "failed":
                task.status = "failed"
                task.error_message = str(error)[:2000]
                task.completed_at = datetime.now(timezone.utc)
                add_task_event(session, task, "failed", task.error_message)
            session.commit()
            return task
        if (
            task is not None
            and task.status == "running"
            and external_claim is not None
            and not _external_read_claim_is_current(
                session,
                task,
                external_claim,
            )
        ):
            session.rollback()
            return task
        if task is not None and not _task_is_terminal(task):
            if (
                task.requested_action
                == ANALYZE_ORDINANCE_CORPUS_ACTION
                and task.result.get("mode")
                == "ordinance_corpus_analysis"
            ):
                checkpoint = dict(task.result)
                checkpoint["resume_required"] = (
                    checkpoint.get("phase")
                    == "enumeration_complete"
                )
                task.result_json = _json_dumps(checkpoint)
            task.status = "failed"
            task.error_message = str(error)[:2000]
            task.completed_at = datetime.now(timezone.utc)
            add_task_event(session, task, "failed", task.error_message)
            session.commit()
            return task
        if task is not None:
            session.rollback()
            return task
        raise
    finally:
        if owns_session:
            session.close()


def validate_routine_values(kind: str, status: str, cadence: str, target_channel: str) -> None:
    if kind not in AGENT_OFFICE_ROUTINE_KINDS:
        raise HTTPException(status_code=400, detail="Invalid routine kind")
    if status not in AGENT_OFFICE_ROUTINE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid routine status")
    if cadence not in AGENT_OFFICE_ROUTINE_CADENCES:
        raise HTTPException(status_code=400, detail="Invalid routine cadence")
    if target_channel not in AGENT_OFFICE_TARGET_CHANNELS:
        raise HTTPException(status_code=400, detail="Invalid routine target channel")


def create_routine(
    db: Session,
    current_user: User,
    *,
    organization_id: int,
    name: str,
    kind: str,
    routine_status: str,
    cadence: str,
    schedule_time: str,
    timezone_name: str,
    target_channel: str,
    config: dict,
    next_run_at: datetime | None,
) -> AgentOfficeRoutine:
    ensure_organization_exists(db, organization_id)
    require_agent_office_permission(db, current_user, organization_id, "agent_office.manage")
    validate_routine_values(kind, routine_status, cadence, target_channel)
    routine = AgentOfficeRoutine(
        organization_id=organization_id,
        name=name,
        kind=kind,
        status=routine_status,
        cadence=cadence,
        schedule_time=schedule_time,
        timezone=timezone_name,
        target_channel=target_channel,
        config_json=_json_dumps(config),
        next_run_at=next_run_at,
        created_by_id=current_user.id,
    )
    db.add(routine)
    db.commit()
    db.refresh(routine)
    return routine


def list_routines_for_user(
    db: Session,
    current_user: User,
    organization_id: int | None = None,
) -> list[AgentOfficeRoutine]:
    query = select(AgentOfficeRoutine).order_by(AgentOfficeRoutine.id.desc())
    if organization_id is not None:
        ensure_organization_exists(db, organization_id)
        query = query.where(AgentOfficeRoutine.organization_id == organization_id)
    visible_org_ids = visible_agent_office_organization_ids(db, current_user)
    if visible_org_ids is not None:
        if not visible_org_ids:
            return []
        query = query.where(AgentOfficeRoutine.organization_id.in_(visible_org_ids))
    return list(db.scalars(query.limit(200)))


def get_routine_for_user(db: Session, current_user: User, routine_id: int) -> AgentOfficeRoutine:
    routine = db.get(AgentOfficeRoutine, routine_id)
    if routine is None:
        raise HTTPException(status_code=404, detail="Agent office routine not found")
    if not (
        current_user.is_superuser
        or has_permission(current_user, "agent_office.view", db, organization_id=routine.organization_id)
        or has_permission(current_user, "agent_office.manage", db, organization_id=routine.organization_id)
    ):
        raise HTTPException(status_code=404, detail="Agent office routine not found")
    return routine


def trigger_routine(db: Session, current_user: User, routine: AgentOfficeRoutine) -> AgentOfficeTask:
    require_agent_office_permission(db, current_user, routine.organization_id, "agent_office.execute")
    task = create_task(
        db,
        current_user,
        organization_id=routine.organization_id,
        title=f"Informe diario: {routine.name}",
        description="Preparar informe diario supervisado de proyectos y necesidades visibles.",
        department="daily_briefing",
        requested_action="daily_briefing",
        priority="medium",
        approval_policy="never",
        requires_human_approval=False,
        input_payload={"routine_id": routine.id, **routine.config},
    )
    routine.last_run_at = datetime.now(timezone.utc)
    db.commit()
    return task
