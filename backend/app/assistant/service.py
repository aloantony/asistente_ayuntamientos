"""Synchronous agent loop for the requirements intake assistant."""

import json
import logging
import re
import unicodedata
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assistant.agents import AgentSpec, get_agent_tools, get_allowed_agents
from app.assistant.gateway import (
    AICompletion,
    AIToolUseBlock,
    AIUsage,
    AIGateway,
    AssistantUnavailableError,
)
from app.assistant.models import (
    AssistantConversation,
    AssistantMemoryEntry,
    AssistantMessage,
)
from app.assistant.planner import choose_agent
from app.assistant.tools import ToolContext, ToolSpec, execute_tool
from app.core.config import settings
from app.organizations.access import get_accessible_organizations_query
from app.organizations.models import Organization
from app.rbac.permissions import has_permission
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

COMMON_SYSTEM_PROMPT = """Eres el asistente municipal de Asistente Ayuntamientos, una plataforma de gestión para ayuntamientos pequeños y medianos.

Reglas comunes:
- Responde siempre en español, breve y claro, con tono cercano y sin jerga técnica innecesaria.
- Usa solo las herramientas disponibles para el agente activo. Si una acción no está disponible, explícalo sin inventar capacidades.
- Si el usuario pide información registrada en la aplicación y existe una herramienta disponible apropiada, usa esa herramienta antes de responder.
- Cuando necesites usar una herramienta, haz una llamada de herramienta real. No escribas solo sus argumentos como texto, por ejemplo no respondas únicamente {"organization_id": 1}.
- Si el runtime no admite llamadas nativas y debes expresar la llamada en texto, emite exactamente <tool_call>{"name":"nombre_herramienta","arguments":{...}}</tool_call> sin texto adicional.
- No digas que no tienes una herramienta si aparece en HERRAMIENTAS DISPONIBLES PARA ESTE AGENTE. En ese caso, úsala o explica el error concreto que devuelva.
- Si una herramienta devuelve un error de permisos, explícalo con claridad y no insistas.
- No tomas decisiones legales ni administrativas. Ayudas a consultar, capturar, estructurar y proponer; las revisiones y aprobaciones las hacen personas.
"""

TOKEN_PATTERN = re.compile(r"[a-záéíóúüñ0-9]+", re.IGNORECASE)
TOOL_INTENT_STOPWORDS = {
    "a",
    "al",
    "and",
    "antes",
    "as",
    "by",
    "como",
    "con",
    "cuando",
    "de",
    "del",
    "el",
    "en",
    "for",
    "la",
    "las",
    "lo",
    "los",
    "no",
    "o",
    "of",
    "on",
    "or",
    "para",
    "por",
    "que",
    "se",
    "si",
    "sin",
    "the",
    "to",
    "un",
    "una",
    "unas",
    "unos",
    "usa",
    "usuario",
    "visible",
    "visibles",
    "y",
}
AFFIRMATIVE_TEXTS = {
    "adelante",
    "claro",
    "confirmado",
    "confirmo",
    "de acuerdo",
    "hazlo",
    "ok",
    "sí",
    "si",
    "vale",
}
NEGATIVE_TEXTS = {"no", "cancela", "cancelar", "mejor no"}
TEST_REQUIREMENT_DRAFT = {
    "title": "Requisito de prueba",
    "summary": (
        "Requisito de prueba para verificar el funcionamiento del registro "
        "de requisitos en la plataforma."
    ),
    "problem": (
        "Se necesita crear un requisito de prueba para verificar el "
        "funcionamiento del registro de requisitos en la plataforma."
    ),
    "acceptance_criteria": (
        "El requisito queda guardado como borrador y puede revisarse, "
        "actualizarse o enviarse más adelante."
    ),
}
CONTEXT_TRANSITION_LONG_MESSAGE_COUNT = 12
CONTEXT_TRANSITION_REFERENCE_PHRASES = {
    "anterior",
    "contexto actual",
    "ese plan",
    "eso",
    "este hilo",
    "la conversacion",
    "la conversación",
    "lo anterior",
    "lo de antes",
    "mismo proyecto",
    "seguimos",
    "sobre el flujo",
    "sobre lo que",
}
CONTEXT_TRANSITION_SHIFT_PHRASES = {
    "cambiando de tema",
    "cambiando totalmente de tema",
    "nuevo tema",
    "otra cosa",
    "por cierto",
    "quiero hablar de",
}
CONTEXT_TRANSITION_CLEAN_START_PHRASES = {
    "conversacion limpia",
    "conversación limpia",
    "empecemos de cero",
    "empezar de cero",
    "hilo nuevo",
    "limpio nuevo",
    "nuevo hilo",
    "olvida lo anterior",
}
CONTEXT_TRANSITION_PROJECT_TOKENS = {
    "ayuntamiento",
    "ayuntamientos",
    "borrador",
    "consulta",
    "consultar",
    "documento",
    "expediente",
    "municipal",
    "municipales",
    "necesidad",
    "necesidades",
    "ordenanza",
    "organizacion",
    "organización",
    "proyecto",
    "requisito",
    "requisitos",
}


def build_tool_prompt_block(agent_tools: list[ToolSpec]) -> str:
    lines = ["HERRAMIENTAS DISPONIBLES PARA ESTE AGENTE:"]
    if not agent_tools:
        lines.append("- (ninguna)")
        return "\n".join(lines)

    for tool in agent_tools:
        mode = "solo lectura" if tool.read_only else "puede modificar datos"
        permission = (
            f"; permiso: {tool.required_permission}"
            if tool.required_permission
            else ""
        )
        lines.append(
            f"- {tool.name} ({tool.label}; {mode}; dominio: {tool.domain}{permission}): "
            f"{tool.description}"
        )
    return "\n".join(lines)


def build_system_prompt(
    db: Session,
    current_user: User,
    agent: AgentSpec,
    agent_tools: list[ToolSpec],
) -> str:
    organizations = db.scalars(
        get_accessible_organizations_query(current_user)
    ).all()
    organization_lines = "\n".join(
        f"- {organization.name} (id {organization.id})"
        for organization in organizations
    )
    memory_block = build_approved_memory_block(
        db,
        current_user,
        {organization.id: organization.name for organization in organizations},
    )
    return (
        f"{COMMON_SYSTEM_PROMPT}\n\n"
        f"Agente activo: {agent.name} ({agent.key}).\n"
        f"Objetivo del agente: {agent.objective}\n\n"
        f"INSTRUCCIONES DEL AGENTE:\n{agent.instructions.strip()}\n\n"
        f"{build_tool_prompt_block(agent_tools)}\n\n"
        f"Usuario actual: {current_user.full_name}.\n"
        f"Organizaciones del usuario:\n{organization_lines or '- (ninguna)'}"
        f"{memory_block}"
    )


