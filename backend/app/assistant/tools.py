"""Tools the intake agent can execute on behalf of the current user.

Every executor runs with the calling user's RBAC permissions by reusing the
same validation helpers as the REST routes. The agent can only do what the
user could do through the API. Human-supervision principle: the agent creates
requirements as drafts (or moves them to 'submitted'); review states stay
human-only.
"""
import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.assistant.hermes_web import HermesWebUnavailableError, hermes_web_client
from app.assistant.models import (
    AssistantAdminFeedback,
    AssistantKnowledgeProposal,
    AssistantMemoryEntry,
    AssistantTransversalFeature,
    AssistantTransversalFeatureAdoption,
)
from app.documents.models import Document, DocumentWorkArtifact
from app.geo.access import (
    get_visible_entity,
    has_any_map_view_permission,
    has_map_view_permission,
)
from app.geo.models import EntityLocation, GeoLocation
from app.municipalities.models import Municipality
from app.organizations.access import (
    get_accessible_organizations_query,
    get_user_organization_ids,
)
from app.ordinances.embeddings import embed_text, vector_similarity
from app.ordinances.models import Ordinance, OrdinanceLegalChunk
from app.projects.access import select_visible_projects, user_can_access_project
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

ToolExecutor = Callable[..., object]
logger = logging.getLogger(__name__)

VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
VALID_MEMORY_CATEGORIES = {
    "protocol",
    "preference",
    "context",
    "decision",
    "open_question",
}
VALID_MEMORY_SENSITIVITIES = {"normal", "personal", "sensitive", "legal"}
VALID_KNOWLEDGE_SOURCE_TYPES = {
    "official",
    "public_administration",
    "news",
    "provider",
    "blog",
    "unknown",
}
VALID_KNOWLEDGE_CONFIDENCES = {"low", "medium", "high"}
VALID_TRANSVERSAL_FEATURE_CATEGORIES = {
    "process",
    "compliance",
    "automation",
    "documents",
    "citizen_service",
    "other",
}
VALID_ADMIN_FEEDBACK_CATEGORIES = {
    "bug",
    "improvement",
    "missing_capability",
    "data_issue",
    "ux",
    "other",
}
VALID_DOCUMENT_WORK_ARTIFACT_TYPES = {
    "report",
    "note",
    "comparison",
    "communication",
    "checklist",
}
MAX_WEB_QUERY_CHARS = 400
MAX_WEB_RESULTS = 5
MAX_WEB_RESULT_TITLE_CHARS = 300
MAX_WEB_RESULT_URL_CHARS = 2000
MAX_WEB_RESULT_SNIPPET_CHARS = 1000
MAX_WEB_RESULT_PUBLISHED_AT_CHARS = 100
MAX_ORDINANCE_QUERY_CHARS = 400
MAX_ORDINANCE_RESULTS = 5
MAX_TRANSVERSAL_TITLE_CHARS = 255
MAX_TRANSVERSAL_TEXT_CHARS = 2000
MAX_ADMIN_FEEDBACK_DESCRIPTION_CHARS = 4000
MAX_DOCUMENT_WORK_TITLE_CHARS = 255
MAX_DOCUMENT_WORK_CONTENT_CHARS = 20000
MAX_DOCUMENT_WORK_SOURCE_SUMMARY_CHARS = 4000
PERSONAL_DATA_PATTERN = re.compile(
    r"(\b\d{8}[A-Za-z]\b|\b[XYZ]\d{7}[A-Za-z]\b|[\w.+-]+@[\w-]+\.[\w.-]+|\b(?:\+34\s?)?[6789]\d{8}\b)",
    re.IGNORECASE,
)
PRIVATE_WEB_QUERY_MARKERS = {
    "api key",
    "archivo adjunto",
    "base de datos interna",
    "clave privada",
    "contrasena",
    "contraseña",
    "conversacion completa",
    "conversación completa",
    "correo privado",
    "credencial",
    "credenciales",
    "datos internos",
    "datos personales",
    "documento adjunto",
    "documento interno",
    "documento privado",
    "documentos internos",
    "documentos privados",
    "expediente interno",
    "historial interno",
    "historial municipal",
    "secreto",
    "secretos",
    "token",
    "todo el historial",
}
AGENT_OFFICE_DEFAULT_ACTION_BY_DEPARTMENT = {
    "front_desk": "triage",
    "requirements": "list_requirements",
    "ordinances": "semantic_search_ordinances",
    "documents": "prepare_document_work",
    "projects": "list_projects",
    "map": "get_map_items",
    "admin_feedback": "send_admin_feedback",
    "daily_briefing": "daily_briefing",
}
AGENT_OFFICE_SIMPLE_READ_ACTIONS = frozenset(
    {
        "list_organizations",
        "list_projects",
        "list_requirements",
        "get_requirement",
        "get_map_items",
        "semantic_search_ordinances",
    }
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

_TOOL_DEFINITIONS: list[dict] = [
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
        "name": "get_map_items",
        "description": (
            "Consulta ubicaciones visibles del mapa municipal para necesidades "
            "o proyectos. Úsala cuando el usuario pida ver algo en el mapa, "
            "pregunte dónde está un proyecto/necesidad o necesites preparar un "
            "enlace al mapa centrado en una ubicación. Devuelve coordenadas y "
            "una map_url interna para abrir /mapa con foco y zoom."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["requirement", "project"],
                    "description": "Tipo de entidad a buscar (opcional)",
                },
                "entity_id": {
                    "type": "integer",
                    "description": "ID concreto de la necesidad o proyecto (opcional)",
                },
                "organization_id": {
                    "type": "integer",
                    "description": "Filtrar por organización (opcional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número de ubicaciones, máximo 10",
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
        "name": "propose_knowledge_entry",
        "description": (
            "Propone una fuente o hallazgo externo para revisión humana. "
            "No lo convierte en conocimiento aprobado: deja una propuesta "
            "revisable con URL, resumen, confianza y sensibilidad."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": "Organización a la que pertenece la propuesta",
                },
                "title": {"type": "string", "description": "Título breve"},
                "summary": {"type": "string", "description": "Resumen verificable"},
                "content": {
                    "type": "string",
                    "description": "Contenido adicional o notas de revisión",
                },
                "source_url": {"type": "string", "description": "URL de la fuente"},
                "source_title": {"type": "string", "description": "Título de la fuente"},
                "source_type": {
                    "type": "string",
                    "enum": [
                        "official",
                        "public_administration",
                        "news",
                        "provider",
                        "blog",
                        "unknown",
                    ],
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "sensitivity": {
                    "type": "string",
                    "enum": ["normal", "personal", "sensitive", "legal"],
                },
                "requires_legal_review": {"type": "boolean"},
            },
            "required": ["organization_id", "title", "summary", "source_url"],
        },
    },
    {
        "name": "prepare_document_work",
        "description": (
            "Prepara un artefacto documental administrativo como borrador "
            "revisable: informe, nota, comparativa, comunicación o checklist. "
            "Nunca aprueba ni exporta el documento; solo crea un draft interno "
            "pendiente de revisión humana. No envía documentos privados a la web."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": "Organización del proyecto",
                },
                "project_id": {
                    "type": "integer",
                    "description": "Proyecto o expediente donde queda el borrador",
                },
                "artifact_type": {
                    "type": "string",
                    "enum": ["report", "note", "comparison", "communication", "checklist"],
                },
                "title": {"type": "string", "description": "Título del borrador"},
                "content": {
                    "type": "string",
                    "description": "Contenido del borrador revisable",
                },
                "source_summary": {
                    "type": "string",
                    "description": "Resumen de fuentes internas usadas, sin enviar documentos a web",
                },
                "source_document_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Documentos internos del mismo proyecto usados como fuente",
                },
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "create_agent_office_task",
        "description": (
            "Crea una tarea supervisada en la oficina interna de Anacleto para "
            "trabajo diferido, multi-paso o que requiera aprobación humana. "
            "Úsala cuando el usuario pida encargar, preparar o dejar para "
            "revisión un trabajo que no deba ejecutarse como una consulta inmediata. "
            "No la uses para lecturas simples de un solo turno: en esos casos "
            "usa la herramienta de lectura directa correspondiente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": "Organización donde se crea la tarea supervisada",
                },
                "title": {"type": "string", "description": "Título breve de la tarea"},
                "description": {
                    "type": "string",
                    "description": "Descripción accionable del trabajo a preparar",
                },
                "department": {
                    "type": "string",
                    "enum": [
                        "front_desk",
                        "requirements",
                        "ordinances",
                        "documents",
                        "projects",
                        "map",
                        "admin_feedback",
                        "daily_briefing",
                    ],
                    "description": "Capacidad interna sugerida; si falta se usa triage",
                },
                "requested_action": {
                    "type": "string",
                    "description": "Acción backend solicitada, por ejemplo triage o list_requirements",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "description": "Prioridad de la tarea",
                },
                "approval_policy": {
                    "type": "string",
                    "enum": ["never", "before_execution", "after_draft", "always"],
                    "description": "Política de aprobación humana",
                },
                "requires_human_approval": {
                    "type": "boolean",
                    "description": "Si la tarea debe quedar pendiente de aprobación humana",
                },
                "input": {
                    "type": "object",
                    "description": "Payload estructurado para la futura ejecución",
                },
                "due_at": {"type": "string", "description": "Fecha límite ISO opcional"},
                "scheduled_for": {
                    "type": "string",
                    "description": "Fecha programada ISO opcional",
                },
            },
            "required": ["organization_id", "title", "description"],
        },
    },
    {
        "name": "send_admin_feedback",
        "description": (
            "Envía al administrador feedback explícito del usuario sobre la "
            "plataforma o el asistente: errores, fricciones, capacidades que "
            "faltan, problemas de datos o mejoras de UX. Úsala solo después de "
            "que el usuario acepte enviarlo; antes puedes sugerirlo en lenguaje "
            "natural. Resume el problema sin datos personales innecesarios."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "bug",
                        "improvement",
                        "missing_capability",
                        "data_issue",
                        "ux",
                        "other",
                    ],
                    "description": "Tipo de feedback",
                },
                "title": {
                    "type": "string",
                    "description": "Título breve para el administrador",
                },
                "description": {
                    "type": "string",
                    "description": "Descripción accionable del feedback",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "description": "Prioridad estimada",
                },
                "organization_id": {
                    "type": "integer",
                    "description": "Organización relacionada, si procede",
                },
            },
            "required": ["category", "title", "description"],
        },
    },
    {
        "name": "semantic_search_ordinances",
        "description": (
            "Busca en las ordenanzas municipales ya importadas, revisadas y "
            "vectorizadas en la base de datos interna. Úsala para responder "
            "preguntas sobre normativa municipal, obligaciones, tasas, residuos, "
            "agua, convivencia u otras materias reguladas. Devuelve fragmentos "
            "citables con municipio, ordenanza y URL oficial; si no hay resultados, "
            "no inventes normativa y explica la falta de cobertura."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Pregunta o texto breve a buscar en el corpus interno de ordenanzas",
                },
                "municipality_id": {
                    "type": "integer",
                    "description": "Filtrar por municipio si el usuario lo ha indicado",
                },
                "municipality_name": {
                    "type": "string",
                    "description": "Nombre del municipio cuando el usuario lo indique y no se conozca su ID",
                },
                "topic": {
                    "type": "string",
                    "description": "Materia o tema regulado a filtrar, por ejemplo agua, residuos, terrazas o animales",
                },
                "include_pending": {
                    "type": "boolean",
                    "description": "Incluir ordenanzas pendientes de revisión; por defecto false",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número de fragmentos, máximo 5",
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
    {
        "name": "propose_transversal_feature",
        "description": (
            "Propone una funcionalidad transversal nacida de un requisito visible "
            "para el usuario. La propuesta queda pendiente de revisión por el "
            "equipo plataforma. Usa solo un resumen anonimizado: no incluyas "
            "nombres, datos personales, documentos originales ni detalles locales "
            "innecesarios."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_requirement_id": {
                    "type": "integer",
                    "description": "ID del requisito fuente visible para el usuario",
                },
                "title": {
                    "type": "string",
                    "description": "Nombre corto de la funcionalidad transversal",
                },
                "summary": {
                    "type": "string",
                    "description": "Resumen anonimizado de la funcionalidad",
                },
                "rationale": {
                    "type": "string",
                    "description": "Por qué puede ser útil para otros ayuntamientos",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "process",
                        "compliance",
                        "automation",
                        "documents",
                        "citizen_service",
                        "other",
                    ],
                    "description": "Tipo de funcionalidad transversal",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": ["normal", "personal", "sensitive", "legal"],
                    "description": "Nivel de sensibilidad estimado",
                },
            },
            "required": [
                "source_requirement_id",
                "title",
                "summary",
                "rationale",
                "category",
            ],
        },
    },
    {
        "name": "list_available_transversal_features",
        "description": (
            "Lista funcionalidades transversales disponibles para sugerir a una "
            "organización. Devuelve solo resúmenes aprobados y anonimizados; no "
            "incluye el ayuntamiento ni el requisito de origen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "organization_id": {
                    "type": "integer",
                    "description": (
                        "Organización para la que se quiere comprobar el estado "
                        "de adopción (opcional)"
                    ),
                },
            },
        },
    },
    {
        "name": "record_transversal_feature_acceptance",
        "description": (
            "Registra que una organización ha dado un OK explícito para aplicar "
            "una funcionalidad transversal disponible. Si la funcionalidad es "
            "autoactivable queda activa; si no, queda pendiente de activación "
            "humana."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feature_id": {
                    "type": "integer",
                    "description": "ID de la funcionalidad transversal disponible",
                },
                "organization_id": {
                    "type": "integer",
                    "description": "Organización que da el OK explícito",
                },
                "notes": {
                    "type": "string",
                    "description": "Nota breve opcional sobre el OK recibido",
                },
            },
            "required": ["feature_id", "organization_id"],
        },
    },
]


