"""Hermes-backed agent routing for the assistant.

Hermes can choose an agent, but it never executes product actions. The backend
keeps the allowed-agent set, tool ceiling, RBAC checks and audit trail.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from app.assistant.agents import AGENT_REGISTRY, AgentSpec
from app.assistant.gateway import (
    AssistantUnavailableError,
    AITextBlock,
    AIToolUseBlock,
    complete_hermes_agent,
    hermes_agent_enabled,
    hermes_agent_healthy,
)
from app.assistant.models import AssistantConversation
from app.core.config import settings

logger = logging.getLogger(__name__)

RoutingSource = Literal["router", "shortcut", "disabled", "fallback"]

ROUTER_SYSTEM_PROMPT = """Eres el router interno del asistente municipal.

Tu única tarea es elegir el agente más adecuado entre los candidatos permitidos.
No respondas al usuario. No llames herramientas de producto. Devuelve siempre
una llamada a la herramienta route con un agent_key permitido.

Reglas:
- requirements_intake: cuando el usuario quiere capturar, crear, modificar,
  completar, enviar, recordar o reutilizar una necesidad/requisito.
- consultation: cuando el usuario solo pide leer, consultar, resumir o buscar
  información sin cambiar datos.
- Si hay duda, elige requirements_intake.
"""

SEMANTIC_PLANNER_SYSTEM_PROMPT = """Eres el planificador semántico interno del asistente municipal.

Tu tarea no es responder al usuario, sino convertir el último mensaje en una
intención estructurada que el backend pueda validar y ejecutar. No llames
herramientas de producto. Devuelve siempre una llamada a plan_turn.

Reglas:
- Si el usuario pregunta si existe una capacidad o disponibilidad genérica
  (por ejemplo “¿hay mapa municipal?”, “¿tienes ordenanzas?”, “¿la plataforma
  soporta necesidades registradas?”) y no pide listar/consultar/comparar un dato
  concreto, usa intent=global_capabilities y action=none.
- Si pide comparar o consultar ordenanzas/reglamentos/normativa municipal, usa
  intent=read_ordinances y action=semantic_search_ordinances.
- Si pide conclusiones de conjunto/subconjunto, patrones, cobertura, materias
  frecuentes o visión agregada del corpus de ordenanzas, usa
  intent=analyze_ordinances y action=analyze_ordinance_corpus.
- Si pide listar, mostrar, consultar, comparar o localizar datos concretos de
  mapa, necesidades u ordenanzas, usa la intención de lectura correspondiente.
- Si pide consultar elementos geolocalizados del mapa municipal, usa
  intent=read_map_items y action=get_map_items.
- Si el usuario pide ver necesidades/requisitos ya registrados, usa intent=read_requirements.
- Si el usuario pide empezar a contar una necesidad/requisito pero aún no aporta contenido concreto, usa intent=capture_requirement_intro y action=none.
- Si el usuario quiere explorar, explicar o aterrizar una necesidad antes de guardarla, usa intent=capture_requirement y action=list_requirements para comprobar posibles duplicados/contexto.
- Si el usuario describe una nueva necesidad o algo que quiere desarrollar y aporta campos suficientes para borrador,
  usa intent=create_requirement, pero no inventes campos que no estén claros.
- Si confirma un borrador pendiente, usa intent=confirm_pending_work.
- Si pide convertir el feedback o la mejora anterior en necesidad, usa
  intent=convert_feedback_to_requirement, action=create_requirement y
  reference=last_admin_feedback.
- Si pide reintentar una acción pendiente o fallida, usa intent=retry_pending_action.
- Si cancela, descarta o deja sin efecto una acción pendiente, usa intent=cancel_pending_action y action=cancel_pending_action.
- Si pide preparar, encargar o dejar para revisión una tarea supervisada o
  diferida, usa intent=delegate_agent_office y action=create_agent_office_task.
- Si pide que el asistente ejecute una capacidad que no existe o no debe prometer
  todavía, usa intent=unsupported_capability y action=none; incluye draft con
  title y problem para convertirlo en necesidad si el usuario acepta.
- Si describe un fallo, fricción, problema de datos o mejora de la plataforma/asistente que conviene elevar al administrador, usa intent=suggest_admin_feedback y action=send_admin_feedback; incluye draft con category, title, description y priority si están claros.
- Si hay pending_action de send_admin_feedback y el usuario confirma enviarlo, usa intent=suggest_admin_feedback y action=send_admin_feedback.
- Si solo pregunta qué puede hacer el asistente, usa intent=global_capabilities.
- Si no hay intención de producto clara, usa intent=unknown y action=none.

