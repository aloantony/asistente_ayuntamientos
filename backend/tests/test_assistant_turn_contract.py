import json
import logging

import pytest
from sqlalchemy import select

from app.assistant import planner as assistant_planner
from app.assistant import service as assistant_service
from app.assistant import tools as assistant_tools
from app.assistant.models import (
    AssistantConversation,
    AssistantKnowledgeProposal,
    AssistantMemoryEntry,
)
from app.assistant.routes import get_gateway
from app.core.config import settings
from app.main import app
from conftest import headers_for
from test_assistant import FakeGateway, fake_response, text_block


@pytest.fixture()
def use_gateway(client):
    def _use(gateway: FakeGateway) -> FakeGateway:
        app.dependency_overrides[get_gateway] = lambda: gateway
        return gateway

    yield _use
    app.dependency_overrides.pop(get_gateway, None)


def test_web_and_memory_capability_question_does_not_call_model_or_tools(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Capacidades")
    organization = make_organization(name="Ayuntamiento Capacidades")
    grant_permissions(
        user,
        organization,
        [
            "assistant.use",
            "assistant.web.search",
            "assistant.memory.propose",
            "assistant.memory.view",
        ],
    )
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    web_calls = []

    def fake_search(*, query: str, limit: int):
        web_calls.append({"query": query, "limit": limit})
        return []

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Puedo buscar fuentes públicas y proponer guardarlas bajo revisión."
                        )
                    ],
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
        json={
            "content": "¿Puedes buscar en internet y guardar fuentes importantes para futuras consultas?"
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    assert web_calls == []
    assert gateway.calls == []
    content = assistant_message["content"].lower()
    assert "buscar" in content
    assert "fuente" in content or "memoria" in content
    assert "agente de" not in content
    assert "modo consulta" not in content


def test_external_research_plan_executes_controlled_web_search_and_stores_last_research(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcalde Investigación")
    organization = make_organization(name="Ayuntamiento Investigación")
    grant_permissions(user, organization, ["assistant.use", "assistant.web.search"])
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="external_research",
            action="web_search",
            target={
                "organization_id": organization.id,
                "query": "ayudas públicas alumbrado municipal Castilla y León 2026",
                "limit": 5,
            },
            confidence=0.93,
            source="planner",
        ),
    )
    web_calls = []

    def fake_search(*, query: str, limit: int):
        web_calls.append({"query": query, "limit": limit})
        return [
            {
                "title": "Convocatoria pública de alumbrado",
                "url": "https://example.test/ayudas-alumbrado",
                "snippet": "Ayudas públicas para renovar alumbrado municipal.",
                "published_at": "2026-06-01",
            }
        ]

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("He encontrado una fuente pública sobre ayudas.")],
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
        json={"content": "Si no lo tenemos, busca ayudas públicas para renovar alumbrado."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "external_research"
    assert assistant_message["actions"][0]["tool"] == "web_search"
    assert assistant_message["actions"][0]["ok"] is True
    assert web_calls == [
        {"query": "ayudas públicas alumbrado municipal Castilla y León 2026", "limit": 5}
    ]
    assert gateway.calls == []

    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert state["last_research"]["query"] == web_calls[0]["query"]
    assert state["last_research"]["results"][0]["url"] == "https://example.test/ayudas-alumbrado"


def test_external_research_prefers_approved_internal_knowledge_before_web(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Fuente Interna")
    organization = make_organization(name="Ayuntamiento Fuente Interna")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "assistant.web.search", "assistant.knowledge.view"],
    )
    db.add(
        AssistantKnowledgeProposal(
            organization_id=organization.id,
            title="Convocatoria oficial de alumbrado municipal 2026",
            summary="Ayudas públicas para renovar alumbrado municipal en Castilla y León.",
            content="Fuente revisada por el equipo municipal para consultas futuras.",
            source_url="https://bocyl.jcyl.es/example/ayudas-alumbrado-2026",
            source_title="BOCYL - Ayudas alumbrado municipal 2026",
            source_type="official",
            confidence="high",
            status="approved",
            sensitivity="normal",
        )
    )
    db.commit()
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="external_research",
            action="web_search",
            target={
                "organization_id": organization.id,
                "query": "ayudas públicas alumbrado municipal Castilla y León 2026",
                "limit": 5,
            },
            confidence=0.94,
            source="planner",
        ),
    )
    web_calls = []

    def fake_search(*, query: str, limit: int):
        web_calls.append({"query": query, "limit": limit})
        return [
            {
                "title": "Resultado web que no debe usarse",
                "url": "https://example.test/no-usar",
                "snippet": "Solo debería consultarse si no hay fuente interna aprobada.",
            }
        ]

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Si no lo tenemos, busca ayudas públicas para alumbrado."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "external_research"
    assert assistant_message["routing"]["reason"] == "internal_approved_knowledge_before_web"
    assert assistant_message["actions"][0]["tool"] == "approved_knowledge_lookup"
    assert assistant_message["actions"][0]["ok"] is True
    assert web_calls == []
    assert gateway.calls == []
    content = assistant_message["content"].lower()
    assert "fuente aprobada" in content
    assert "https://bocyl.jcyl.es/example/ayudas-alumbrado-2026" in content
    assert "no he buscado en la web" in content


