"""Tests for the admin RBAC endpoints (roles, permissions, group-role links)."""

from conftest import headers_for, unique_suffix

from app.rbac.models import Group, Permission, Role
from app.rbac.permissions import INITIAL_PERMISSION_CODES


def make_role(db, name: str | None = None) -> Role:
    role = Role(name=name or f"role-{unique_suffix()}")
    db.add(role)
    db.commit()
    return role


def make_group(db, organization) -> Group:
    group = Group(
        name=f"grp-{unique_suffix()}",
        organization_id=organization.id,
    )
    db.add(group)
    db.commit()
    return group


def get_permission(db, code: str) -> Permission:
    permission = db.query(Permission).filter(Permission.code == code).one()
    return permission


# ---------------------------------------------------------------------------
# Read endpoints: available to non-superusers holding roles.manage
# ---------------------------------------------------------------------------


def test_list_roles_returns_200_for_member_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])

    response = client.get("/admin/roles", headers=headers_for(user))

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_list_permissions_returns_200_for_member_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])

    response = client.get("/admin/permissions", headers=headers_for(user))

    assert response.status_code == 200
    codes = {item["code"] for item in response.json()}
    assert set(INITIAL_PERMISSION_CODES) <= codes


def test_list_roles_returns_403_without_roles_manage(client, make_user):
    user = make_user()

    response = client.get("/admin/roles", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: roles.manage"


# ---------------------------------------------------------------------------
# Role write endpoints: superuser only, even with roles.manage
# ---------------------------------------------------------------------------


def test_create_role_returns_403_for_non_superuser_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])

    response = client.post(
        "/admin/roles",
        json={"name": f"role-{unique_suffix()}"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Superuser privileges required"


def test_update_role_returns_403_for_non_superuser_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])
    role = make_role(db)

    response = client.patch(
        f"/admin/roles/{role.id}",
        json={"name": f"role-{unique_suffix()}"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Superuser privileges required"


def test_delete_role_returns_403_for_non_superuser_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])
    role = make_role(db)

    response = client.delete(
        f"/admin/roles/{role.id}",
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Superuser privileges required"


def test_assign_permission_to_role_returns_403_for_non_superuser_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])
    role = make_role(db)
    permission = get_permission(db, "documents.view")

    response = client.post(
        f"/admin/roles/{role.id}/permissions/{permission.id}",
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Superuser privileges required"


def test_remove_permission_from_role_returns_403_for_non_superuser_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])
    role = make_role(db)
    permission = get_permission(db, "documents.view")

    response = client.delete(
        f"/admin/roles/{role.id}/permissions/{permission.id}",
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Superuser privileges required"


# ---------------------------------------------------------------------------
# Superuser full role lifecycle
# ---------------------------------------------------------------------------


def test_superuser_can_manage_role_lifecycle(client, db, superuser):
    auth = headers_for(superuser)
    name = f"role-{unique_suffix()}"

    created = client.post("/admin/roles", json={"name": name}, headers=auth)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == name
    assert body["permissions"] == []
    role_id = body["id"]

    new_name = f"role-{unique_suffix()}"
    renamed = client.patch(
        f"/admin/roles/{role_id}",
        json={"name": new_name},
        headers=auth,
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == new_name

    permission = get_permission(db, "documents.view")
    assigned = client.post(
        f"/admin/roles/{role_id}/permissions/{permission.id}",
        headers=auth,
    )
    assert assigned.status_code == 200
    assert assigned.json() == {
        "role_id": role_id,
        "permission_id": permission.id,
        "detail": "Permission is assigned to role",
    }

    roles = client.get("/admin/roles", headers=auth).json()
    role_view = next(r for r in roles if r["id"] == role_id)
    assert [p["code"] for p in role_view["permissions"]] == ["documents.view"]

    removed = client.delete(
        f"/admin/roles/{role_id}/permissions/{permission.id}",
        headers=auth,
    )
    assert removed.status_code == 200
    assert removed.json() == {
        "role_id": role_id,
        "permission_id": permission.id,
        "detail": "Permission is not assigned to role",
    }

    deleted = client.delete(f"/admin/roles/{role_id}", headers=auth)
    assert deleted.status_code == 200
    assert deleted.json() == {"role_id": role_id, "detail": "Role deleted"}

    roles_after = client.get("/admin/roles", headers=auth).json()
    assert role_id not in {r["id"] for r in roles_after}


# ---------------------------------------------------------------------------
# Permission bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_permissions_returns_403_for_non_superuser_with_roles_manage(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])

    response = client.post("/admin/permissions/bootstrap", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Superuser privileges required"


def test_bootstrap_permissions_is_idempotent_for_superuser(client, superuser):
    auth = headers_for(superuser)

    first = client.post("/admin/permissions/bootstrap", headers=auth)
    assert first.status_code == 200

    second = client.post("/admin/permissions/bootstrap", headers=auth)
    assert second.status_code == 200
    body = second.json()
    assert body["created_codes"] == []
    assert body["detail"] == "Base permissions initialized"
    codes = {item["code"] for item in body["permissions"]}
    assert set(INITIAL_PERMISSION_CODES) <= codes


# ---------------------------------------------------------------------------
# Group-role assignment: roles.manage scoped to the group's organization
# ---------------------------------------------------------------------------


def test_assign_role_to_group_succeeds_with_roles_manage_in_group_org(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])
    target_group = make_group(db, organization)
    role = make_role(db)

    response = client.post(
        f"/admin/groups/{target_group.id}/roles/{role.id}",
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.json() == {
        "group_id": target_group.id,
        "role_id": role.id,
        "detail": "Role is assigned to group",
    }


def test_assign_role_to_group_returns_403_when_roles_manage_is_from_other_org(
    client, db, make_user, make_organization, grant_permissions, add_member
):
    user = make_user()
    org_with_permission = make_organization()
    org_of_group = make_organization()
    grant_permissions(user, org_with_permission, ["roles.manage"])
    # Member of the group's org but without roles.manage there.
    add_member(user, org_of_group)
    target_group = make_group(db, org_of_group)
    role = make_role(db)

    response = client.post(
        f"/admin/groups/{target_group.id}/roles/{role.id}",
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: roles.manage"


def test_remove_role_from_group_succeeds_with_roles_manage_in_group_org(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["roles.manage"])
    target_group = make_group(db, organization)
    role = make_role(db)
    auth = headers_for(user)
    assert (
        client.post(
            f"/admin/groups/{target_group.id}/roles/{role.id}", headers=auth
        ).status_code
        == 200
    )

    response = client.delete(
        f"/admin/groups/{target_group.id}/roles/{role.id}",
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json() == {
        "group_id": target_group.id,
        "role_id": role.id,
        "detail": "Role is not assigned to group",
    }


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_create_role_with_duplicate_name_returns_409(client, superuser):
    auth = headers_for(superuser)
    name = f"role-{unique_suffix()}"

    first = client.post("/admin/roles", json={"name": name}, headers=auth)
    assert first.status_code == 201

    duplicate = client.post("/admin/roles", json={"name": name}, headers=auth)

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Role already exists"


def test_assign_role_to_nonexistent_group_returns_404(client, db, superuser):
    role = make_role(db)

    response = client.post(
        f"/admin/groups/999999/roles/{role.id}",
        headers=headers_for(superuser),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Group not found"


def test_assign_nonexistent_role_to_group_returns_404(
    client, db, superuser, make_organization
):
    organization = make_organization()
    group = make_group(db, organization)

    response = client.post(
        f"/admin/groups/{group.id}/roles/999999",
        headers=headers_for(superuser),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Role not found"
