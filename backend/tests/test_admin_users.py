"""Tests for the /admin/users endpoints (app/admin/users.py)."""

import pytest

from app.users.crud import get_user_by_email
from conftest import headers_for, unique_suffix

GLOBAL_CREATE_DETAIL = "Only superusers can create global user accounts"
GLOBAL_UPDATE_DETAIL = "Only superusers can update global user accounts"
GLOBAL_DELETE_DETAIL = "Only superusers can delete user accounts"
LAST_SUPERUSER_PATCH_DETAIL = "Cannot demote the last active superuser"
LAST_SUPERUSER_DELETE_DETAIL = "Cannot delete the last active superuser"
PERMISSION_DETAIL = "Permission required: users.manage"
SELF_DELETE_DETAIL = "Cannot delete your own account"


def user_payload(**overrides) -> dict:
    payload = {
        "email": f"new-{unique_suffix()}@example.com",
        "password": "password-123",
        "full_name": "New User",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# POST /admin/users
# ---------------------------------------------------------------------------


def test_org_admin_cannot_create_global_user_account(
    client, db, make_user, make_organization, grant_permissions
):
    admin = make_user()
    org = make_organization()
    grant_permissions(admin, org, ["users.manage"])

    payload = user_payload()
    response = client.post(
        "/admin/users", json=payload, headers=headers_for(admin)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == GLOBAL_CREATE_DETAIL
    assert get_user_by_email(db, payload["email"]) is None


def test_superuser_can_create_regular_user(client, db, superuser):
    payload = user_payload()

    response = client.post(
        "/admin/users", json=payload, headers=headers_for(superuser)
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == payload["email"]
    assert body["full_name"] == payload["full_name"]
    assert body["is_active"] is True
    assert body["is_superuser"] is False
    assert get_user_by_email(db, payload["email"]) is not None


def test_admin_cannot_create_superuser(
    client, make_user, make_organization, grant_permissions
):
    admin = make_user()
    org = make_organization()
    grant_permissions(admin, org, ["users.manage"])

    response = client.post(
        "/admin/users",
        json=user_payload(is_superuser=True),
        headers=headers_for(admin),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == GLOBAL_CREATE_DETAIL


def test_org_admin_cannot_probe_global_email_collisions(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    managed_org = make_organization()
    other_org = make_organization()
    grant_permissions(admin, managed_org, ["users.manage"])
    existing = make_user(email="existing-in-other-org@example.com")
    add_member(existing, other_org)
    unused_email = "unused-global-email@example.com"

    responses = [
        client.post(
            "/admin/users",
            json=user_payload(email=email),
            headers=headers_for(admin),
        )
        for email in (existing.email, unused_email)
    ]

    assert [response.status_code for response in responses] == [403, 403]
    assert [response.json() for response in responses] == [
        {"detail": GLOBAL_CREATE_DETAIL},
        {"detail": GLOBAL_CREATE_DETAIL},
    ]
    assert get_user_by_email(db, unused_email) is None


def test_superuser_can_create_superuser(client, superuser):
    response = client.post(
        "/admin/users",
        json=user_payload(is_superuser=True),
        headers=headers_for(superuser),
    )

    assert response.status_code == 201
    assert response.json()["is_superuser"] is True


# ---------------------------------------------------------------------------
# PATCH /admin/users/{id} — superuser flag handling
# ---------------------------------------------------------------------------


def test_admin_cannot_promote_user_in_own_org_to_superuser(
    client, make_user, make_organization, grant_permissions, add_member
):
    admin = make_user()
    org = make_organization()
    grant_permissions(admin, org, ["users.manage"])
    target = make_user()
    add_member(target, org)

    response = client.patch(
        f"/admin/users/{target.id}",
        json={"is_superuser": True},
        headers=headers_for(admin),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == GLOBAL_UPDATE_DETAIL


def test_superuser_can_promote_user_via_patch(client, superuser, make_user):
    target = make_user()

    response = client.patch(
        f"/admin/users/{target.id}",
        json={"is_superuser": True},
        headers=headers_for(superuser),
    )

    assert response.status_code == 200
    assert response.json()["is_superuser"] is True


def test_superuser_can_demote_other_superuser_via_patch(
    client, superuser, make_user
):
    other_superuser = make_user(is_superuser=True)

    response = client.patch(
        f"/admin/users/{other_superuser.id}",
        json={"is_superuser": False},
        headers=headers_for(superuser),
    )

    assert response.status_code == 200
    assert response.json()["is_superuser"] is False


# ---------------------------------------------------------------------------
# Org scoping on PATCH / DELETE
# ---------------------------------------------------------------------------


def test_admin_cannot_patch_user_of_other_org(
    client, make_user, make_organization, grant_permissions, add_member
):
    admin = make_user()
    org_a = make_organization()
    org_b = make_organization()
    grant_permissions(admin, org_a, ["users.manage"])
    target = make_user()
    add_member(target, org_b)

    response = client.patch(
        f"/admin/users/{target.id}",
        json={"full_name": "Renamed"},
        headers=headers_for(admin),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == PERMISSION_DETAIL


def test_admin_cannot_delete_user_of_other_org(
    client, make_user, make_organization, grant_permissions, add_member
):
    admin = make_user()
    org_a = make_organization()
    org_b = make_organization()
    grant_permissions(admin, org_a, ["users.manage"])
    target = make_user()
    add_member(target, org_b)

    response = client.delete(
        f"/admin/users/{target.id}", headers=headers_for(admin)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == PERMISSION_DETAIL


@pytest.mark.parametrize(
    ("payload", "unchanged_attribute"),
    [
        pytest.param(
            {"password": "attacker-password"},
            "hashed_password",
            id="password",
        ),
        pytest.param({"is_active": False}, "is_active", id="active-status"),
        pytest.param(
            {"is_superuser": True},
            "is_superuser",
            id="superuser-status",
        ),
        pytest.param(
            {"full_name": "Changed By Other Tenant"},
            "full_name",
            id="full-name",
        ),
    ],
)
def test_org_admin_cannot_update_shared_users_global_identity(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
    payload,
    unchanged_attribute,
):
    admin = make_user()
    managed_org = make_organization()
    other_org = make_organization()
    grant_permissions(admin, managed_org, ["users.manage"])
    target = make_user(full_name="Shared User")
    add_member(target, managed_org)
    add_member(target, other_org)
    original_value = getattr(target, unchanged_attribute)

    response = client.patch(
        f"/admin/users/{target.id}",
        json=payload,
        headers=headers_for(admin),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == GLOBAL_UPDATE_DETAIL
    db.refresh(target)
    assert getattr(target, unchanged_attribute) == original_value


def test_org_admin_cannot_delete_user_shared_with_another_organization(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    managed_org = make_organization()
    other_org = make_organization()
    grant_permissions(admin, managed_org, ["users.manage"])
    target = make_user()
    add_member(target, managed_org)
    add_member(target, other_org)

    response = client.delete(
        f"/admin/users/{target.id}", headers=headers_for(admin)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == GLOBAL_DELETE_DETAIL
    assert db.get(type(target), target.id) is not None


def test_org_admin_cannot_patch_global_identity_of_user_in_own_org(
    client, make_user, make_organization, grant_permissions, add_member
):
    admin = make_user()
    org = make_organization()
    grant_permissions(admin, org, ["users.manage"])
    target = make_user(full_name="Before Rename")
    add_member(target, org)

    response = client.patch(
        f"/admin/users/{target.id}",
        json={"full_name": "After Rename"},
        headers=headers_for(admin),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == GLOBAL_UPDATE_DETAIL


def test_org_admin_cannot_delete_global_account_of_user_in_own_org(
    client, db, make_user, make_organization, grant_permissions, add_member
):
    admin = make_user()
    org = make_organization()
    grant_permissions(admin, org, ["users.manage"])
    target = make_user()
    add_member(target, org)

    response = client.delete(
        f"/admin/users/{target.id}", headers=headers_for(admin)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == GLOBAL_DELETE_DETAIL
    assert db.get(type(target), target.id) is not None


# ---------------------------------------------------------------------------
# Users without any organization
# ---------------------------------------------------------------------------


def test_admin_cannot_patch_orgless_user(
    client, make_user, make_organization, grant_permissions
):
    admin = make_user()
    org = make_organization()
    grant_permissions(admin, org, ["users.manage"])
    orgless = make_user()

    response = client.patch(
        f"/admin/users/{orgless.id}",
        json={"full_name": "Renamed"},
        headers=headers_for(admin),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == PERMISSION_DETAIL


def test_admin_cannot_delete_orgless_user(
    client, make_user, make_organization, grant_permissions
):
    admin = make_user()
    org = make_organization()
    grant_permissions(admin, org, ["users.manage"])
    orgless = make_user()

    response = client.delete(
        f"/admin/users/{orgless.id}", headers=headers_for(admin)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == PERMISSION_DETAIL


def test_superuser_can_patch_orgless_user(client, superuser, make_user):
    orgless = make_user(full_name="No Org")

    response = client.patch(
        f"/admin/users/{orgless.id}",
        json={"full_name": "Managed By Super"},
        headers=headers_for(superuser),
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Managed By Super"


def test_superuser_can_delete_orgless_user(client, superuser, make_user):
    orgless = make_user()

    response = client.delete(
        f"/admin/users/{orgless.id}", headers=headers_for(superuser)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == orgless.id
    assert body["detail"] == "User deleted"


# ---------------------------------------------------------------------------
# Last-superuser guard
# ---------------------------------------------------------------------------


def test_patch_deactivating_last_superuser_conflicts(client, superuser):
    response = client.patch(
        f"/admin/users/{superuser.id}",
        json={"is_active": False},
        headers=headers_for(superuser),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == LAST_SUPERUSER_PATCH_DETAIL


def test_patch_demoting_last_superuser_conflicts(client, superuser):
    response = client.patch(
        f"/admin/users/{superuser.id}",
        json={"is_superuser": False},
        headers=headers_for(superuser),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == LAST_SUPERUSER_PATCH_DETAIL


def test_patch_deactivating_superuser_succeeds_with_two_active(
    client, superuser, make_user
):
    other_superuser = make_user(is_superuser=True)

    response = client.patch(
        f"/admin/users/{other_superuser.id}",
        json={"is_active": False},
        headers=headers_for(superuser),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_delete_last_active_superuser_conflicts(
    client, superuser
):
    response = client.delete(
        f"/admin/users/{superuser.id}", headers=headers_for(superuser)
    )

    assert response.status_code == 409
    assert response.json()["detail"] == LAST_SUPERUSER_DELETE_DETAIL


def test_delete_superuser_succeeds_with_two_active(
    client, superuser, make_user
):
    other_superuser = make_user(is_superuser=True)

    response = client.delete(
        f"/admin/users/{other_superuser.id}", headers=headers_for(superuser)
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == other_superuser.id


# ---------------------------------------------------------------------------
# Self-delete guard
# ---------------------------------------------------------------------------


def test_cannot_delete_own_account(
    client, superuser, make_user
):
    make_user(is_superuser=True)

    response = client.delete(
        f"/admin/users/{superuser.id}", headers=headers_for(superuser)
    )

    assert response.status_code == 400
    assert response.json()["detail"] == SELF_DELETE_DETAIL


# ---------------------------------------------------------------------------
# GET /admin/users listing
# ---------------------------------------------------------------------------


def test_list_users_scoped_to_admin_organizations(
    client, make_user, make_organization, grant_permissions, add_member
):
    admin = make_user()
    org_a = make_organization()
    org_b = make_organization()
    grant_permissions(admin, org_a, ["users.manage"])
    user_in_a = make_user()
    add_member(user_in_a, org_a)
    user_in_b = make_user()
    add_member(user_in_b, org_b)
    orgless_user = make_user()

    response = client.get("/admin/users", headers=headers_for(admin))

    assert response.status_code == 200
    listed_ids = {user["id"] for user in response.json()}
    assert admin.id in listed_ids
    assert user_in_a.id in listed_ids
    assert user_in_b.id not in listed_ids
    assert orgless_user.id not in listed_ids


def test_list_users_as_superuser_returns_everyone(
    client, superuser, make_user, make_organization, add_member
):
    org_a = make_organization()
    org_b = make_organization()
    user_in_a = make_user()
    add_member(user_in_a, org_a)
    user_in_b = make_user()
    add_member(user_in_b, org_b)
    orgless_user = make_user()

    response = client.get("/admin/users", headers=headers_for(superuser))

    assert response.status_code == 200
    listed_ids = {user["id"] for user in response.json()}
    assert {
        superuser.id,
        user_in_a.id,
        user_in_b.id,
        orgless_user.id,
    } <= listed_ids


def test_list_users_without_qualifying_permission_forbidden(
    client, make_user, make_organization, grant_permissions
):
    user = make_user()
    org = make_organization()
    # Member of an organization, but no permission that qualifies for the
    # admin user listing (users/groups/organizations/projects management).
    grant_permissions(user, org, ["documents.view"])

    response = client.get("/admin/users", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == PERMISSION_DETAIL
