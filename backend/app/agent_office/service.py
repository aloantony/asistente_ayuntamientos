import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status as http_status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.agent_office.models import (
    AGENT_OFFICE_APPROVAL_POLICIES,
    AGENT_OFFICE_DEPARTMENTS,
    AGENT_OFFICE_PRIORITIES,
    AGENT_OFFICE_ROUTINE_CADENCES,
    AGENT_OFFICE_ROUTINE_KINDS,
    AGENT_OFFICE_ROUTINE_STATUSES,
    AGENT_OFFICE_TARGET_CHANNELS,
    AgentOfficeRoutine,
    AgentOfficeTask,
    AgentOfficeTaskEvent,
)
from app.assistant.tools import ToolContext, execute_tool
from app.db.session import SessionLocal
from app.organizations.access import get_user_organization_ids
from app.organizations.models import Organization
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
            "requires_approval_by_default": self.requires_approval_by_default,
        }


OFFICE_AGENTS: dict[str, AgentOfficeAgentSpec] = {
    "front_desk": AgentOfficeAgentSpec(
        key="front_desk",
        name="Anacleto Recepción",
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
        description="Busca y compara normativa municipal ya importada y aprobada.",
        assistant_agent_key="consultation",
        tool_names=frozenset({"semantic_search_ordinances"}),
        mutating_actions=frozenset(),
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
    return task


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

    return list(db.scalars(query.limit(200)))


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
    db.commit()
    return get_task_for_user(db, current_user, task.id)


def approve_or_cancel_task(
    db: Session,
    current_user: User,
    task: AgentOfficeTask,
    *,
    decision: str,
    notes: str | None = None,
) -> AgentOfficeTask:
    require_agent_office_permission(db, current_user, task.organization_id, "agent_office.approve")
    now = datetime.now(timezone.utc)
    if decision == "cancel":
        if task.status in {"running", "completed"}:
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


def mark_task_queued(db: Session, current_user: User, task: AgentOfficeTask) -> AgentOfficeTask:
    require_agent_office_permission(db, current_user, task.organization_id, "agent_office.execute")
    if task.status not in {"approved", "failed"}:
        raise HTTPException(status_code=409, detail="Only approved or failed tasks can be queued")
    task.status = "queued"
    task.error_message = None
    add_task_event(db, task, "queued", "Task queued for agent office execution.", created_by_id=current_user.id)
    db.commit()
    return get_task_for_user(db, current_user, task.id)


def _tool_input_for_task(task: AgentOfficeTask) -> dict:
    tool_input = dict(task.input)
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


def _run_tool_action(db: Session, user: User, task: AgentOfficeTask) -> dict:
    agent = OFFICE_AGENTS[task.department]
    if task.requested_action not in TOOL_ACTIONS:
        raise ValueError(f"Unsupported tool action: {task.requested_action}")
    if task.requested_action not in agent.tool_names:
        raise ValueError(
            f"Action {task.requested_action} is not available for department {task.department}"
        )
    tool_input = _tool_input_for_task(task)
    _validate_task_tool_scope(db, task, tool_input)
    result = execute_tool(
        db,
        user,
        task.requested_action,
        tool_input,
        ToolContext(
            conversation_id=task.source_conversation_id,
            user_message_id=task.source_message_id,
        ),
        allowed=agent.tool_names,
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


def execute_task_body(db: Session, user: User, task: AgentOfficeTask) -> dict:
    if task.requested_action == "daily_briefing":
        return _run_daily_briefing(db, user, task)
    if task.requested_action in {"triage", "prepare_document_work"}:
        return _run_preparatory_action(task)
    return _run_tool_action(db, user, task)


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


def run_agent_office_task(task_id: int, db: Session | None = None) -> AgentOfficeTask:
    owns_session = db is None
    session = db or SessionLocal()
    try:
        task = session.scalar(
            select(AgentOfficeTask)
            .options(selectinload(AgentOfficeTask.events))
            .where(AgentOfficeTask.id == task_id)
        )
        if task is None:
            raise ValueError(f"Agent office task not found: {task_id}")
        if task.status not in {"approved", "queued"}:
            raise ValueError(f"Task cannot run from status {task.status}")
        if task.requires_human_approval and task.approval_policy == "before_execution" and task.approved_by_id is None:
            raise ValueError("Task requires human approval before execution")
        user = task.requested_by or task.approved_by
        if user is None:
            raise ValueError("Task has no user context for RBAC execution")

        now = datetime.now(timezone.utc)
        task.status = "running"
        task.started_at = now
        task.error_message = None
        add_task_event(session, task, "started", "Agent office task execution started.")
        session.commit()

        result = execute_task_body(session, user, task)
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
        if "task" in locals() and task is not None:
            task.status = "failed"
            task.error_message = str(error)[:2000]
            task.completed_at = datetime.now(timezone.utc)
            add_task_event(session, task, "failed", task.error_message)
            session.commit()
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