@dataclass
class ToolResult:
    content: str
    ok: bool


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    description: str
    input_schema: dict
    executor: ToolExecutor
    read_only: bool
    domain: str
    risk_level: str
    requires_confirmation: bool
    requires_review: bool
    input_schema_summary: str
    output_summary_shape: str
    user_visible_summary_template: str
    required_permission: str | None = None

    @property
    def definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @property
    def metadata(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "read_only": self.read_only,
            "domain": self.domain,
            "required_permission": self.required_permission,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "requires_review": self.requires_review,
            "input_schema_summary": self.input_schema_summary,
            "output_summary_shape": self.output_summary_shape,
            "user_visible_summary_template": self.user_visible_summary_template,
        }


@dataclass(frozen=True)
class ToolContext:
    conversation_id: int | None = None
    user_message_id: int | None = None


def _log_tool_execution(
    *,
    name: str,
    ok: bool,
    tool_input: dict,
    context: ToolContext | None,
    current_user: User,
    spec: ToolSpec | None = None,
) -> None:
    """Emit structured operational telemetry without payload values."""
    safe_context = context or ToolContext()
    logger.info(
        "assistant tool execution",
        extra={
            "assistant_event": "tool_execution",
            "assistant_tool": name,
            "assistant_tool_ok": ok,
            "assistant_tool_domain": spec.domain if spec is not None else None,
            "assistant_tool_read_only": spec.read_only if spec is not None else None,
            "assistant_input_keys": sorted(str(key) for key in tool_input.keys()),
            "assistant_actor_user_id": current_user.id,
            "assistant_conversation_id": safe_context.conversation_id,
            "assistant_user_message_id": safe_context.user_message_id,
        },
    )


def execute_tool(
    db: Session,
    current_user: User,
    name: str,
    tool_input: dict,
    context: ToolContext | None = None,
    allowed: frozenset[str] | None = None,
) -> ToolResult:
    if allowed is not None and name not in allowed:
        result = ToolResult(
            content=f"Herramienta no disponible para este agente: {name}",
            ok=False,
        )
        _log_tool_execution(
            name=name,
            ok=result.ok,
            tool_input=tool_input,
            context=context,
            current_user=current_user,
        )
        return result

    spec = TOOL_CATALOG.get(name)
    if spec is None:
        result = ToolResult(content=f"Herramienta desconocida: {name}", ok=False)
        _log_tool_execution(
            name=name,
            ok=result.ok,
            tool_input=tool_input,
            context=context,
            current_user=current_user,
        )
        return result

    try:
        result = spec.executor(db, current_user, tool_input, context or ToolContext())
    except HTTPException as error:
        db.rollback()
        result = ToolResult(
            content=f"Error ({error.status_code}): {error.detail}",
            ok=False,
        )
        _log_tool_execution(
            name=name,
            ok=result.ok,
            tool_input=tool_input,
            context=context,
            current_user=current_user,
            spec=spec,
        )
        return result
    except (KeyError, TypeError, ValueError) as error:
        db.rollback()
        result = ToolResult(content=f"Entrada inválida: {error}", ok=False)
        _log_tool_execution(
            name=name,
            ok=result.ok,
            tool_input=tool_input,
            context=context,
            current_user=current_user,
            spec=spec,
        )
        return result
    except SQLAlchemyError:
        db.rollback()
        result = ToolResult(
            content="Error de base de datos al ejecutar la herramienta",
            ok=False,
        )
        _log_tool_execution(
            name=name,
            ok=result.ok,
            tool_input=tool_input,
            context=context,
            current_user=current_user,
            spec=spec,
        )
        return result

    tool_result = ToolResult(content=json.dumps(result, ensure_ascii=False), ok=True)
    _log_tool_execution(
        name=name,
        ok=tool_result.ok,
        tool_input=tool_input,
        context=context,
        current_user=current_user,
        spec=spec,
    )
    return tool_result


def normalize_public_web_query_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    )


def web_search_privacy_violation(query: str) -> str | None:
    """Return the deterministic reason a query must not leave the backend."""
    if PERSONAL_DATA_PATTERN.search(query):
        return "query no puede contener datos personales identificables"
    normalized = normalize_public_web_query_text(query)
    if any(
        normalize_public_web_query_text(marker) in normalized
        for marker in PRIVATE_WEB_QUERY_MARKERS
    ):
        return (
            "query no puede contener datos internos, documentos privados, "
            "historial municipal ni secretos"
        )
    return None


def _clean_optional_web_text(value: object, *, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_chars]


def _sanitize_web_search_results(
    raw_results: object,
    *,
    limit: int,
) -> list[dict[str, str | None]]:
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, str | None]] = []
    for raw in raw_results[:limit]:
        if not isinstance(raw, dict):
            continue
        url = _clean_optional_web_text(
            raw.get("url"),
            max_chars=MAX_WEB_RESULT_URL_CHARS,
        )
        if not url:
            continue
        title = _clean_optional_web_text(
            raw.get("title"),
            max_chars=MAX_WEB_RESULT_TITLE_CHARS,
        )
        snippet = _clean_optional_web_text(
            raw.get("snippet"),
            max_chars=MAX_WEB_RESULT_SNIPPET_CHARS,
        )
        published_at = _clean_optional_web_text(
            raw.get("published_at"),
            max_chars=MAX_WEB_RESULT_PUBLISHED_AT_CHARS,
        )
        results.append(
            {
                "title": title or url,
                "url": url,
                "snippet": snippet,
                "published_at": published_at,
            }
        )
    return results


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


