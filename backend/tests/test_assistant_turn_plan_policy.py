from app.assistant.capabilities import ACTION_POLICIES, CAPABILITIES, policy_for_turn_plan
from app.assistant.planner import SemanticTurnPlan, TurnPlan, normalize_turn_plan


def test_semantic_turn_plan_normalizes_to_capability_action_policy():
    semantic_plan = SemanticTurnPlan(
        intent="read_requirements",
        action="none",
        target={"organization_id": 7},
        confidence=0.82,
        source="planner",
    )

    turn_plan = normalize_turn_plan(semantic_plan)

    assert turn_plan is not None
    assert isinstance(turn_plan, TurnPlan)
    assert turn_plan.intent == "read_requirements"
    assert turn_plan.capability == "requirements_or_needs"
    assert turn_plan.action == "list_requirements"
    assert turn_plan.slots["organization_id"] == 7
    assert turn_plan.missing_slots == ()
    assert turn_plan.risk_level == "low"
    assert turn_plan.requires_confirmation is False
    assert turn_plan.source == "planner"

    policy = policy_for_turn_plan(turn_plan)
    assert policy == ACTION_POLICIES["read_requirements"]
    assert policy.outcome == "execute_read_tool"


def test_mismatched_semantic_action_does_not_match_action_policy():
    semantic_plan = SemanticTurnPlan(
        intent="read_requirements",
        action="create_agent_office_task",
        confidence=0.9,
        source="planner",
    )

    turn_plan = normalize_turn_plan(semantic_plan)

    assert turn_plan is not None
    assert turn_plan.intent == "read_requirements"
    assert turn_plan.capability == "requirements_or_needs"
    assert turn_plan.action == "create_agent_office_task"
    assert policy_for_turn_plan(turn_plan) is None


def test_write_plan_keeps_policy_confirmation_even_if_planner_omits_it():
    semantic_plan = SemanticTurnPlan(
        intent="create_requirement",
        action="create_requirement",
        target={"organization_id": 7},
        draft={"title": "Control de llaves", "problem": "No hay inventario común"},
        confidence=0.91,
        requires_confirmation=False,
        source="planner",
    )

    turn_plan = normalize_turn_plan(semantic_plan)

    assert turn_plan is not None
    assert turn_plan.capability == "requirements_or_needs"
    assert turn_plan.action == "create_requirement"
    assert turn_plan.risk_level == "medium"
    assert turn_plan.requires_confirmation is True
    assert policy_for_turn_plan(turn_plan) == ACTION_POLICIES["create_requirement"]


def test_document_work_plan_is_reviewable_draft_policy():
    semantic_plan = SemanticTurnPlan(
        intent="prepare_document_work",
        action="none",
        target={"organization_id": 7, "project_id": 3},
        draft={
            "artifact_type": "checklist",
            "title": "Checklist de expediente",
            "content": "Puntos a revisar antes de firma.",
        },
        confidence=0.9,
        requires_confirmation=False,
        source="planner",
    )

    turn_plan = normalize_turn_plan(semantic_plan)

    assert turn_plan is not None
    assert turn_plan.intent == "prepare_document_work"
    assert turn_plan.capability == "document_work"
    assert turn_plan.action == "prepare_document_work"
    assert turn_plan.risk_level == "medium"
    assert turn_plan.requires_confirmation is True
    policy = policy_for_turn_plan(turn_plan)
    assert policy == ACTION_POLICIES["prepare_document_work"]
    assert policy.outcome == "create_reviewable_proposal"
    assert policy.requires_review is True
    assert policy.read_only is False


def test_capability_registry_covers_action_policies():
    assert "requirements_or_needs" in CAPABILITIES
    assert "map" in CAPABILITIES
    assert "web_research" in CAPABILITIES

    for policy in ACTION_POLICIES.values():
        capability = CAPABILITIES[policy.capability]
        assert policy.action in capability.allowed_actions
        if policy.tool_name is not None:
            assert policy.tool_name in capability.allowed_tools
