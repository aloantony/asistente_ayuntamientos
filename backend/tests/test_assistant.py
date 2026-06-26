import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.assistant import planner as assistant_planner
from app.assistant import tools as assistant_tools
from app.assistant import service as assistant_service
from app.assistant.agents import AGENT_REGISTRY, get_agent_tools
from app.assistant.gateway import _from_openai_response, _hermes_agent_url
from app.assistant.models import (
    AssistantConversation,
    AssistantMemoryEntry,
    AssistantMessage,
    AssistantTransversalFeature,
    AssistantTransversalFeatureAdoption,
)
from app.assistant.planner import choose_agent
from app.assistant.routes import get_gateway
from app.core.config import settings
from app.main import app
from app.requirements.models import Requirement
from conftest import headers_for


class FakeTextBlock(SimpleNamespace):
    pass


class FakeToolUseBlock(SimpleNamespace):
    pass


def text_block(text: str) -> FakeTextBlock:
    return FakeTextBlock(type="text", text=text)


def tool_use_block(block_id: str, name: str, tool_input: dict) -> FakeToolUseBlock:
    return FakeToolUseBlock(type="tool_use", id=block_id, name=name, input=tool_input)


class FakeGateway:
    def __init__(
        self,
        responses: list,
        enabled: bool = True,
        runtime_healthy: bool | None = None,
    ):
        self.enabled = enabled
        self.runtime_healthy = runtime_healthy
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("FakeGateway ran out of scripted responses")
        return self.responses.pop(0)


def fake_response(stop_reason: str, content: list):
    return SimpleNamespace(stop_reason=stop_reason, content=content)


@pytest.fixture()
def use_gateway(client):
    def _use(gateway: FakeGateway) -> FakeGateway:
        app.dependency_overrides[get_gateway] = lambda: gateway
        return gateway

    yield _use
    app.dependency_overrides.pop(get_gateway, None)


@pytest.fixture()
def assistant_user(make_user, make_organization, grant_permissions):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Ayuntamiento Test")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    return user, organization


def test_assistant_requires_permission(client, make_user):
    user = make_user()

    response = client.get("/assistant/conversations", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.use"


def test_conversation_folders_are_private_and_persisted(
    client,
    assistant_user,
    make_user,
    make_organization,
    grant_permissions,
):
    user, _ = assistant_user
    other_user = make_user(full_name="Otra Alcaldesa")
    grant_permissions(other_user, make_organization(), ["assistant.use"])

    folder_response = client.post(
        "/assistant/conversation-folders",
        json={"name": "Seguimiento político"},
        headers=headers_for(user),
    )
    assert folder_response.status_code == 201
    folder = folder_response.json()
    assert folder["name"] == "Seguimiento político"

    conversation = client.post(
        "/assistant/conversations",
        json={"title": "Licencias urbanísticas"},
        headers=headers_for(user),
    ).json()
    assigned = client.patch(
        f"/assistant/conversations/{conversation['id']}",
        json={"folder_id": folder["id"]},
        headers=headers_for(user),
    )

    assert assigned.status_code == 200
    assert assigned.json()["folder_id"] == folder["id"]
    listed = client.get("/assistant/conversations", headers=headers_for(user)).json()
    assert listed[0]["folder_id"] == folder["id"]

    other_folders = client.get(
        "/assistant/conversation-folders",
        headers=headers_for(other_user),
    )
    assert other_folders.status_code == 200
    assert other_folders.json() == []


def test_conversation_folder_delete_unassigns_conversations(client, assistant_user):
    user, _ = assistant_user
    folder = client.post(
        "/assistant/conversation-folders",
        json={"name": "Borradores"},
        headers=headers_for(user),
    ).json()
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    client.patch(
        f"/assistant/conversations/{conversation['id']}",
        json={"folder_id": folder["id"]},
        headers=headers_for(user),
    )

    response = client.delete(
        f"/assistant/conversation-folders/{folder['id']}",
        headers=headers_for(user),
    )

    assert response.status_code == 204
    refreshed = client.get(
        f"/assistant/conversations/{conversation['id']}",
        headers=headers_for(user),
    ).json()
    assert refreshed["folder_id"] is None


def test_status_reports_disabled_gateway(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    use_gateway(FakeGateway([], enabled=False))

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["planner"]["runtime"] == "disabled"
    assert body["planner"]["enabled"] is False
    assert {agent["key"] for agent in body["agents"]} == {
        "requirements_intake",
        "consultation",
    }
    assert "create_requirement" in {tool["name"] for tool in body["tools"]}


def test_status_reports_hermes_agent_runtime(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "hermes_agent")
    monkeypatch.setattr(settings, "hermes_agent_model", "hermes-agent-test")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    use_gateway(FakeGateway([], runtime_healthy=True))

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["runtime"] == "hermes_agent"
    assert body["model"] == "hermes-agent-test"
    assert body["runtime_healthy"] is True
    assert body["planner"]["runtime"] == "disabled"


def test_hermes_agent_urls_support_v1_base_url(monkeypatch):
    monkeypatch.setattr(settings, "hermes_agent_base_url", "http://127.0.0.1:8642/v1")

    assert _hermes_agent_url("chat/completions") == (
        "http://127.0.0.1:8642/v1/chat/completions"
    )
    assert _hermes_agent_url("health") == "http://127.0.0.1:8642/health"


def test_hermes_agent_openai_tool_calls_are_normalized():
    completion = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "Voy a registrar el requisito.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "create_requirement",
                                    "arguments": json.dumps(
                                        {"title": "Cita previa"},
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 7},
        }
    )

    assert completion.model == "hermes-agent"
    assert completion.stop_reason == "tool_use"
    assert completion.usage.input_tokens == 10
    assert completion.usage.output_tokens == 7
    assert completion.content[0].text == "Voy a registrar el requisito."
    assert completion.content[1].name == "create_requirement"
    assert completion.content[1].input == {"title": "Cita previa"}


