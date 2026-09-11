"""Tests for the /auth routes: login, me, and bootstrap-admin."""

from conftest import headers_for, unique_suffix
from sqlalchemy import select

from app.core.config import settings
from app.municipalities.models import Municipality
from app.users.models import User

BOOTSTRAP_TOKEN = "test-bootstrap-token"


def _bootstrap_payload() -> dict[str, str]:
    return {
        "email": f"admin-{unique_suffix()}@example.com",
        "password": "bootstrap-password-123",
        "full_name": "Bootstrap Admin",
    }


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


def test_login_with_valid_credentials_returns_token_usable_on_me(client, make_user):
    user = make_user(password="correct-horse-battery")

    response = client.post(
        "/auth/login",
        json={"email": user.email, "password": "correct-horse-battery"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    me_response = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me_response.status_code == 200
    assert me_response.json()["id"] == user.id
    assert me_response.json()["email"] == user.email


def test_login_with_wrong_password_returns_401(client, make_user):
    user = make_user(password="right-password")

    response = client.post(
        "/auth/login",
        json={"email": user.email, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


def test_login_with_unknown_email_returns_401(client):
    response = client.post(
        "/auth/login",
        json={
            "email": f"nobody-{unique_suffix()}@example.com",
            "password": "whatever-password",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


def test_login_with_inactive_user_returns_403(client, make_user):
    user = make_user(password="inactive-password", is_active=False)

    response = client.post(
        "/auth/login",
        json={"email": user.email, "password": "inactive-password"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Inactive user"


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


def test_me_returns_sorted_permissions_and_member_organizations(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    municipality = Municipality(
        name="Fuentelcesped",
        province="Burgos",
        autonomous_community="Castilla y Leon",
        ine_code=f"09{unique_suffix()[:3]}",
    )
    db.add(municipality)
    db.commit()
    organization = make_organization(municipality_id=municipality.id)
    granted = ["users.manage", "documents.view", "projects.create"]
    grant_permissions(user, organization, granted)

    response = client.get("/auth/me", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == user.id
    assert body["email"] == user.email
    assert body["permissions"] == sorted(granted)
    assert [org["id"] for org in body["organizations"]] == [organization.id]
    assert body["organizations"][0]["name"] == organization.name
    assert body["organizations"][0]["municipality_id"] == municipality.id
    assert body["organizations"][0]["municipality"] == {
        "id": municipality.id,
        "ine_code": municipality.ine_code,
        "name": municipality.name,
        "province": municipality.province,
        "autonomous_community": municipality.autonomous_community,
    }


def test_me_without_token_returns_401(client):
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


def test_me_with_token_of_deleted_user_returns_401(client, db, make_user):
    user = make_user()
    headers = headers_for(user)
    db.delete(user)
    db.commit()

    response = client.get("/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


# ---------------------------------------------------------------------------
# POST /auth/bootstrap-admin
# ---------------------------------------------------------------------------


def test_bootstrap_admin_returns_503_when_token_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_token", None)

    response = client.post(
        "/auth/bootstrap-admin",
        json=_bootstrap_payload(),
        headers={"X-Bootstrap-Admin-Token": "anything"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Bootstrap admin token is not configured"


def test_bootstrap_admin_with_wrong_header_token_returns_401(client, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_token", BOOTSTRAP_TOKEN)

    response = client.post(
        "/auth/bootstrap-admin",
        json=_bootstrap_payload(),
        headers={"X-Bootstrap-Admin-Token": "not-the-right-token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bootstrap admin token"


def test_bootstrap_admin_without_header_returns_401(client, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_token", BOOTSTRAP_TOKEN)

    response = client.post("/auth/bootstrap-admin", json=_bootstrap_payload())

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bootstrap admin token"


def test_bootstrap_admin_returns_409_when_users_already_exist(
    client, make_user, monkeypatch
):
    monkeypatch.setattr(settings, "bootstrap_admin_token", BOOTSTRAP_TOKEN)
    make_user()

    response = client.post(
        "/auth/bootstrap-admin",
        json=_bootstrap_payload(),
        headers={"X-Bootstrap-Admin-Token": BOOTSTRAP_TOKEN},
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Bootstrap admin can only be created before any users exist"
    )


def test_bootstrap_admin_creates_superuser_on_empty_users_table(
    client, db, monkeypatch
):
    monkeypatch.setattr(settings, "bootstrap_admin_token", BOOTSTRAP_TOKEN)
    payload = _bootstrap_payload()

    response = client.post(
        "/auth/bootstrap-admin",
        json=payload,
        headers={"X-Bootstrap-Admin-Token": BOOTSTRAP_TOKEN},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == payload["email"]
    assert body["full_name"] == payload["full_name"]
    assert body["is_superuser"] is True
    assert body["is_active"] is True

    created = db.scalar(select(User).where(User.email == payload["email"]))
    assert created is not None
    assert created.is_superuser is True
    assert created.is_active is True
