import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from openpyxl import Workbook
from pydantic import ValidationError
from sqlalchemy import insert, select

from app.assistant import attachments as assistant_attachments
from app.assistant.models import (
    AssistantConversation,
    AssistantMemoryEntry,
    AssistantMessageAttachment,
)
from app.assistant.routes import get_gateway
from app.assistant.schemas import AssistantUserMessageCreate
from app.core.config import settings
from app.main import app
from app.projects.models import Project, project_users
from conftest import headers_for, unique_suffix


def text_response(text: str):
    return SimpleNamespace(
        model="fake-model",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        deltas=[],
        provider_state=(),
    )


class RecordingGateway:
    enabled = True
    runtime_healthy = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def complete_stream(self, **kwargs):
        response = self.complete(**kwargs)
        if False:
            yield None
        return response


@pytest.fixture()
def attachment_gateway(client):
    gateways: list[RecordingGateway] = []

    def use(responses):
        gateway = RecordingGateway(responses)
        gateways.append(gateway)
        app.dependency_overrides[get_gateway] = lambda: gateway
        return gateway

    yield use
    app.dependency_overrides.pop(get_gateway, None)


@pytest.fixture()
def attachment_user(make_user, make_organization, grant_permissions):
    user = make_user(full_name="Attachment User")
    organization = make_organization(name="Attachment Organization")
    grant_permissions(
        user,
        organization,
        [
            "assistant.use",
            "assistant.memory.propose",
            "assistant.web.search",
            "documents.view",
            "requirements.create",
            "requirements.view",
        ],
    )
    return user, organization


def create_project(db, organization, user=None):
    project = Project(
        name=f"Attachment project {unique_suffix()}",
        organization_id=organization.id,
    )
    db.add(project)
    db.commit()
    if user is not None:
        db.execute(
            insert(project_users).values(project_id=project.id, user_id=user.id)
        )
        db.commit()
    return project


def upload_document(
    client,
    uploader,
    project,
    *,
    content: bytes,
    filename: str,
    content_type: str,
):
    response = client.post(
        f"/projects/{project.id}/documents",
        headers=headers_for(uploader),
        files={"file": (filename, io.BytesIO(content), content_type)},
    )
    assert response.status_code == 201
    return response.json()


def create_conversation(client, user):
    response = client.post(
        "/assistant/conversations",
        headers=headers_for(user),
        json={},
    )
    assert response.status_code == 201
    return response.json()


def parse_sse_events(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in payload.strip().split("\n\n"):
        event_name = "message"
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if data_lines:
            events.append((event_name, json.loads("\n".join(data_lines))))
    return events


def test_attachment_context_is_ephemeral_non_persistent_and_disables_egress(
    client,
    db,
    attachment_user,
    attachment_gateway,
    superuser,
    monkeypatch,
):
    user, organization = attachment_user
    project = create_project(db, organization, user)
    secret = "CLAVE-INTERNA-ADJUNTO-9f4b"
    document = upload_document(
        client,
        superuser,
        project,
        content=f"Informe municipal\n{secret}\nFin".encode(),
        filename="informe-interno.txt",
        content_type="text/plain",
    )
    monkeypatch.setattr(settings, "web_search_provider", "brave")
    monkeypatch.setattr(settings, "brave_search_api_key", "test-key")
    monkeypatch.setattr(settings, "brave_search_storage_rights_confirmed", True)
    gateway = attachment_gateway(
        [text_response("Resumen realizado."), text_response("Seguimos.")]
    )
    conversation = create_conversation(client, user)

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        headers=headers_for(user),
        json={
            "content": "Resume el archivo adjunto.",
            "attachment_ids": [document["id"]],
        },
    )

    assert first.status_code == 200
    first_user_message = first.json()["messages"][-2]
    assert first_user_message["attachments"] == [
        {
            "id": first_user_message["attachments"][0]["id"],
            "document_id": document["id"],
            "project_id": project.id,
            "project_name": project.name,
            "filename": "informe-interno.txt",
            "content_type": "text/plain",
            "size_bytes": document["size_bytes"],
            "context_status": "ready",
            "context_char_count": len(f"Informe municipal\n{secret}\nFin"),
        }
    ]
    assert secret in gateway.calls[0]["messages"][-1]["content"]
    tool_names = {tool["name"] for tool in gateway.calls[0]["tools"]}
    assert "web_search" not in tool_names
    assert "read_web_page" not in tool_names
    assert "propose_memory_entry" not in tool_names
    assert "create_requirement" in tool_names

    assert "context_text" not in AssistantMessageAttachment.__table__.columns
    relation = db.scalar(select(AssistantMessageAttachment))
    assert relation is not None
    assert relation.context_status == "ready"
    assert not hasattr(relation, "context_text")
    assert db.scalar(select(AssistantMemoryEntry)) is None

    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        headers=headers_for(user),
        json={"content": "Ahora responde sin volver a leer el archivo."},
    )

    assert second.status_code == 200
    second_provider_payload = repr(gateway.calls[1]["messages"])
    assert secret not in second_provider_payload
    assert "CONTEXTO DE ADJUNTOS" not in second_provider_payload


