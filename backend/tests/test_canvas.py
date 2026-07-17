import hashlib

import pytest
from sqlalchemy import func, select

from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.tools import (
    CANVAS_EXTERNAL_TOOL_BLOCKED,
    ToolContext,
    execute_tool,
    tool_input_for_activity,
)
from app.canvas.models import AssistantCanvasDocument, AssistantCanvasRevision
from conftest import headers_for


@pytest.fixture()
def canvas_user(make_user, make_organization, grant_permissions):
    user = make_user(full_name="Secretaria municipal")
    organization = make_organization(name="Ayuntamiento del lienzo")
    grant_permissions(user, organization, ["assistant.use"])
    return user, organization


def _conversation(db, user, *, title="Borradores municipales"):
    conversation = AssistantConversation(
        title=title,
        status="active",
        channel="web",
        created_by_id=user.id,
    )
    db.add(conversation)
    db.commit()
    return conversation


def test_canvas_api_lifecycle_history_and_active_selection(
    client,
    db,
    canvas_user,
):
    user, organization = canvas_user
    conversation = _conversation(db, user)
    headers = headers_for(user)
    create_payload = {
        "title": "Ordenanza de convivencia",
        "document_type": "municipal_ordinance",
        "organization_id": organization.id,
        "creation_id": "browser-create-1",
    }

    created = client.post(
        f"/assistant/conversations/{conversation.id}/canvas/documents",
        headers=headers,
        json=create_payload,
    )

    assert created.status_code == 201
    document = created.json()
    document_id = document["id"]
    assert document["current_revision"] == 1
    assert document["status"] == "draft"
    assert "Borrador de trabajo. No aprobado ni publicado." in document["content"]
    assert "## Articulado" in document["content"]

    replay = client.post(
        f"/assistant/conversations/{conversation.id}/canvas/documents",
        headers=headers,
        json=create_payload,
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == document_id
    assert db.scalar(select(func.count(AssistantCanvasRevision.id))) == 1

    create_id_conflict = client.post(
        f"/assistant/conversations/{conversation.id}/canvas/documents",
        headers=headers,
        json={**create_payload, "title": "Otro contenido con la misma clave"},
    )
    assert create_id_conflict.status_code == 409
    assert "different payload" in create_id_conflict.json()["detail"]

    workspace = client.get(
        f"/assistant/conversations/{conversation.id}/canvas",
        headers=headers,
    )
    assert workspace.status_code == 200
    assert workspace.json()["active_document_id"] == document_id
    assert [item["id"] for item in workspace.json()["documents"]] == [document_id]

    updated_content = "# Ordenanza de convivencia\n\n## Artículo 1. Objeto\n\nRegular la convivencia."
    updated = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 1,
            "content": updated_content,
            "change_summary": "Desarrollado el objeto",
            "mutation_id": "browser-save-1",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["current_revision"] == 2
    assert updated.json()["content"] == updated_content

    update_replay = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 1,
            "content": updated_content,
            "change_summary": "Desarrollado el objeto",
            "mutation_id": "browser-save-1",
        },
    )
    assert update_replay.status_code == 200
    assert update_replay.json()["current_revision"] == 2

    mutation_id_conflict = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 1,
            "content": "Contenido distinto con la misma clave",
            "change_summary": "Desarrollado el objeto",
            "mutation_id": "browser-save-1",
        },
    )
    assert mutation_id_conflict.status_code == 409
    assert "different payload" in mutation_id_conflict.json()["detail"]

    conflict = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 1,
            "content": "texto obsoleto",
            "mutation_id": "browser-save-stale",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Canvas document revision conflict"

    restored = client.post(
        f"/assistant/canvas/documents/{document_id}/revisions/1/restore",
        headers=headers,
        json={
            "expected_revision": 2,
            "mutation_id": "browser-restore-1",
        },
    )
    assert restored.status_code == 200
    assert restored.json()["current_revision"] == 3
    assert restored.json()["content"] == document["content"]

    restore_replay = client.post(
        f"/assistant/canvas/documents/{document_id}/revisions/1/restore",
        headers=headers,
        json={
            "expected_revision": 2,
            "mutation_id": "browser-restore-1",
        },
    )
    assert restore_replay.status_code == 200
    assert restore_replay.json()["current_revision"] == 3

    restore_id_conflict = client.post(
        f"/assistant/canvas/documents/{document_id}/revisions/2/restore",
        headers=headers,
        json={
            "expected_revision": 2,
            "mutation_id": "browser-restore-1",
        },
    )
    assert restore_id_conflict.status_code == 409
    assert "different payload" in restore_id_conflict.json()["detail"]

    revisions = client.get(
        f"/assistant/canvas/documents/{document_id}/revisions",
        headers=headers,
    )
    assert revisions.status_code == 200
    revision_rows = revisions.json()
    assert [row["revision_number"] for row in revision_rows] == [3, 2, 1]
    assert [row["edit_source"] for row in revision_rows] == [
        "restore",
        "user",
        "user",
    ]
    assert all(row["created_at"] for row in revision_rows)

    archived = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 3,
            "status": "archived",
            "mutation_id": "browser-archive-1",
        },
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert archived.json()["current_revision"] == 4

    archived_replay = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 3,
            "status": "archived",
            "mutation_id": "browser-archive-1",
        },
    )
    assert archived_replay.status_code == 200
    assert archived_replay.json()["current_revision"] == 4

    visible_workspace = client.get(
        f"/assistant/conversations/{conversation.id}/canvas",
        headers=headers,
    ).json()
    assert visible_workspace == {"documents": [], "active_document_id": None}
    archived_workspace = client.get(
        f"/assistant/conversations/{conversation.id}/canvas?include_archived=true",
        headers=headers,
    ).json()
    assert archived_workspace["active_document_id"] is None
    assert archived_workspace["documents"][0]["status"] == "archived"

    rejected_edit = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 4,
            "content": "No debe guardarse",
            "mutation_id": "browser-after-archive",
        },
    )
    assert rejected_edit.status_code == 409
    assert rejected_edit.json()["detail"] == "Canvas document is archived"

    conversation.status = "archived"
    db.commit()
    creation_replay_after_archive = client.post(
        f"/assistant/conversations/{conversation.id}/canvas/documents",
        headers=headers,
        json=create_payload,
    )
    assert creation_replay_after_archive.status_code == 201
    assert creation_replay_after_archive.json()["id"] == document_id

    mutation_replay_after_archive = client.patch(
        f"/assistant/canvas/documents/{document_id}",
        headers=headers,
        json={
            "expected_revision": 3,
            "status": "archived",
            "mutation_id": "browser-archive-1",
        },
    )
    assert mutation_replay_after_archive.status_code == 200
    assert mutation_replay_after_archive.json()["current_revision"] == 4

    rejected_create = client.post(
        f"/assistant/conversations/{conversation.id}/canvas/documents",
        headers=headers,
        json={"title": "Otro borrador", "document_type": "other"},
    )
    assert rejected_create.status_code == 409
    assert rejected_create.json()["detail"] == "Conversation is archived"


