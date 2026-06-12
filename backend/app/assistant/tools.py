"""Tools the intake agent can execute on behalf of the current user.

Every executor runs with the calling user's RBAC permissions by reusing the
same validation helpers as the REST routes. The agent can only do what the
user could do through the API. Human-supervision principle: the agent creates
requirements as drafts (or moves them to 'submitted'); review states stay
human-only.
"""

import json
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.organizations.access import (
    get_accessible_organizations_query,
    get_user_organization_ids,
)
from app.projects.access import select_visible_projects
from app.projects.models import Project
from app.rbac.permissions import has_permission
from app.requirements.models import Requirement, RequirementMessage
from app.requirements.routes import (
    build_requirement_visibility_filter,
    ensure_organization_exists,
    ensure_project_matches_organization,
    get_existing_requirement,
    require_requirement_content_edit,
    require_requirement_permission,
    require_requirement_view,
)
from app.users.models import User

VALID_PRIORITIES = {"low", "medium", "high", "urgent"}

REQUIREMENT_CONTENT_FIELDS = (
    "title",
    "summary",
    "problem",
    "current_process",
    "desired_process",
    "affected_users",
    "involved_documents",
    "data_sensitivity_notes",
    "legal_notes",
    "acceptance_criteria",
    "open_questions",
)

_REQUIREMENT_FIELD_PROPERTIES = {
    "title": {"type": "string", "description": "Título corto del requisito"},
    "summary": {"type": "string", "description": "Resumen en una o dos frases"},
    "problem": {"type": "string", "description": "Problema o necesidad detectada"},
    "current_process": {
        "type": "string",
        "description": "Cómo se hace hoy ese proceso",
    },
    "desired_process": {
        "type": "string",
        "description": "Cómo debería funcionar idealmente",
    },
    "affected_users": {
        "type": "string",
        "description": "Quiénes se ven afectados (vecinos, funcionarios...)",
    },
    "involved_documents": {
        "type": "string",
        "description": "Documentos o formularios implicados",
    },
    "data_sensitivity_notes": {
        "type": "string",
        "description": "Notas sobre datos sensibles o personales implicados",
    },
    "legal_notes": {
        "type": "string",
        "description": "Notas legales o normativa aplicable",
    },
    "acceptance_criteria": {
        "type": "string",
        "description": "Criterios para dar el requisito por cumplido",
    },
    "open_questions": {
        "type": "string",
        "description": "Dudas pendientes de aclarar",
    },
    "priority": {
        "type": "string",
        "enum": ["low", "medium", "high", "urgent"],
        "description": "Prioridad del requisito",
    },
    "project_id": {
        "type": "integer",
        "description": "ID del proyecto al que pertenece (opcional)",
    },
}

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "list_organizations",
        "description": (
            "Lista las organizaciones a las que pertenece el usuario actual. "
            "Úsala para resolver organization_id antes de crear o listar "
            "requisitos cuando el usuario pertenece a más de una."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_projects",
        "description": (
            "Lista los proyectos (expedientes/áreas de trabajo) visibles para "
            "el usuario. Úsala cuando el usuario quiera vincular un requisito "
            "a un proyecto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": "Filtrar por organización (opcional)",
                },
            },
        },
    },
    {
        "name": "list_requirements",
        "description": (
            "Lista los requisitos visibles para el usuario, con id, título, "
            "estado y prioridad. Úsala antes de crear un requisito para "
            "evitar duplicados, o cuando el usuario pregunte qué hay "
            "registrado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": "Filtrar por organización (opcional)",
                },
                "status": {
                    "type": "string",
                    "enum": [
                        "draft",
                        "submitted",
                        "in_review",
                        "needs_clarification",
                        "accepted",
                        "rejected",
                        "converted",
                        "archived",
                    ],
                    "description": "Filtrar por estado (opcional)",
                },
            },
        },
    },
    {
        "name": "get_requirement",
        "description": "Devuelve el detalle completo de un requisito por su id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "requirement_id": {"type": "integer"},
            },
            "required": ["requirement_id"],
        },
    },
    {
        "name": "create_requirement",
        "description": (
            "Crea un requisito nuevo como BORRADOR (status=draft, "
            "source_type=conversation) en nombre del usuario. Antes de "
            "llamarla, confirma con el usuario al menos el título y el "
            "problema. Rellena todos los campos que la conversación permita."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": "Organización a la que pertenece el requisito",
                },
                **_REQUIREMENT_FIELD_PROPERTIES,
            },
            "required": ["organization_id", "title"],
        },
    },
    {
        "name": "update_requirement",
        "description": (
            "Actualiza los campos de contenido de un requisito existente, o "
            "lo pasa de borrador a 'submitted' cuando el usuario dé el visto "
            "bueno. No puede cambiar estados de revisión (eso es trabajo "
            "humano)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requirement_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["draft", "submitted"],
                    "description": "Nuevo estado (solo draft o submitted)",
                },
                **_REQUIREMENT_FIELD_PROPERTIES,
            },
            "required": ["requirement_id"],
        },
    },
    {
        "name": "add_requirement_message",
        "description": (
            "Añade una nota o aclaración al hilo de un requisito existente, "
            "firmada por el usuario actual."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requirement_id": {"type": "integer"},
                "body": {"type": "string"},
                "message_type": {
                    "type": "string",
                    "enum": ["note", "question", "answer", "clarification"],
                    "description": "Tipo de mensaje (por defecto note)",
                },
            },
            "required": ["requirement_id", "body"],
        },
    },
]