def test_hermes_agent_inline_tool_calls_are_normalized():
    completion = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": (
                            'Hecho <tool_call>{"name":"propose_memory_entry",'
                            '"arguments":{"category":"protocol"}}</tool_call>'
                        ),
                    },
                }
            ],
        }
    )

    assert completion.stop_reason == "tool_use"
    assert completion.content[0].text == "Hecho"
    assert completion.content[1].name == "propose_memory_entry"
    assert completion.content[1].input == {"category": "protocol"}


def test_hermes_agent_standalone_json_tool_call_is_normalized():
    completion = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "name": "list_requirements",
                                "arguments": {"organization_id": 1},
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }
    )

    assert completion.stop_reason == "tool_use"
    assert len(completion.content) == 1
    assert completion.content[0].name == "list_requirements"
    assert completion.content[0].input == {"organization_id": 1}


def test_consultation_agent_has_only_read_only_tools():
    consultation = AGENT_REGISTRY["consultation"]

    assert all(
        assistant_tools.TOOL_CATALOG[tool_name].read_only
        for tool_name in consultation.tool_names
    )


def test_consultation_prompt_lists_read_tools(db, assistant_user):
    user, _ = assistant_user
    agent = AGENT_REGISTRY["consultation"]

    prompt = assistant_service.build_system_prompt(
        db,
        user,
        agent,
        get_agent_tools(agent),
    )

    assert "Agente activo: Consulta (consultation)" in prompt
    assert "Tu tarea es consultar información visible" in prompt
    assert "No digas que estás en modo consulta" in prompt
    assert "HERRAMIENTAS DISPONIBLES PARA ESTE AGENTE" in prompt
    assert "- list_requirements" in prompt
    assert "- get_requirement" in prompt
    assert "- create_requirement" not in prompt
    assert "No digas que no tienes una herramienta" in prompt
    assert "no respondas como si solo pudieras consultar" in prompt
    assert "crear o actualizar necesidades/requisitos como borrador" in prompt


def test_requirements_intake_prompt_lists_write_tools(db, assistant_user):
    user, _ = assistant_user
    agent = AGENT_REGISTRY["requirements_intake"]

    prompt = assistant_service.build_system_prompt(
        db,
        user,
        agent,
        get_agent_tools(agent),
    )

    assert "Agente activo: Necesidades (requirements_intake)" in prompt
    assert "Tu tarea es capturar necesidades" in prompt
    assert "- create_requirement" in prompt
    assert "- update_requirement" in prompt
    assert "- record_transversal_feature_acceptance" in prompt
    assert "borrador" in prompt