def test_private_external_research_plan_is_blocked_before_web_call(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Privacidad")
    organization = make_organization(name="Ayuntamiento Privacidad")
    grant_permissions(user, organization, ["assistant.use", "assistant.web.search"])
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="external_research",
            action="web_search",
            target={
                "organization_id": organization.id,
                "query": "buscar expediente interno de vecino@example.com",
            },
            confidence=0.94,
            source="planner",
        ),
    )
    web_calls = []

    def fake_search(*, query: str, limit: int):
        web_calls.append({"query": query, "limit": limit})
        return []

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("No puedo buscar datos personales o expedientes internos en la web.")],
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
        json={"content": "búscalo fuera para ese expediente"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"] == []
    assert web_calls == []
    assert gateway.calls == []
    assert "datos personales" in assistant_message["content"].lower()


def test_misplanned_external_research_capability_question_does_not_call_web(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Capacidades Web")
    organization = make_organization(name="Ayuntamiento Capacidades Web")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "assistant.web.search", "assistant.memory.propose"],
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="external_research",
            action="web_search",
            target={"query": "capacidades de búsqueda web del asistente"},
            confidence=0.91,
            source="planner",
        ),
    )
    web_calls = []

    def fake_search(*, query: str, limit: int):
        web_calls.append({"query": query, "limit": limit})
        return []

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Puedes buscar en internet y guardar fuentes para futuras consultas?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    assert web_calls == []
    assert gateway.calls == []
    content = assistant_message["content"].lower()
    assert "buscar información pública" in content
    assert "propuesta revisable" in content


def test_external_research_reply_cites_sources_and_stores_bounded_state(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Fuentes")
    organization = make_organization(name="Ayuntamiento Fuentes Web")
    grant_permissions(user, organization, ["assistant.use", "assistant.web.search"])
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="external_research",
            action="web_search",
            target={"query": "ayudas públicas alumbrado municipal 2026", "limit": 5},
            confidence=0.94,
            source="planner",
        ),
    )

    def fake_search(*, query: str, limit: int):
        return [
            {
                "title": f"Fuente pública {index}",
                "url": f"https://example{index}.test/ayudas",
                "snippet": "Resumen público " + ("x" * 900),
                "published_at": "2026-06-01",
                "raw_private_field": "no debe persistirse",
            }
            for index in range(1, 8)
        ]

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Busca fuentes públicas de ayudas para renovar alumbrado."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"][0]["tool"] == "web_search"
    content = assistant_message["content"]
    assert "Fuentes:" in content
    assert "Fuente pública 1" in content
    assert "https://example1.test/ayudas" in content
    assert "https://example2.test/ayudas" in content
    assert gateway.calls == []

    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    last_research = state["last_research"]
    assert last_research["query"] == "ayudas públicas alumbrado municipal 2026"
    assert last_research["policy_notes"] == ["privacy_gate_passed", "public_sources_only"]
    assert len(last_research["results"]) == 5
    assert set(last_research["results"][0]) == {
        "title",
        "url",
        "snippet",
        "published_at",
        "source_type",
    }
    assert len(last_research["results"][0]["snippet"]) <= 500


def test_save_last_research_creates_reviewable_proposal_not_approved_memory(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Secretaria Fuentes")
    organization = make_organization(name="Ayuntamiento Fuentes")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "assistant.knowledge.propose", "assistant.knowledge.view"],
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="save_last_research",
            action="propose_knowledge_entry",
            target={"organization_id": organization.id},
            confidence=0.95,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("La dejo como propuesta revisable, pendiente de aprobación.")],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "last_research": {
                "query": "ayudas alumbrado municipal",
                "retrieved_at": "2026-07-01T00:00:00+00:00",
                "results": [
                    {
                        "title": "Convocatoria pública de alumbrado",
                        "url": "https://example.test/ayudas-alumbrado",
                        "snippet": "Ayudas públicas para renovar alumbrado municipal.",
                        "published_at": "2026-06-01",
                    }
                ],
            }
        },
        ensure_ascii=False,
    )
    db.add(stored_conversation)
    db.commit()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí, guarda esa fuente para revisarla luego"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "save_last_research"
    assert assistant_message["actions"][0]["tool"] == "propose_knowledge_entry"
    assert assistant_message["actions"][0]["ok"] is True
    action_result = json.loads(assistant_message["actions"][0]["result"])
    assert action_result["status"] == "proposed"
    assert action_result["source_url"] == "https://example.test/ayudas-alumbrado"
    proposal = db.scalar(select(AssistantKnowledgeProposal))
    assert proposal is not None
    assert proposal.status == "proposed"
    assert proposal.source_url == "https://example.test/ayudas-alumbrado"
    assert db.scalar(select(AssistantMemoryEntry)) is None
    assert db.scalar(
        select(AssistantMemoryEntry).where(AssistantMemoryEntry.status == "approved")
    ) is None
    assert gateway.calls == []


