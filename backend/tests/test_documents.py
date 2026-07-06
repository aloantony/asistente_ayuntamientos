"""Tests for the documents domain: upload, listing, download, access control
and archiving."""

import hashlib
import io

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.documents.models import DocumentWorkArtifact
from app.projects.models import Project, project_users
from conftest import headers_for, unique_suffix


def make_project(db: Session, organization, name: str | None = None) -> Project:
    project = Project(
        name=name or f"Project {unique_suffix()}",
        organization_id=organization.id,
    )
    db.add(project)
    db.commit()
    return project


def add_project_member(db: Session, project: Project, user) -> None:
    db.execute(
        insert(project_users).values(project_id=project.id, user_id=user.id)
    )
    db.commit()


def upload_document(
    client,
    headers: dict[str, str],
    project_id: int,
    *,
    content: bytes = b"hello ayuntamiento",
    filename: str = "notes.txt",
    content_type: str = "text/plain",
):
    return client.post(
        f"/projects/{project_id}/documents",
        headers=headers,
        files={"file": (filename, io.BytesIO(content), content_type)},
    )


def test_project_member_with_upload_permission_can_upload_text_file(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    user = make_user()
    grant_permissions(user, organization, ["documents.upload"])
    add_project_member(db, project, user)

    content = b"acta del pleno municipal"
    response = upload_document(
        client,
        headers_for(user),
        project.id,
        content=content,
        filename="acta.txt",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "acta.txt"
    assert body["size_bytes"] == len(content)
    assert body["checksum_sha256"] == hashlib.sha256(content).hexdigest()
    assert body["status"] == "active"


def test_uploaded_document_appears_in_project_list_for_viewer(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    user = make_user()
    grant_permissions(user, organization, ["documents.upload", "documents.view"])
    add_project_member(db, project, user)

    upload = upload_document(client, headers_for(user), project.id)
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    response = client.get(
        f"/projects/{project.id}/documents",
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert document_id in [doc["id"] for doc in response.json()]


def test_uploaded_document_bytes_round_trip_via_download(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    user = make_user()
    grant_permissions(user, organization, ["documents.upload", "documents.view"])
    add_project_member(db, project, user)

    content = b"contenido exacto del documento\n" * 5
    upload = upload_document(
        client, headers_for(user), project.id, content=content
    )
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    response = client.get(
        f"/documents/{document_id}/download",
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.content == content


def test_upload_unsupported_content_type_returns_415(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    user = make_user()
    grant_permissions(user, organization, ["documents.upload"])
    add_project_member(db, project, user)

    response = upload_document(
        client,
        headers_for(user),
        project.id,
        content=b"PK\x03\x04 fake zip",
        filename="archive.zip",
        content_type="application/zip",
    )

    assert response.status_code == 415


def test_upload_empty_file_returns_400(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    user = make_user()
    grant_permissions(user, organization, ["documents.upload"])
    add_project_member(db, project, user)

    response = upload_document(
        client,
        headers_for(user),
        project.id,
        content=b"",
        filename="empty.txt",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Empty document upload"


def test_upload_permission_without_project_membership_returns_403(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    user = make_user()
    # Org member with documents.upload, but no project assignment
    # (no project_users row, no group assignment, no projects.view_all).
    grant_permissions(user, organization, ["documents.upload"])

    response = upload_document(client, headers_for(user), project.id)

    assert response.status_code == 403


def test_user_from_other_organization_cannot_get_document(
    client, db, make_organization, make_user, superuser, grant_permissions
):
    organization_a = make_organization()
    organization_b = make_organization()
    project = make_project(db, organization_a)

    upload = upload_document(client, headers_for(superuser), project.id)
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    outsider = make_user()
    grant_permissions(outsider, organization_b, ["documents.view"])

    response = client.get(
        f"/documents/{document_id}",
        headers=headers_for(outsider),
    )

    assert response.status_code == 403


def test_manage_permission_allows_viewing_without_project_membership(
    client, db, make_organization, make_user, superuser, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)

    upload = upload_document(client, headers_for(superuser), project.id)
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    manager = make_user()
    # Org-scoped documents.manage, but NOT a project member.
    grant_permissions(manager, organization, ["documents.manage"])

    response = client.get(
        f"/documents/{document_id}",
        headers=headers_for(manager),
    )

    assert response.status_code == 200
    assert response.json()["id"] == document_id


def test_patch_archive_with_only_view_permission_returns_403(
    client, db, make_organization, make_user, superuser, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)

    upload = upload_document(client, headers_for(superuser), project.id)
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    viewer = make_user()
    grant_permissions(viewer, organization, ["documents.view"])
    add_project_member(db, project, viewer)

    response = client.patch(
        f"/documents/{document_id}",
        headers=headers_for(viewer),
        json={"status": "archived"},
    )

    assert response.status_code == 403


def test_archived_document_hidden_by_default_and_shown_with_include_archived(
    client, db, make_organization, make_user, superuser, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)

    upload = upload_document(client, headers_for(superuser), project.id)
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    archiver = make_user()
    grant_permissions(
        archiver, organization, ["documents.view", "documents.archive"]
    )
    add_project_member(db, project, archiver)

    patch_response = client.patch(
        f"/documents/{document_id}",
        headers=headers_for(archiver),
        json={"status": "archived"},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["status"] == "archived"

    default_list = client.get(
        f"/projects/{project.id}/documents",
        headers=headers_for(archiver),
    )
    assert default_list.status_code == 200
    assert document_id not in [doc["id"] for doc in default_list.json()]

    archived_list = client.get(
        f"/projects/{project.id}/documents",
        headers=headers_for(archiver),
        params={"include_archived": "true"},
    )
    assert archived_list.status_code == 200
    assert document_id in [doc["id"] for doc in archived_list.json()]


def test_download_nonexistent_document_returns_404(client, superuser):
    response = client.get(
        "/documents/999999/download",
        headers=headers_for(superuser),
    )

    assert response.status_code == 404


def test_project_member_can_prepare_document_work_artifact_as_reviewable_draft(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    user = make_user()
    grant_permissions(user, organization, ["documents.draft", "documents.view"])
    add_project_member(db, project, user)

    response = client.post(
        f"/projects/{project.id}/document-work-artifacts",
        headers=headers_for(user),
        json={
            "artifact_type": "report",
            "title": "Informe de contratación menor",
            "content": "Borrador de trabajo para revisión de secretaría.",
            "source_summary": "Preparado desde notas internas, sin enviar documentos a la web.",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["artifact_type"] == "report"
    assert body["status"] == "draft"
    assert body["review_notes"] is None
    assert body["reviewed_by_id"] is None
    assert body["export_format"] is None
    assert body["export_requested_at"] is None
    assert body["exported_at"] is None
    assert body["source_document_ids"] == []

    artifact = db.get(DocumentWorkArtifact, body["id"])
    assert artifact is not None
    assert artifact.status == "draft"
    assert artifact.project_id == project.id
    assert artifact.created_by_id == user.id


def test_document_work_artifact_export_request_requires_human_review_approval(
    client, db, make_organization, make_user, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    author = make_user()
    reviewer = make_user()
    exporter = make_user()
    grant_permissions(author, organization, ["documents.draft", "documents.view"])
    grant_permissions(reviewer, organization, ["documents.review", "documents.view"])
    grant_permissions(exporter, organization, ["documents.export", "documents.view"])
    add_project_member(db, project, author)
    add_project_member(db, project, reviewer)
    add_project_member(db, project, exporter)

    created = client.post(
        f"/projects/{project.id}/document-work-artifacts",
        headers=headers_for(author),
        json={
            "artifact_type": "communication",
            "title": "Comunicación a vecinos",
            "content": "Texto de trabajo pendiente de revisión.",
        },
    )
    assert created.status_code == 201
    artifact_id = created.json()["id"]

    premature_export = client.patch(
        f"/document-work-artifacts/{artifact_id}",
        headers=headers_for(exporter),
        json={"status": "export_requested", "export_format": "docx"},
    )

    assert premature_export.status_code == 409
    assert "approved" in premature_export.json()["detail"]

    reviewed = client.patch(
        f"/document-work-artifacts/{artifact_id}",
        headers=headers_for(reviewer),
        json={"status": "approved", "review_notes": "Revisado por secretaría."},
    )

    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["reviewed_by_id"] == reviewer.id

    export_requested = client.patch(
        f"/document-work-artifacts/{artifact_id}",
        headers=headers_for(exporter),
        json={"status": "export_requested", "export_format": "docx"},
    )

    assert export_requested.status_code == 200
    body = export_requested.json()
    assert body["status"] == "export_requested"
    assert body["export_format"] == "docx"
    assert body["export_requested_by_id"] == exporter.id
    assert body["export_requested_at"] is not None
    assert body["exported_at"] is None