def test_agent_turn_persists_disabled_planner_routing(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Puedo ayudarte a capturar el requisito.")],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Necesitamos gestionar citas previas"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["routing"]["chosen"] == "requirements_intake"


def test_short_followup_keeps_previous_agent():
    conversation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                content="¿De qué organización quieres que consulte los requisitos?",
                agent_key="consultation",
            )
        ]
    )

    decision = choose_agent(
        conversation=conversation,
        user_text="defaul",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "consultation"
    assert decision.routing["source"] == "shortcut"
    assert decision.routing["fallback_reason"] == "short_followup_previous_agent"


def test_global_capability_question_returns_product_capabilities_without_gateway(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "hola, qué puedes hacer?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "global_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "solo consultar" not in normalized_content
    assert "crear o actualizar necesidades/requisitos como borrador" in normalized_content
    assert "no apruebo trámites" in normalized_content
    assert gateway.calls == []


def test_classify_turn_intent_maps_common_direct_requests():
    assert assistant_service.classify_turn_intent(
        "hola, qué puedes hacer?"
    ) == assistant_service.TurnIntent("global_capabilities", "global_capabilities")
    assert assistant_service.classify_turn_intent(
        "Qué necesidades tenemos registradas?"
    ) == assistant_service.TurnIntent(
        "read_requirements",
        "direct_list_requirements",
        use_needs=True,
    )
    assert assistant_service.classify_turn_intent(
        "crea otra necesidad"
    ) == assistant_service.TurnIntent(
        "create_requirement",
        "direct_create_requirement",
    )
    assert assistant_service.classify_turn_intent(
        "crea un requisito de prueba"
    ) == assistant_service.TurnIntent(
        "create_test_requirement",
        "direct_create_test_requirement",
    )


def test_invalid_planner_fallback_routes_new_need_to_requirements_intake(monkeypatch):
    conversation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                content="¿De qué organización quieres consultarlo?",
                agent_key="consultation",
            )
        ]
    )
    monkeypatch.setattr(assistant_planner, "planner_enabled", lambda: True)
    monkeypatch.setattr(
        assistant_planner,
        "_route_with_hermes",
        lambda **kwargs: "Consulta",
    )

    decision = assistant_planner.choose_agent(
        conversation=conversation,
        user_text="quiero que el sistema pueda cargar datos en un mapa del pueblo",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "requirements_intake"
    assert decision.routing["source"] == "fallback"
    assert decision.routing["fallback_reason"] == "invalid_agent_key"
    assert decision.routing["raw_agent_key"] == "Consulta"


def test_valid_planner_choice_is_overridden_for_obvious_new_need(monkeypatch):
    conversation = SimpleNamespace(messages=[])
    monkeypatch.setattr(assistant_planner, "planner_enabled", lambda: True)
    monkeypatch.setattr(
        assistant_planner,
        "_route_with_hermes",
        lambda **kwargs: "consultation",
    )

    decision = assistant_planner.choose_agent(
        conversation=conversation,
        user_text="quiero que el sistema pueda cargar datos en un mapa del pueblo",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "requirements_intake"
    assert decision.routing["source"] == "shortcut"
    assert decision.routing["fallback_reason"] == "heuristic_override"
    assert decision.routing["raw_agent_key"] == "consultation"


def test_invalid_planner_fallback_keeps_explicit_read_requests_on_consultation(
    monkeypatch,
):
    conversation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                content="He creado el borrador.",
                agent_key="requirements_intake",
            )
        ]
    )
    monkeypatch.setattr(assistant_planner, "planner_enabled", lambda: True)
    monkeypatch.setattr(
        assistant_planner,
        "_route_with_hermes",
        lambda **kwargs: "Consulta",
    )

    decision = assistant_planner.choose_agent(
        conversation=conversation,
        user_text="qué requisitos tenemos registrados",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "consultation"
    assert decision.routing["source"] == "fallback"
    assert decision.routing["fallback_reason"] == "invalid_agent_key"


def test_agent_tool_ceiling_blocks_tools_outside_selected_agent(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
    db,
):
    user, organization = assistant_user

    def fake_choose_agent(**kwargs):
        return SimpleNamespace(
            agent=AGENT_REGISTRY["consultation"],
            routing={
                "candidates": ["requirements_intake", "consultation"],
                "chosen": "consultation",
                "source": "router",
            },
        )

    monkeypatch.setattr(assistant_service, "choose_agent", fake_choose_agent)
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "No debería crearse",
                            },
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("No puedo crear requisitos desde consulta.")],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Haz algo"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "No debería crearse")
    )
    assert requirement is None
    assistant_message = response.json()["messages"][1]
    assert assistant_message["agent_key"] == "consultation"
    action = assistant_message["actions"][0]
    assert action["ok"] is False
    assert "Herramienta no disponible para este agente" in action["result"]


def test_agent_recovers_hermes_argument_only_read_tool_call(
    assistant_user,
):
    _, organization = assistant_user
    response = fake_response(
        "end_turn",
        [
            text_block(
                f'{{"organization_id":{organization.id}}}\n'
                "La herramienta no ha devuelto resultados visibles."
            )
        ],
    )

    recovered = assistant_service.recover_textual_read_tool_call(
        response,
        get_agent_tools(AGENT_REGISTRY["consultation"]),
        [{"role": "user", "content": "Qué requisitos tenemos registrados?"}],
    )

    assert recovered.stop_reason == "tool_use"
    assert recovered.content[0].name == "list_requirements"
    assert recovered.content[0].input == {"organization_id": organization.id}


def test_direct_list_requirements_handles_default_empty_result(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    other = make_organization(name="Tenant Smoke B")
    grant_permissions(user, other, ["assistant.use", "requirements.view"])
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué requisitos tenemos registrados?"},
        headers=headers_for(user),
    )
    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "DEFAULT"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert "¿De qué organización" in first.json()["messages"][1]["content"]
    assert second.status_code == 200
    assistant_message = second.json()["messages"][-1]
    assert assistant_message["agent_key"] == "consultation"
    assert assistant_message["routing"]["source"] == "deterministic"
    assert first.json()["messages"][1]["routing"]["intent"] == "read_requirements"
    assert assistant_message["content"] == (
        f"No hay requisitos visibles registrados en {organization.name}."
    )
    action = assistant_message["actions"][0]
    assert action["tool"] == "list_requirements"
    assert action["ok"] is True
    assert action["input"] == {"organization_id": organization.id}
    assert json.loads(action["result"]) == []
    assert gateway.calls == []


def test_direct_empty_requirements_followup_uses_last_result(
    client,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    listed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué requisitos tenemos registrados?"},
        headers=headers_for(user),
    )
    followup = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "puede ser por qué no hay ningún requisito?"},
        headers=headers_for(user),
    )

    assert listed.status_code == 200
    assert followup.status_code == 200
    assistant_message = followup.json()["messages"][-1]
    assert assistant_message["agent_key"] == "consultation"
    assert assistant_message["actions"] == []
    assert f"0 requisitos visibles en {organization.name}" in assistant_message["content"]
    assert gateway.calls == []