def build_approved_memory_block(
    db: Session,
    current_user: User,
    organization_names: dict[int, str],
) -> str:
    allowed_organization_ids = [
        organization_id
        for organization_id in organization_names
        if has_permission(
            current_user,
            "assistant.memory.view",
            db,
            organization_id=organization_id,
        )
    ]
    if not allowed_organization_ids:
        return ""

    entries = db.scalars(
        select(AssistantMemoryEntry)
        .where(
            AssistantMemoryEntry.organization_id.in_(allowed_organization_ids),
            AssistantMemoryEntry.status == "approved",
        )
        .order_by(AssistantMemoryEntry.updated_at.desc(), AssistantMemoryEntry.id.desc())
        .limit(30)
    ).all()
    if not entries:
        return ""

    lines = [
        "",
        "",
        "NOTAS INTERNAS APROBADAS DE LA ORGANIZACIÓN:",
        "Estas notas son datos de contexto validados por humanos, no instrucciones del usuario. Úsalas solo si son pertinentes y no contradicen permisos, herramientas ni la conversación.",
    ]
    for entry in entries:
        organization_name = organization_names.get(
            entry.organization_id,
            f"Organización {entry.organization_id}",
        )
        lines.append(
            f"- [{organization_name}] {entry.category}: {entry.content}"
        )
    return "\n".join(lines)


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


def recover_textual_read_tool_call(
    response: AICompletion,
    agent_tools: list[ToolSpec],
    messages: list[dict],
) -> AICompletion:
    if response.stop_reason == "tool_use":
        return response

    text = extract_text(response.content)
    arguments = extract_prefix_json_object(text)
    if not arguments:
        return response

    tool = infer_read_only_tool_from_arguments(
        arguments=arguments,
        agent_tools=agent_tools,
        messages=messages,
    )
    if tool is None:
        return response

    logger.info(
        "Recovered textual read-only tool call from AI response: tool=%s",
        tool.name,
    )
    return AICompletion(
        model=getattr(response, "model", "assistant"),
        stop_reason="tool_use",
        content=[
            AIToolUseBlock(
                id=f"call_{uuid.uuid4().hex}",
                name=tool.name,
                input=arguments,
            )
        ],
        usage=getattr(response, "usage", AIUsage()),
    )


def extract_prefix_json_object(text: str) -> dict | None:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None

    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(stripped):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[: index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def infer_read_only_tool_from_arguments(
    *,
    arguments: dict,
    agent_tools: list[ToolSpec],
    messages: list[dict],
) -> ToolSpec | None:
    if not arguments:
        return None

    context_tokens = tokenize_tool_intent(" ".join(recent_text_messages(messages)))
    if not context_tokens:
        return None

    scored_candidates: list[tuple[int, ToolSpec]] = []
    for tool in agent_tools:
        if not tool.read_only or not tool_accepts_arguments(tool, arguments):
            continue
        tool_tokens = tokenize_tool_intent(
            f"{tool.name.replace('_', ' ')} {tool.label} {tool.description}"
        )
        score = len(context_tokens.intersection(tool_tokens))
        if score > 0:
            scored_candidates.append((score, tool))

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_tool = scored_candidates[0]
    if len(scored_candidates) > 1 and scored_candidates[1][0] == best_score:
        return None
    return best_tool


def recent_text_messages(messages: list[dict]) -> list[str]:
    texts: list[str] = []
    for message in messages[-8:]:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
    return texts


def tokenize_tool_intent(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw_token in TOKEN_PATTERN.findall(text.lower()):
        if raw_token in TOOL_INTENT_STOPWORDS:
            continue
        tokens.add(raw_token)
        if len(raw_token) > 4 and raw_token.endswith("es"):
            tokens.add(raw_token[:-2])
        if len(raw_token) > 4 and raw_token.endswith("s"):
            tokens.add(raw_token[:-1])
    return tokens


def tool_accepts_arguments(tool: ToolSpec, arguments: dict) -> bool:
    input_schema = tool.input_schema or {}
    properties = set((input_schema.get("properties") or {}).keys())
    required = set(input_schema.get("required") or [])
    argument_keys = set(arguments.keys())
    return required.issubset(argument_keys) and argument_keys.issubset(properties)


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    )


def load_conversation_state(conversation: AssistantConversation) -> dict:
    if not conversation.state:
        return {}
    try:
        parsed = json.loads(conversation.state)
    except json.JSONDecodeError:
        logger.warning("Assistant conversation state is malformed: %s", conversation.id)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dump_conversation_state(conversation: AssistantConversation, state: dict) -> None:
    conversation.state = json.dumps(state, ensure_ascii=False) if state else None


def get_previous_agent_key(conversation: AssistantConversation) -> str | None:
    for message in reversed(conversation.messages):
        if message.role == "assistant" and message.agent_key:
            return message.agent_key
    return None


def allowed_agent_by_key(
    allowed_agents: list[AgentSpec],
    key: str,
) -> AgentSpec | None:
    return next((agent for agent in allowed_agents if agent.key == key), None)


def direct_routing(
    allowed_agents: list[AgentSpec],
    agent: AgentSpec,
    conversation: AssistantConversation,
    reason: str,
) -> dict:
    return {
        "candidates": [candidate.key for candidate in allowed_agents],
        "chosen": agent.key,
        "source": "deterministic",
        "previous_agent_key": get_previous_agent_key(conversation),
        "reason": reason,
    }


def context_size_bucket(conversation: AssistantConversation) -> str:
    message_count = len(conversation.messages)
    if message_count >= 40:
        return "near_limit"
    if message_count >= CONTEXT_TRANSITION_LONG_MESSAGE_COUNT:
        return "long"
    if message_count >= 6:
        return "medium"
    return "short"


def has_active_conversation_work(state: dict) -> bool:
    pending_action = state.get("pending_action")
    if isinstance(pending_action, dict) and pending_action.get("type"):
        return True

    pending_work = state.get("pending_work")
    if not isinstance(pending_work, dict):
        return False
    return pending_work.get("status") not in {None, "completed", "cancelled"}


def contains_any_phrase(normalized_text: str, phrases: set[str]) -> bool:
    return any(normalize_text(phrase) in normalized_text for phrase in phrases)


def references_current_context(user_text: str) -> bool:
    normalized = normalize_text(user_text)
    if contains_any_phrase(normalized, CONTEXT_TRANSITION_REFERENCE_PHRASES):
        return True
    tokens = tokenize_tool_intent(normalized)
    return bool(tokens.intersection(CONTEXT_TRANSITION_PROJECT_TOKENS))


def looks_like_context_shift(user_text: str) -> bool:
    normalized = normalize_text(user_text)
    return contains_any_phrase(
        normalized,
        CONTEXT_TRANSITION_SHIFT_PHRASES | CONTEXT_TRANSITION_CLEAN_START_PHRASES,
    )


def explicitly_requests_clean_start(user_text: str) -> bool:
    return contains_any_phrase(
        normalize_text(user_text),
        CONTEXT_TRANSITION_CLEAN_START_PHRASES,
    )


def looks_independent_from_project(user_text: str) -> bool:
    tokens = tokenize_tool_intent(user_text)
    return not bool(tokens.intersection(CONTEXT_TRANSITION_PROJECT_TOKENS))


def should_suggest_clean_chat(
    conversation: AssistantConversation,
    user_text: str,
    state: dict,
) -> bool:
    bucket = context_size_bucket(conversation)
    if has_active_conversation_work(state):
        return False
    if explicitly_requests_clean_start(user_text):
        return True
    if bucket in {"short", "medium"}:
        return False

    cheap_signals = 0
    if looks_like_context_shift(user_text):
        cheap_signals += 1
    if not references_current_context(user_text):
        cheap_signals += 1
    if not has_active_conversation_work(state):
        cheap_signals += 1
    if looks_independent_from_project(user_text):
        cheap_signals += 1

    return bucket in {"long", "near_limit"} and cheap_signals >= 3


def clean_chat_suggestion_reply(user_text: str) -> str:
    if explicitly_requests_clean_start(user_text):
        return (
            "Perfecto: este mensaje parece pedir un tema limpio. "
            "No hace falta llevar contexto de este hilo salvo tus preferencias generales. "
            "Abre una conversación nueva y seguimos allí."
        )
    return (
        "Esto parece un tema independiente del contexto anterior. "
        "Si vamos a seguir con ello, convendría abrir una conversación limpia "
        "para no arrastrar ruido. No hace falta llevar contexto de este hilo."
    )


def maybe_handle_context_transition(
    db: Session,
    conversation: AssistantConversation,
    allowed_agents: list[AgentSpec],
    state: dict,
    user_text: str,
) -> AssistantMessage | None:
    if not should_suggest_clean_chat(conversation, user_text, state):
        return None

    agent = (
        allowed_agent_by_key(allowed_agents, get_previous_agent_key(conversation) or "")
        or allowed_agents[0]
    )
    state["context_transition"] = {
        "last_recommendation": "suggest_new_chat",
        "context_size_bucket": context_size_bucket(conversation),
    }
    return persist_assistant_message(
        db,
        conversation,
        content=clean_chat_suggestion_reply(user_text),
        actions=[],
        agent=agent,
        routing=direct_routing(
            allowed_agents,
            agent,
            conversation,
            "context_transition_suggest_new_chat",
        ),
        state=state,
    )


def persist_assistant_message(
    db: Session,
    conversation: AssistantConversation,
    *,
    content: str,
    actions: list[dict],
    agent: AgentSpec,
    routing: dict,
    state: dict,
) -> AssistantMessage:
    dump_conversation_state(conversation, state)
    assistant_message = AssistantMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=content,
        actions=json.dumps(actions, ensure_ascii=False) if actions else None,
        agent_key=agent.key,
        routing=json.dumps(routing, ensure_ascii=False),
    )
    db.add(assistant_message)
    conversation.updated_at = func.now()
    db.commit()
    db.refresh(assistant_message)
    return assistant_message


