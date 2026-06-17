"""Tools the intake agent can execute on behalf of the current user.

Every executor runs with the calling user's RBAC permissions by reusing the
same validation helpers as the REST routes. The agent can only do what the
user could do through the API. Human-supervision principle: the agent creates
requirements as drafts (or moves them to 'submitted'); review states stay
human-only.
"""

import json
from dataclasses import dataclass
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.assistant.hermes_web import HermesWebUnavailableError, hermes_web_client
from app.assistant.models import AssistantMemoryEntry
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
VALID_MEMORY_CATEGORIES = {
    "protocol",
    "preference",
    "context",
    "decision",
    "open_question",
}
VALID_MEMORY_SENSITIVITIES = {"normal", "personal", "sensitive", "legal"}
MAX_WEB_QUERY_CHARS = 400
MAX_WEB_RESULTS = 5
PERSONAL_DATA_PATTERN = re.compile(
    r"(\b\d{8}[A-Za-z]\b|\b[XYZ]\d{7}[A-Za-z]\b|[\w.+-]+@[\w-]+\.[\w.-]+|\b(?:\+34\s?)?[6789]\d{8}\b)",
    re.IGNORECASE,
)

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
        "name": "web_search",
        "description": (
            "Busca información pública actual en internet usando una instancia "
            "Hermes Web controlada por el backend. Úsala solo cuando el usuario "
            "pida buscar o verificar información externa. No incluyas datos "
            "internos, documentos, historial ni información personal en la "
            "consulta; envía únicamente una consulta explícita y mínima."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta pública explícita para buscar en la web",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número de resultados, máximo 5",
                },
            },
            "required": ["query"],
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
    {
        "name": "propose_memory_entry",
        "description": (
            "Propone una entrada de memoria institucional para revisión humana. "
            "No la guarda como conocimiento aprobado: solo crea una propuesta "
            "pendiente para que un responsable la valide, edite o rechace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": "Organización a la que pertenece la propuesta",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "protocol",
                        "preference",
                        "context",
                        "decision",
                        "open_question",
                    ],
                    "description": "Tipo de memoria institucional propuesta",
                },
                "content": {
                    "type": "string",
                    "description": "Resumen breve, verificable y redactado como dato interno",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": ["normal", "personal", "sensitive", "legal"],
                    "description": "Nivel de sensibilidad estimado",
                },
            },
            "required": ["organization_id", "category", "content"],
        },
    },
]


@dataclass
class ToolResult:
    content: str
    ok: bool


@dataclass(frozen=True)
class ToolContext:
    conversation_id: int | None = None
    user_message_id: int | None = None


def execute_tool(
    db: Session,
    current_user: User,
    name: str,
    tool_input: dict,
    context: ToolContext | None = None,
) -> ToolResult:
    executor = _EXECUTORS.get(name)
    if executor is None:
        return ToolResult(content=f"Herramienta desconocida: {name}", ok=False)

    try:
        result = executor(db, current_user, tool_input, context or ToolContext())
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


def _list_organizations(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> list:
    organizations = db.scalars(
        get_accessible_organizations_query(current_user)
    ).all()
    return [
        {"id": organization.id, "name": organization.name, "status": organization.status}
        for organization in organizations
    ]


def _list_projects(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> list:
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


def _list_requirements(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> list:
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


def _get_requirement(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    requirement = get_existing_requirement(db, int(tool_input["requirement_id"]))
    require_requirement_view(db, current_user, requirement)
    return _serialize_requirement(requirement, full=True)


def _web_search(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    if not has_permission(current_user, "assistant.web.search", db):
        raise HTTPException(
            status_code=403,
            detail="Permission required: assistant.web.search",
        )

    query = str(tool_input["query"]).strip()
    if not query:
        raise ValueError("query no puede estar vacío")
    if len(query) > MAX_WEB_QUERY_CHARS:
        raise ValueError(f"query no puede superar {MAX_WEB_QUERY_CHARS} caracteres")
    if PERSONAL_DATA_PATTERN.search(query):
        raise ValueError(
            "query no puede contener datos personales identificables"
        )

    limit = int(tool_input.get("limit") or MAX_WEB_RESULTS)
    if limit < 1:
        raise ValueError("limit debe ser mayor o igual que 1")
    limit = min(limit, MAX_WEB_RESULTS)

    try:
        results = hermes_web_client.search(query=query, limit=limit)
    except HermesWebUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    return {
        "query": query,
        "limit": limit,
        "results": results,
    }


def _create_requirement(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
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


def _update_requirement(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
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
    context: ToolContext,
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


def _propose_memory_entry(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    organization_id = int(tool_input["organization_id"])
    ensure_organization_exists(db, organization_id)
    if not has_permission(
        current_user,
        "assistant.memory.propose",
        db,
        organization_id=organization_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Permission required: assistant.memory.propose",
        )

    category = str(tool_input["category"]).strip()
    if category not in VALID_MEMORY_CATEGORIES:
        raise ValueError(f"category inválida: {category}")

    content = str(tool_input["content"]).strip()
    if not content:
        raise ValueError("content no puede estar vacío")
    if len(content) > 1000:
        raise ValueError("content no puede superar 1000 caracteres")

    sensitivity = str(tool_input.get("sensitivity") or "normal").strip()
    if sensitivity not in VALID_MEMORY_SENSITIVITIES:
        raise ValueError(f"sensitivity inválida: {sensitivity}")
    if sensitivity == "normal" and PERSONAL_DATA_PATTERN.search(content):
        sensitivity = "personal"

    entry = AssistantMemoryEntry(
        organization_id=organization_id,
        category=category,
        content=content,
        sensitivity=sensitivity,
        status="proposed",
        source_conversation_id=context.conversation_id,
        source_message_id=context.user_message_id,
        proposed_by_id=current_user.id,
    )
    db.add(entry)
    db.commit()
    return {
        "id": entry.id,
        "organization_id": entry.organization_id,
        "category": entry.category,
        "status": entry.status,
        "sensitivity": entry.sensitivity,
    }


_EXECUTORS = {
    "list_organizations": _list_organizations,
    "list_projects": _list_projects,
    "web_search": _web_search,
    "list_requirements": _list_requirements,
    "get_requirement": _get_requirement,
    "create_requirement": _create_requirement,
    "update_requirement": _update_requirement,
    "add_requirement_message": _add_requirement_message,
    "propose_memory_entry": _propose_memory_entry,
}
