"""Synchronous agent loop for the requirements intake assistant."""

import json
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.assistant.gateway import AIGateway, AssistantUnavailableError
from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.tools import TOOL_DEFINITIONS, execute_tool
from app.core.config import settings
from app.organizations.access import get_accessible_organizations_query
from app.users.models import User

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 500
FALLBACK_REPLY = (
    "No he podido generar una respuesta esta vez. ¿Puedes repetirlo o "
    "formularlo de otra manera?"
)
REFUSAL_REPLY = (
    "No puedo ayudarte con esa petición. Si crees que es un error, "
    "reformúlala o coméntalo con el equipo."
)
ERROR_REPLY = (
    "Ha habido un problema al contactar con el servicio de IA. Tu mensaje "
    "queda guardado; inténtalo de nuevo en unos minutos."
)

SYSTEM_PROMPT = """Eres el asistente de captura de requisitos de Asistente \
Ayuntamientos, una plataforma de gestión para ayuntamientos pequeños y medianos.

Tu trabajo: conversar en español, de forma cercana y sin jerga técnica, con \
personal municipal (a menudo cargos electos sin perfil técnico) para capturar \
necesidades, problemas e ideas como requisitos estructurados en la base de \
datos, usando las herramientas disponibles.

Cómo trabajar:
- Haz preguntas de descubrimiento: qué problema hay, cómo se hace hoy, cómo \
debería funcionar, a quién afecta, qué documentos intervienen, si hay datos \
sensibles o normativa implicada. No interrogues: 1-2 preguntas por turno.
- Antes de crear un requisito, comprueba con list_requirements si ya existe \
algo parecido; si existe, propone actualizarlo o añadir una nota en lugar de \
duplicar.
- Crea los requisitos siempre como borrador y resume al usuario lo que has \
guardado. Solo pásalos a 'submitted' cuando el usuario lo confirme.
- Si el usuario pertenece a varias organizaciones, confirma en cuál trabajar.
- No tomas decisiones legales ni administrativas: capturas, estructuras y \
propones. Las revisiones y aprobaciones las hacen personas.
- Si una herramienta devuelve un error de permisos, explícalo con claridad y \
no insistas.
- Responde siempre en español, breve y claro.
"""


def build_system_prompt(db: Session, current_user: User) -> str:
    organizations = db.scalars(
        get_accessible_organizations_query(current_user)
    ).all()
    organization_lines = "\n".join(
        f"- {organization.name} (id {organization.id})"
        for organization in organizations
    )
    return (
        f"{SYSTEM_PROMPT}\n"
        f"Usuario actual: {current_user.full_name}.\n"
        f"Organizaciones del usuario:\n{organization_lines or '- (ninguna)'}"
    )


def build_history(conversation: AssistantConversation) -> list[dict]:
    return [
        {"role": message.role, "content": message.content}
        for message in conversation.messages
        if message.content
    ]


def extract_text(content_blocks) -> str:
    return "\n\n".join(
        block.text for block in content_blocks if block.type == "text"
    ).strip()


def run_agent_turn(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_text: str,
    gateway: AIGateway,
) -> AssistantMessage:
    """Persist the user message, run the tool loop, persist the reply."""
    user_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content=user_text,
    )
    db.add(user_message)
    if conversation.title == "Conversación":
        conversation.title = user_text[:255]
    db.commit()
    db.refresh(conversation)

    system = build_system_prompt(db, current_user)
    messages = build_history(conversation)

    actions: list[dict] = []
    reply_text = ""
    # Gateway failures mid-turn must not bubble up as a 500: tool side
    # effects may already be committed, and the user message is persisted.
    # Persisting an error reply keeps the conversation consistent and makes
    # a retry a new, clean turn instead of a duplicate.
    try:
        response = gateway.complete(
            system=system,
            messages=messages,
            tools=TOOL_DEFINITIONS,
        )

        for _ in range(settings.assistant_max_tool_iterations):
            if response.stop_reason == "refusal":
                reply_text = REFUSAL_REPLY
                break

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                response = gateway.complete(
                    system=system,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                )
                continue

            if response.stop_reason != "tool_use":
                reply_text = extract_text(response.content)
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = execute_tool(db, current_user, block.name, dict(block.input))
                actions.append(
                    {
                        "tool": block.name,
                        "ok": result.ok,
                        "input": dict(block.input),
                        "result": result.content[:MAX_TOOL_RESULT_CHARS],
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.content,
                        "is_error": not result.ok,
                    }
                )

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            response = gateway.complete(
                system=system,
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
        else:
            logger.warning(
                "Assistant hit the tool iteration limit (conversation=%s)",
                conversation.id,
            )
            reply_text = extract_text(response.content)
    except AssistantUnavailableError:
        logger.warning(
            "Assistant gateway failed mid-turn (conversation=%s)",
            conversation.id,
        )
        reply_text = ERROR_REPLY

    if not reply_text:
        reply_text = FALLBACK_REPLY

    assistant_message = AssistantMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=reply_text,
        actions=json.dumps(actions, ensure_ascii=False) if actions else None,
    )
    db.add(assistant_message)
    # Touch the conversation so updated_at reflects the latest activity and
    # the conversation list keeps a meaningful order (messages alone do not
    # update the parent row).
    conversation.updated_at = func.now()
    db.commit()
    db.refresh(assistant_message)
    return assistant_message