def test_cross_tenant_attachment_is_hidden_before_message_persistence(
    client,
    db,
    attachment_user,
    attachment_gateway,
    make_organization,
    superuser,
):
    user, _ = attachment_user
    foreign_organization = make_organization(name="Foreign attachment org")
    foreign_project = create_project(db, foreign_organization)
    document = upload_document(
        client,
        superuser,
        foreign_project,
        content=b"foreign internal material",
        filename="foreign.txt",
        content_type="text/plain",
    )
    gateway = attachment_gateway([text_response("Must not run")])
    conversation = create_conversation(client, user)

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        headers=headers_for(user),
        json={"content": "Lee esto", "attachment_ids": [document["id"]]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Assistant attachment not found"
    assert gateway.calls == []
    stored = db.get(AssistantConversation, conversation["id"])
    assert stored is not None
    assert stored.messages == []


def test_image_attachment_is_visible_but_never_sent_as_model_context(
    client,
    db,
    attachment_user,
    attachment_gateway,
    superuser,
):
    user, organization = attachment_user
    project = create_project(db, organization, user)
    binary_marker = "BINARY-PIXELS-MUST-NOT-EGRESS"
    document = upload_document(
        client,
        superuser,
        project,
        content=(b"\x89PNG\r\n\x1a\n" + binary_marker.encode()),
        filename="plano.png",
        content_type="image/png",
    )
    gateway = attachment_gateway([text_response("No puedo interpretar la imagen.")])
    conversation = create_conversation(client, user)

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages/stream",
        headers=headers_for(user),
        json={"content": "Que aparece aqui?", "attachment_ids": [document["id"]]},
    )

    assert response.status_code == 200
    events = parse_sse_events(response.text)
    message_start = next(data for name, data in events if name == "message_start")
    done = next(data for name, data in events if name == "done")
    attachment = message_start["user_message"]["attachments"][0]
    assert attachment["context_status"] == "vision_unavailable"
    assert attachment["context_char_count"] == 0
    assert done["user_message"]["attachments"] == [attachment]
    provider_payload = repr(gateway.calls[0]["messages"])
    assert binary_marker not in provider_payload
    assert "plano.png" not in provider_payload
    assert "image/png" in provider_payload
    assert "ANÁLISIS VISUAL NO DISPONIBLE" in provider_payload
    assert "NO INFIERAS EL CONTENIDO" in provider_payload


def test_extracted_text_strips_unicode_controls_and_pdf_pages_are_bounded(
    tmp_path,
    monkeypatch,
):
    normalized = assistant_attachments._normalize_extracted_text(
        "dato\u202einvertido\x00\nlinea"
    )
    assert normalized == "datoinvertido\nlinea"
    assert (
        assistant_attachments._safe_attachment_metadata(
            "informe\nINSTRUCCION:\u202e falsa.pdf"
        )
        == "informe INSTRUCCION: falsa.pdf"
    )

    extracted_pages: list[int] = []

    class FakePage:
        def __init__(self, number):
            self.number = number

        def extract_text(self):
            extracted_pages.append(self.number)
            return f"page-{self.number}"

    monkeypatch.setattr(
        assistant_attachments,
        "PdfReader",
        lambda _path: SimpleNamespace(
            pages=[
                FakePage(number)
                for number in range(assistant_attachments.MAX_PDF_PAGES + 5)
            ]
        ),
    )

    result = assistant_attachments._extract_pdf_text(tmp_path / "fake.pdf", 10_000)

    assert len(extracted_pages) == assistant_attachments.MAX_PDF_PAGES
    assert f"page-{assistant_attachments.MAX_PDF_PAGES}" not in result


def test_xlsx_and_docx_zip_extraction_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_attachments, "MAX_XLSX_SHEETS", 1)
    monkeypatch.setattr(assistant_attachments, "MAX_XLSX_ROWS_PER_SHEET", 2)
    monkeypatch.setattr(assistant_attachments, "MAX_XLSX_CELLS_PER_ROW", 2)
    workbook = Workbook()
    first_sheet = workbook.active
    first_sheet.title = "Permitida"
    first_sheet.append(["A1", "B1", "C1-omitida"])
    first_sheet.append(["A2", "B2", "C2-omitida"])
    first_sheet.append(["A3-omitida", "B3-omitida"])
    second_sheet = workbook.create_sheet("Omitida")
    second_sheet.append(["secreto-otra-hoja"])
    xlsx_path = tmp_path / "limitado.xlsx"
    workbook.save(xlsx_path)
    workbook.close()

    extracted = assistant_attachments._extract_xlsx_text(xlsx_path, 10_000)

    assert "Hoja: Permitida" in extracted
    assert "A1 | B1" in extracted
    assert "A2 | B2" in extracted
    assert "C1-omitida" not in extracted
    assert "A3-omitida" not in extracted
    assert "secreto-otra-hoja" not in extracted

    docx_path = tmp_path / "zip-bomb.docx"
    with zipfile.ZipFile(docx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * 128)
    monkeypatch.setattr(settings, "assistant_attachment_max_extract_bytes", 32)

    assert assistant_attachments._zip_archive_within_limit(docx_path) is False


def test_attachment_identifiers_reject_duplicates_and_configured_maximum(
    monkeypatch,
):
    with pytest.raises(ValidationError):
        AssistantUserMessageCreate(content="consulta", attachment_ids=[1, 1])

    monkeypatch.setattr(settings, "assistant_max_attachments_per_message", 2)
    with pytest.raises(HTTPException) as error:
        assistant_attachments._normalize_attachment_ids([1, 2, 3])

    assert error.value.status_code == 422
    assert "at most 2 attachments" in error.value.detail