def test_direct_empty_needs_followup_uses_last_result(
    client,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    listed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué necesidades tenemos registradas?"},
        headers=headers_for(user),
    )
    followup = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "puede ser por qué no hay ninguna necesidad?"},
        headers=headers_for(user),
    )

    assert listed.status_code == 200
    assert followup.status_code == 200
    assistant_message = followup.json()["messages"][-1]
    assert assistant_message["agent_key"] == "consultation"
    assert assistant_message["actions"] == []
    assert f"0 necesidades visibles en {organization.name}" in assistant_message["content"]
    assert gateway.calls == []


def test_direct_create_test_requirement_confirmed(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    ask_content = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea un requisito de prueba"},
        headers=headers_for(user),
    )
    proposed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "tú decides"},
        headers=headers_for(user),
    )
    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert ask_content.status_code == 200
    assert "necesito al menos confirmar" in ask_content.json()["messages"][-1]["content"]
    assert proposed.status_code == 200
    assert "¿Confirmas que lo cree como borrador?" in proposed.json()["messages"][-1]["content"]
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Requisito de prueba")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.status == "draft"
    assert requirement.source_type == "conversation"
    assert requirement.created_by_id == user.id
    assert f"borrador #{requirement.id}" in assistant_message["content"]
    assert gateway.calls == []


def test_direct_create_test_requirement_avoids_duplicate(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    db.add(
        Requirement(
            organization_id=organization.id,
            title="Requisito de prueba",
            status="draft",
            source_type="conversation",
            created_by_id=user.id,
        )
    )
    db.commit()
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea un requisito de prueba"},
        headers=headers_for(user),
    )
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "tú decides"},
        headers=headers_for(user),
    )
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert "No he creado un duplicado" in assistant_message["content"]
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements"
    ]
    assert (
        db.scalar(
            select(func.count()).select_from(Requirement).where(
                Requirement.title == "Requisito de prueba"
            )
        )
        == 1
    )
    assert gateway.calls == []


def test_direct_create_another_requirement_collects_org_and_content(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    ask_organization = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea otro requisito"},
        headers=headers_for(user),
    )
    refuse_invention = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "en default. decide tú el resto"},
        headers=headers_for(user),
    )
    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "prueba2, y el problema es probar2"},
        headers=headers_for(user),
    )

    assert ask_organization.status_code == 200
    assert "¿En qué organización" in ask_organization.json()["messages"][-1]["content"]
    assert "Título breve" in ask_organization.json()["messages"][-1]["content"]
    assert refuse_invention.status_code == 200
    assert "no debo inventar" in refuse_invention.json()["messages"][-1]["content"]
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "prueba2")
    )
    assert requirement is not None
    assert requirement.organization_id == default_organization.id
    assert requirement.problem == "probar2"
    assert requirement.summary == "probar2"
    assert requirement.status == "draft"
    assert f"borrador #{requirement.id}" in assistant_message["content"]
    assert gateway.calls == []


def test_direct_create_another_need_collects_org_and_content(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    ask_organization = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea otra necesidad"},
        headers=headers_for(user),
    )
    refuse_invention = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "en default. decide tú el resto"},
        headers=headers_for(user),
    )
    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "prueba2, y el problema es probar2"},
        headers=headers_for(user),
    )

    assert ask_organization.status_code == 200
    assert "¿En qué organización" in ask_organization.json()["messages"][-1]["content"]
    assert "Título breve" in ask_organization.json()["messages"][-1]["content"]
    assert refuse_invention.status_code == 200
    assert "no debo inventar" in refuse_invention.json()["messages"][-1]["content"]
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "prueba2")
    )
    assert requirement is not None
    assert requirement.organization_id == default_organization.id
    assert requirement.problem == "probar2"
    assert requirement.summary == "probar2"
    assert requirement.status == "draft"
    assert f"borrador #{requirement.id}" in assistant_message["content"]
    assert gateway.calls == []


def test_confirming_exact_generic_map_proposal_creates_draft_deterministically(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Claro. ¿Para qué organización quieres plantearlo?\n\n"
                            "Y para entenderlo bien: ¿qué tipo de información queréis guardar en el mapa?"
                        )
                    ],
                ),
                fake_response(
                    "stop",
                    [
                        text_block(
                            "De acuerdo, lo planteamos en “Default organization” salvo que luego me digas otra.\n\n"
                            "Para poder guardarlo como requisito necesito concretar un poco:\n"
                            "¿Qué problema queréis resolver hoy con ese mapa?"
                        )
                    ],
                ),
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Puedo prepararlo como requisito genérico, pero necesito tu OK sobre este enfoque:\n\n"
                            "Título: “Gestión de información municipal desde un mapa”\n"
                            "Problema: “El ayuntamiento necesita registrar, consultar y actualizar información geolocalizada en un mapa para facilitar su gestión diaria.”\n\n"
                            "¿Te vale así como borrador para la organización “Default organization”?"
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "quiero que podamos guardar y gestionar desde un mapa información"},
        headers=headers_for(user),
    )
    organization_reply = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "cualquiera"},
        headers=headers_for(user),
    )
    proposal = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "cualquiera"},
        headers=headers_for(user),
    )
    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert organization_reply.status_code == 200
    assert proposal.status_code == 200
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(
            Requirement.title == "Gestión de información municipal desde un mapa"
        )
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.status == "draft"
    assert "información geolocalizada" in requirement.problem
    assert len(gateway.calls) == 3