def test_canvas_access_is_personal_and_organization_scoped(
    client,
    db,
    canvas_user,
    make_user,
    make_organization,
    grant_permissions,
):
    user, organization = canvas_user
    conversation = _conversation(db, user)
    inaccessible_organization = make_organization(name="Ayuntamiento ajeno")
    headers = headers_for(user)

    inaccessible = client.post(
        f"/assistant/conversations/{conversation.id}/canvas/documents",
        headers=headers,
        json={
            "title": "Borrador ajeno",
            "document_type": "report",
            "organization_id": inaccessible_organization.id,
        },
    )
    assert inaccessible.status_code == 404
    assert inaccessible.json()["detail"] == "Organization not found"

    created = client.post(
        f"/assistant/conversations/{conversation.id}/canvas/documents",
        headers=headers,
        json={
            "title": "Borrador propio",
            "document_type": "report",
            "organization_id": organization.id,
        },
    ).json()

    no_permission_user = make_user(full_name="Sin permiso")
    forbidden = client.get(
        f"/assistant/canvas/documents/{created['id']}",
        headers=headers_for(no_permission_user),
    )
    assert forbidden.status_code == 403

    other_user = make_user(full_name="Otra secretaria")
    other_organization = make_organization(name="Otro ayuntamiento")
    grant_permissions(other_user, other_organization, ["assistant.use"])
    other_headers = headers_for(other_user)
    for path in (
        f"/assistant/canvas/documents/{created['id']}",
        f"/assistant/canvas/documents/{created['id']}/revisions",
    ):
        response = client.get(path, headers=other_headers)
        assert response.status_code == 404

    foreign_update = client.patch(
        f"/assistant/canvas/documents/{created['id']}",
        headers=other_headers,
        json={"expected_revision": 1, "content": "intrusión"},
    )
    assert foreign_update.status_code == 404

    second_conversation = _conversation(db, user, title="Otra conversación")
    wrong_conversation = client.put(
        f"/assistant/conversations/{second_conversation.id}/canvas/active",
        headers=headers,
        json={"document_id": created["id"]},
    )
    assert wrong_conversation.status_code == 409