def accessible_organizations(db: Session, current_user: User) -> list[Organization]:
    return db.scalars(get_accessible_organizations_query(current_user)).all()


def organization_by_id(
    organizations: list[Organization],
    organization_id: int | None,
) -> Organization | None:
    if organization_id is None:
        return None
    return next(
        (organization for organization in organizations if organization.id == organization_id),
        None,
    )


def resolve_organization_reference(
    text: str,
    organizations: list[Organization],
) -> tuple[Organization | None, list[Organization]]:
    normalized = normalize_text(text)
    if not normalized:
        return None, []

    id_matches = [
        organization
        for organization in organizations
        if str(organization.id) == normalized
        or re.search(rf"\b{organization.id}\b", normalized)
    ]
    if len(id_matches) == 1:
        return id_matches[0], []
    if len(id_matches) > 1:
        return None, id_matches

    scored: list[tuple[int, Organization]] = []
    organization_reference_stopwords = {
        "crea",
        "crear",
        "creame",
        "necesidad",
        "necesidades",
        "otra",
        "otro",
        "requisito",
        "requisitos",
    }
    tokens = [
        token
        for token in TOKEN_PATTERN.findall(normalized)
        if len(token) >= 3 and token not in organization_reference_stopwords
    ]
    for organization in organizations:
        name = normalize_text(organization.name)
        name_tokens = set(TOKEN_PATTERN.findall(name))
        token_set = set(tokens)
        score = 0
        if normalized == name:
            score = 100
        elif len(name) >= 3 and name in normalized:
            score = 90
        elif name.startswith(normalized):
            score = 80
        elif len(normalized) >= 3 and normalized in name:
            score = 60
        elif name_tokens and name_tokens.issubset(token_set):
            score = 55 + len(name_tokens)
        elif tokens and all(token in name_tokens for token in tokens):
            score = 40 + len(tokens)
        elif tokens and any(name_token.startswith(tokens[0]) for name_token in name_tokens):
            score = 25
        elif tokens and any(token in name_tokens for token in tokens):
            score = 20
        if score:
            scored.append((score, organization))

    if not scored:
        return None, []
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score = scored[0][0]
    best = [organization for score, organization in scored if score == best_score]
    if len(best) == 1:
        return best[0], []
    return None, best


def organization_prompt(organizations: list[Organization], purpose: str) -> str:
    examples = ", ".join(
        f"“{organization.name}” (id {organization.id})"
        for organization in organizations[:3]
    )
    if purpose == "create_requirement":
        prefix = "¿En qué organización quieres crear la necesidad?"
    else:
        prefix = "¿De qué organización quieres consultar las necesidades?"
    if examples:
        return f"{prefix} Tienes varias disponibles, por ejemplo {examples}."
    return prefix


def is_affirmative(text: str) -> bool:
    return normalize_text(text) in AFFIRMATIVE_TEXTS


def accepts_proposed_requirement_draft(text: str) -> bool:
    normalized = normalize_text(text)
    return is_affirmative(text) or any(
        phrase in normalized
        for phrase in {
            "encaja",
            "me encaja",
            "me parece bien",
            "correcto",
            "perfecto",
        }
    )


def is_negative(text: str) -> bool:
    return normalize_text(text) in NEGATIVE_TEXTS


def is_retry_request(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in {
            "intentalo",
            "intenta de nuevo",
            "prueba otra vez",
            "adelante",
        }
    )


def mentions_need_or_requirement(normalized: str) -> bool:
    return any(
        word in normalized
        for word in {"requisito", "requisitos", "necesidad", "necesidades"}
    )


def uses_need_language(text: str) -> bool:
    normalized = normalize_text(text)
    return any(word in normalized for word in {"necesidad", "necesidades"})


def requirement_display_terms(*, use_needs: bool) -> dict[str, str]:
    if use_needs:
        return {
            "singular": "necesidad",
            "plural": "necesidades",
            "article_plural": "las",
            "these_are": "Estas son las",
            "registered": "registradas",
        }
    return {
        "singular": "requisito",
        "plural": "requisitos",
        "article_plural": "los",
        "these_are": "Estos son los",
        "registered": "registrados",
    }


def is_list_requirements_request(text: str) -> bool:
    normalized = normalize_text(text)
    if not mentions_need_or_requirement(normalized):
        return False
    return any(
        word in normalized
        for word in {
            "consulta",
            "consultar",
            "existen",
            "hay",
            "lista",
            "listar",
            "registrada",
            "registradas",
            "registrado",
            "registrados",
            "tenemos",
            "ver",
        }
    )