def test_confirming_gateway_proposed_need_creates_draft_deterministically(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Perfecto. ¿Para qué organización quieres registrarlo?"
                        )
                    ],
                ),
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Para guardarlo necesito concretar un poco más el borrador.\n\n"
                            "Te propongo este enfoque:\n"
                            "Título: “Carga y visualización de datos municipales en mapa”\n"
                            "Problema: “El ayuntamiento necesita centralizar en un mapa del municipio distintos datos útiles para consulta y gestión.”\n\n"
                            "¿Te encaja así?"
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "quiero que el sistema pueda cargar datos en un mapa del pueblo"},
        headers=headers_for(user),
    )
    proposal = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "default. todo tipo de datos que puedan ser útiles"},
        headers=headers_for(user),
    )

    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert state["pending_work"] == {
        "type": "create_requirement",
        "status": "awaiting_confirmation",
        "organization_id": organization.id,
        "draft": {
            "title": "Carga y visualización de datos municipales en mapa",
            "problem": "El ayuntamiento necesita centralizar en un mapa del municipio distintos datos útiles para consulta y gestión.",
        },
    }

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "me encaja y se cargarán de ambos"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert proposal.status_code == 200
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(
            Requirement.title == "Carga y visualización de datos municipales en mapa"
        )
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.status == "draft"
    assert "centralizar en un mapa" in requirement.problem
    assert len(gateway.calls) == 2


def test_affirmative_after_legacy_unpersisted_proposal_recovers_and_creates_draft(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(FakeGateway([]))
    conversation_response = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    conversation = conversation_response.json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {"selected_organization_id": organization.id},
        ensure_ascii=False,
    )
    db.add(
        AssistantMessage(
            conversation_id=stored_conversation.id,
            role="assistant",
            agent_key="requirements_intake",
            content=(
                "Puedo prepararlo como requisito genérico, pero necesito tu OK sobre este enfoque:\n\n"
                "Título: “Gestión de información municipal desde un mapa”\n"
                "Problema: “El ayuntamiento necesita registrar, consultar y actualizar información geolocalizada en un mapa para facilitar su gestión diaria.”\n\n"
                "¿Te vale así como borrador para la organización “Default organization”?"
            ),
        )
    )
    db.commit()

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "legacy_proposal_create_requirement_confirmed"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(
            Requirement.title == "Gestión de información municipal desde un mapa"
        )
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert "información geolocalizada" in requirement.problem
    assert gateway.calls == []



def test_confirming_pending_work_creates_need_without_reparsing_assistant_text(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(FakeGateway([]))
    conversation_response = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    conversation = conversation_response.json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "selected_organization_id": organization.id,
            "pending_work": {
                "type": "create_requirement",
                "status": "awaiting_confirmation",
                "organization_id": organization.id,
                "draft": {
                    "title": "Mapa municipal de datos",
                    "problem": "Centralizar datos municipales útiles sobre un mapa del pueblo.",
                },
            },
        },
        ensure_ascii=False,
    )
    db.add(
        AssistantMessage(
            conversation_id=stored_conversation.id,
            role="assistant",
            content="Tengo una propuesta pendiente. ¿La guardo?",
        )
    )
    db.commit()

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "me encaja"},
        headers=headers_for(user),
    )

    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Mapa municipal de datos")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.problem == "Centralizar datos municipales útiles sobre un mapa del pueblo."
    db.expire_all()
    updated_conversation = db.get(AssistantConversation, conversation["id"])
    assert updated_conversation is not None
    updated_state = json.loads(updated_conversation.state or "{}")
    assert "pending_work" not in updated_state
    assert gateway.calls == []


def test_create_capability_question_answers_without_starting_intake(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "y puedes crear un requisito en default desde aquí?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "global_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "claro. la creo" not in normalized_content
    assert "título breve" not in normalized_content
    assert "crear o actualizar necesidades/requisitos como borrador" in normalized_content
    assert gateway.calls == []


def test_direct_create_agent_reference_switches_to_intake_without_internal_copy(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué requisitos tenemos registrados?"},
        headers=headers_for(user),
    )
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "DEFAULT"},
        headers=headers_for(user),
    )

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "bueno pero puedes llamar al agente de crear requisitos no?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "global_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "solo lectura" not in normalized_content
    assert "copia" not in normalized_content
    assert "agente" not in normalized_content
    assert "crear o actualizar necesidades/requisitos como borrador" in normalized_content
    assert gateway.calls == []


def test_direct_create_another_requirement_accepts_labeled_content(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    ask_content = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea otro requisito"},
        headers=headers_for(user),
    )
    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                'el título es prueba 2 y el problema a resolver es un texto '
                'de prueba "prueba 2"'
            )
        },
        headers=headers_for(user),
    )

    assert ask_content.status_code == 200
    assert "Título breve" in ask_content.json()["messages"][-1]["content"]
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "prueba 2")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.problem == 'un texto de prueba "prueba 2"'
    assert gateway.calls == []


