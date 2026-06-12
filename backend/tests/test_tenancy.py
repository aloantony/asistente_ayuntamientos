"""Tenancy tests: organization/project access control and scoping.

Covers organization CRUD/membership permissions and project visibility
rules (direct membership, group assignment, view_all, cross-org denial).
"""

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from conftest import headers_for, unique_suffix

from app.organizations.models import Organization, organization_users
from app.projects.models import Project, project_groups, project_users
from app.rbac.models import Group


def create_project(db: Session, organization: Organization, **kwargs) -> Project:
    project = Project(
        name=kwargs.pop("name", f"Proj {unique_suffix()}"),
        organization_id=organization.id,
        **kwargs,
    )
    db.add(project)
    db.commit()
    return project


def create_group(db: Session, organization: Organization) -> Group:
    group = Group(
        name=f"plain-grp-{unique_suffix()}",
        organization_id=organization.id,
    )
    db.add(group)
    db.commit()
    return group


# ---------------------------------------------------------------------------
# 1. POST /organizations is superuser-only
# ---------------------------------------------------------------------------


def test_create_organization_forbidden_for_non_superuser_with_manage(
    client, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    grant_permissions(user, org, ["organizations.manage"])

    response = client.post(
        "/organizations",
        json={"name": f"Org {unique_suffix()}"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Superuser privileges required"


def test_create_organization_superuser_created_and_auto_enrolled(client, superuser):
    name = f"Org {unique_suffix()}"

    response = client.post(
        "/organizations",
        json={"name": name},
        headers=headers_for(superuser),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == name
    assert superuser.id in [user["id"] for user in body["users"]]


# ---------------------------------------------------------------------------
# 2. GET /organizations lists only member orgs (superuser sees all)
# ---------------------------------------------------------------------------


def test_list_organizations_returns_only_member_orgs(
    client, make_user, make_organization, add_member
):
    user = make_user()
    member_org = make_organization()
    other_org = make_organization()
    add_member(user, member_org)

    response = client.get("/organizations", headers=headers_for(user))

    assert response.status_code == 200
    ids = [org["id"] for org in response.json()]
    assert member_org.id in ids
    assert other_org.id not in ids


def test_list_organizations_superuser_sees_all(
    client, superuser, make_organization
):
    org_a = make_organization()
    org_b = make_organization()

    response = client.get("/organizations", headers=headers_for(superuser))

    assert response.status_code == 200
    ids = [org["id"] for org in response.json()]
    assert org_a.id in ids
    assert org_b.id in ids


# ---------------------------------------------------------------------------
# 3. GET /organizations/{id} denied for non-members
# ---------------------------------------------------------------------------


def test_get_organization_denied_for_non_member(
    client, make_user, make_organization
):
    user = make_user()
    org = make_organization()

    response = client.get(f"/organizations/{org.id}", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Organization access denied"


def test_get_organization_allowed_for_member(
    client, make_user, make_organization, add_member
):
    user = make_user()
    org = make_organization()
    add_member(user, org)

    response = client.get(f"/organizations/{org.id}", headers=headers_for(user))

    assert response.status_code == 200
    assert response.json()["id"] == org.id


# ---------------------------------------------------------------------------
# 4. PATCH /organizations/{id} requires organizations.manage scoped to the org
# ---------------------------------------------------------------------------


def test_update_organization_denied_when_manage_granted_in_other_org(
    client, make_user, make_organization, grant_permissions, add_member
):
    user = make_user()
    target_org = make_organization()
    other_org = make_organization()
    grant_permissions(user, other_org, ["organizations.manage"])
    add_member(user, target_org)

    response = client.patch(
        f"/organizations/{target_org.id}",
        json={"description": "should not work"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: organizations.manage"


def test_update_organization_denied_for_member_without_manage(
    client, make_user, make_organization, add_member
):
    user = make_user()
    org = make_organization()
    add_member(user, org)

    response = client.patch(
        f"/organizations/{org.id}",
        json={"description": "should not work"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: organizations.manage"


def test_update_organization_allowed_with_scoped_manage(
    client, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    grant_permissions(user, org, ["organizations.manage"])

    response = client.patch(
        f"/organizations/{org.id}",
        json={"description": "updated by manager"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.json()["description"] == "updated by manager"


# ---------------------------------------------------------------------------
# 5. POST /organizations/{id}/users/{uid} needs scoped manage; idempotent
# ---------------------------------------------------------------------------


def test_add_member_denied_when_manage_granted_in_other_org(
    client, make_user, make_organization, grant_permissions, add_member
):
    actor = make_user()
    target_user = make_user()
    target_org = make_organization()
    other_org = make_organization()
    grant_permissions(actor, other_org, ["organizations.manage"])
    add_member(actor, target_org)

    response = client.post(
        f"/organizations/{target_org.id}/users/{target_user.id}",
        headers=headers_for(actor),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: organizations.manage"


def test_add_member_with_scoped_manage_is_idempotent(
    client, db, make_user, make_organization, grant_permissions
):
    actor = make_user()
    target_user = make_user()
    org = make_organization()
    grant_permissions(actor, org, ["organizations.manage"])

    first = client.post(
        f"/organizations/{org.id}/users/{target_user.id}",
        headers=headers_for(actor),
    )
    second = client.post(
        f"/organizations/{org.id}/users/{target_user.id}",
        headers=headers_for(actor),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    for response in (first, second):
        body = response.json()
        assert body["organization_id"] == org.id
        assert body["user_id"] == target_user.id
        assert body["detail"] == "User is in organization"

    memberships = db.execute(
        select(organization_users).where(
            organization_users.c.organization_id == org.id,
            organization_users.c.user_id == target_user.id,
        )
    ).all()
    assert len(memberships) == 1


# ---------------------------------------------------------------------------
# 6. POST /projects requires projects.create in the target org and membership
# ---------------------------------------------------------------------------


def test_create_project_denied_for_non_member(
    client, make_user, make_organization
):
    user = make_user()
    org = make_organization()

    response = client.post(
        "/projects",
        json={"name": f"Proj {unique_suffix()}", "organization_id": org.id},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Organization access denied"


def test_create_project_denied_in_org_without_permission(
    client, make_user, make_organization, grant_permissions, add_member
):
    user = make_user()
    granted_org = make_organization()
    target_org = make_organization()
    grant_permissions(user, granted_org, ["projects.create"])
    add_member(user, target_org)

    response = client.post(
        "/projects",
        json={"name": f"Proj {unique_suffix()}", "organization_id": target_org.id},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: projects.create"


def test_create_project_allowed_with_permission_in_target_org(
    client, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    grant_permissions(user, org, ["projects.create"])
    name = f"Proj {unique_suffix()}"

    response = client.post(
        "/projects",
        json={"name": name, "organization_id": org.id},
        headers=headers_for(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == name
    assert body["organization_id"] == org.id


# ---------------------------------------------------------------------------
# 7. GET /projects/{id} visibility rules inside the org
# ---------------------------------------------------------------------------


def test_get_project_accessible_to_direct_project_member(
    client, db, make_user, make_organization, add_member
):
    user = make_user()
    org = make_organization()
    add_member(user, org)
    project = create_project(db, org)
    db.execute(
        insert(project_users).values(project_id=project.id, user_id=user.id)
    )
    db.commit()

    response = client.get(f"/projects/{project.id}", headers=headers_for(user))

    assert response.status_code == 200
    assert response.json()["id"] == project.id


def test_get_project_accessible_via_assigned_group_of_same_org(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    # Empty grant: enrolls the user and puts them in a group of the org
    # without giving any permission codes.
    group = grant_permissions(user, org, [])
    project = create_project(db, org)
    db.execute(
        insert(project_groups).values(project_id=project.id, group_id=group.id)
    )
    db.commit()

    response = client.get(f"/projects/{project.id}", headers=headers_for(user))

    assert response.status_code == 200
    assert response.json()["id"] == project.id


def test_get_project_accessible_with_view_all_in_org(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    grant_permissions(user, org, ["projects.view_all"])
    project = create_project(db, org)

    response = client.get(f"/projects/{project.id}", headers=headers_for(user))

    assert response.status_code == 200
    assert response.json()["id"] == project.id


def test_get_project_denied_for_plain_org_member(
    client, db, make_user, make_organization, add_member
):
    user = make_user()
    org = make_organization()
    add_member(user, org)
    project = create_project(db, org)

    response = client.get(f"/projects/{project.id}", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Project access denied"


# ---------------------------------------------------------------------------
# 8. GET /projects/{id} denied across organizations
# ---------------------------------------------------------------------------


def test_get_project_denied_for_user_of_different_org(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    home_org = make_organization()
    foreign_org = make_organization()
    grant_permissions(user, home_org, ["projects.view_all"])
    project = create_project(db, foreign_org)

    response = client.get(f"/projects/{project.id}", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Project access denied"


# ---------------------------------------------------------------------------
# 9. PATCH /projects/{id}: archiving needs projects.archive on top of edit
# ---------------------------------------------------------------------------


def test_archive_project_denied_with_only_projects_edit(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    grant_permissions(user, org, ["projects.edit"])
    project = create_project(db, org)

    response = client.patch(
        f"/projects/{project.id}",
        json={"status": "archived"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: projects.archive"


def test_edit_project_fields_allowed_with_only_projects_edit(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    grant_permissions(user, org, ["projects.edit"])
    project = create_project(db, org)

    response = client.patch(
        f"/projects/{project.id}",
        json={"name": "Renamed project"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed project"


def test_archive_project_allowed_with_edit_and_archive(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    grant_permissions(user, org, ["projects.edit", "projects.archive"])
    project = create_project(db, org)

    response = client.patch(
        f"/projects/{project.id}",
        json={"status": "archived"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


# ---------------------------------------------------------------------------
# 10. POST /projects/{id}/users/{uid}: manage_members + target in same org
# ---------------------------------------------------------------------------


def test_assign_user_to_project_denied_without_manage_members(
    client, db, make_user, make_organization, add_member
):
    actor = make_user()
    target_user = make_user()
    org = make_organization()
    add_member(actor, org)
    add_member(target_user, org)
    project = create_project(db, org)

    response = client.post(
        f"/projects/{project.id}/users/{target_user.id}",
        headers=headers_for(actor),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: projects.manage_members"


def test_assign_user_outside_project_org_returns_conflict(
    client, db, make_user, make_organization, grant_permissions
):
    actor = make_user()
    outsider = make_user()
    org = make_organization()
    grant_permissions(actor, org, ["projects.manage_members"])
    project = create_project(db, org)

    response = client.post(
        f"/projects/{project.id}/users/{outsider.id}",
        headers=headers_for(actor),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "User does not belong to the project organization"


def test_assign_user_to_project_with_manage_members_succeeds(
    client, db, make_user, make_organization, grant_permissions, add_member
):
    actor = make_user()
    target_user = make_user()
    org = make_organization()
    grant_permissions(actor, org, ["projects.manage_members"])
    add_member(target_user, org)
    project = create_project(db, org)

    response = client.post(
        f"/projects/{project.id}/users/{target_user.id}",
        headers=headers_for(actor),
    )

    assert response.status_code == 200
    assert target_user.id in [user["id"] for user in response.json()["users"]]


# ---------------------------------------------------------------------------
# 11. POST /projects/{id}/groups/{gid}: group must belong to the project org
# ---------------------------------------------------------------------------


def test_assign_group_from_other_org_returns_conflict(
    client, db, make_user, make_organization, grant_permissions
):
    actor = make_user()
    org = make_organization()
    other_org = make_organization()
    grant_permissions(actor, org, ["projects.manage_members"])
    project = create_project(db, org)
    foreign_group = create_group(db, other_org)

    response = client.post(
        f"/projects/{project.id}/groups/{foreign_group.id}",
        headers=headers_for(actor),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Group does not belong to the project organization"


def test_assign_group_of_same_org_succeeds(
    client, db, make_user, make_organization, grant_permissions
):
    actor = make_user()
    org = make_organization()
    grant_permissions(actor, org, ["projects.manage_members"])
    project = create_project(db, org)
    group = create_group(db, org)

    response = client.post(
        f"/projects/{project.id}/groups/{group.id}",
        headers=headers_for(actor),
    )

    assert response.status_code == 200
    assert group.id in [grp["id"] for grp in response.json()["groups"]]