def test_save_last_research_without_previous_research_asks_for_source_instead_of_inventing(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Secretaria Sin Fuente")
    organization = make_organization(name="Ayuntamiento Sin Fuente")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "assistant.knowledge.propose", "assistant.knowledge.view"],
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="save_last_research",
            action="propose_knowledge_entry",
            target={"organization_id": organization.id},
            confidence=0.95,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "guarda esa fuente para revisarla luego"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "save_last_research"
    assert assistant_message["actions"] == []
    content = assistant_message["content"].lower()
    assert "no tengo una fuente" in content
    assert "buscar" in content or "url" in content
    assert db.scalar(select(AssistantKnowledgeProposal)) is None
    assert db.scalar(select(AssistantMemoryEntry)) is None
    assert gateway.calls == []


def test_tool_actions_are_audited_and_logs_do_not_include_private_input_values(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
    caplog,
):
    user = make_user(full_name="Alcaldesa Auditoría")
    organization = make_organization(name="Ayuntamiento Auditoría")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    private_problem = (
        "No hay inventario de llaves y el aviso incluye vecino@example.com, "
        "que no debe aparecer en logs operativos."
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="create_requirement",
            action="create_requirement",
            target={"organization_id": organization.id},
            draft={
                "title": "Control de llaves municipal",
                "problem": private_problem,
            },
            confidence=0.95,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    caplog.set_level(logging.INFO, logger="app.assistant.tools")
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Crea una necesidad de control de llaves."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    messages = response.json()["messages"]
    assistant_message = messages[-1]
    user_message = messages[-2]
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    create_action = assistant_message["actions"][1]
    audit = create_action["audit"]
    assert audit["actor_user_id"] == user.id
    assert audit["conversation_id"] == conversation["id"]
    assert audit["user_message_id"] == user_message["id"]
    assert audit["tool"] == "create_requirement"
    assert audit["domain"] == "requirements"
    assert audit["risk_level"] == "medium"
    assert audit["read_only"] is False
    assert audit["requires_confirmation"] is True
    assert audit["requires_review"] is True
    created = json.loads(create_action["result"])
    assert audit["created_refs"] == [{"type": "requirement", "id": created["id"]}]
    assert set(audit["input_keys"]) == {"organization_id", "problem", "summary", "title"}

    tool_records = [
        record
        for record in caplog.records
        if getattr(record, "assistant_event", None) == "tool_execution"
    ]
    assert {getattr(record, "assistant_tool", None) for record in tool_records} >= {
        "list_requirements",
        "create_requirement",
    }
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "vecino@example.com" not in rendered_logs
    assert "Control de llaves municipal" not in rendered_logs
    assert private_problem not in rendered_logs
    assert gateway.calls == []


def test_private_web_research_safety_block_is_observable_without_logging_query_values(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
    caplog,
):
    user = make_user(full_name="Alcaldesa Bloqueo Web")
    organization = make_organization(name="Ayuntamiento Bloqueo Web")
    grant_permissions(user, organization, ["assistant.use", "assistant.web.search"])
    private_query = "buscar expediente interno de vecino@example.com con token secreto"
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="external_research",
            action="web_search",
            target={"organization_id": organization.id, "query": private_query},
            confidence=0.94,
            source="planner",
        ),
    )
    web_calls = []

    def fake_search(*, query: str, limit: int):
        web_calls.append({"query": query, "limit": limit})
        return []

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    gateway = use_gateway(FakeGateway([]))
    caplog.set_level(logging.INFO, logger="app.assistant.service")
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "búscalo fuera"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"] == []
    assert web_calls == []
    safety_records = [
        record
        for record in caplog.records
        if getattr(record, "assistant_event", None) == "safety_block"
    ]
    assert safety_records
    assert safety_records[-1].assistant_reason == "external_research_blocked_private_data"
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "vecino@example.com" not in rendered_logs
    assert "token secreto" not in rendered_logs
    assert private_query not in rendered_logs
    assert gateway.calls == []
