"""Declarative capability and action policy skeleton for Anacleto turns.

This module is intentionally small and data-oriented.  The assistant service can
keep its legacy direct handlers while new behavior is described through a
normalized TurnPlan plus these capability/action policies.
"""

from dataclasses import dataclass
from typing import Literal

PolicyOutcome = Literal[
    "answer_without_tool",
    "ask_clarification",
    "execute_read_tool",
    "ask_confirmation",
    "execute_confirmed_write",
    "create_reviewable_proposal",
    "create_agent_office_task",
    "block_for_safety",
    "report_denied_permission",
]

RiskLevel = Literal["low", "medium", "high"]
ReadOrWrite = Literal["read", "write", "proposal", "none"]


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    label: str
    internal_only: bool
    allowed_actions: frozenset[str]
    allowed_tools: frozenset[str]
    required_permissions_by_action: dict[str, str | None]
    slot_requirements_by_action: dict[str, frozenset[str]]
    read_or_write_by_action: dict[str, ReadOrWrite]
    confirmation_policy_by_action: dict[str, str]
    review_policy_by_action: dict[str, str]
    risk_level_by_action: dict[str, RiskLevel]


@dataclass(frozen=True)
class ActionPolicy:
    intent: str
    agent_key: str
    tool_name: str | None
    reason: str
    capability: str = "general"
    action: str = "none"
    outcome: PolicyOutcome = "execute_read_tool"
    required_slots: frozenset[str] = frozenset()
    risk_level: RiskLevel = "low"
    requires_confirmation: bool = False
    requires_review: bool = False
    read_only: bool = True
    required_permission: str | None = None