def _build_map_url(
    *,
    entity_type: str,
    entity_id: int,
    latitude: float | None,
    longitude: float | None,
    label: str,
) -> str:
    params = [
        f"entity_type={entity_type}",
        f"entity_id={entity_id}",
        "zoom=17",
    ]
    if latitude is not None and longitude is not None:
        params.extend(
            [
                f"lat={latitude}",
                f"lng={longitude}",
                f"label={quote(label)}",
            ]
        )
    return "/mapa?" + "&".join(params)


def _get_map_items(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    if not has_any_map_view_permission(db, current_user):
        raise HTTPException(status_code=403, detail="Permission required: map.view")

    limit = int(tool_input.get("limit") or 5)
    if limit < 1:
        raise ValueError("limit debe ser mayor o igual que 1")
    limit = min(limit, 10)

    entity_type = tool_input.get("entity_type")
    if entity_type is not None and entity_type not in {"requirement", "project"}:
        raise ValueError("entity_type debe ser 'requirement' o 'project'")
    entity_id = tool_input.get("entity_id")
    organization_id = tool_input.get("organization_id")

    query = (
        select(EntityLocation)
        .join(EntityLocation.location)
        .where(GeoLocation.review_status != "rejected")
        .order_by(EntityLocation.id.desc())
        .limit(100)
    )
    if entity_type is not None:
        query = query.where(EntityLocation.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(EntityLocation.entity_id == int(entity_id))
    if organization_id is not None:
        query = query.where(GeoLocation.organization_id == int(organization_id))

    results: list[dict] = []
    for attachment in db.scalars(query):
        visible = get_visible_entity(
            db,
            current_user,
            attachment.entity_type,
            attachment.entity_id,
        )
        if visible is None:
            continue
        if organization_id is not None and visible.organization_id != int(organization_id):
            continue
        if not has_map_view_permission(db, current_user, visible.organization_id):
            continue
        location = attachment.location
        results.append(
            {
                "entity_type": visible.entity_type,
                "entity_id": attachment.entity_id,
                "title": visible.title,
                "subtitle": visible.subtitle,
                "status": visible.status,
                "organization_id": visible.organization_id,
                "organization_name": visible.organization_name,
                "detail_path": visible.detail_path,
                "location": {
                    "id": location.id,
                    "label": location.label,
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "review_status": location.review_status,
                },
                "map_url": _build_map_url(
                    entity_type=visible.entity_type,
                    entity_id=attachment.entity_id,
                    latitude=location.latitude,
                    longitude=location.longitude,
                    label=location.label,
                ),
            }
        )
        if len(results) >= limit:
            break

    return {"limit": limit, "results": results}


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
    privacy_error = web_search_privacy_violation(query)
    if privacy_error is not None:
        raise ValueError(privacy_error)

    limit = int(tool_input.get("limit") or MAX_WEB_RESULTS)
    if limit < 1:
        raise ValueError("limit debe ser mayor o igual que 1")
    limit = min(limit, MAX_WEB_RESULTS)

    try:
        raw_results = hermes_web_client.search(query=query, limit=limit)
    except HermesWebUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    return {
        "query": query,
        "limit": limit,
        "results": _sanitize_web_search_results(raw_results, limit=limit),
    }


def _semantic_search_ordinances(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    if not has_permission(current_user, "ordinances.compare", db):
        raise HTTPException(
            status_code=403,
            detail="Permission required: ordinances.compare",
        )

    query_text = str(tool_input["query"]).strip()
    if not query_text:
        raise ValueError("query no puede estar vacío")
    if len(query_text) > MAX_ORDINANCE_QUERY_CHARS:
        raise ValueError(
            f"query no puede superar {MAX_ORDINANCE_QUERY_CHARS} caracteres"
        )

    limit = int(tool_input.get("limit") or MAX_ORDINANCE_RESULTS)
    if limit < 1:
        raise ValueError("limit debe ser mayor o igual que 1")
    limit = min(limit, MAX_ORDINANCE_RESULTS)

    query_vector, _, embedding_status = embed_text(query_text)
    if embedding_status != "ready" or query_vector is None:
        return {"query": query_text, "limit": limit, "results": []}

    include_pending = bool(tool_input.get("include_pending") or False)
    query = (
        select(OrdinanceLegalChunk)
        .join(OrdinanceLegalChunk.ordinance)
        .join(Ordinance.municipality)
        .where(OrdinanceLegalChunk.embedding_status == "ready")
        .options(
            selectinload(OrdinanceLegalChunk.ordinance).selectinload(
                Ordinance.municipality
            )
        )
    )
    municipality_id = tool_input.get("municipality_id")
    if municipality_id is not None:
        query = query.where(Ordinance.municipality_id == int(municipality_id))
    municipality_name = str(tool_input.get("municipality_name") or "").strip()
    if municipality_name:
        query = query.where(Municipality.name.ilike(municipality_name))
    topic = str(tool_input.get("topic") or "").strip()
    if topic:
        topic_pattern = f"%{topic}%"
        query = query.where(
            (Ordinance.topic.ilike(topic_pattern))
            | (Ordinance.subtopic.ilike(topic_pattern))
            | (Ordinance.title.ilike(topic_pattern))
        )
    if include_pending:
        query = query.where(Ordinance.curation_status != "rejected")
    else:
        query = query.where(
            Ordinance.curation_status == "approved",
            OrdinanceLegalChunk.review_status == "approved",
        )

    scored: list[tuple[float, OrdinanceLegalChunk]] = []
    structured_filter_applied = municipality_id is not None or bool(
        municipality_name or topic
    )
    for chunk in db.scalars(query.limit(500)):
        score = vector_similarity(query_vector, chunk.embedding)
        if score <= 0 and not structured_filter_applied:
            continue
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)

    return {
        "query": query_text,
        "municipality_name": municipality_name or None,
        "topic": topic or None,
        "limit": limit,
        "results": [
            {
                "chunk_id": chunk.id,
                "ordinance_id": chunk.ordinance_id,
                "title": chunk.ordinance.title,
                "municipality_id": chunk.ordinance.municipality_id,
                "municipality_name": chunk.ordinance.municipality.name,
                "topic": chunk.ordinance.topic,
                "curation_status": chunk.ordinance.curation_status,
                "citation": chunk.citation,
                "text": chunk.text,
                "source_url": chunk.source_url or chunk.ordinance.source_url,
                "score": round(score, 4),
            }
            for score, chunk in scored[:limit]
        ],
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


def _propose_knowledge_entry(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    """Create a reviewable external-source proposal.

    Knowledge proposals are separate from AssistantMemoryEntry so proposed
    sources are never injected into future prompts until a reviewer approves
    them.
    """
    organization_id = int(tool_input["organization_id"])
    ensure_organization_exists(db, organization_id)
    if not has_permission(
        current_user,
        "assistant.knowledge.propose",
        db,
        organization_id=organization_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Permission required: assistant.knowledge.propose",
        )

    title = str(tool_input["title"]).strip()
    summary = str(tool_input["summary"]).strip()
    source_url = str(tool_input["source_url"]).strip()
    if not title:
        raise ValueError("title no puede estar vacío")
    if not summary:
        raise ValueError("summary no puede estar vacío")
    if not source_url:
        raise ValueError("source_url no puede estar vacío")

    source_title = str(tool_input.get("source_title") or title).strip()
    source_type = str(tool_input.get("source_type") or "unknown").strip()
    confidence = str(tool_input.get("confidence") or "medium").strip()
    sensitivity = str(tool_input.get("sensitivity") or "normal").strip()
    if source_type not in VALID_KNOWLEDGE_SOURCE_TYPES:
        raise ValueError(f"source_type inválido: {source_type}")
    if confidence not in VALID_KNOWLEDGE_CONFIDENCES:
        raise ValueError(f"confidence inválida: {confidence}")
    if sensitivity not in VALID_MEMORY_SENSITIVITIES:
        raise ValueError(f"sensitivity inválida: {sensitivity}")

    extra_content = str(tool_input.get("content") or "").strip()
    sensitivity_probe = "\n".join(
        part for part in (title, summary, extra_content, source_url, source_title) if part
    )
    if sensitivity == "normal" and PERSONAL_DATA_PATTERN.search(sensitivity_probe):
        sensitivity = "personal"

    proposal = AssistantKnowledgeProposal(
        organization_id=organization_id,
        title=title,
        summary=summary,
        content=extra_content or None,
        source_url=source_url,
        source_title=source_title or None,
        source_type=source_type,
        confidence=confidence,
        sensitivity=sensitivity,
        status="proposed",
        requires_legal_review=bool(tool_input.get("requires_legal_review") or False),
        source_conversation_id=context.conversation_id,
        source_message_id=context.user_message_id,
        proposed_by_id=current_user.id,
    )
    db.add(proposal)
    db.commit()
    return {
        "id": proposal.id,
        "organization_id": proposal.organization_id,
        "status": proposal.status,
        "title": proposal.title,
        "summary": proposal.summary,
        "source_url": proposal.source_url,
        "source_title": proposal.source_title,
        "source_type": proposal.source_type,
        "confidence": proposal.confidence,
        "sensitivity": proposal.sensitivity,
        "requires_legal_review": proposal.requires_legal_review,
    }



def _send_admin_feedback(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    organization_id = tool_input.get("organization_id")
    if organization_id is not None:
        organization_id = int(organization_id)
        ensure_organization_exists(db, organization_id)
        if not has_permission(
            current_user,
            "assistant.use",
            db,
            organization_id=organization_id,
        ):
            raise HTTPException(
                status_code=403,
                detail="Permission required: assistant.use",
            )

    category = str(tool_input["category"]).strip()
    if category not in VALID_ADMIN_FEEDBACK_CATEGORIES:
        raise ValueError(f"category inválida: {category}")

    title = _clean_transversal_text("title", tool_input["title"], 255)
    description = str(tool_input["description"]).strip()
    if not description:
        raise ValueError("description no puede estar vacío")
    if len(description) > MAX_ADMIN_FEEDBACK_DESCRIPTION_CHARS:
        raise ValueError(
            "description no puede superar "
            f"{MAX_ADMIN_FEEDBACK_DESCRIPTION_CHARS} caracteres"
        )

    priority = str(tool_input.get("priority") or "medium").strip()
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"priority inválida: {priority}")

    feedback = AssistantAdminFeedback(
        organization_id=organization_id,
        category=category,
        title=title,
        description=description,
        priority=priority,
        status="submitted",
        source_conversation_id=context.conversation_id,
        source_message_id=context.user_message_id,
        submitted_by_id=current_user.id,
    )
    db.add(feedback)
    db.commit()
    return {
        "id": feedback.id,
        "status": feedback.status,
        "category": feedback.category,
        "priority": feedback.priority,
        "organization_id": feedback.organization_id,
    }


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def agent_office_effective_requested_action(tool_input: dict) -> str | None:
    requested_action = str(tool_input.get("requested_action") or "").strip()
    if requested_action:
        return requested_action
    department = str(tool_input.get("department") or "").strip()
    if not department:
        return None
    return AGENT_OFFICE_DEFAULT_ACTION_BY_DEPARTMENT.get(department)


def _create_agent_office_task(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    from app.agent_office.service import create_task

    effective_action = agent_office_effective_requested_action(tool_input)
    if effective_action in AGENT_OFFICE_SIMPLE_READ_ACTIONS:
        raise ValueError(
            "create_agent_office_task no debe usarse para lecturas simples; "
            "usa la herramienta de lectura directa correspondiente"
        )

    input_payload = tool_input.get("input")
    if input_payload is not None and not isinstance(input_payload, dict):
        raise ValueError("input debe ser un objeto")

    task = create_task(
        db,
        current_user,
        organization_id=int(tool_input["organization_id"]),
        title=str(tool_input["title"]).strip(),
        description=str(tool_input["description"]).strip(),
        department=tool_input.get("department"),
        requested_action=tool_input.get("requested_action"),
        priority=str(tool_input.get("priority") or "medium"),
        approval_policy=tool_input.get("approval_policy"),
        requires_human_approval=tool_input.get("requires_human_approval"),
        input_payload=input_payload,
        due_at=_parse_optional_datetime(tool_input.get("due_at")),
        scheduled_for=_parse_optional_datetime(tool_input.get("scheduled_for")),
        source_conversation_id=context.conversation_id,
        source_message_id=context.user_message_id,
    )

    return {
        "id": task.id,
        "organization_id": task.organization_id,
        "title": task.title,
        "department": task.department,
        "requested_action": task.requested_action,
        "status": task.status,
        "approval_policy": task.approval_policy,
        "requires_human_approval": task.requires_human_approval,
        "next_step": "human_approval" if task.requires_human_approval else "ready_to_run",
    }


def _clean_transversal_text(name: str, value: object, max_chars: int) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} no puede estar vacío")
    if len(text) > max_chars:
        raise ValueError(f"{name} no puede superar {max_chars} caracteres")
    if PERSONAL_DATA_PATTERN.search(text):
        raise ValueError(
            f"{name} no puede contener datos personales identificables"
        )
    return text


def _clean_optional_document_work_text(
    name: str,
    value: object,
    max_chars: int,
) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise ValueError(f"{name} no puede superar {max_chars} caracteres")
    return text


def _normalize_document_work_source_ids(value: object) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("source_document_ids debe ser una lista")
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_document_id in value:
        try:
            document_id = int(raw_document_id)
        except (TypeError, ValueError):
            raise ValueError("source_document_ids contiene un id inválido") from None
        if document_id <= 0:
            raise ValueError("source_document_ids contiene un id inválido")
        if document_id in seen:
            continue
        normalized.append(document_id)
        seen.add(document_id)
    return normalized


def _prepare_document_work(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    artifact_type = str(tool_input.get("artifact_type") or "note").strip()
    if artifact_type not in VALID_DOCUMENT_WORK_ARTIFACT_TYPES:
        raise ValueError(f"artifact_type inválido: {artifact_type}")
    project_id = int(tool_input["project_id"])
    project = db.scalar(
        select(Project)
        .options(selectinload(Project.organization))
        .where(Project.id == project_id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    organization_id = tool_input.get("organization_id")
    if organization_id is not None and int(organization_id) != project.organization_id:
        raise HTTPException(
            status_code=409,
            detail="Project organization mismatch",
        )
    if not has_permission(
        current_user,
        "documents.draft",
        db,
        organization_id=project.organization_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Permission required: documents.draft",
        )
    if not user_can_access_project(db, current_user, project):
        raise HTTPException(status_code=403, detail="Project access denied")

    source_document_ids = _normalize_document_work_source_ids(
        tool_input.get("source_document_ids")
    )
    if source_document_ids and not has_permission(
        current_user,
        "documents.view",
        db,
        organization_id=project.organization_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Permission required: documents.view",
        )
    for document_id in source_document_ids:
        document = db.scalar(
            select(Document).where(Document.id == document_id)
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Source document not found")
        if document.project_id != project.id or document.organization_id != project.organization_id:
            raise HTTPException(
                status_code=409,
                detail="Source document does not belong to this project",
            )
        if document.status != "active":
            raise HTTPException(status_code=409, detail="Source document is not active")

    artifact = DocumentWorkArtifact(
        organization_id=project.organization_id,
        project_id=project.id,
        artifact_type=artifact_type,
        title=_clean_transversal_text(
            "title",
            tool_input["title"],
            MAX_DOCUMENT_WORK_TITLE_CHARS,
        ),
        content=_clean_transversal_text(
            "content",
            tool_input["content"],
            MAX_DOCUMENT_WORK_CONTENT_CHARS,
        ),
        status="draft",
        source_summary=_clean_optional_document_work_text(
            "source_summary",
            tool_input.get("source_summary"),
            MAX_DOCUMENT_WORK_SOURCE_SUMMARY_CHARS,
        ),
        source_document_ids=source_document_ids,
        created_by_id=current_user.id,
        source_conversation_id=context.conversation_id,
        source_message_id=context.user_message_id,
    )
    db.add(artifact)
    db.commit()
    return {
        "id": artifact.id,
        "organization_id": artifact.organization_id,
        "project_id": artifact.project_id,
        "artifact_type": artifact.artifact_type,
        "title": artifact.title,
        "status": artifact.status,
        "review_status": "pending_human_review",
        "export_status": "not_exported",
        "approved": False,
        "exported_at": None,
    }


def _serialize_transversal_feature_for_tool(
    feature: AssistantTransversalFeature,
    *,
    adoption_status: str | None = None,
) -> dict:
    data = {
        "id": feature.id,
        "title": feature.title,
        "summary": feature.summary,
        "category": feature.category,
        "auto_activatable": feature.auto_activatable,
    }
    if adoption_status is not None:
        data["adoption_status"] = adoption_status
    return data


def _propose_transversal_feature(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    requirement = get_existing_requirement(db, int(tool_input["source_requirement_id"]))
    require_requirement_view(db, current_user, requirement)

    title = _clean_transversal_text(
        "title",
        tool_input["title"],
        MAX_TRANSVERSAL_TITLE_CHARS,
    )
    summary = _clean_transversal_text(
        "summary",
        tool_input["summary"],
        MAX_TRANSVERSAL_TEXT_CHARS,
    )
    rationale = _clean_transversal_text(
        "rationale",
        tool_input["rationale"],
        MAX_TRANSVERSAL_TEXT_CHARS,
    )

    category = str(tool_input["category"]).strip()
    if category not in VALID_TRANSVERSAL_FEATURE_CATEGORIES:
        raise ValueError(f"category inválida: {category}")

    sensitivity = str(tool_input.get("sensitivity") or "normal").strip()
    if sensitivity not in VALID_MEMORY_SENSITIVITIES:
        raise ValueError(f"sensitivity inválida: {sensitivity}")

    feature = AssistantTransversalFeature(
        source_requirement_id=requirement.id,
        source_organization_id=requirement.organization_id,
        source_conversation_id=context.conversation_id,
        source_message_id=context.user_message_id,
        title=title,
        summary=summary,
        rationale=rationale,
        category=category,
        sensitivity=sensitivity,
        status="proposed",
        proposed_by_id=current_user.id,
    )
    db.add(feature)
    db.commit()
    return {
        "id": feature.id,
        "status": feature.status,
        "source_requirement_id": feature.source_requirement_id,
        "source_organization_id": feature.source_organization_id,
        "sensitivity": feature.sensitivity,
    }


def _list_available_transversal_features(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> list:
    organization_id = tool_input.get("organization_id")
    adoption_statuses: dict[int, str] = {}
    if organization_id is not None:
        organization_id = int(organization_id)
        ensure_organization_exists(db, organization_id)
        if not has_permission(
            current_user,
            "assistant.use",
            db,
            organization_id=organization_id,
        ):
            raise HTTPException(
                status_code=403,
                detail="Permission required: assistant.use",
            )
        adoptions = db.scalars(
            select(AssistantTransversalFeatureAdoption).where(
                AssistantTransversalFeatureAdoption.organization_id == organization_id
            )
        ).all()
        adoption_statuses = {
            adoption.feature_id: adoption.status
            for adoption in adoptions
        }

    features = db.scalars(
        select(AssistantTransversalFeature)
        .where(AssistantTransversalFeature.status == "available")
        .order_by(
            AssistantTransversalFeature.updated_at.desc(),
            AssistantTransversalFeature.id.desc(),
        )
        .limit(20)
    ).all()
    return [
        _serialize_transversal_feature_for_tool(
            feature,
            adoption_status=adoption_statuses.get(feature.id),
        )
        for feature in features
    ]


def _record_transversal_feature_acceptance(
    db: Session,
    current_user: User,
    tool_input: dict,
    context: ToolContext,
) -> dict:
    feature = db.get(AssistantTransversalFeature, int(tool_input["feature_id"]))
    if feature is None:
        raise HTTPException(status_code=404, detail="Transversal feature not found")
    if feature.status != "available":
        raise HTTPException(
            status_code=409,
            detail="Transversal feature is not available",
        )

    organization_id = int(tool_input["organization_id"])
    ensure_organization_exists(db, organization_id)
    if not has_permission(
        current_user,
        "assistant.use",
        db,
        organization_id=organization_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Permission required: assistant.use",
        )

    notes = tool_input.get("notes")
    if notes is not None:
        notes = _clean_transversal_text("notes", notes, MAX_TRANSVERSAL_TEXT_CHARS)

    adoption = db.scalar(
        select(AssistantTransversalFeatureAdoption).where(
            AssistantTransversalFeatureAdoption.feature_id == feature.id,
            AssistantTransversalFeatureAdoption.organization_id == organization_id,
        )
    )
    new_status = "active" if feature.auto_activatable else "activation_pending"
    activated_at = datetime.now(timezone.utc) if new_status == "active" else None

    if adoption is None:
        adoption = AssistantTransversalFeatureAdoption(
            feature_id=feature.id,
            organization_id=organization_id,
        )
        db.add(adoption)

    adoption.status = new_status
    adoption.requested_by_id = current_user.id
    adoption.approved_by_id = current_user.id if new_status == "active" else None
    adoption.source_conversation_id = context.conversation_id
    adoption.source_message_id = context.user_message_id
    adoption.notes = notes
    adoption.activated_at = activated_at

    db.commit()
    return {
        "id": adoption.id,
        "feature_id": adoption.feature_id,
        "organization_id": adoption.organization_id,
        "status": adoption.status,
        "activated_at": adoption.activated_at.isoformat()
        if adoption.activated_at
        else None,
    }


_EXECUTORS = {
    "list_organizations": _list_organizations,
    "list_projects": _list_projects,
    "get_map_items": _get_map_items,
    "web_search": _web_search,
    "semantic_search_ordinances": _semantic_search_ordinances,
    "list_requirements": _list_requirements,
    "get_requirement": _get_requirement,
    "create_requirement": _create_requirement,
    "update_requirement": _update_requirement,
    "add_requirement_message": _add_requirement_message,
    "propose_memory_entry": _propose_memory_entry,
    "propose_knowledge_entry": _propose_knowledge_entry,
    "prepare_document_work": _prepare_document_work,
    "create_agent_office_task": _create_agent_office_task,
    "send_admin_feedback": _send_admin_feedback,
    "propose_transversal_feature": _propose_transversal_feature,
    "list_available_transversal_features": _list_available_transversal_features,
    "record_transversal_feature_acceptance": _record_transversal_feature_acceptance,
}

_TOOL_METADATA: dict[str, dict] = {
    "list_organizations": {
        "label": "Consultar organizaciones",
        "read_only": True,
        "domain": "organizations",
    },
    "list_projects": {
        "label": "Consultar proyectos",
        "read_only": True,
        "domain": "projects",
    },
    "get_map_items": {
        "label": "Consultar mapa",
        "read_only": True,
        "domain": "map",
        "required_permission": "map.view",
    },
    "web_search": {
        "label": "Buscar en web",
        "read_only": True,
        "domain": "web",
        "required_permission": "assistant.web.search",
    },
    "semantic_search_ordinances": {
        "label": "Buscar ordenanzas",
        "read_only": True,
        "domain": "ordinances",
        "required_permission": "ordinances.compare",
    },
    "list_requirements": {
        "label": "Consultar necesidades",
        "read_only": True,
        "domain": "requirements",
        "required_permission": "requirements.view",
    },
    "get_requirement": {
        "label": "Leer necesidad",
        "read_only": True,
        "domain": "requirements",
        "required_permission": "requirements.view",
    },
    "create_requirement": {
        "label": "Crear necesidad",
        "read_only": False,
        "domain": "requirements",
        "required_permission": "requirements.create",
    },
    "update_requirement": {
        "label": "Actualizar necesidad",
        "read_only": False,
        "domain": "requirements",
        "required_permission": "requirements.edit",
    },
    "add_requirement_message": {
        "label": "Añadir nota a necesidad",
        "read_only": False,
        "domain": "requirements",
        "required_permission": "requirements.view",
    },
    "propose_memory_entry": {
        "label": "Proponer memoria",
        "read_only": False,
        "domain": "memory",
        "required_permission": "assistant.memory.propose",
    },
    "propose_knowledge_entry": {
        "label": "Proponer fuente",
        "read_only": False,
        "domain": "knowledge",
        "required_permission": "assistant.knowledge.propose",
    },
    "prepare_document_work": {
        "label": "Preparar borrador documental",
        "read_only": False,
        "domain": "documents",
        "required_permission": "documents.draft",
    },
    "create_agent_office_task": {
        "label": "Crear tarea supervisada",
        "read_only": False,
        "domain": "agent_office",
        "required_permission": "agent_office.create",
    },
    "send_admin_feedback": {
        "label": "Enviar feedback al admin",
        "read_only": False,
        "domain": "feedback",
        "required_permission": "assistant.use",
    },
    "propose_transversal_feature": {
        "label": "Proponer funcionalidad transversal",
        "read_only": False,
        "domain": "transversal_features",
        "required_permission": "requirements.view",
    },
    "list_available_transversal_features": {
        "label": "Consultar funcionalidades disponibles",
        "read_only": True,
        "domain": "transversal_features",
        "required_permission": "assistant.use",
    },
    "record_transversal_feature_acceptance": {
        "label": "Registrar activación transversal",
        "read_only": False,
        "domain": "transversal_features",
        "required_permission": "assistant.use",
    },
}

_TOOL_CONTRACT_METADATA: dict[str, dict] = {
    "list_organizations": {
        "risk_level": "low",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Lista de organizaciones visibles con id, nombre y estado.",
        "user_visible_summary_template": "He consultado las organizaciones disponibles.",
    },
    "list_projects": {
        "risk_level": "low",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Lista de proyectos visibles con id, nombre, organización y estado.",
        "user_visible_summary_template": "He consultado los proyectos visibles.",
    },
    "get_map_items": {
        "risk_level": "low",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Resultados de mapa con entidad visible, ubicación y enlace interno map_url.",
        "user_visible_summary_template": "He localizado elementos visibles en el mapa municipal.",
    },
    "web_search": {
        "risk_level": "medium",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Consulta pública, límite y resultados con título, URL y extracto.",
        "user_visible_summary_template": "He buscado fuentes públicas en la web.",
    },
    "propose_knowledge_entry": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Propuesta revisable de fuente externa con estado, URL, confianza y sensibilidad.",
        "user_visible_summary_template": "He dejado una fuente como propuesta revisable pendiente de aprobación.",
    },
    "prepare_document_work": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Artefacto documental revisable con id, tipo, estado draft y estado de exportación no_exportado.",
        "user_visible_summary_template": "He preparado un borrador documental pendiente de revisión humana.",
    },
    "create_agent_office_task": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Tarea supervisada con id, estado, política de aprobación y siguiente paso.",
        "user_visible_summary_template": "He creado una tarea supervisada para revisión o ejecución controlada.",
    },
    "send_admin_feedback": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Feedback interno enviado con id, estado, categoría, prioridad y organización.",
        "user_visible_summary_template": "He enviado el feedback al administrador para su revisión.",
    },
    "semantic_search_ordinances": {
        "risk_level": "medium",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Fragmentos citables de ordenanzas aprobadas con municipio, fuente y puntuación.",
        "user_visible_summary_template": "He consultado el corpus interno de ordenanzas municipales.",
    },
    "list_requirements": {
        "risk_level": "low",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Lista resumida de necesidades visibles con id, título, estado, prioridad y resumen.",
        "user_visible_summary_template": "He consultado las necesidades registradas visibles.",
    },
    "get_requirement": {
        "risk_level": "low",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Detalle completo de una necesidad visible con campos de contenido y estado.",
        "user_visible_summary_template": "He leído el detalle de la necesidad indicada.",
    },
    "create_requirement": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Necesidad creada como borrador con campos estructurados y estado draft.",
        "user_visible_summary_template": "He creado un borrador de necesidad para revisión.",
    },
    "update_requirement": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Necesidad actualizada con campos estructurados y estado permitido.",
        "user_visible_summary_template": "He actualizado la necesidad indicada para que pueda revisarse.",
    },
    "add_requirement_message": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": False,
        "output_summary_shape": "Nota o aclaración añadida al hilo de una necesidad con id y tipo de mensaje.",
        "user_visible_summary_template": "He añadido una nota o aclaración a la necesidad.",
    },
    "propose_memory_entry": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Propuesta de memoria institucional con id, estado, categoría y sensibilidad.",
        "user_visible_summary_template": "He dejado una propuesta de memoria pendiente de revisión humana.",
    },
    "propose_transversal_feature": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Funcionalidad transversal propuesta con id, estado, origen y sensibilidad.",
        "user_visible_summary_template": "He propuesto una funcionalidad transversal para revisión.",
    },
    "list_available_transversal_features": {
        "risk_level": "low",
        "requires_confirmation": False,
        "requires_review": False,
        "output_summary_shape": "Funcionalidades transversales disponibles con resumen anonimizado y estado de adopción.",
        "user_visible_summary_template": "He consultado funcionalidades transversales disponibles.",
    },
    "record_transversal_feature_acceptance": {
        "risk_level": "medium",
        "requires_confirmation": True,
        "requires_review": True,
        "output_summary_shape": "Adopción de funcionalidad transversal con id, organización, estado y fecha de activación.",
        "user_visible_summary_template": "He registrado el OK para la funcionalidad transversal.",
    },
}

_VALID_TOOL_RISK_LEVELS = {"low", "medium", "high"}


def _summarize_input_schema(schema: dict) -> str:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return "Sin parámetros."

    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    required_fields = [name for name in properties if name in required]
    optional_fields = [name for name in properties if name not in required]
    parts: list[str] = []
    if required_fields:
        parts.append(f"Requiere: {', '.join(required_fields)}")
    else:
        parts.append("Sin campos obligatorios")
    if optional_fields:
        parts.append(f"Opcionales: {', '.join(optional_fields)}")
    return ". ".join(parts) + "."


def _validated_tool_contract(name: str, metadata: dict, contract: dict) -> dict:
    risk_level = str(contract["risk_level"]).strip()
    if risk_level not in _VALID_TOOL_RISK_LEVELS:
        raise RuntimeError(f"Invalid risk level for tool {name}: {risk_level}")
    requires_confirmation = bool(contract["requires_confirmation"])
    requires_review = bool(contract["requires_review"])
    if not metadata["read_only"] and not (requires_confirmation or requires_review):
        raise RuntimeError(
            f"Mutating tool {name} must require confirmation or review"
        )
    for field in ("output_summary_shape", "user_visible_summary_template"):
        if not str(contract.get(field) or "").strip():
            raise RuntimeError(f"Tool {name} is missing {field}")
    return {
        "risk_level": risk_level,
        "requires_confirmation": requires_confirmation,
        "requires_review": requires_review,
        "output_summary_shape": str(contract["output_summary_shape"]).strip(),
        "user_visible_summary_template": str(
            contract["user_visible_summary_template"]
        ).strip(),
    }


def _build_tool_catalog() -> dict[str, ToolSpec]:
    catalog: dict[str, ToolSpec] = {}
    for definition in _TOOL_DEFINITIONS:
        name = definition["name"]
        metadata = _TOOL_METADATA[name]
        input_schema = definition.get("input_schema", {"type": "object"})
        contract = _validated_tool_contract(
            name,
            metadata,
            _TOOL_CONTRACT_METADATA[name],
        )
        catalog[name] = ToolSpec(
            name=name,
            label=metadata["label"],
            description=definition.get("description", ""),
            input_schema=input_schema,
            executor=_EXECUTORS[name],
            read_only=metadata["read_only"],
            domain=metadata["domain"],
            required_permission=metadata.get("required_permission"),
            risk_level=contract["risk_level"],
            requires_confirmation=contract["requires_confirmation"],
            requires_review=contract["requires_review"],
            input_schema_summary=_summarize_input_schema(input_schema),
            output_summary_shape=contract["output_summary_shape"],
            user_visible_summary_template=contract["user_visible_summary_template"],
        )
    return catalog


TOOL_CATALOG = _build_tool_catalog()
TOOL_DEFINITIONS = [spec.definition for spec in TOOL_CATALOG.values()]


def get_tool_definitions(tool_names: frozenset[str]) -> list[dict]:
    return [
        TOOL_CATALOG[name].definition
        for name in TOOL_CATALOG
        if name in tool_names
    ]


def get_tool_metadata() -> list[dict]:
    return [spec.metadata for spec in TOOL_CATALOG.values()]
