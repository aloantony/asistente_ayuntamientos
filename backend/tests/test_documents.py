"""Tests for the documents domain: upload, listing, download, access control
and archiving."""

import hashlib
import io

from sqlalchemy import insert
from sqlalchemy.orm import Session

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
