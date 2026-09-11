"""Tests for account-scoped sidebar shortcut preferences."""

from conftest import headers_for
import pytest
from sqlalchemy import select

from app.users.models import User


def test_me_returns_null_sidebar_shortcuts_for_unconfigured_user(client, make_user):
    user = make_user()

    response = client.get("/auth/me", headers=headers_for(user))

    assert response.status_code == 200
    assert response.json()["sidebar_shortcut_ids"] is None


def test_update_sidebar_shortcuts_preserves_order_without_permission_checks(
    client,
    db,
    make_user,
):
    user = make_user()
    shortcut_ids = [
        "ordinance_library",
        "requirements",
        "projects",
        "inventory",
        "maintenance",
        "admin",
        "municipal_ordinances",
        "municipal_facilities",
        "municipal_people",
        "municipal_roadmap",
        "map_municipalities",
        "admin_product",
        "admin_memory",
        "admin_users",
        "admin_groups",
        "admin_organizations",
        "admin_roles",
        "admin_municipalities",
        "admin_ordinances",
    ]

    response = client.put(
        "/auth/me/sidebar-shortcuts",
        headers=headers_for(user),
        json={"shortcut_ids": shortcut_ids},
    )

    assert response.status_code == 200
    assert response.json() == {"shortcut_ids": shortcut_ids}
    db.expire_all()
    assert db.scalar(select(User).where(User.id == user.id)).sidebar_shortcut_ids == (
        shortcut_ids
    )
    me_response = client.get("/auth/me", headers=headers_for(user))
    assert me_response.status_code == 200
    assert me_response.json()["sidebar_shortcut_ids"] == shortcut_ids


def test_empty_sidebar_shortcuts_is_an_explicit_saved_preference(
    client,
    db,
    make_user,
):
    user = make_user()

    response = client.put(
        "/auth/me/sidebar-shortcuts",
        headers=headers_for(user),
        json={"shortcut_ids": []},
    )

    assert response.status_code == 200
    assert response.json() == {"shortcut_ids": []}
    db.expire_all()
    assert db.get(User, user.id).sidebar_shortcut_ids == []


def test_delete_sidebar_shortcuts_resets_preference_to_null(client, db, make_user):
    user = make_user()
    user.sidebar_shortcut_ids = ["projects", "inventory"]
    db.commit()

    response = client.delete(
        "/auth/me/sidebar-shortcuts",
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.json() == {"shortcut_ids": None}
    db.expire_all()
    assert db.get(User, user.id).sidebar_shortcut_ids is None


@pytest.mark.parametrize(
    "shortcut_ids",
    [
        ["requirements", "requirements"],
        ["unknown_section"],
        ["iconcejo"],
        ["home"],
        ["municipality"],
        ["map"],
        ["account"],
    ],
)
def test_update_sidebar_shortcuts_rejects_duplicates_unknown_and_fixed_ids(
    client,
    make_user,
    shortcut_ids,
):
    user = make_user()

    response = client.put(
        "/auth/me/sidebar-shortcuts",
        headers=headers_for(user),
        json={"shortcut_ids": shortcut_ids},
    )

    assert response.status_code == 422


def test_sidebar_shortcuts_are_isolated_per_user(client, make_user):
    first_user = make_user()
    second_user = make_user()

    response = client.put(
        "/auth/me/sidebar-shortcuts",
        headers=headers_for(first_user),
        json={"shortcut_ids": ["admin", "maintenance"]},
    )
    first_me = client.get("/auth/me", headers=headers_for(first_user))
    second_me = client.get("/auth/me", headers=headers_for(second_user))

    assert response.status_code == 200
    assert first_me.status_code == 200
    assert first_me.json()["sidebar_shortcut_ids"] == ["admin", "maintenance"]
    assert second_me.status_code == 200
    assert second_me.json()["sidebar_shortcut_ids"] is None


@pytest.mark.parametrize("method", ["put", "delete"])
def test_sidebar_shortcut_mutations_require_authentication(client, method):
    request = getattr(client, method)
    kwargs = {"json": {"shortcut_ids": ["projects"]}} if method == "put" else {}

    response = request("/auth/me/sidebar-shortcuts", **kwargs)

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"
