"""Tests for municipalities (global reference data) and ordinances,
including the document-linking access rule."""

import io

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.projects.models import Project, project_users
from conftest import headers_for, unique_suffix


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def municipality_payload(**overrides) -> dict:
    payload = {
        "name": f"Municipio {unique_suffix()}",
        "province": "Madrid",
        "autonomous_community": "Comunidad de Madrid",
    }
    payload.update(overrides)
    return payload


def create_municipality(client, headers, **overrides) -> dict:
    response = client.post(
        "/municipalities",
        headers=headers,
        json=municipality_payload(**overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def ordinance_payload(municipality_id: int, **overrides) -> dict:
    payload = {
        "municipality_id": municipality_id,
        "title": f"Ordenanza {unique_suffix()}",
        "topic": f"tema-{unique_suffix()}",
        "ordinance_type": "ordinance",
    }
    payload.update(overrides)
    return payload


def create_ordinance(client, headers, municipality_id: int, **overrides) -> dict:
    response = client.post(
        "/ordinances",
        headers=headers,
        json=ordinance_payload(municipality_id, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_project(db: Session, organization) -> Project:
    project = Project(
        name=f"Project {unique_suffix()}",
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


def upload_document(client, headers, project_id: int) -> dict:
    """Upload a text/plain document (allowed type) and return its JSON body."""
    response = client.post(
        f"/projects/{project_id}/documents",
        headers=headers,
        files={
            "file": (
                "ordenanza.txt",
                io.BytesIO(b"texto consolidado de la ordenanza"),
                "text/plain",
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. POST /municipalities permissions
# ---------------------------------------------------------------------------


def test_create_municipality_without_permission_returns_403(
    client, make_user, make_organization, add_member
):
    user = make_user()
    add_member(user, make_organization())

    response = client.post(
        "/municipalities",
        headers=headers_for(user),
        json=municipality_payload(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: municipalities.create"


def test_create_municipality_with_permission_granted_in_any_org_returns_201(
    client, make_user, make_organization, grant_permissions
):
    # Reference data is global: a grant in any organization is enough.
    user = make_user()
    grant_permissions(user, make_organization(), ["municipalities.create"])

    response = client.post(
        "/municipalities",
        headers=headers_for(user),
        json=municipality_payload(),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "active"


# ---------------------------------------------------------------------------
# 2. Density is computed server-side
# ---------------------------------------------------------------------------


def test_create_municipality_computes_density_overriding_client_value(
    client, superuser
):
    body = create_municipality(
        client,
        headers_for(superuser),
        population=10000,
        surface_km2=40.0,
        density=1.0,  # client-supplied value must be ignored
    )

    assert body["density"] == 10000 / 40.0


# ---------------------------------------------------------------------------
# 3. Archiving a municipality needs municipalities.archive
# ---------------------------------------------------------------------------


def test_archive_municipality_with_only_edit_permission_returns_403(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    editor = make_user()
    grant_permissions(editor, make_organization(), ["municipalities.edit"])

    response = client.patch(
        f"/municipalities/{municipality['id']}",
        headers=headers_for(editor),
        json={"status": "archived"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: municipalities.archive"


def test_archive_municipality_with_archive_permission_succeeds(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    archiver = make_user()
    grant_permissions(archiver, make_organization(), ["municipalities.archive"])

    response = client.patch(
        f"/municipalities/{municipality['id']}",
        headers=headers_for(archiver),
        json={"status": "archived"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


# ---------------------------------------------------------------------------
# 4. Listing hides archived municipalities unless include_archived=true
# ---------------------------------------------------------------------------


def test_list_municipalities_hides_archived_by_default(client, superuser):
    headers = headers_for(superuser)
    active = create_municipality(client, headers)
    archived = create_municipality(client, headers, status="archived")

    response = client.get("/municipalities", headers=headers)

    assert response.status_code == 200
    listed_ids = [m["id"] for m in response.json()]
    assert active["id"] in listed_ids
    assert archived["id"] not in listed_ids


def test_list_municipalities_shows_archived_with_include_archived(client, superuser):
    headers = headers_for(superuser)
    active = create_municipality(client, headers)
    archived = create_municipality(client, headers, status="archived")

    response = client.get(
        "/municipalities",
        headers=headers,
        params={"include_archived": "true"},
    )

    assert response.status_code == 200
    listed_ids = [m["id"] for m in response.json()]
    assert active["id"] in listed_ids
    assert archived["id"] in listed_ids


# ---------------------------------------------------------------------------
# 5. POST /ordinances: permission + municipality validation
# ---------------------------------------------------------------------------


def test_create_ordinance_without_permission_returns_403(
    client, superuser, make_user, make_organization, add_member
):
    municipality = create_municipality(client, headers_for(superuser))
    user = make_user()
    add_member(user, make_organization())

    response = client.post(
        "/ordinances",
        headers=headers_for(user),
        json=ordinance_payload(municipality["id"]),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.create"


def test_create_ordinance_for_archived_municipality_returns_409(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(
        client, headers_for(superuser), status="archived"
    )
    creator = make_user()
    grant_permissions(creator, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(municipality["id"]),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Municipality is archived"


def test_create_ordinance_for_nonexistent_municipality_returns_404(
    client, make_user, make_organization, grant_permissions
):
    creator = make_user()
    grant_permissions(creator, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(99999999),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Municipality not found"


def test_create_ordinance_with_status_archived_requires_archive_permission(
    client, superuser, make_user, make_organization, grant_permissions
):
    # Discovered behavior: creating directly in archived status also needs
    # ordinances.archive, not just ordinances.create.
    municipality = create_municipality(client, headers_for(superuser))
    creator = make_user()
    grant_permissions(creator, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(municipality["id"], status="archived"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.archive"


# ---------------------------------------------------------------------------
# 6-7. Document linking on create
# ---------------------------------------------------------------------------


def test_create_ordinance_with_inaccessible_document_returns_404(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    # Document lives in org A; the creator only belongs to org B, so the
    # document must be hidden behind a 404 even though it exists.
    org_a = make_organization()
    project = make_project(db, org_a)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))

    outsider = make_user()
    grant_permissions(outsider, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(outsider),
        json=ordinance_payload(municipality["id"], document_id=document["id"]),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"


def test_create_ordinance_with_accessible_document_embeds_summary(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))

    member = make_user()
    grant_permissions(member, organization, ["ordinances.create", "documents.view"])
    add_project_member(db, project, member)

    response = client.post(
        "/ordinances",
        headers=headers_for(member),
        json=ordinance_payload(municipality["id"], document_id=document["id"]),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["document_id"] == document["id"]
    assert body["document"]["id"] == document["id"]
    assert body["document"]["original_filename"] == "ordenanza.txt"
    assert body["document"]["content_type"] == "text/plain"
    assert body["document"]["status"] == "active"


def test_superuser_can_create_ordinance_with_document(
    client, db, superuser, make_organization
):
    organization = make_organization()
    project = make_project(db, organization)
    headers = headers_for(superuser)
    document = upload_document(client, headers, project.id)
    municipality = create_municipality(client, headers)

    body = create_ordinance(
        client, headers, municipality["id"], document_id=document["id"]
    )

    assert body["document"]["id"] == document["id"]


# ---------------------------------------------------------------------------
# 8. Document linking on PATCH
# ---------------------------------------------------------------------------


def test_patch_ordinance_with_inaccessible_document_returns_404(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    org_a = make_organization()
    project = make_project(db, org_a)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])

    outsider = make_user()
    grant_permissions(outsider, make_organization(), ["ordinances.edit"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(outsider),
        json={"document_id": document["id"]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"


def test_patch_ordinance_with_accessible_document_embeds_summary(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])

    member = make_user()
    grant_permissions(member, organization, ["ordinances.edit", "documents.view"])
    add_project_member(db, project, member)

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(member),
        json={"document_id": document["id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == document["id"]
    # Specified behavior: like on create, the response embeds the summary of
    # the newly linked document. The implementation returns a stale value
    # because the already-loaded `document` relationship is not refreshed
    # after commit (expire_on_commit=False + identity map reuse).
    assert body["document"] is not None, (
        "PATCH response should embed the newly linked document summary"
    )
    assert body["document"]["id"] == document["id"]
    assert body["document"]["original_filename"] == "ordenanza.txt"


# ---------------------------------------------------------------------------
# 9. PATCH /ordinances permissions: archive vs content edits
# ---------------------------------------------------------------------------


def test_archive_ordinance_with_only_edit_permission_returns_403(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    editor = make_user()
    grant_permissions(editor, make_organization(), ["ordinances.edit"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(editor),
        json={"status": "archived"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.archive"


def test_archive_ordinance_with_archive_permission_succeeds(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    archiver = make_user()
    grant_permissions(archiver, make_organization(), ["ordinances.archive"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(archiver),
        json={"status": "archived"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


def test_edit_ordinance_content_without_edit_permission_returns_403(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    archiver = make_user()
    grant_permissions(archiver, make_organization(), ["ordinances.archive"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(archiver),
        json={"title": "Título modificado"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.edit"


def test_edit_ordinance_content_with_edit_permission_succeeds(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    editor = make_user()
    grant_permissions(editor, make_organization(), ["ordinances.edit"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(editor),
        json={"title": "Ordenanza reguladora actualizada"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Ordenanza reguladora actualizada"
    assert body["updated_by_id"] == editor.id


# ---------------------------------------------------------------------------
# 10. GET /ordinances filters
# ---------------------------------------------------------------------------


def test_list_ordinances_q_filters_by_title(client, superuser):
    headers = headers_for(superuser)
    municipality = create_municipality(client, headers)
    token = unique_suffix()
    match = create_ordinance(
        client, headers, municipality["id"], title=f"Ruidos {token}"
    )
    other = create_ordinance(client, headers, municipality["id"])

    response = client.get("/ordinances", headers=headers, params={"q": token})

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids


def test_list_ordinances_q_filters_by_topic(client, superuser):
    headers = headers_for(superuser)
    municipality = create_municipality(client, headers)
    token = unique_suffix()
    match = create_ordinance(
        client, headers, municipality["id"], topic=f"residuos-{token}"
    )
    other = create_ordinance(client, headers, municipality["id"])

    response = client.get("/ordinances", headers=headers, params={"q": token})

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids


def test_list_ordinances_q_filters_by_municipality_name(client, superuser):
    headers = headers_for(superuser)
    token = unique_suffix()
    named = create_municipality(client, headers, name=f"Villanueva {token}")
    other_muni = create_municipality(client, headers)
    match = create_ordinance(client, headers, named["id"])
    other = create_ordinance(client, headers, other_muni["id"])

    response = client.get("/ordinances", headers=headers, params={"q": token})

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids


def test_list_ordinances_filters_by_municipality_id(client, superuser):
    headers = headers_for(superuser)
    muni_1 = create_municipality(client, headers)
    muni_2 = create_municipality(client, headers)
    match = create_ordinance(client, headers, muni_1["id"])
    other = create_ordinance(client, headers, muni_2["id"])

    response = client.get(
        "/ordinances",
        headers=headers,
        params={"municipality_id": muni_1["id"]},
    )

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids
