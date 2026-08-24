"""Cobertura de la traza de eventos de seguridad (ADR-036)."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from conftest import headers_for

from app.security.events import (
    LOGIN_BLOCKED,
    LOGIN_FAILED,
    LOGIN_SUCCEEDED,
    LOGOUT,
)
from app.security.models import SecurityEvent


def events_of_type(db, event_type: str) -> list[SecurityEvent]:
    return list(
        db.scalars(
            select(SecurityEvent).where(SecurityEvent.event_type == event_type)
        )
    )


def test_successful_login_is_recorded(client, db, make_user):
    user = make_user(password="a-strong-password")

    response = client.post(
        "/auth/login",
        json={"email": user.email, "password": "a-strong-password"},
    )

    assert response.status_code == 200
    recorded = events_of_type(db, LOGIN_SUCCEEDED)
    assert len(recorded) == 1
    assert recorded[0].user_id == user.id
    assert recorded[0].actor_label == user.email
    assert recorded[0].outcome == "success"


def test_failed_login_is_recorded_with_the_attempted_account(client, db, make_user):
    user = make_user(password="a-strong-password")

    response = client.post(
        "/auth/login",
        json={"email": user.email, "password": "wrong-password"},
    )

    assert response.status_code == 401
    recorded = events_of_type(db, LOGIN_FAILED)
    assert len(recorded) == 1
    assert recorded[0].outcome == "failure"
    assert recorded[0].actor_label == user.email
    assert recorded[0].user_id == user.id


def test_failed_login_for_an_unknown_account_is_recorded(client, db):
    response = client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-password"},
    )

    assert response.status_code == 401
    recorded = events_of_type(db, LOGIN_FAILED)
    assert len(recorded) == 1
    assert recorded[0].user_id is None
    assert recorded[0].actor_label == "nobody@example.com"


def test_rate_limit_blocks_are_recorded(client, db, make_user):
    """Sin esto no hay forma de ver una campaña de fuerza bruta."""
    user = make_user(password="a-strong-password")

    for _ in range(12):
        client.post(
            "/auth/login",
            json={"email": user.email, "password": "wrong-password"},
        )

    blocked = events_of_type(db, LOGIN_BLOCKED)
    assert blocked
    assert all(event.outcome == "blocked" for event in blocked)


def test_logout_is_recorded(client, db, make_user):
    user = make_user()

    response = client.post("/auth/logout", headers=headers_for(user))

    assert response.status_code == 200
    assert len(events_of_type(db, LOGOUT)) == 1


def test_document_upload_and_download_are_recorded(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    """El acceso a documentos es la traza que exige el piloto con datos reales."""
    from app.security.events import DOCUMENT_DOWNLOADED, DOCUMENT_UPLOADED
    from test_documents import add_project_member, make_project, upload_document

    organization = make_organization()
    user = make_user()
    project = make_project(db, organization)
    grant_permissions(user, organization, ["documents.upload", "documents.view"])
    add_project_member(db, project, user)
    headers = headers_for(user)

    upload = upload_document(client, headers, project.id)
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    download = client.get(f"/documents/{document_id}/download", headers=headers)
    assert download.status_code == 200

    uploaded = events_of_type(db, DOCUMENT_UPLOADED)
    downloaded = events_of_type(db, DOCUMENT_DOWNLOADED)
    assert len(uploaded) == 1
    assert uploaded[0].target_id == document_id
    assert uploaded[0].organization_id == organization.id
    assert len(downloaded) == 1
    assert downloaded[0].target_id == document_id


def test_events_cannot_be_modified(client, db, make_user):
    """Inmutabilidad garantizada por la base, no por convención."""
    user = make_user(password="a-strong-password")
    client.post(
        "/auth/login",
        json={"email": user.email, "password": "a-strong-password"},
    )
    recorded = events_of_type(db, LOGIN_SUCCEEDED)
    assert recorded

    with pytest.raises(OperationalError):
        db.execute(
            text("UPDATE security_events SET detail = 'tampered' WHERE id = :id"),
            {"id": recorded[0].id},
        )
    db.rollback()


def test_events_cannot_be_deleted(client, db, make_user):
    user = make_user(password="a-strong-password")
    client.post(
        "/auth/login",
        json={"email": user.email, "password": "a-strong-password"},
    )
    recorded = events_of_type(db, LOGIN_SUCCEEDED)
    assert recorded

    with pytest.raises(OperationalError):
        db.execute(
            text("DELETE FROM security_events WHERE id = :id"),
            {"id": recorded[0].id},
        )
    db.rollback()


def test_listing_requires_a_superuser(client, make_user, superuser):
    plain_user = make_user()

    forbidden = client.get(
        "/admin/security-events",
        headers=headers_for(plain_user),
    )
    allowed = client.get("/admin/security-events", headers=headers_for(superuser))

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert "X-Total-Count" in allowed.headers


def test_listing_filters_by_type_and_outcome(client, make_user, superuser):
    user = make_user(password="a-strong-password")
    client.post(
        "/auth/login",
        json={"email": user.email, "password": "wrong-password"},
    )

    response = client.get(
        "/admin/security-events",
        params={"event_type": LOGIN_FAILED, "outcome": "failure"},
        headers=headers_for(superuser),
    )

    assert response.status_code == 200
    body = response.json()
    assert body
    assert all(item["event_type"] == LOGIN_FAILED for item in body)