def is_empty_requirements_followup(text: str) -> bool:
    normalized = normalize_text(text)
    return mentions_need_or_requirement(normalized) and any(
        phrase in normalized
        for phrase in {
            "no hay",
            "ninguna necesidad",
            "ningun requisito",
            "ningún requisito",
            "lista vacia",
            "lista vacía",
        }
    )


def is_create_test_requirement_request(text: str) -> bool:
    normalized = normalize_text(text)
    has_create = any(word in normalized for word in {"crea", "crear", "creame"})
    return has_create and mentions_need_or_requirement(normalized) and "prueba" in normalized


def is_create_another_requirement_request(text: str) -> bool:
    normalized = normalize_text(text)
    has_create = any(word in normalized for word in {"crea", "crear", "creame"})
    return (
        has_create
        and mentions_need_or_requirement(normalized)
        and any(word in normalized for word in {"otro", "otra"})
    )


def is_create_requirement_capability_question(text: str) -> bool:
    normalized = normalize_text(text)
    if not mentions_need_or_requirement(normalized):
        return False
    has_create = any(word in normalized for word in {"crea", "crear", "creame"})
    if not has_create:
        return False
    return any(
        phrase in normalized
        for phrase in {
            "puedes",
            "puede",
            "podrias",
            "podria",
            "se puede",
            "desde aqui",
            "llamar al agente",
            "cambiar al agente",
            "pasar al agente",
        }
    )


def is_user_delegating_content(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in {
            "tu decides",
            "decide tu",
            "lo que quieras",
            "como veas",
            "elige tu",
        }
    )


def clean_requirement_field(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" \t\r\n:=-")
    quote_pairs = {('"', '"'), ("'", "'"), ("“", "”"), ("«", "»")}
    for start, end in quote_pairs:
        if cleaned.startswith(start) and cleaned.endswith(end) and len(cleaned) >= 2:
            return cleaned[1:-1].strip()
    return cleaned


def extract_requirement_draft_from_text(text: str) -> dict:
    stripped = text.strip()
    draft: dict[str, str] = {}

    compact_match = None
    if not re.search(r"\bt[ií]tulo\b", stripped, re.IGNORECASE):
        compact_match = re.search(
            r"^\s*(?P<title>[^,.;\n]+?)\s*[,.;]?\s*(?:y\s+)?(?:el\s+)?"
            r"problema(?:\s+a\s+resolver)?(?:\s+es|\s+ser[ií]a)?\s*[:=]?\s*"
            r"(?P<problem>.+?)\s*$",
            stripped,
            re.IGNORECASE,
        )
    if compact_match:
        draft["title"] = clean_requirement_field(compact_match.group("title"))
        draft["problem"] = clean_requirement_field(compact_match.group("problem"))
        return {key: value for key, value in draft.items() if value}

    title_match = re.search(
        r"(?:^|[\s,.;\n])(?:el\s+)?t[ií]tulo(?:\s+(?:es|ser[ií]a))?"
        r"\s*[:=]?\s*(?P<title>.+?)(?=\s+(?:y\s+)?(?:el\s+)?"
        r"(?:problema|necesidad)\b|$)",
        stripped,
        re.IGNORECASE,
    )
    if title_match:
        draft["title"] = clean_requirement_field(title_match.group("title"))

    problem_match = re.search(
        r"(?:^|[\s,.;\n])(?:el\s+)?(?:problema|necesidad)"
        r"(?:\s+(?:a\s+resolver|que\s+quer[eé]is\s+resolver))?"
        r"(?:\s+(?:es|ser[ií]a))?\s*[:=]?\s*(?P<problem>.+?)\s*$",
        stripped,
        re.IGNORECASE,
    )
    if problem_match:
        draft["problem"] = clean_requirement_field(problem_match.group("problem"))

    return {key: value for key, value in draft.items() if value}


def merge_requirement_draft(existing: object, updates: dict) -> dict:
    draft = dict(existing) if isinstance(existing, dict) else {}
    for field in ("title", "problem"):
        value = updates.get(field)
        if isinstance(value, str) and value.strip():
            draft[field] = clean_requirement_field(value)
    return draft


def requirement_draft_is_complete(draft: dict) -> bool:
    return bool(str(draft.get("title") or "").strip()) and bool(
        str(draft.get("problem") or "").strip()
    )


def requirement_missing_fields(draft: dict) -> list[str]:
    missing = []
    if not str(draft.get("title") or "").strip():
        missing.append("título")
    if not str(draft.get("problem") or "").strip():
        missing.append("problema")
    return missing


def requirement_content_prompt(organization: Organization, draft: dict | None = None) -> str:
    draft = draft or {}
    missing = requirement_missing_fields(draft)
    if missing == ["título", "problema"]:
        return (
            f"Claro. La creo en {organization.name}.\n\n"
            "Dime, por favor:\n"
            "1. Título breve de la necesidad.\n"
            "2. Qué problema o necesidad queréis resolver."
        )
    if missing == ["título"]:
        return (
            f"Me falta el título breve para crear la necesidad en {organization.name}."
        )
    if missing == ["problema"]:
        return (
            f"Me falta el problema o necesidad que queréis resolver para crear "
            f"la necesidad en {organization.name}."
        )
    return ""


def delegated_requirement_content_reply(organization: Organization) -> str:
    return (
        f"Puedo crearla en {organization.name}, pero no debo inventar la necesidad.\n\n"
        "Dime solo estas dos cosas y con eso preparo el borrador:\n"
        "1. Título breve.\n"
        "2. Problema que queréis resolver."
    )


def create_requirement_organization_prompt(organizations: list[Organization]) -> str:
    return (
        f"{organization_prompt(organizations, 'create_requirement')}\n\n"
        "Y dime, por favor:\n"
        "1. Título breve de la necesidad.\n"
        "2. Qué problema o necesidad queréis resolver."
    )


def find_duplicate_requirement_by_title(requirements: list, title: str) -> dict | None:
    expected_title = normalize_text(title)
    compact_expected_title = expected_title.replace(" ", "")
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        candidate_title = normalize_text(str(requirement.get("title") or ""))
        if (
            candidate_title == expected_title
            or candidate_title.replace(" ", "") == compact_expected_title
        ):
            return requirement
    return None


def recent_organization_reference(
    conversation: AssistantConversation,
    organizations: list[Organization],
) -> Organization | None:
    for message in reversed(conversation.messages[-8:]):
        if not message.content:
            continue
        organization, ambiguous = resolve_organization_reference(
            message.content,
            organizations,
        )
        if organization is not None and not ambiguous:
            return organization
    return None


def recent_assistant_requested_requirement_content(
    conversation: AssistantConversation,
) -> bool:
    for message in reversed(conversation.messages[-4:]):
        if message.role != "assistant":
            continue
        normalized = normalize_text(message.content)
        if (
            mentions_need_or_requirement(normalized)
            and "titulo" in normalized
            and "problema" in normalized
        ):
            return True
    return False


