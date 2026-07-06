from app.assistant.evaluations import ASSISTANT_MVP_EVALUATION_SCENARIOS
from app.rbac.permissions import INITIAL_PERMISSION_DEFINITIONS


EXPECTED_SCENARIO_CATEGORIES = {
    "capabilities",
    "internal_read",
    "unsupported_action",
    "web_research",
    "save_proposal",
    "need_creation",
    "ordinance_coverage",
    "map_query",
    "document_draft",
    "agent_office_delegation",
    "privacy_block",
}


MVP_SECURITY_PERMISSION_CODES = {
    "assistant.knowledge.propose",
    "assistant.knowledge.view",
    "assistant.knowledge.review",
    "assistant.drafts.create",
    "assistant.drafts.view",
    "assistant.drafts.review",
    "assistant.documents.summarize",
    "assistant.external.integrations",
    "assistant.debug.view_actions",
}


def test_mvp_evaluation_scenarios_cover_security_observability_quality_gates():
    scenarios = ASSISTANT_MVP_EVALUATION_SCENARIOS

    assert {scenario.category for scenario in scenarios} >= EXPECTED_SCENARIO_CATEGORIES
    assert all(scenario.key and scenario.user_message for scenario in scenarios)
    assert all(scenario.expected_intent for scenario in scenarios)
    assert all(scenario.quality_gates for scenario in scenarios)
    assert all(
        "no_internal_agent_leak" in scenario.quality_gates
        for scenario in scenarios
    )

    mutating = [scenario for scenario in scenarios if scenario.mutating]
    assert mutating
    assert all(scenario.required_permission for scenario in mutating)
    assert all(
        scenario.requires_confirmation or scenario.requires_review
        for scenario in mutating
    )

    privacy_blocks = [
        scenario for scenario in scenarios if scenario.category == "privacy_block"
    ]
    assert privacy_blocks
    assert all(scenario.must_not_call_web for scenario in privacy_blocks)
    assert all(
        "no_private_content_in_logs" in scenario.quality_gates
        for scenario in privacy_blocks
    )


def test_security_permission_family_is_seeded_for_mvp_controls():
    assert MVP_SECURITY_PERMISSION_CODES.issubset(INITIAL_PERMISSION_DEFINITIONS)