CAPABILITIES: dict[str, CapabilitySpec] = {
    "general": CapabilitySpec(
        key="general",
        label="Conversación general",
        internal_only=False,
        allowed_actions=frozenset({"none"}),
        allowed_tools=frozenset(),
        required_permissions_by_action={"none": None},
        slot_requirements_by_action={"none": frozenset()},
        read_or_write_by_action={"none": "none"},
        confirmation_policy_by_action={"none": "never"},
        review_policy_by_action={"none": "never"},
        risk_level_by_action={"none": "low"},
    ),
    "platform_help": CapabilitySpec(
        key="platform_help",
        label="Ayuda sobre capacidades de la plataforma",
        internal_only=False,
        allowed_actions=frozenset({"none"}),
        allowed_tools=frozenset(),
        required_permissions_by_action={"none": "assistant.use"},
        slot_requirements_by_action={"none": frozenset()},
        read_or_write_by_action={"none": "none"},
        confirmation_policy_by_action={"none": "never"},
        review_policy_by_action={"none": "never"},
        risk_level_by_action={"none": "low"},
    ),
    "requirements_or_needs": CapabilitySpec(
        key="requirements_or_needs",
        label="Necesidades y requisitos",
        internal_only=False,
        allowed_actions=frozenset(
            {
                "none",
                "list_requirements",
                "capture_requirement",
                "capture_requirement_intro",
                "create_requirement",
                "confirm_pending_work",
                "retry_pending_action",
                "cancel_pending_action",
            }
        ),
        allowed_tools=frozenset(
            {"list_requirements", "get_requirement", "create_requirement"}
        ),
        required_permissions_by_action={
            "none": "assistant.use",
            "list_requirements": "requirements.view",
            "capture_requirement": "assistant.use",
            "capture_requirement_intro": "assistant.use",
            "create_requirement": "requirements.create",
            "confirm_pending_work": "requirements.create",
            "retry_pending_action": "assistant.use",
            "cancel_pending_action": "assistant.use",
        },
        slot_requirements_by_action={
            "none": frozenset(),
            "list_requirements": frozenset({"organization_id"}),
            "capture_requirement": frozenset({"organization_id"}),
            "capture_requirement_intro": frozenset(),
            "create_requirement": frozenset({"organization_id", "title", "problem"}),
            "confirm_pending_work": frozenset(),
            "retry_pending_action": frozenset(),
            "cancel_pending_action": frozenset(),
        },
        read_or_write_by_action={
            "none": "none",
            "list_requirements": "read",
            "capture_requirement": "read",
            "capture_requirement_intro": "none",
            "create_requirement": "write",
            "confirm_pending_work": "write",
            "retry_pending_action": "write",
            "cancel_pending_action": "none",
        },
        confirmation_policy_by_action={
            "none": "never",
            "list_requirements": "never",
            "capture_requirement": "never",
            "capture_requirement_intro": "never",
            "create_requirement": "when_missing_prior_confirmation",
            "confirm_pending_work": "already_confirmed_by_user",
            "retry_pending_action": "already_confirmed_by_user",
            "cancel_pending_action": "never",
        },
        review_policy_by_action={
            "none": "never",
            "list_requirements": "never",
            "capture_requirement": "never",
            "capture_requirement_intro": "never",
            "create_requirement": "draft_human_review",
            "confirm_pending_work": "draft_human_review",
            "retry_pending_action": "preserve_existing_policy",
            "cancel_pending_action": "never",
        },
        risk_level_by_action={
            "none": "low",
            "list_requirements": "low",
            "capture_requirement": "low",
            "capture_requirement_intro": "low",
            "create_requirement": "medium",
            "confirm_pending_work": "medium",
            "retry_pending_action": "medium",
            "cancel_pending_action": "low",
        },
    ),
    "ordinances": CapabilitySpec(
        key="ordinances",
        label="Ordenanzas y normativa municipal aprobada",
        internal_only=False,
        allowed_actions=frozenset({"semantic_search_ordinances"}),
        allowed_tools=frozenset({"semantic_search_ordinances"}),
        required_permissions_by_action={
            "semantic_search_ordinances": "ordinances.compare"
        },
        slot_requirements_by_action={
            "semantic_search_ordinances": frozenset({"query"})
        },
        read_or_write_by_action={"semantic_search_ordinances": "read"},
        confirmation_policy_by_action={"semantic_search_ordinances": "never"},
        review_policy_by_action={"semantic_search_ordinances": "approved_sources_only"},
        risk_level_by_action={"semantic_search_ordinances": "medium"},
    ),
    "map": CapabilitySpec(
        key="map",
        label="Mapa municipal",
        internal_only=False,
        allowed_actions=frozenset({"get_map_items"}),
        allowed_tools=frozenset({"get_map_items"}),
        required_permissions_by_action={"get_map_items": "map.view"},
        slot_requirements_by_action={"get_map_items": frozenset()},
        read_or_write_by_action={"get_map_items": "read"},
        confirmation_policy_by_action={"get_map_items": "never"},
        review_policy_by_action={"get_map_items": "never"},
        risk_level_by_action={"get_map_items": "low"},
    ),
    "web_research": CapabilitySpec(
        key="web_research",
        label="Búsqueda web pública controlada",
        internal_only=False,
        allowed_actions=frozenset({"web_search"}),
        allowed_tools=frozenset({"web_search"}),
        required_permissions_by_action={"web_search": "assistant.web.search"},
        slot_requirements_by_action={"web_search": frozenset({"query"})},
        read_or_write_by_action={"web_search": "read"},
        confirmation_policy_by_action={"web_search": "privacy_gate"},
        review_policy_by_action={"web_search": "cite_sources"},
        risk_level_by_action={"web_search": "medium"},
    ),
    "knowledge_sources": CapabilitySpec(
        key="knowledge_sources",
        label="Fuentes y conocimiento revisable",
        internal_only=False,
        allowed_actions=frozenset({"propose_knowledge_entry"}),
        allowed_tools=frozenset({"propose_knowledge_entry", "propose_memory_entry"}),
        required_permissions_by_action={
            "propose_knowledge_entry": "assistant.knowledge.propose"
        },
        slot_requirements_by_action={
            "propose_knowledge_entry": frozenset({"organization_id", "source_url"})
        },
        read_or_write_by_action={"propose_knowledge_entry": "proposal"},
        confirmation_policy_by_action={"propose_knowledge_entry": "user_requested"},
        review_policy_by_action={"propose_knowledge_entry": "human_review_required"},
        risk_level_by_action={"propose_knowledge_entry": "medium"},
    ),
    "document_work": CapabilitySpec(
        key="document_work",
        label="Borradores documentales revisables",
        internal_only=False,
        allowed_actions=frozenset({"prepare_document_work"}),
        allowed_tools=frozenset({"prepare_document_work"}),
        required_permissions_by_action={
            "prepare_document_work": "documents.draft"
        },
        slot_requirements_by_action={
            "prepare_document_work": frozenset({"project_id", "title", "content"})
        },
        read_or_write_by_action={"prepare_document_work": "proposal"},
        confirmation_policy_by_action={"prepare_document_work": "user_requested"},
        review_policy_by_action={"prepare_document_work": "human_review_required"},
        risk_level_by_action={"prepare_document_work": "medium"},
    ),
    "admin_feedback": CapabilitySpec(
        key="admin_feedback",
        label="Feedback interno para administración",
        internal_only=True,
        allowed_actions=frozenset({"send_admin_feedback"}),
        allowed_tools=frozenset({"send_admin_feedback"}),
        required_permissions_by_action={"send_admin_feedback": "assistant.use"},
        slot_requirements_by_action={
            "send_admin_feedback": frozenset({"category", "title", "description"})
        },
        read_or_write_by_action={"send_admin_feedback": "write"},
        confirmation_policy_by_action={"send_admin_feedback": "explicit_user_confirmation"},
        review_policy_by_action={"send_admin_feedback": "admin_review"},
        risk_level_by_action={"send_admin_feedback": "medium"},
    ),
    "agent_office": CapabilitySpec(
        key="agent_office",
        label="Oficina interna de agentes supervisados",
        internal_only=True,
        allowed_actions=frozenset({"create_agent_office_task"}),
        allowed_tools=frozenset({"create_agent_office_task"}),
        required_permissions_by_action={
            "create_agent_office_task": "agent_office.create"
        },
        slot_requirements_by_action={
            "create_agent_office_task": frozenset({"organization_id", "title", "description"})
        },
        read_or_write_by_action={"create_agent_office_task": "write"},
        confirmation_policy_by_action={"create_agent_office_task": "user_requested"},
        review_policy_by_action={"create_agent_office_task": "human_approval_gate"},
        risk_level_by_action={"create_agent_office_task": "medium"},
    ),
}