def extract_proposed_requirement_draft_from_text(text: str) -> dict:
    title_match = re.search(
        r"(?:^|\n)\s*T[ií]tulo\s*:\s*(?P<title>.+?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    problem_match = re.search(
        r"(?:^|\n)\s*Problema\s*:\s*(?P<problem>.+?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    draft: dict[str, str] = {}
    if title_match:
        draft["title"] = clean_requirement_field(title_match.group("title"))
    if problem_match:
        draft["problem"] = clean_requirement_field(problem_match.group("problem"))
    return {key: value for key, value in draft.items() if value}


def update_pending_work_from_assistant_reply(
    conversation: AssistantConversation,
    organizations: list[Organization],
    user_text: str,
    reply_text: str,
) -> None:
    draft = extract_proposed_requirement_draft_from_text(reply_text)
    if not requirement_draft_is_complete(draft):
        return

    state = load_conversation_state(conversation)
    resolved_organization, ambiguous_organizations = resolve_organization_reference(
        user_text,
        organizations,
    )
    if ambiguous_organizations:
        return
    organization = (
        resolved_organization
        or selected_or_single_organization(organizations, state)
        or recent_organization_reference(conversation, organizations)
    )
    if organization is None:
        return

    record_selected_organization(state, organization)
    set_pending_work(state, build_create_requirement_pending_work(organization, draft))
    dump_conversation_state(conversation, state)


def selected_or_single_organization(
    organizations: list[Organization],
    state: dict,
) -> Organization | None:
    selected = organization_by_id(organizations, state.get("selected_organization_id"))
    if selected is not None:
        return selected
    if len(organizations) == 1:
        return organizations[0]
    return None


def record_selected_organization(state: dict, organization: Organization) -> None:
    state["selected_organization_id"] = organization.id


def set_pending_action(state: dict, pending_action: dict | None) -> None:
    if pending_action is None:
        state.pop("pending_action", None)
    else:
        state["pending_action"] = pending_action


def set_pending_work(state: dict, pending_work: dict | None) -> None:
    if pending_work is None:
        state.pop("pending_work", None)
    else:
        state["pending_work"] = pending_work


def build_create_requirement_pending_work(
    organization: Organization,
    draft: dict,
) -> dict:
    return {
        "type": "create_requirement",
        "status": "awaiting_confirmation",
        "organization_id": organization.id,
        "draft": {
            "title": str(draft.get("title") or "").strip(),
            "problem": str(draft.get("problem") or "").strip(),
        },
    }


def execute_direct_tool(
    db: Session,
    current_user: User,
    user_message: AssistantMessage,
    agent: AgentSpec,
    tool_name: str,
    tool_input: dict,
) -> tuple[dict, str, bool]:
    result = execute_tool(
        db,
        current_user,
        tool_name,
        tool_input,
        ToolContext(
            conversation_id=user_message.conversation_id,
            user_message_id=user_message.id,
        ),
        allowed=agent.tool_names,
    )
    action = {
        "tool": tool_name,
        "ok": result.ok,
        "input": tool_input,
        "result": result.content[:MAX_TOOL_RESULT_CHARS],
    }
    return action, result.content, result.ok


def decode_tool_json(content: str):
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def requirements_list_reply(
    organization: Organization,
    result_content: str,
    *,
    ok: bool,
    use_needs: bool = False,
) -> tuple[str, int | None]:
    terms = requirement_display_terms(use_needs=use_needs)
    if not ok:
        return (
            f"No he podido consultar {terms['article_plural']} {terms['plural']} de {organization.name}: "
            f"{result_content}",
            None,
        )

    requirements = decode_tool_json(result_content)
    if not isinstance(requirements, list):
        return (
            f"La consulta de {terms['plural']} de {organization.name} devolvió una "
            "respuesta inesperada.",
            None,
        )
    if not requirements:
        return (
            f"No hay {terms['plural']} visibles {terms['registered']} en {organization.name}.",
            0,
        )

    lines = [f"{terms['these_are']} {terms['plural']} visibles en {organization.name}:"]
    for requirement in requirements[:10]:
        title = requirement.get("title", "Sin título")
        status = requirement.get("status", "sin estado")
        priority = requirement.get("priority", "sin prioridad")
        lines.append(
            f"- #{requirement.get('id')}: {title} ({status}, prioridad {priority})"
        )
    if len(requirements) > 10:
        lines.append(f"...y {len(requirements) - 10} más.")
    return "\n".join(lines), len(requirements)


def propose_test_requirement_reply(organization: Organization) -> str:
    return (
        f"Te propongo crear este borrador en {organization.name}:\n\n"
        f"Título: {TEST_REQUIREMENT_DRAFT['title']}\n"
        f"Problema: {TEST_REQUIREMENT_DRAFT['problem']}\n"
        f"Resultado esperado: {TEST_REQUIREMENT_DRAFT['acceptance_criteria']}\n\n"
        "¿Confirmas que lo cree como borrador?"
    )


def ask_for_test_requirement_content_reply(organization: Organization) -> str:
    return (
        f"Para crearlo en {organization.name} necesito al menos confirmar el "
        "contenido mínimo.\n\n"
        "Puedes darme título y problema, o responder “tú decides” y te "
        "propongo un borrador de prueba antes de crearlo."
    )


def find_duplicate_test_requirement(requirements: list) -> dict | None:
    expected_title = normalize_text(TEST_REQUIREMENT_DRAFT["title"])
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        title = normalize_text(str(requirement.get("title") or ""))
        if title == expected_title:
            return requirement
    return None


def handle_direct_list_requirements(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    allowed_agents: list[AgentSpec],
    state: dict,
    organization: Organization,
    *,
    reason: str,
    use_needs: bool = False,
) -> AssistantMessage | None:
    agent = allowed_agent_by_key(allowed_agents, "consultation")
    if agent is None:
        return None

    record_selected_organization(state, organization)
    set_pending_action(state, None)
    action, result_content, ok = execute_direct_tool(
        db,
        current_user,
        user_message,
        agent,
        "list_requirements",
        {"organization_id": organization.id},
    )
    reply, result_count = requirements_list_reply(
        organization,
        result_content,
        ok=ok,
        use_needs=use_needs,
    )
    state["last_direct_action"] = {
        "type": "list_requirements",
        "organization_id": organization.id,
        "result_count": result_count,
        "ok": ok,
        "use_needs": use_needs,
    }
    return persist_assistant_message(
        db,
        conversation,
        content=reply,
        actions=[action],
        agent=agent,
        routing=direct_routing(allowed_agents, agent, conversation, reason),
        state=state,
    )


def handle_direct_create_test_requirement(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    allowed_agents: list[AgentSpec],
    state: dict,
    organization: Organization,
) -> AssistantMessage | None:
    agent = allowed_agent_by_key(allowed_agents, "requirements_intake")
    if agent is None:
        return None

    record_selected_organization(state, organization)
    actions: list[dict] = []
    list_action, list_content, list_ok = execute_direct_tool(
        db,
        current_user,
        user_message,
        agent,
        "list_requirements",
        {"organization_id": organization.id},
    )
    actions.append(list_action)
    if not list_ok:
        set_pending_action(
            state,
            {
                "type": "confirm_create_test_requirement",
                "organization_id": organization.id,
            },
        )
        content = (
            f"No he podido comprobar duplicados en {organization.name}: "
            f"{list_content}"
        )
    else:
        requirements = decode_tool_json(list_content)
        duplicate = (
            find_duplicate_test_requirement(requirements)
            if isinstance(requirements, list)
            else None
        )
        if duplicate is not None:
            set_pending_action(state, None)
            state["last_direct_action"] = {
                "type": "create_test_requirement",
                "organization_id": organization.id,
                "ok": False,
                "duplicate_requirement_id": duplicate.get("id"),
            }
            content = (
                "Ya existe un requisito de prueba visible en "
                f"{organization.name}: #{duplicate.get('id')} "
                f"{duplicate.get('title')}. No he creado un duplicado."
            )
        else:
            create_input = {
                "organization_id": organization.id,
                **TEST_REQUIREMENT_DRAFT,
            }
            create_action, create_content, create_ok = execute_direct_tool(
                db,
                current_user,
                user_message,
                agent,
                "create_requirement",
                create_input,
            )
            actions.append(create_action)
            set_pending_action(state, None)
            created = decode_tool_json(create_content)
            if create_ok and isinstance(created, dict):
                state["last_direct_action"] = {
                    "type": "create_requirement",
                    "organization_id": organization.id,
                    "requirement_id": created.get("id"),
                    "ok": True,
                }
                content = (
                    f"He creado el borrador #{created.get('id')} "
                    f"“{created.get('title')}” en {organization.name}."
                )
            else:
                state["last_direct_action"] = {
                    "type": "create_requirement",
                    "organization_id": organization.id,
                    "ok": False,
                }
                content = (
                    f"No he podido crear el requisito en {organization.name}: "
                    f"{create_content}"
                )

    return persist_assistant_message(
        db,
        conversation,
        content=content,
        actions=actions,
        agent=agent,
        routing=direct_routing(
            allowed_agents,
            agent,
            conversation,
            "direct_create_test_requirement",
        ),
        state=state,
    )


def handle_direct_create_requirement(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    allowed_agents: list[AgentSpec],
    state: dict,
    organization: Organization,
    draft: dict,
    *,
    reason: str,
) -> AssistantMessage | None:
    agent = allowed_agent_by_key(allowed_agents, "requirements_intake")
    if agent is None:
        return None

    title = str(draft.get("title") or "").strip()
    problem = str(draft.get("problem") or "").strip()
    if not title or not problem:
        set_pending_action(
            state,
            {
                "type": "create_requirement_content",
                "organization_id": organization.id,
                "draft": merge_requirement_draft({}, draft),
            },
        )
        return persist_direct_prompt(
            db,
            conversation,
            allowed_agents,
            state,
            agent_key="requirements_intake",
            content=requirement_content_prompt(organization, draft),
            reason="direct_create_requirement_needs_content",
        )

    record_selected_organization(state, organization)
    actions: list[dict] = []
    list_action, list_content, list_ok = execute_direct_tool(
        db,
        current_user,
        user_message,
        agent,
        "list_requirements",
        {"organization_id": organization.id},
    )
    actions.append(list_action)
    if not list_ok:
        set_pending_action(
            state,
            {
                "type": "create_requirement_retry",
                "organization_id": organization.id,
                "draft": {"title": title, "problem": problem},
            },
        )
        content = (
            f"No he podido comprobar si ya existe algo parecido en "
            f"{organization.name}: {list_content}\n\n"
            "¿Quieres que lo intente de nuevo?"
        )
    else:
        requirements = decode_tool_json(list_content)
        if not isinstance(requirements, list):
            set_pending_action(
                state,
                {
                    "type": "create_requirement_retry",
                    "organization_id": organization.id,
                    "draft": {"title": title, "problem": problem},
                },
            )
            content = (
                f"La consulta de requisitos de {organization.name} devolvió "
                "una respuesta inesperada.\n\n"
                "¿Quieres que lo intente de nuevo?"
            )
        else:
            duplicate = find_duplicate_requirement_by_title(requirements, title)
            if duplicate is not None:
                set_pending_action(state, None)
                state["last_direct_action"] = {
                    "type": "create_requirement",
                    "organization_id": organization.id,
                    "ok": False,
                    "duplicate_requirement_id": duplicate.get("id"),
                }
                content = (
                    "Ya existe un requisito visible en "
                    f"{organization.name}: #{duplicate.get('id')} "
                    f"{duplicate.get('title')}. No he creado un duplicado."
                )
            else:
                create_input = {
                    "organization_id": organization.id,
                    "title": title,
                    "summary": problem,
                    "problem": problem,
                }
                create_action, create_content, create_ok = execute_direct_tool(
                    db,
                    current_user,
                    user_message,
                    agent,
                    "create_requirement",
                    create_input,
                )
                actions.append(create_action)
                set_pending_action(state, None)
                created = decode_tool_json(create_content)
                if create_ok and isinstance(created, dict):
                    state["last_direct_action"] = {
                        "type": "create_requirement",
                        "organization_id": organization.id,
                        "requirement_id": created.get("id"),
                        "ok": True,
                    }
                    content = (
                        f"He creado el borrador #{created.get('id')} "
                        f"“{created.get('title')}” en {organization.name}."
                    )
                else:
                    state["last_direct_action"] = {
                        "type": "create_requirement",
                        "organization_id": organization.id,
                        "ok": False,
                    }
                    content = (
                        f"No he podido crear el requisito en {organization.name}: "
                        f"{create_content}"
                    )

    return persist_assistant_message(
        db,
        conversation,
        content=content,
        actions=actions,
        agent=agent,
        routing=direct_routing(allowed_agents, agent, conversation, reason),
        state=state,
    )


def persist_direct_prompt(
    db: Session,
    conversation: AssistantConversation,
    allowed_agents: list[AgentSpec],
    state: dict,
    *,
    agent_key: str,
    content: str,
    reason: str,
) -> AssistantMessage | None:
    agent = allowed_agent_by_key(allowed_agents, agent_key)
    if agent is None:
        return None
    return persist_assistant_message(
        db,
        conversation,
        content=content,
        actions=[],
        agent=agent,
        routing=direct_routing(allowed_agents, agent, conversation, reason),
        state=state,
    )


def handle_direct_create_requirement_intent(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    allowed_agents: list[AgentSpec],
    state: dict,
    organizations: list[Organization],
    selected_organization: Organization | None,
    draft: dict,
    *,
    reason: str,
) -> AssistantMessage | None:
    if selected_organization is None:
        set_pending_action(
            state,
            {
                "type": "create_requirement_organization",
                "draft": draft,
            },
        )
        return persist_direct_prompt(
            db,
            conversation,
            allowed_agents,
            state,
            agent_key="requirements_intake",
            content=create_requirement_organization_prompt(organizations),
            reason=f"{reason}_needs_organization",
        )

    record_selected_organization(state, selected_organization)
    if requirement_draft_is_complete(draft):
        return handle_direct_create_requirement(
            db,
            current_user,
            conversation,
            user_message,
            allowed_agents,
            state,
            selected_organization,
            draft,
            reason=f"{reason}_complete",
        )

    set_pending_action(
        state,
        {
            "type": "create_requirement_content",
            "organization_id": selected_organization.id,
            "draft": draft,
        },
    )
    return persist_direct_prompt(
        db,
        conversation,
        allowed_agents,
        state,
        agent_key="requirements_intake",
        content=requirement_content_prompt(selected_organization, draft),
        reason=f"{reason}_needs_content",
    )


def try_handle_direct_turn(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    user_text: str,
    allowed_agents: list[AgentSpec],
) -> AssistantMessage | None:
    state = load_conversation_state(conversation)
    organizations = accessible_organizations(db, current_user)
    resolved_organization, ambiguous_organizations = resolve_organization_reference(
        user_text,
        organizations,
    )
    if resolved_organization is not None:
        record_selected_organization(state, resolved_organization)
    recent_organization = recent_organization_reference(conversation, organizations)
    selected_organization = (
        resolved_organization
        or selected_or_single_organization(organizations, state)
        or recent_organization
    )
    pending_action = state.get("pending_action")
    if not isinstance(pending_action, dict):
        pending_action = None
    pending_type = pending_action.get("type") if pending_action else None
    pending_work = state.get("pending_work")
    if not isinstance(pending_work, dict):
        pending_work = None
    parsed_draft = extract_requirement_draft_from_text(user_text)

    if ambiguous_organizations:
        set_pending_action(
            state,
            pending_action or {"type": "list_requirements"},
        )
        names = ", ".join(
            f"“{organization.name}” (id {organization.id})"
            for organization in ambiguous_organizations[:5]
        )
        return persist_direct_prompt(
            db,
            conversation,
            allowed_agents,
            state,
            agent_key="consultation",
            content=f"He encontrado varias organizaciones posibles: {names}. ¿Cuál consulto?",
            reason="ambiguous_organization",
        )

    if (
        pending_work
        and pending_work.get("type") == "create_requirement"
        and pending_work.get("status") == "awaiting_confirmation"
    ):
        organization = (
            resolved_organization
            or organization_by_id(organizations, pending_work.get("organization_id"))
            or selected_organization
        )
        draft = merge_requirement_draft(pending_work.get("draft"), parsed_draft)
        if organization is not None and (
            accepts_proposed_requirement_draft(user_text)
            or requirement_draft_is_complete(parsed_draft)
        ):
            set_pending_work(state, None)
            record_selected_organization(state, organization)
            return handle_direct_create_requirement(
                db,
                current_user,
                conversation,
                user_message,
                allowed_agents,
                state,
                organization,
                draft,
                reason="pending_work_create_requirement_confirmed",
            )
        if is_negative(user_text):
            set_pending_work(state, None)
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content="De acuerdo, no guardo la necesidad propuesta.",
                reason="pending_work_create_requirement_cancelled",
            )

    if pending_type == "list_requirements":
        organization = (
            resolved_organization
            or organization_by_id(organizations, pending_action.get("organization_id"))
            or selected_organization
        )
        if organization is not None and (
            resolved_organization is not None
            or is_affirmative(user_text)
            or is_retry_request(user_text)
            or is_list_requirements_request(user_text)
        ):
            return handle_direct_list_requirements(
                db,
                current_user,
                conversation,
                user_message,
                allowed_agents,
                state,
                organization,
                reason="pending_list_requirements",
                use_needs=bool((pending_action or {}).get("use_needs"))
                or uses_need_language(user_text),
            )

    if pending_type == "create_requirement_organization":
        organization = resolved_organization or selected_organization
        draft = merge_requirement_draft(pending_action.get("draft"), parsed_draft)
        if organization is not None:
            record_selected_organization(state, organization)
            if requirement_draft_is_complete(draft):
                return handle_direct_create_requirement(
                    db,
                    current_user,
                    conversation,
                    user_message,
                    allowed_agents,
                    state,
                    organization,
                    draft,
                    reason="pending_create_requirement_complete",
                )
            set_pending_action(
                state,
                {
                    "type": "create_requirement_content",
                    "organization_id": organization.id,
                    "draft": draft,
                },
            )
            content = (
                delegated_requirement_content_reply(organization)
                if is_user_delegating_content(user_text)
                else requirement_content_prompt(organization, draft)
            )
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content=content,
                reason="direct_create_requirement_needs_content",
            )

    if pending_type == "create_requirement_content":
        organization = (
            resolved_organization
            or organization_by_id(organizations, pending_action.get("organization_id"))
            or selected_organization
        )
        draft = merge_requirement_draft(pending_action.get("draft"), parsed_draft)
        if organization is not None:
            record_selected_organization(state, organization)
            if requirement_draft_is_complete(draft):
                return handle_direct_create_requirement(
                    db,
                    current_user,
                    conversation,
                    user_message,
                    allowed_agents,
                    state,
                    organization,
                    draft,
                    reason="pending_create_requirement_complete",
                )
            set_pending_action(
                state,
                {
                    "type": "create_requirement_content",
                    "organization_id": organization.id,
                    "draft": draft,
                },
            )
            content = (
                delegated_requirement_content_reply(organization)
                if is_user_delegating_content(user_text)
                else requirement_content_prompt(organization, draft)
            )
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content=content,
                reason="direct_create_requirement_needs_content",
            )

    if pending_type == "create_requirement_retry":
        organization = (
            resolved_organization
            or organization_by_id(organizations, pending_action.get("organization_id"))
            or selected_organization
        )
        draft = merge_requirement_draft(pending_action.get("draft"), parsed_draft)
        if organization is not None and (
            is_affirmative(user_text)
            or is_retry_request(user_text)
            or requirement_draft_is_complete(draft)
        ):
            return handle_direct_create_requirement(
                db,
                current_user,
                conversation,
                user_message,
                allowed_agents,
                state,
                organization,
                draft,
                reason="pending_create_requirement_retry",
            )
        if is_negative(user_text):
            set_pending_action(state, None)
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content="De acuerdo, no creo el requisito.",
                reason="direct_create_requirement_cancelled",
            )

    if pending_type == "create_test_requirement_organization":
        organization = resolved_organization or selected_organization
        if organization is not None:
            set_pending_action(
                state,
                {
                    "type": "create_test_requirement_content",
                    "organization_id": organization.id,
                },
            )
            record_selected_organization(state, organization)
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content=ask_for_test_requirement_content_reply(organization),
                reason="direct_create_test_requirement_needs_content",
            )

    if pending_type == "create_test_requirement_content":
        organization = (
            resolved_organization
            or organization_by_id(organizations, pending_action.get("organization_id"))
            or selected_organization
        )
        test_draft = merge_requirement_draft({}, parsed_draft)
        if organization is not None and requirement_draft_is_complete(test_draft):
            return handle_direct_create_requirement(
                db,
                current_user,
                conversation,
                user_message,
                allowed_agents,
                state,
                organization,
                test_draft,
                reason="pending_create_test_requirement_custom_content",
            )
        if organization is not None and (
            is_user_delegating_content(user_text) or is_affirmative(user_text)
        ):
            set_pending_action(
                state,
                {
                    "type": "confirm_create_test_requirement",
                    "organization_id": organization.id,
                },
            )
            record_selected_organization(state, organization)
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content=propose_test_requirement_reply(organization),
                reason="direct_create_test_requirement_proposal",
            )

    if pending_type == "confirm_create_test_requirement":
        organization = (
            resolved_organization
            or organization_by_id(organizations, pending_action.get("organization_id"))
            or selected_organization
        )
        if organization is not None and is_affirmative(user_text):
            return handle_direct_create_test_requirement(
                db,
                current_user,
                conversation,
                user_message,
                allowed_agents,
                state,
                organization,
            )
        if is_negative(user_text):
            set_pending_action(state, None)
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content="De acuerdo, no creo el requisito de prueba.",
                reason="direct_create_test_requirement_cancelled",
            )

    last_action = state.get("last_direct_action")
    if (
        isinstance(last_action, dict)
        and last_action.get("type") == "list_requirements"
        and last_action.get("result_count") == 0
        and is_empty_requirements_followup(user_text)
    ):
        organization = organization_by_id(
            organizations,
            last_action.get("organization_id"),
        )
        organization_name = organization.name if organization else "esa organización"
        use_needs = bool(last_action.get("use_needs")) or uses_need_language(user_text)
        terms = requirement_display_terms(use_needs=use_needs)
        return persist_direct_prompt(
            db,
            conversation,
            allowed_agents,
            state,
            agent_key="consultation",
            content=(
                f"Sí. La última consulta devolvió 0 {terms['plural']} visibles en "
                f"{organization_name}; eso significa que ahora mismo no hay "
                f"{terms['plural']} {terms['registered']} o visibles ahí."
            ),
            reason="direct_empty_requirements_followup",
        )

    if (
        requirement_draft_is_complete(parsed_draft)
        and selected_organization is not None
        and recent_assistant_requested_requirement_content(conversation)
    ):
        record_selected_organization(state, selected_organization)
        return handle_direct_create_requirement(
            db,
            current_user,
            conversation,
            user_message,
            allowed_agents,
            state,
            selected_organization,
            parsed_draft,
            reason="direct_create_requirement_content_followup",
        )

    if is_list_requirements_request(user_text):
        use_needs = uses_need_language(user_text)
        if selected_organization is not None:
            return handle_direct_list_requirements(
                db,
                current_user,
                conversation,
                user_message,
                allowed_agents,
                state,
                selected_organization,
                reason="direct_list_requirements",
                use_needs=use_needs,
            )
        set_pending_action(
            state,
            {"type": "list_requirements", "use_needs": use_needs},
        )
        return persist_direct_prompt(
            db,
            conversation,
            allowed_agents,
            state,
            agent_key="consultation",
            content=organization_prompt(organizations, "list_requirements"),
            reason="direct_list_requirements_needs_organization",
        )

    if is_create_requirement_capability_question(user_text):
        return handle_direct_create_requirement_intent(
            db,
            current_user,
            conversation,
            user_message,
            allowed_agents,
            state,
            organizations,
            selected_organization,
            parsed_draft,
            reason="direct_create_requirement_capability",
        )

    if is_create_another_requirement_request(user_text):
        return handle_direct_create_requirement_intent(
            db,
            current_user,
            conversation,
            user_message,
            allowed_agents,
            state,
            organizations,
            selected_organization,
            parsed_draft,
            reason="direct_create_requirement",
        )

    if is_create_test_requirement_request(user_text):
        if selected_organization is None:
            set_pending_action(
                state,
                {"type": "create_test_requirement_organization"},
            )
            return persist_direct_prompt(
                db,
                conversation,
                allowed_agents,
                state,
                agent_key="requirements_intake",
                content=organization_prompt(organizations, "create_requirement"),
                reason="direct_create_test_requirement_needs_organization",
            )
        record_selected_organization(state, selected_organization)
        if is_user_delegating_content(user_text):
            set_pending_action(
                state,
                {
                    "type": "confirm_create_test_requirement",
                    "organization_id": selected_organization.id,
                },
            )
            content = propose_test_requirement_reply(selected_organization)
            reason = "direct_create_test_requirement_proposal"
        else:
            set_pending_action(
                state,
                {
                    "type": "create_test_requirement_content",
                    "organization_id": selected_organization.id,
                },
            )
            content = ask_for_test_requirement_content_reply(selected_organization)
            reason = "direct_create_test_requirement_needs_content"
        return persist_direct_prompt(
            db,
            conversation,
            allowed_agents,
            state,
            agent_key="requirements_intake",
            content=content,
            reason=reason,
        )

    return None