def test_direct_canvas_tools_are_turn_scoped_private_and_idempotent(
    db,
    canvas_user,
    monkeypatch,
):
    user, organization = canvas_user
    conversation = _conversation(db, user)
    first_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content="Crea el borrador",
    )
    db.add(first_message)
    db.commit()
    context = ToolContext(
        conversation_id=conversation.id,
        user_message_id=first_message.id,
        tool_call_id="call-reused-by-provider",
    )
    sentinel = "SECRETO-LIENZO-9fdd0a"
    tool_input = {
        "title": "Ordenanza privada",
        "document_type": "municipal_ordinance",
        "organization_id": organization.id,
        "content": sentinel,
    }

    created = execute_tool(
        db,
        user,
        "create_canvas_document",
        tool_input,
        context,
    )
    assert created.ok is True
    replay = execute_tool(
        db,
        user,
        "create_canvas_document",
        tool_input,
        context,
    )
    assert replay.ok is True
    assert db.scalar(select(func.count(AssistantCanvasDocument.id))) == 1
    assert db.scalar(select(func.count(AssistantCanvasRevision.id))) == 1
    assert context.canvas_content_seen is True

    audited = tool_input_for_activity("create_canvas_document", tool_input)
    assert sentinel not in str(audited)
    assert audited["content"] == {
        "redacted": True,
        "char_count": len(sentinel),
        "sha256": hashlib.sha256(sentinel.encode("utf-8")).hexdigest(),
    }

    web_called = False

    def forbidden_web_search(*args, **kwargs):
        nonlocal web_called
        web_called = True
        raise AssertionError("web search must stay behind the canvas taint boundary")

    monkeypatch.setattr(
        "app.assistant.tools.web_search_client.search",
        forbidden_web_search,
    )
    blocked_after_create = execute_tool(
        db,
        user,
        "web_search",
        {"query": sentinel},
        context,
    )
    assert blocked_after_create.ok is False
    assert blocked_after_create.content == CANVAS_EXTERNAL_TOOL_BLOCKED
    assert web_called is False

    opened = execute_tool(
        db,
        user,
        "get_canvas_document",
        {},
        context,
    )
    assert opened.ok is True
    assert sentinel in opened.content

    blocked = execute_tool(
        db,
        user,
        "web_search",
        {"query": sentinel},
        context,
    )
    assert blocked.ok is False
    assert blocked.content == CANVAS_EXTERNAL_TOOL_BLOCKED
    assert web_called is False

    second_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content="Continúa con el borrador",
    )
    db.add(second_message)
    db.commit()
    document = db.scalar(select(AssistantCanvasDocument))
    second_context = ToolContext(
        conversation_id=conversation.id,
        user_message_id=second_message.id,
        tool_call_id="call-reused-by-provider",
    )
    reopened = execute_tool(
        db,
        user,
        "get_canvas_document",
        {"document_id": document.id},
        second_context,
    )
    assert reopened.ok is True
    updated = execute_tool(
        db,
        user,
        "update_canvas_document",
        {
            "document_id": document.id,
            "expected_revision": 1,
            "content": f"{sentinel}\n\nArtículo 1.",
        },
        second_context,
    )
    assert updated.ok is True
    assert db.scalar(select(AssistantCanvasDocument)).current_revision == 2

    stale = execute_tool(
        db,
        user,
        "update_canvas_document",
        {
            "document_id": document.id,
            "expected_revision": 2,
            "content": "Un turno obsoleto no puede escribir",
        },
        context,
    )
    assert stale.ok is False
    assert "turno fue sustituido" in stale.content
    assert db.scalar(select(AssistantCanvasDocument)).current_revision == 2


def test_canvas_tools_require_same_turn_read_and_revision_list_blocks_web(
    db,
    canvas_user,
    monkeypatch,
):
    user, organization = canvas_user
    conversation = _conversation(db, user)
    document = AssistantCanvasDocument(
        conversation_id=conversation.id,
        organization_id=organization.id,
        document_type="report",
        title="Informe privado",
        content="Contenido privado del historial",
        current_revision=1,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(document)
    db.flush()
    db.add(
        AssistantCanvasRevision(
            document_id=document.id,
            revision_number=1,
            title=document.title,
            content=document.content,
            content_sha256=hashlib.sha256(
                document.content.encode("utf-8")
            ).hexdigest(),
            edit_source="user",
            created_by_id=user.id,
        )
    )
    message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content="Edita el informe",
    )
    db.add(message)
    db.commit()
    context = ToolContext(
        conversation_id=conversation.id,
        user_message_id=message.id,
        tool_call_id="canvas-update-without-read",
    )

    rejected = execute_tool(
        db,
        user,
        "update_canvas_document",
        {
            "document_id": document.id,
            "expected_revision": 1,
            "content": "Sobrescritura sin lectura",
        },
        context,
    )
    assert rejected.ok is False
    assert "get_canvas_document" in rejected.content

    listed = execute_tool(
        db,
        user,
        "list_canvas_revisions",
        {"document_id": document.id},
        context,
    )
    assert listed.ok is True
    assert "Contenido privado" in listed.content

    web_called = False

    def forbidden_web_search(*args, **kwargs):
        nonlocal web_called
        web_called = True
        raise AssertionError("web search must remain blocked after revision history")

    monkeypatch.setattr(
        "app.assistant.tools.web_search_client.search",
        forbidden_web_search,
    )
    blocked = execute_tool(
        db,
        user,
        "web_search",
        {"query": "Contenido privado"},
        context,
    )
    assert blocked.ok is False
    assert blocked.content == CANVAS_EXTERNAL_TOOL_BLOCKED
    assert web_called is False