El backend decide permisos, visibilidad, duplicados y ejecución real.
"""


@dataclass(frozen=True)
class RoutingDecision:
    agent: AgentSpec
    routing: dict


@dataclass(frozen=True)
class SemanticTurnPlan:
    intent: str
    action: str = "none"
    query: str | None = None
    target: dict | None = None
    draft: dict | None = None
    delegation: dict | None = None
    reference: str | None = None
    confidence: float = 0.0
    source: str = "planner"

    def as_routing_payload(self) -> dict:
        payload: dict[str, object] = {
            "intent": self.intent,
            "action": self.action,
            "confidence": self.confidence,
            "source": self.source,
        }
        if self.query:
            payload["query"] = self.query
        if isinstance(self.target, dict) and self.target:
            payload["target"] = self.target
        if isinstance(self.draft, dict) and self.draft:
            payload["draft"] = self.draft
        if isinstance(self.delegation, dict) and self.delegation:
            payload["delegation"] = self.delegation
        if self.reference:
            payload["reference"] = self.reference
        return payload


def effective_planner_runtime() -> str:
    """Return the planner runtime the assistant must use for this deployment.

    Older local `.env` files may still carry `ASSISTANT_PLANNER_RUNTIME=disabled`.
    For the Hermes-backed assistant runtime, the semantic planner is no longer an
    optional layer: without it Anacleto falls back to rigid shortcuts and feels
    like a scripted automaton even when the underlying model is strong.
    """
    if settings.assistant_planner_runtime == "hermes_agent":
        return "hermes_agent"
    if settings.assistant_runtime == "hermes_agent":
        return "hermes_agent"
    return "disabled"


def planner_enabled() -> bool:
    return effective_planner_runtime() == "hermes_agent" and hermes_agent_enabled()


def planner_healthy() -> bool | None:
    if effective_planner_runtime() != "hermes_agent":
        return None
    if not hermes_agent_enabled():
        return False
    return hermes_agent_healthy(timeout=settings.hermes_agent_health_timeout_seconds)


def plan_turn(
    *,
    conversation: AssistantConversation,
    user_text: str,
    context: dict | None = None,
) -> SemanticTurnPlan | None:
    if not planner_enabled():
        return None
    try:
        return _plan_with_hermes(
            conversation=conversation,
            user_text=user_text,
            context=context or {},
        )
    except AssistantUnavailableError:
        logger.warning("Hermes semantic planner failed; continuing without plan")
        return None


def choose_agent(
    *,
    conversation: AssistantConversation,
    user_text: str,
    allowed_agents: list[AgentSpec],
) -> RoutingDecision:
    candidates = [agent.key for agent in allowed_agents]
    if not allowed_agents:
        raise ValueError("allowed_agents cannot be empty")

    previous_agent_key = _previous_agent_key(conversation)
    if len(allowed_agents) == 1:
        return _decision(
            allowed_agents[0],
            candidates=candidates,
            source="shortcut",
            reason="single_allowed_agent",
            previous_agent_key=previous_agent_key,
        )

    previous_agent = _agent_by_key(previous_agent_key, allowed_agents)
    if previous_agent is not None and _is_short_followup(user_text):
        return _decision(
            previous_agent,
            candidates=candidates,
            source="shortcut",
            reason="short_followup_previous_agent",
            previous_agent_key=previous_agent_key,
        )

    if not planner_enabled():
        return _fallback_decision(
            allowed_agents,
            candidates=candidates,
            source="disabled",
            reason="planner_disabled",
            previous_agent_key=previous_agent_key,
            user_text=user_text,
        )

    try:
        routed_agent_key = _route_with_hermes(
            conversation=conversation,
            user_text=user_text,
            allowed_agents=allowed_agents,
        )
    except AssistantUnavailableError:
        logger.warning("Hermes planner failed; falling back to default agent")
        return _fallback_decision(
            allowed_agents,
            candidates=candidates,
            source="fallback",
            reason="planner_unavailable",
            previous_agent_key=previous_agent_key,
            user_text=user_text,
        )

    routed_agent = _agent_by_key(routed_agent_key, allowed_agents)
    if routed_agent is None:
        return _fallback_decision(
            allowed_agents,
            candidates=candidates,
            source="fallback",
            reason="invalid_agent_key",
            previous_agent_key=previous_agent_key,
            raw_agent_key=routed_agent_key,
            user_text=user_text,
        )

    heuristic_agent = _heuristic_agent(user_text, allowed_agents)
    if heuristic_agent is not None and heuristic_agent.key != routed_agent.key:
        return _decision(
            heuristic_agent,
            candidates=candidates,
            source="shortcut",
            reason="heuristic_override",
            previous_agent_key=previous_agent_key,
            raw_agent_key=routed_agent_key,
        )

    return _decision(
        routed_agent,
        candidates=candidates,
        source="router",
        reason=None,
        previous_agent_key=previous_agent_key,
    )


def _plan_with_hermes(
    *,
    conversation: AssistantConversation,
    user_text: str,
    context: dict,
) -> SemanticTurnPlan | None:
    plan_tool = {
        "name": "plan_turn",
        "description": "Devuelve la intención estructurada del siguiente turno.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "global_capabilities",
                        "read_ordinances",
                        "analyze_ordinances",
                        "read_map_items",
                        "read_requirements",
                        "capture_requirement_intro",
                        "capture_requirement",
                        "create_requirement",
                        "confirm_pending_work",
                        "convert_feedback_to_requirement",
                        "retry_pending_action",
                        "cancel_pending_action",
                        "delegate_agent_office",
                        "unsupported_capability",
                        "suggest_admin_feedback",
                        "unknown",
                    ],
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "none",
                        "semantic_search_ordinances",
                        "analyze_ordinance_corpus",
                        "get_map_items",
                        "list_requirements",
                        "create_requirement",
                        "confirm_pending_work",
                        "retry_pending_action",
                        "cancel_pending_action",
                        "create_agent_office_task",
                        "send_admin_feedback",
                    ],
                },
                "query": {"type": "string"},
                "target": {"type": "object"},
                "draft": {"type": "object"},
                "delegation": {"type": "object"},
                "reference": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["intent", "action", "confidence"],
        },
    }
    messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "recent_history": _recent_history(conversation),
                    "conversation_context": context,
                    "new_user_message": user_text,
                },
                ensure_ascii=False,
            ),
        }
    ]
    completion = complete_hermes_agent(
        system=SEMANTIC_PLANNER_SYSTEM_PROMPT,
        messages=messages,
        tools=[plan_tool],
        model=settings.assistant_planner_model,
        max_tokens=settings.assistant_planner_max_tokens,
        timeout=settings.assistant_planner_timeout_seconds,
        tool_choice={
            "type": "function",
            "function": {"name": "plan_turn"},
        },
        log_context="assistant_semantic_planner",
    )

    for block in completion.content:
        if isinstance(block, AIToolUseBlock) and block.name == "plan_turn":
            return _semantic_plan_from_payload(block.input)
    for block in completion.content:
        if isinstance(block, AITextBlock):
            parsed = _extract_json(block.text)
            if parsed is not None:
                return _semantic_plan_from_payload(parsed)
    return None


def _semantic_plan_from_payload(payload: object) -> SemanticTurnPlan | None:
    if not isinstance(payload, dict):
        return None
    intent = str(payload.get("intent") or "unknown").strip() or "unknown"
    action = str(payload.get("action") or "none").strip() or "none"
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    target = payload.get("target") if isinstance(payload.get("target"), dict) else None
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else None
    delegation = payload.get("delegation") if isinstance(payload.get("delegation"), dict) else None
    query = payload.get("query")
    reference = payload.get("reference")
    return SemanticTurnPlan(
        intent=intent,
        action=action,
        query=str(query).strip() if query else None,
        target=target,
        draft=draft,
        delegation=delegation,
        reference=str(reference).strip() if reference else None,
        confidence=confidence,
        source="planner",
    )


def _route_with_hermes(
    *,
    conversation: AssistantConversation,
    user_text: str,
    allowed_agents: list[AgentSpec],
) -> str | None:
    route_tool = {
        "name": "route",
        "description": "Selecciona el agente que debe atender el siguiente turno.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_key": {
                    "type": "string",
                    "enum": [agent.key for agent in allowed_agents],
                }
            },
            "required": ["agent_key"],
        },
    }
    agent_lines = "\n".join(
        f"- {agent.key}: {agent.description}" for agent in allowed_agents
    )
    history = _recent_history(conversation)
    messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "allowed_agents": [agent.metadata for agent in allowed_agents],
                    "recent_history": history,
                    "new_user_message": user_text,
                    "agent_descriptions": agent_lines,
                },
                ensure_ascii=False,
            ),
        }
    ]
    completion = complete_hermes_agent(
        system=ROUTER_SYSTEM_PROMPT,
        messages=messages,
        tools=[route_tool],
        model=settings.assistant_planner_model,
        max_tokens=settings.assistant_planner_max_tokens,
        timeout=settings.assistant_planner_timeout_seconds,
        tool_choice={
            "type": "function",
            "function": {"name": "route"},
        },
        log_context="assistant_planner",
    )

    for block in completion.content:
        if isinstance(block, AIToolUseBlock) and block.name == "route":
            return str(block.input.get("agent_key") or "")
    for block in completion.content:
        if isinstance(block, AITextBlock):
            parsed = _extract_json(block.text)
            if parsed is not None:
                return str(parsed.get("agent_key") or "")
    return None


def _recent_history(conversation: AssistantConversation) -> list[dict]:
    messages = [
        message
        for message in conversation.messages
        if message.content
    ][-6:]
    return [
        {
            "role": message.role,
            "content": message.content[:1200],
            "agent_key": message.agent_key,
        }
        for message in messages
    ]


def _previous_agent_key(conversation: AssistantConversation) -> str | None:
    for message in reversed(conversation.messages):
        if message.role == "assistant" and message.agent_key:
            return message.agent_key
    return None


def _agent_by_key(key: str | None, allowed_agents: list[AgentSpec]) -> AgentSpec | None:
    if key is None:
        return None
    return next((agent for agent in allowed_agents if agent.key == key), None)


def _is_short_followup(text: str) -> bool:
    normalized = text.strip().lower()
    return 0 < len(normalized) <= 20


def _fallback_decision(
    allowed_agents: list[AgentSpec],
    *,
    candidates: list[str],
    source: RoutingSource,
    reason: str,
    previous_agent_key: str | None,
    user_text: str,
    raw_agent_key: str | None = None,
) -> RoutingDecision:
    heuristic_agent = _heuristic_agent(user_text, allowed_agents)
    agent = (
        heuristic_agent
        or _agent_by_key(previous_agent_key, allowed_agents)
        or _agent_by_key("requirements_intake", allowed_agents)
        or allowed_agents[0]
    )
    return _decision(
        agent,
        candidates=candidates,
        source=source,
        reason=reason,
        previous_agent_key=previous_agent_key,
        raw_agent_key=raw_agent_key,
    )


def _decision(
    agent: AgentSpec,
    *,
    candidates: list[str],
    source: RoutingSource,
    reason: str | None,
    previous_agent_key: str | None,
    raw_agent_key: str | None = None,
) -> RoutingDecision:
    routing = {
        "candidates": candidates,
        "chosen": agent.key,
        "source": source,
        "previous_agent_key": previous_agent_key,
    }
    if reason:
        routing["fallback_reason"] = reason
    if raw_agent_key:
        routing["raw_agent_key"] = raw_agent_key
    return RoutingDecision(agent=agent, routing=routing)


def _heuristic_agent(
    user_text: str,
    allowed_agents: list[AgentSpec],
) -> AgentSpec | None:
    normalized = user_text.strip().lower()
    general_chat_markers = (
        "hola",
        "hol",
        "hey",
        "buenas",
        "qué tal",
        "que tal",
        "probando",
        "prueba",
    )
    write_markers = (
        "crea",
        "crear",
        "captura",
        "capturar",
        "actualiza",
        "modifica",
        "guarda",
        "registra",
        "registrar",
        "enviar",
        "envía",
        "envia",
        "quiero que",
        "queremos que",
        "necesito que",
        "necesitamos que",
        "necesito",
        "necesitamos",
        "me gustaría que",
        "me gustaria que",
        "debería",
        "deberia",
        "pueda",
        "puedan",
        "permita",
        "permitan",
        "se puedan",
        "sistema pueda",
        "sistema permita",
    )
    read_markers = (
        "consulta",
        "consultar",
        "listar",
        "lista",
        "ver",
        "tenemos",
        "registrados",
        "hay",
    )
    if _contains_marker(normalized, general_chat_markers):
        return _agent_by_key("consultation", allowed_agents)
    if _contains_marker(normalized, write_markers):
        return _agent_by_key("requirements_intake", allowed_agents)
    if _contains_marker(normalized, read_markers):
        return _agent_by_key("consultation", allowed_agents)
    return None


def _contains_marker(normalized_text: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        if " " in marker:
            if marker in normalized_text:
                return True
            continue
        if re.search(rf"\b{re.escape(marker)}\b", normalized_text):
            return True
    return False


def _extract_json(text: str) -> dict | None:
    stripped = text.strip()
    if not stripped:
        return None
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


DEFAULT_AGENT = AGENT_REGISTRY["requirements_intake"]