def sanitize_model_reply(text: str) -> str:
    stripped = text.strip()
    technical_markers = (
        "⚠️ No reply:",
        "No reply: the model returned empty content",
        "the model returned empty content after retries",
    )
    if any(marker in stripped for marker in technical_markers):
        return ""
    return stripped


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

    allowed_agents = get_allowed_agents(db, current_user)
    if not allowed_agents:
        raise AssistantUnavailableError("No assistant agents are available")
    direct_message = try_handle_direct_turn(
        db,
        current_user,
        conversation,
        user_message,
        user_text,
        allowed_agents,
    )
    if direct_message is not None:
        return direct_message

    transition_message = maybe_handle_context_transition(
        db,
        conversation,
        allowed_agents,
        load_conversation_state(conversation),
        user_text,
    )
    if transition_message is not None:
        return transition_message

    routing_decision = choose_agent(
        conversation=conversation,
        user_text=user_text,
        allowed_agents=allowed_agents,
    )
    agent = routing_decision.agent
    agent_tools = get_agent_tools(agent)
    agent_tool_names = frozenset(tool.name for tool in agent_tools)
    tool_definitions = [tool.definition for tool in agent_tools]

    system = build_system_prompt(db, current_user, agent, agent_tools)
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
            tools=tool_definitions,
        )

        for _ in range(settings.assistant_max_tool_iterations):
            response = recover_textual_read_tool_call(response, agent_tools, messages)

            if response.stop_reason == "refusal":
                reply_text = REFUSAL_REPLY
                break

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                response = gateway.complete(
                    system=system,
                    messages=messages,
                    tools=tool_definitions,
                )
                continue

            if response.stop_reason != "tool_use":
                reply_text = sanitize_model_reply(extract_text(response.content))
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = execute_tool(
                    db,
                    current_user,
                    block.name,
                    dict(block.input),
                    ToolContext(
                        conversation_id=conversation.id,
                        user_message_id=user_message.id,
                    ),
                    allowed=agent_tool_names,
                )
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
                tools=tool_definitions,
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

    update_pending_work_from_assistant_reply(
        conversation,
        accessible_organizations(db, current_user),
        user_text,
        reply_text,
    )

    assistant_message = AssistantMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=reply_text,
        actions=json.dumps(actions, ensure_ascii=False) if actions else None,
        agent_key=agent.key,
        routing=json.dumps(routing_decision.routing, ensure_ascii=False),
    )
    db.add(assistant_message)
    # Touch the conversation so updated_at reflects the latest activity and
    # the conversation list keeps a meaningful order (messages alone do not
    # update the parent row).
    conversation.updated_at = func.now()
    db.commit()
    db.refresh(assistant_message)
    return assistant_message
