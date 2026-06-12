"""Tests for the /requirements endpoints (requirements intake domain)."""

import pytest
from conftest import headers_for, unique_suffix

from app.projects.models import Project
from app.requirements.models import Requirement


@pytest.fixture()
def make_project(db):
    def _make_project(organization, name: str | None = None) -> Project:
        project = Project(
            name=name or f"Proj {unique_suffix()}",
            organization_id=organization.id,
        )
        db.add(project)
        db.commit()
        return project

    return _make_project


@pytest.fixture()
def make_requirement(db):
    def _make_requirement(
        organization,
        *,
        created_by=None,
        status: str = "draft",
        title: str | None = None,
        project=None,
    ) -> Requirement:
        requirement = Requirement(
            organization_id=organization.id,
            project_id=project.id if project is not None else None,
            title=title or f"Req {unique_suffix()}",
            status=status,
            created_by_id=created_by.id if created_by is not None else None,
        )
        db.add(requirement)
        db.commit()
        return requirement

    return _make_requirement


def create_payload(organization, **overrides) -> dict:
    payload = {
        "organization_id": organization.id,
        "title": f"Requirement {unique_suffix()}",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. POST /requirements: permission and created_by stamping
# ---------------------------------------------------------------------------


def test_create_requirement_without_create_permission_returns_403(
    client, make_user, make_organization, add_member
):
    organization = make_organization()
    user = make_user()
    add_member(user, organization)  # member, but no permissions

    response = client.post(
        "/requirements",
        json=create_payload(organization),
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: requirements.create"


def test_create_requirement_stamps_creator_as_created_by(
    client, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    user = make_user()
    grant_permissions(user, organization, ["requirements.create"])

    response = client.post(
        "/requirements",
        json=create_payload(organization, title="Padron automation"),
        headers=headers_for(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Padron automation"
    assert body["organization_id"] == organization.id
    assert body["status"] == "draft"
    assert body["created_by_id"] == user.id
    assert body["created_by"]["email"] == user.email
    assert body["reviewed_by_id"] is None


# ---------------------------------------------------------------------------
# 2. GET /requirements visibility rules
# ---------------------------------------------------------------------------


def test_creator_with_only_create_sees_own_but_not_others_requirements(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    creator = make_user()
    other_author = make_user()
    grant_permissions(creator, organization, ["requirements.create"])

    own = make_requirement(organization, created_by=creator)
    foreign = make_requirement(organization, created_by=other_author)

    response = client.get("/requirements", headers=headers_for(creator))

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert own.id in ids
    assert foreign.id not in ids


def test_user_with_view_permission_sees_all_org_requirements(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    viewer = make_user()
    author_a = make_user()
    author_b = make_user()
    grant_permissions(viewer, organization, ["requirements.view"])

    req_a = make_requirement(organization, created_by=author_a)
    req_b = make_requirement(organization, created_by=author_b)

    response = client.get("/requirements", headers=headers_for(viewer))

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert req_a.id in ids
    assert req_b.id in ids


def test_user_of_other_org_sees_no_requirements_of_the_org(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    org_a = make_organization()
    org_b = make_organization()
    outsider = make_user()
    author = make_user()
    grant_permissions(outsider, org_b, ["requirements.view", "requirements.create"])

    requirement = make_requirement(org_a, created_by=author)

    response = client.get("/requirements", headers=headers_for(outsider))

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert requirement.id not in ids


# ---------------------------------------------------------------------------
# 3. Creating directly with a review status requires requirements.review
# ---------------------------------------------------------------------------


def test_create_with_review_status_and_only_create_permission_returns_403(
    client, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    user = make_user()
    grant_permissions(user, organization, ["requirements.create"])

    response = client.post(
        "/requirements",
        json=create_payload(organization, status="in_review"),
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: requirements.review"


def test_create_with_review_status_and_review_permission_stamps_reviewed_by(
    client, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    user = make_user()
    grant_permissions(
        user, organization, ["requirements.create", "requirements.review"]
    )

    response = client.post(
        "/requirements",
        json=create_payload(organization, status="in_review"),
        headers=headers_for(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "in_review"
    assert body["reviewed_by_id"] == user.id
    assert body["reviewed_by"]["email"] == user.email


# ---------------------------------------------------------------------------
# 4. PATCH content fields: edit permission and status gating
# ---------------------------------------------------------------------------


def test_patch_content_on_draft_without_edit_permission_returns_403(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    creator = make_user()
    grant_permissions(creator, organization, ["requirements.create"])
    requirement = make_requirement(organization, created_by=creator, status="draft")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"title": "New title"},
        headers=headers_for(creator),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: requirements.edit"


def test_patch_content_on_draft_with_edit_permission_succeeds(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    editor = make_user()
    grant_permissions(editor, organization, ["requirements.edit"])
    requirement = make_requirement(organization, status="draft")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"title": "Updated title", "summary": "Updated summary"},
        headers=headers_for(editor),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Updated title"
    assert body["summary"] == "Updated summary"


def test_patch_content_on_in_review_requirement_returns_409(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    editor = make_user()
    grant_permissions(editor, organization, ["requirements.edit"])
    requirement = make_requirement(organization, status="in_review")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"title": "Should be blocked"},
        headers=headers_for(editor),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "Requirement status does not allow content edits"
    )


def test_patch_content_on_in_review_with_manage_permission_succeeds(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    manager = make_user()
    grant_permissions(manager, organization, ["requirements.manage"])
    requirement = make_requirement(organization, status="in_review")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"title": "Edited by manager"},
        headers=headers_for(manager),
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Edited by manager"


# ---------------------------------------------------------------------------
# 5. PATCH status transitions: archive and review permissions
# ---------------------------------------------------------------------------


def test_patch_status_archived_without_archive_permission_returns_403(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    editor = make_user()
    grant_permissions(editor, organization, ["requirements.edit"])
    requirement = make_requirement(organization, status="draft")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"status": "archived"},
        headers=headers_for(editor),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: requirements.archive"


def test_patch_status_archived_with_archive_permission_succeeds(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    archiver = make_user()
    grant_permissions(archiver, organization, ["requirements.archive"])
    requirement = make_requirement(organization, status="draft")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"status": "archived"},
        headers=headers_for(archiver),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


def test_patch_status_in_review_without_review_permission_returns_403(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    editor = make_user()
    grant_permissions(editor, organization, ["requirements.edit"])
    requirement = make_requirement(organization, status="submitted")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"status": "in_review"},
        headers=headers_for(editor),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: requirements.review"


def test_patch_status_in_review_stamps_reviewed_by(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    reviewer = make_user()
    grant_permissions(reviewer, organization, ["requirements.review"])
    requirement = make_requirement(organization, status="submitted")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"status": "in_review"},
        headers=headers_for(reviewer),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_review"
    assert body["reviewed_by_id"] == reviewer.id


def test_patch_status_accepted_stamps_reviewed_by(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    reviewer = make_user()
    grant_permissions(reviewer, organization, ["requirements.review"])
    requirement = make_requirement(organization, status="in_review")

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"status": "accepted"},
        headers=headers_for(reviewer),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["reviewed_by_id"] == reviewer.id


# ---------------------------------------------------------------------------
# 6. project_id must belong to the requirement organization
# ---------------------------------------------------------------------------


def test_create_with_project_of_other_org_returns_409(
    client, make_user, make_organization, grant_permissions, make_project
):
    org_a = make_organization()
    org_b = make_organization()
    user = make_user()
    grant_permissions(user, org_a, ["requirements.create"])
    foreign_project = make_project(org_b)

    response = client.post(
        "/requirements",
        json=create_payload(org_a, project_id=foreign_project.id),
        headers=headers_for(user),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Project does not belong to the requirement organization"
    )


def test_create_with_project_of_same_org_succeeds(
    client, make_user, make_organization, grant_permissions, make_project
):
    organization = make_organization()
    user = make_user()
    grant_permissions(user, organization, ["requirements.create"])
    project = make_project(organization)

    response = client.post(
        "/requirements",
        json=create_payload(organization, project_id=project.id),
        headers=headers_for(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["project_id"] == project.id
    assert body["project"]["id"] == project.id


def test_patch_project_of_other_org_returns_409(
    client,
    make_user,
    make_organization,
    grant_permissions,
    make_project,
    make_requirement,
):
    org_a = make_organization()
    org_b = make_organization()
    editor = make_user()
    grant_permissions(editor, org_a, ["requirements.edit"])
    requirement = make_requirement(org_a, status="draft")
    foreign_project = make_project(org_b)

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={"project_id": foreign_project.id},
        headers=headers_for(editor),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Project does not belong to the requirement organization"
    )


def test_create_with_missing_project_returns_404(
    client, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    user = make_user()
    grant_permissions(user, organization, ["requirements.create"])

    response = client.post(
        "/requirements",
        json=create_payload(organization, project_id=999999),
        headers=headers_for(user),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found"


# ---------------------------------------------------------------------------
# 7. GET /requirements/{id}: outsider access and missing id
# ---------------------------------------------------------------------------


def test_get_requirement_as_org_outsider_returns_403(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    org_a = make_organization()
    org_b = make_organization()
    outsider = make_user()
    grant_permissions(outsider, org_b, ["requirements.view"])
    requirement = make_requirement(org_a)

    response = client.get(
        f"/requirements/{requirement.id}",
        headers=headers_for(outsider),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Requirement access denied"


def test_get_missing_requirement_returns_404(client, make_user):
    user = make_user()

    response = client.get("/requirements/999999", headers=headers_for(user))

    assert response.status_code == 404
    assert response.json()["detail"] == "Requirement not found"


def test_creator_with_only_create_can_get_own_requirement(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    creator = make_user()
    grant_permissions(creator, organization, ["requirements.create"])
    requirement = make_requirement(organization, created_by=creator)

    response = client.get(
        f"/requirements/{requirement.id}",
        headers=headers_for(creator),
    )

    assert response.status_code == 200
    assert response.json()["id"] == requirement.id


# ---------------------------------------------------------------------------
# 8. Requirement messages
# ---------------------------------------------------------------------------


def test_user_with_view_access_can_post_and_get_messages(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    viewer = make_user()
    grant_permissions(viewer, organization, ["requirements.view"])
    requirement = make_requirement(organization)

    post_response = client.post(
        f"/requirements/{requirement.id}/messages",
        json={"body": "Necesitamos aclarar el plazo", "message_type": "question"},
        headers=headers_for(viewer),
    )

    assert post_response.status_code == 201
    message = post_response.json()
    assert message["requirement_id"] == requirement.id
    assert message["body"] == "Necesitamos aclarar el plazo"
    assert message["message_type"] == "question"
    assert message["author_id"] == viewer.id
    assert message["author"]["email"] == viewer.email

    get_response = client.get(
        f"/requirements/{requirement.id}/messages",
        headers=headers_for(viewer),
    )

    assert get_response.status_code == 200
    messages = get_response.json()
    assert [m["id"] for m in messages] == [message["id"]]


def test_user_without_view_access_cannot_get_or_post_messages(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    org_a = make_organization()
    org_b = make_organization()
    outsider = make_user()
    grant_permissions(outsider, org_b, ["requirements.view"])
    requirement = make_requirement(org_a)

    get_response = client.get(
        f"/requirements/{requirement.id}/messages",
        headers=headers_for(outsider),
    )
    post_response = client.post(
        f"/requirements/{requirement.id}/messages",
        json={"body": "should not be allowed"},
        headers=headers_for(outsider),
    )

    assert get_response.status_code == 403
    assert get_response.json()["detail"] == "Requirement access denied"
    assert post_response.status_code == 403
    assert post_response.json()["detail"] == "Requirement access denied"


# ---------------------------------------------------------------------------
# 9. Archived requirements hidden by default in listings
# ---------------------------------------------------------------------------


def test_list_hides_archived_requirements_by_default(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    viewer = make_user()
    grant_permissions(viewer, organization, ["requirements.view"])
    active = make_requirement(organization, status="draft")
    archived = make_requirement(organization, status="archived")

    response = client.get("/requirements", headers=headers_for(viewer))

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert active.id in ids
    assert archived.id not in ids


def test_list_with_include_archived_true_shows_archived_requirements(
    client, make_user, make_organization, grant_permissions, make_requirement
):
    organization = make_organization()
    viewer = make_user()
    grant_permissions(viewer, organization, ["requirements.view"])
    active = make_requirement(organization, status="draft")
    archived = make_requirement(organization, status="archived")

    response = client.get(
        "/requirements",
        params={"include_archived": "true"},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert active.id in ids
    assert archived.id in ids