@dataclass
class ToolResult:
    content: str
    ok: bool


def execute_tool(
    db: Session,
    current_user: User,
    name: str,
    tool_input: dict,
) -> ToolResult:
    executor = _EXECUTORS.get(name)
    if executor is None:
        return ToolResult(content=f"Herramienta desconocida: {name}", ok=False)

    try:
        result = executor(db, current_user, tool_input)
    except HTTPException as error:
        db.rollback()
        return ToolResult(
            content=f"Error ({error.status_code}): {error.detail}",
            ok=False,
        )
    except (KeyError, TypeError, ValueError) as error:
        db.rollback()
        return ToolResult(content=f"Entrada inválida: {error}", ok=False)
    except SQLAlchemyError:
        db.rollback()
        return ToolResult(
            content="Error de base de datos al ejecutar la herramienta",
            ok=False,
        )

    return ToolResult(content=json.dumps(result, ensure_ascii=False), ok=True)


def _serialize_requirement(requirement: Requirement, *, full: bool) -> dict:
    data = {
        "id": requirement.id,
        "organization_id": requirement.organization_id,
        "project_id": requirement.project_id,
        "title": requirement.title,
        "status": requirement.status,
        "priority": requirement.priority,
    }
    if full:
        for field in REQUIREMENT_CONTENT_FIELDS:
            data[field] = getattr(requirement, field)
        data["source_type"] = requirement.source_type
    else:
        data["summary"] = requirement.summary
    return data


def _list_organizations(db: Session, current_user: User, tool_input: dict) -> list:
    organizations = db.scalars(
        get_accessible_organizations_query(current_user)
    ).all()
    return [
        {"id": organization.id, "name": organization.name, "status": organization.status}
        for organization in organizations
    ]


def _list_projects(db: Session, current_user: User, tool_input: dict) -> list:
    organization_ids = get_user_organization_ids(db, current_user)
    view_all_organization_ids = [
        organization_id
        for organization_id in organization_ids
        if has_permission(
            current_user,
            "projects.view_all",
            db,
            organization_id=organization_id,
        )
    ]
    query = select_visible_projects(
        current_user,
        organization_ids=organization_ids,
        view_all_organization_ids=view_all_organization_ids,
    )
    requested_organization_id = tool_input.get("organization_id")
    if requested_organization_id is not None:
        query = query.where(Project.organization_id == requested_organization_id)

    projects = db.scalars(query).all()
    return [
        {
            "id": project.id,
            "name": project.name,
            "organization_id": project.organization_id,
            "status": project.status,
        }
        for project in projects
    ]


def _list_requirements(db: Session, current_user: User, tool_input: dict) -> list:
    query = select(Requirement).order_by(
        Requirement.created_at.desc(),
        Requirement.id.desc(),
    )
    requested_organization_id = tool_input.get("organization_id")
    if requested_organization_id is not None:
        query = query.where(Requirement.organization_id == requested_organization_id)
    requested_status = tool_input.get("status")
    if requested_status is not None:
        query = query.where(Requirement.status == requested_status)
    else:
        query = query.where(Requirement.status != "archived")
    if not current_user.is_superuser:
        query = query.where(build_requirement_visibility_filter(db, current_user))

    requirements = db.scalars(query.limit(50)).all()
    return [
        _serialize_requirement(requirement, full=False)
        for requirement in requirements
    ]


def _get_requirement(db: Session, current_user: User, tool_input: dict) -> dict:
    requirement = get_existing_requirement(db, int(tool_input["requirement_id"]))
    require_requirement_view(db, current_user, requirement)
    return _serialize_requirement(requirement, full=True)


def _create_requirement(db: Session, current_user: User, tool_input: dict) -> dict:
    organization_id = int(tool_input["organization_id"])
    title = str(tool_input["title"]).strip()
    if not title:
        raise ValueError("title no puede estar vacío")

    ensure_organization_exists(db, organization_id)
    require_requirement_permission(
        db,
        current_user,
        organization_id,
        "requirements.create",
    )
    project_id = tool_input.get("project_id")
    ensure_project_matches_organization(
        db,
        project_id=project_id,
        organization_id=organization_id,
    )

    priority = tool_input.get("priority") or "medium"
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"priority inválida: {priority}")

    requirement = Requirement(
        organization_id=organization_id,
        project_id=project_id,
        title=title[:255],
        priority=priority,
        status="draft",
        source_type="conversation",
        created_by_id=current_user.id,
    )
    for field in REQUIREMENT_CONTENT_FIELDS:
        if field == "title":
            continue
        value = tool_input.get(field)
        if value is not None:
            setattr(requirement, field, str(value))

    db.add(requirement)
    db.commit()
    return _serialize_requirement(requirement, full=True)


def _update_requirement(db: Session, current_user: User, tool_input: dict) -> dict:
    requirement = get_existing_requirement(db, int(tool_input["requirement_id"]))
    # View access is the floor: without it, a no-op update would leak the
    # serialized requirement to users who cannot read it.
    require_requirement_view(db, current_user, requirement)

    requested_status = tool_input.get("status")
    if requested_status is not None and requested_status not in {"draft", "submitted"}:
        raise ValueError("El asistente solo puede usar los estados draft y submitted")
    requested_priority = tool_input.get("priority")
    if requested_priority is not None and requested_priority not in VALID_PRIORITIES:
        raise ValueError(f"priority inválida: {requested_priority}")

    content_updates = {
        field: tool_input[field]
        for field in (*REQUIREMENT_CONTENT_FIELDS, "priority")
        if field in tool_input and tool_input[field] is not None
    }
    project_id_present = "project_id" in tool_input

    if content_updates or project_id_present or requested_status is not None:
        require_requirement_content_edit(db, current_user, requirement)

    if project_id_present:
        ensure_project_matches_organization(
            db,
            project_id=tool_input["project_id"],
            organization_id=requirement.organization_id,
        )
        requirement.project_id = tool_input["project_id"]

    for field, value in content_updates.items():
        setattr(requirement, field, str(value) if field != "priority" else value)
    if requested_status is not None:
        requirement.status = requested_status

    db.commit()
    return _serialize_requirement(requirement, full=True)


def _add_requirement_message(
    db: Session,
    current_user: User,
    tool_input: dict,
) -> dict:
    requirement = get_existing_requirement(db, int(tool_input["requirement_id"]))
    require_requirement_view(db, current_user, requirement)

    message_type = tool_input.get("message_type") or "note"
    if message_type not in {"note", "question", "answer", "clarification"}:
        raise ValueError("message_type inválido para el asistente")

    message = RequirementMessage(
        requirement_id=requirement.id,
        author_id=current_user.id,
        body=str(tool_input["body"]),
        message_type=message_type,
    )
    db.add(message)
    db.commit()
    return {
        "id": message.id,
        "requirement_id": requirement.id,
        "message_type": message.message_type,
    }


_EXECUTORS = {
    "list_organizations": _list_organizations,
    "list_projects": _list_projects,
    "list_requirements": _list_requirements,
    "get_requirement": _get_requirement,
    "create_requirement": _create_requirement,
    "update_requirement": _update_requirement,
    "add_requirement_message": _add_requirement_message,
}