def test_conversations_are_private_to_their_creator(
    client,
    assistant_user,
    make_user,
    make_organization,
    grant_permissions,
):
    user, _ = assistant_user
    other = make_user()
    other_org = make_organization()
    grant_permissions(other, other_org, ["assistant.use"])

    created = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    own_list = client.get("/assistant/conversations", headers=headers_for(user))
    assert [c["id"] for c in own_list.json()] == [conversation_id]

    foreign_list = client.get("/assistant/conversations", headers=headers_for(other))
    assert foreign_list.json() == []

    foreign_get = client.get(
        f"/assistant/conversations/{conversation_id}",
        headers=headers_for(other),
    )
    assert foreign_get.status_code == 404


def test_send_message_when_gateway_disabled_returns_503(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([], enabled=False))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Assistant is not configured"


def test_agent_turn_creates_requirement_draft_with_audit_trail(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        text_block("Voy a registrar el requisito."),
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "Cita previa para padrón",
                                "problem": "Colas en el registro",
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("He creado el borrador del requisito.")],
                ),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Necesitamos cita previa para el padrón"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    detail = response.json()

    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Cita previa para padrón")
    )
    assert requirement is not None
    assert requirement.status == "draft"
    assert requirement.source_type == "conversation"
    assert requirement.created_by_id == user.id
    assert requirement.problem == "Colas en el registro"

    roles = [message["role"] for message in detail["messages"]]
    assert roles == ["user", "assistant"]
    assistant_message = detail["messages"][1]
    assert assistant_message["content"] == "He creado el borrador del requisito."
    assert len(assistant_message["actions"]) == 1
    action = assistant_message["actions"][0]
    assert action["tool"] == "create_requirement"
    assert action["ok"] is True
    assert json.loads(action["result"])["id"] == requirement.id

    # Conversation title is taken from the first user message.
    assert detail["title"] == "Necesitamos cita previa para el padrón"
    # Two API round-trips: tool call + final reply.
    assert len(gateway.calls) == 2


def test_agent_tool_respects_rbac_of_current_user(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    organization = make_organization()
    # assistant.use but NOT requirements.create
    grant_permissions(user, organization, ["assistant.use"])

    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "No debería crearse",
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("No tienes permiso para crear requisitos.")],
                ),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Crea un requisito"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "No debería crearse")
    )
    assert requirement is None

    action = response.json()["messages"][1]["actions"][0]
    assert action["ok"] is False
    assert "requirements.create" in action["result"]


def test_agent_web_search_uses_controlled_hermes_web_tool(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "assistant.web.search"])
    calls = []

    def fake_search(*, query: str, limit: int):
        calls.append({"query": query, "limit": limit})
        return [
            {
                "title": "Normativa ejemplo",
                "url": "https://example.test/normativa",
                "snippet": "Resumen público de la fuente.",
                "published_at": None,
            }
        ]

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "web_search",
                            {
                                "query": "normativa municipal 2026",
                                "limit": 20,
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("He encontrado una fuente pública.")],
                ),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Busca si hay novedades de normativa municipal"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert calls == [{"query": "normativa municipal 2026", "limit": 5}]

    action = response.json()["messages"][1]["actions"][0]
    assert action["tool"] == "web_search"
    assert action["ok"] is True
    result = json.loads(action["result"])
    assert result["query"] == "normativa municipal 2026"
    assert result["limit"] == 5
    assert result["results"][0]["url"] == "https://example.test/normativa"


def test_agent_web_search_requires_permission(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use"])

    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "web_search",
                            {"query": "normativa municipal 2026"},
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("No tienes permiso para buscar en la web.")],
                ),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Busca en internet"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    action = response.json()["messages"][1]["actions"][0]
    assert action["tool"] == "web_search"
    assert action["ok"] is False
    assert "assistant.web.search" in action["result"]


def test_web_search_tool_rejects_empty_query(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.web.search"])

    result = assistant_tools.execute_tool(
        db,
        user,
        "web_search",
        {"query": "   "},
    )

    assert result.ok is False
    assert "query no puede estar vacío" in result.content


def test_web_search_tool_rejects_personal_data_query(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.web.search"])

    result = assistant_tools.execute_tool(
        db,
        user,
        "web_search",
        {"query": "buscar expediente de vecino@example.com"},
    )

    assert result.ok is False
    assert "datos personales" in result.content


def test_agent_can_only_propose_memory_until_human_approval(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Secretario Test")
    organization = make_organization(name="Ayuntamiento Memoria")
    grant_permissions(
        user,
        organization,
        [
            "assistant.use",
            "assistant.memory.propose",
            "assistant.memory.review",
            "assistant.memory.view",
        ],
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "propose_memory_entry",
                            {
                                "organization_id": organization.id,
                                "category": "protocol",
                                "content": (
                                    "En empadronamiento incompleto se pide primero "
                                    "el justificante de domicilio."
                                ),
                            },
                        ),
                    ],
                ),
                fake_response("end_turn", [text_block("Lo dejo propuesto.")]),
                fake_response("end_turn", [text_block("Sigo sin usarlo.")]),
                fake_response("end_turn", [text_block("Ahora puedo tenerlo en cuenta.")]),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Ese trámite lo hacemos siempre así"},
        headers=headers_for(user),
    )
    assert first.status_code == 200

    entry = db.scalar(select(AssistantMemoryEntry))
    assert entry is not None
    assert entry.status == "proposed"
    assert entry.source_conversation_id == conversation["id"]
    assert entry.source_message_id is not None

    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Qué recuerdas del empadronamiento?"},
        headers=headers_for(user),
    )
    assert second.status_code == 200
    assert "justificante de domicilio" not in gateway.calls[-1]["system"]

    approved = client.patch(
        f"/assistant/memory/{entry.id}",
        json={"status": "approved"},
        headers=headers_for(user),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    third = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Y ahora?"},
        headers=headers_for(user),
    )
    assert third.status_code == 200
    assert "justificante de domicilio" in gateway.calls[-1]["system"]