ACTION_POLICIES: dict[str, ActionPolicy] = {
    "global_capabilities": ActionPolicy(
        intent="global_capabilities",
        agent_key="requirements_intake",
        tool_name=None,
        reason="action_policy_global_capabilities",
        capability="platform_help",
        action="none",
        outcome="answer_without_tool",
    ),
    "capture_requirement_intro": ActionPolicy(
        intent="capture_requirement_intro",
        agent_key="requirements_intake",
        tool_name=None,
        reason="action_policy_capture_requirement_intro",
        capability="requirements_or_needs",
        action="none",
        outcome="answer_without_tool",
    ),
    "read_map_items": ActionPolicy(
        intent="read_map_items",
        agent_key="consultation",
        tool_name="get_map_items",
        reason="action_policy_read_map_items",
        capability="map",
        action="get_map_items",
        outcome="execute_read_tool",
        required_permission="map.view",
    ),
    "read_requirements": ActionPolicy(
        intent="read_requirements",
        agent_key="consultation",
        tool_name="list_requirements",
        reason="action_policy_read_requirements",
        capability="requirements_or_needs",
        action="list_requirements",
        outcome="execute_read_tool",
        required_slots=frozenset({"organization_id"}),
        required_permission="requirements.view",
    ),
    "read_ordinances": ActionPolicy(
        intent="read_ordinances",
        agent_key="consultation",
        tool_name="semantic_search_ordinances",
        reason="action_policy_read_ordinances",
        capability="ordinances",
        action="semantic_search_ordinances",
        outcome="execute_read_tool",
        required_slots=frozenset({"query"}),
        risk_level="medium",
        required_permission="ordinances.compare",
    ),
    "capture_requirement": ActionPolicy(
        intent="capture_requirement",
        agent_key="requirements_intake",
        tool_name="list_requirements",
        reason="action_policy_capture_requirement",
        capability="requirements_or_needs",
        action="list_requirements",
        outcome="execute_read_tool",
        required_slots=frozenset({"organization_id"}),
    ),
    "create_requirement": ActionPolicy(
        intent="create_requirement",
        agent_key="requirements_intake",
        tool_name="create_requirement",
        reason="action_policy_create_requirement",
        capability="requirements_or_needs",
        action="create_requirement",
        outcome="execute_confirmed_write",
        required_slots=frozenset({"organization_id", "title", "problem"}),
        risk_level="medium",
        requires_confirmation=True,
        requires_review=True,
        read_only=False,
        required_permission="requirements.create",
    ),
    "confirm_pending_work": ActionPolicy(
        intent="confirm_pending_work",
        agent_key="requirements_intake",
        tool_name="create_requirement",
        reason="action_policy_confirm_pending_work",
        capability="requirements_or_needs",
        action="confirm_pending_work",
        outcome="execute_confirmed_write",
        risk_level="medium",
        requires_review=True,
        read_only=False,
        required_permission="requirements.create",
    ),
    "retry_pending_action": ActionPolicy(
        intent="retry_pending_action",
        agent_key="requirements_intake",
        tool_name=None,
        reason="action_policy_retry_pending_action",
        capability="requirements_or_needs",
        action="retry_pending_action",
        outcome="execute_confirmed_write",
        risk_level="medium",
        read_only=False,
    ),
    "cancel_pending_action": ActionPolicy(
        intent="cancel_pending_action",
        agent_key="requirements_intake",
        tool_name=None,
        reason="action_policy_cancel_pending_action",
        capability="requirements_or_needs",
        action="cancel_pending_action",
        outcome="answer_without_tool",
    ),
    "delegate_agent_office": ActionPolicy(
        intent="delegate_agent_office",
        agent_key="requirements_intake",
        tool_name="create_agent_office_task",
        reason="action_policy_delegate_agent_office",
        capability="agent_office",
        action="create_agent_office_task",
        outcome="create_agent_office_task",
        required_slots=frozenset({"organization_id", "title", "description"}),
        risk_level="medium",
        requires_review=True,
        read_only=False,
        required_permission="agent_office.create",
    ),
    "prepare_document_work": ActionPolicy(
        intent="prepare_document_work",
        agent_key="requirements_intake",
        tool_name="prepare_document_work",
        reason="action_policy_prepare_document_work",
        capability="document_work",
        action="prepare_document_work",
        outcome="create_reviewable_proposal",
        required_slots=frozenset({"project_id", "title", "content"}),
        risk_level="medium",
        requires_confirmation=True,
        requires_review=True,
        read_only=False,
        required_permission="documents.draft",
    ),
    "suggest_admin_feedback": ActionPolicy(
        intent="suggest_admin_feedback",
        agent_key="requirements_intake",
        tool_name="send_admin_feedback",
        reason="action_policy_suggest_admin_feedback",
        capability="admin_feedback",
        action="send_admin_feedback",
        outcome="ask_confirmation",
        required_slots=frozenset({"category", "title", "description"}),
        risk_level="medium",
        requires_confirmation=True,
        requires_review=True,
        read_only=False,
        required_permission="assistant.use",
    ),
    "external_research": ActionPolicy(
        intent="external_research",
        agent_key="consultation",
        tool_name="web_search",
        reason="action_policy_external_research",
        capability="web_research",
        action="web_search",
        outcome="execute_read_tool",
        required_slots=frozenset({"query"}),
        risk_level="medium",
        required_permission="assistant.web.search",
    ),
    "answer_with_web_research": ActionPolicy(
        intent="answer_with_web_research",
        agent_key="consultation",
        tool_name="web_search",
        reason="action_policy_external_research",
        capability="web_research",
        action="web_search",
        outcome="execute_read_tool",
        required_slots=frozenset({"query"}),
        risk_level="medium",
        required_permission="assistant.web.search",
    ),
    "save_last_research": ActionPolicy(
        intent="save_last_research",
        agent_key="requirements_intake",
        tool_name="propose_knowledge_entry",
        reason="action_policy_propose_knowledge_entry",
        capability="knowledge_sources",
        action="propose_knowledge_entry",
        outcome="create_reviewable_proposal",
        required_slots=frozenset({"organization_id", "source_url"}),
        risk_level="medium",
        requires_review=True,
        read_only=False,
        required_permission="assistant.memory.propose",
    ),
    "propose_knowledge": ActionPolicy(
        intent="propose_knowledge",
        agent_key="requirements_intake",
        tool_name="propose_knowledge_entry",
        reason="action_policy_propose_knowledge_entry",
        capability="knowledge_sources",
        action="propose_knowledge_entry",
        outcome="create_reviewable_proposal",
        required_slots=frozenset({"organization_id", "source_url"}),
        risk_level="medium",
        requires_review=True,
        read_only=False,
        required_permission="assistant.memory.propose",
    ),
}


def action_policy_for_intent(intent: str | None) -> ActionPolicy | None:
    if not intent:
        return None
    return ACTION_POLICIES.get(str(intent).strip())


def capability_for_intent(intent: str | None) -> str:
    policy = action_policy_for_intent(intent)
    if policy is not None:
        return policy.capability
    return "general"


def default_action_for_intent(intent: str | None) -> str:
    policy = action_policy_for_intent(intent)
    if policy is not None:
        return policy.action
    return "none"


def policy_for_turn_plan(plan: object | None) -> ActionPolicy | None:
    if plan is None:
        return None
    intent = str(getattr(plan, "intent", "") or "").strip()
    action = str(getattr(plan, "action", "") or "none").strip() or "none"
    capability = str(getattr(plan, "capability", "") or capability_for_intent(intent)).strip()
    policy = action_policy_for_intent(intent)
    if policy is None or capability != policy.capability:
        return None
    if action in {"", "none"}:
        return policy if policy.action == "none" else None
    if action == policy.action:
        return policy
    if policy.tool_name is not None and action == policy.tool_name:
        return policy
    return None
