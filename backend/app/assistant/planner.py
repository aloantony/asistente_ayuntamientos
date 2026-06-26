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


@dataclass(frozen=True)
class RoutingDecision:
    agent: AgentSpec
    routing: dict


def planner_enabled() -> bool:
    return (
        settings.assistant_planner_runtime == "hermes_agent"
        and hermes_agent_enabled()
    )


def planner_healthy() -> bool | None:
    if settings.assistant_planner_runtime != "hermes_agent":
        return None
    if not hermes_agent_enabled():
        return False
    return hermes_agent_healthy(timeout=settings.hermes_agent_health_timeout_seconds)


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
