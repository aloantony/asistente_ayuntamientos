import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.assistant.models import AssistantMemoryEntry
from app.assistant.routes import get_gateway
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
    def __init__(self, responses: list, enabled: bool = True):
        self.enabled = enabled
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


def test_status_reports_disabled_gateway(client, assistant_user, use_gateway):
    user, _ = assistant_user
    use_gateway(FakeGateway([], enabled=False))

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    assert response.json()["enabled"] is False


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