def test_memory_review_requires_review_permission(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "assistant.memory.view"])
    entry = AssistantMemoryEntry(
        organization_id=organization.id,
        category="context",
        content="Dato pendiente",
        status="proposed",
        proposed_by_id=user.id,
    )
    db.add(entry)
    db.commit()

    response = client.patch(
        f"/assistant/memory/{entry.id}",
        json={"status": "approved"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.memory.review"


def test_approved_memory_is_scoped_by_organization_permissions(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    visible_org = make_organization(name="Org Visible")
    hidden_org = make_organization(name="Org Oculta")
    grant_permissions(user, visible_org, ["assistant.use", "assistant.memory.view"])
    db.add_all(
        [
            AssistantMemoryEntry(
                organization_id=visible_org.id,
                category="context",
                content="Contexto visible",
                status="approved",
                proposed_by_id=user.id,
            ),
            AssistantMemoryEntry(
                organization_id=hidden_org.id,
                category="context",
                content="Contexto oculto",
                status="approved",
                proposed_by_id=user.id,
            ),
        ]
    )
    db.commit()
    gateway = use_gateway(FakeGateway([fake_response("end_turn", [text_block("Hola")])]))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert "Contexto visible" in gateway.calls[0]["system"]
    assert "Contexto oculto" not in gateway.calls[0]["system"]


def test_agent_can_propose_transversal_feature_from_visible_requirement(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcaldesa Test")
    organization = make_organization(name="Ayuntamiento Origen")
    grant_permissions(user, organization, ["assistant.use", "requirements.create"])
    requirement = Requirement(
        organization_id=organization.id,
        title="Avisos de vencimiento",
        summary="Avisar antes de que venza documentación de expedientes.",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "propose_transversal_feature",
                            {
                                "source_requirement_id": requirement.id,
                                "title": "Avisos de vencimiento documental",
                                "summary": (
                                    "Alertas configurables antes de que venza "
                                    "documentación asociada a expedientes."
                                ),
                                "rationale": (
                                    "Es un patrón común en trámites municipales "
                                    "con plazos y documentación recurrente."
                                ),
                                "category": "automation",
                            },
                        ),
                    ],
                ),
                fake_response("end_turn", [text_block("Lo dejo propuesto.")]),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Esto podría servir a otros ayuntamientos."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    feature = db.scalar(select(AssistantTransversalFeature))
    assert feature is not None
    assert feature.status == "proposed"
    assert feature.source_requirement_id == requirement.id
    assert feature.source_organization_id == organization.id
    assert feature.source_conversation_id == conversation["id"]
    assert feature.source_message_id is not None
    action = response.json()["messages"][-1]["actions"][0]
    assert action["tool"] == "propose_transversal_feature"
    assert action["ok"] is True
    assert gateway.calls[0]["tools"]


def test_transversal_feature_proposal_rejects_personal_data(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "requirements.create"])
    requirement = Requirement(
        organization_id=organization.id,
        title="Trámite sensible",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()

    result = assistant_tools.execute_tool(
        db,
        user,
        "propose_transversal_feature",
        {
            "source_requirement_id": requirement.id,
            "title": "Avisos a vecino@example.com",
            "summary": "Enviar avisos a vecino@example.com",
            "rationale": "Podría ahorrar llamadas.",
            "category": "automation",
        },
    )

    assert result.ok is False
    assert "datos personales" in result.content
    assert db.scalar(select(AssistantTransversalFeature)) is None


def test_transversal_feature_review_is_superuser_only(
    client,
    db,
    make_user,
    make_organization,
    superuser,
):
    user = make_user()
    organization = make_organization()
    feature = AssistantTransversalFeature(
        source_organization_id=organization.id,
        title="Bandeja de avisos",
        summary="Avisos reutilizables para trámites con plazos.",
        rationale="Los plazos administrativos se repiten en varios municipios.",
        category="process",
        status="proposed",
        proposed_by_id=user.id,
    )
    db.add(feature)
    db.commit()

    denied = client.get(
        "/assistant/transversal-features",
        headers=headers_for(user),
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Superuser privileges required"

    listed = client.get(
        "/assistant/transversal-features",
        headers=headers_for(superuser),
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == feature.id

    approved = client.patch(
        f"/assistant/transversal-features/{feature.id}",
        json={"status": "available", "auto_activatable": True},
        headers=headers_for(superuser),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "available"
    assert approved.json()["auto_activatable"] is True
    assert approved.json()["reviewed_by_id"] == superuser.id


def test_available_transversal_features_tool_hides_source_data(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    available = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Avisos de vencimiento",
        summary="Alertas reutilizables para documentación con plazo.",
        rationale="Patrón común en expedientes municipales.",
        category="automation",
        status="available",
    )
    proposed = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Funcionalidad no revisada",
        summary="No debe sugerirse todavía.",
        rationale="Aún no está revisada.",
        category="other",
        status="proposed",
    )
    db.add_all([available, proposed])
    db.commit()

    result = assistant_tools.execute_tool(
        db,
        user,
        "list_available_transversal_features",
        {"organization_id": target_org.id},
    )

    assert result.ok is True
    data = json.loads(result.content)
    assert [feature["id"] for feature in data] == [available.id]
    assert "source_organization_id" not in data[0]
    assert "source_requirement_id" not in data[0]


def test_record_transversal_feature_acceptance_creates_activation_state(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    pending_feature = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Plantilla configurable",
        summary="Plantilla que requiere configuración humana.",
        rationale="Cada ayuntamiento debe ajustar algunos parámetros.",
        category="documents",
        status="available",
        auto_activatable=False,
    )
    automatic_feature = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Aviso automático",
        summary="Aviso que puede activarse sin configuración adicional.",
        rationale="No depende de datos locales.",
        category="automation",
        status="available",
        auto_activatable=True,
    )
    db.add_all([pending_feature, automatic_feature])
    db.commit()
    conversation = AssistantConversation(
        title="Activación transversal",
        created_by_id=user.id,
    )
    db.add(conversation)
    db.flush()
    pending_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content="Sí, queremos aplicar la plantilla.",
    )
    automatic_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content="Activad también el aviso.",
    )
    db.add_all([pending_message, automatic_message])
    db.commit()

    pending = assistant_tools.execute_tool(
        db,
        user,
        "record_transversal_feature_acceptance",
        {
            "feature_id": pending_feature.id,
            "organization_id": target_org.id,
            "notes": "OK confirmado en conversación.",
        },
        assistant_tools.ToolContext(
            conversation_id=conversation.id,
            user_message_id=pending_message.id,
        ),
    )
    automatic = assistant_tools.execute_tool(
        db,
        user,
        "record_transversal_feature_acceptance",
        {
            "feature_id": automatic_feature.id,
            "organization_id": target_org.id,
        },
        assistant_tools.ToolContext(
            conversation_id=conversation.id,
            user_message_id=automatic_message.id,
        ),
    )

    assert pending.ok is True
    assert automatic.ok is True
    adoptions = list(
        db.scalars(
            select(AssistantTransversalFeatureAdoption).order_by(
                AssistantTransversalFeatureAdoption.feature_id
            )
        )
    )
    adoption_by_feature = {adoption.feature_id: adoption for adoption in adoptions}
    assert adoption_by_feature[pending_feature.id].status == "activation_pending"
    assert adoption_by_feature[pending_feature.id].activated_at is None
    assert adoption_by_feature[pending_feature.id].source_message_id == pending_message.id
    assert adoption_by_feature[automatic_feature.id].status == "active"
    assert adoption_by_feature[automatic_feature.id].activated_at is not None
    assert adoption_by_feature[automatic_feature.id].approved_by_id == user.id


def test_agent_records_transversal_feature_acceptance_as_audited_action(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    feature = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Aviso automático",
        summary="Aviso que puede activarse sin configuración adicional.",
        rationale="No depende de datos locales.",
        category="automation",
        status="available",
        auto_activatable=True,
    )
    db.add(feature)
    db.commit()
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "record_transversal_feature_acceptance",
                            {
                                "feature_id": feature.id,
                                "organization_id": target_org.id,
                            },
                        ),
                    ],
                ),
                fake_response("end_turn", [text_block("Queda activada.")]),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, activadlo para nuestro ayuntamiento."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    adoption = db.scalar(select(AssistantTransversalFeatureAdoption))
    assert adoption is not None
    assert adoption.status == "active"
    assert adoption.source_conversation_id == conversation["id"]
    assert adoption.source_message_id is not None
    action = response.json()["messages"][-1]["actions"][0]
    assert action["tool"] == "record_transversal_feature_acceptance"
    assert action["ok"] is True


def test_available_transversal_features_are_not_injected_in_system_prompt(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    db.add(
        AssistantTransversalFeature(
            source_organization_id=source_org.id,
            title="No debe aparecer en el prompt",
            summary="Solo debe consultarse mediante herramienta.",
            rationale="Evita inyectar backlog global en todos los chats.",
            category="other",
            status="available",
        )
    )
    db.commit()
    gateway = use_gateway(FakeGateway([fake_response("end_turn", [text_block("Hola")])]))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert "No debe aparecer en el prompt" not in gateway.calls[0]["system"]


def test_archived_conversation_rejects_messages(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([fake_response("end_turn", [text_block("Hola")])]))

    conversation = client.post(
        "/assistant/conversations",
        json={"title": "Vieja"},
        headers=headers_for(user),
    ).json()
    archived = client.patch(
        f"/assistant/conversations/{conversation['id']}",
        json={"status": "archived"},
        headers=headers_for(user),
    )
    assert archived.status_code == 200

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Conversation is archived"


def test_refusal_stop_reason_returns_polite_message(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([fake_response("refusal", [])]))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][1]
    assert "No puedo ayudarte" in assistant_message["content"]
