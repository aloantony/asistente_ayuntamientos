from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.invitations.models import OrganizationInvitation
from app.invitations.service import (
    accept_invitation,
    create_invitation,
    invitation_token_hash,
)
from app.organizations.models import Organization, organization_users
from app.users.crud import get_user_by_email
from app.users.models import User
from conftest import headers_for, unique_suffix

PERMISSION_DETAIL = "Permission required: users.manage"
UNAVAILABLE_DETAIL = "Invitation is not available"


def invite(client, actor, organization, email: str):
    return client.post(
        f"/organizations/{organization.id}/invitations",
        json={"email": email},
        headers=headers_for(actor),
    )


def accept(client, token: str, actor):
    return client.post(
        "/auth/invitations/accept",
        json={"token": token},
        headers=headers_for(actor),
    )


def membership_count(db: Session, organization_id: int, user_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(organization_users)
            .where(
                organization_users.c.organization_id == organization_id,
                organization_users.c.user_id == user_id,
            )
        )
        or 0
    )


def test_users_manager_can_invite_only_inside_permission_scope(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    managed = make_organization()
    other = make_organization()
    grant_permissions(admin, managed, ["users.manage"])
    add_member(admin, other)

    allowed = invite(client, admin, managed, "new-member@example.com")
    denied_existing = invite(client, admin, other, "other-member@example.com")
    denied_unknown = client.post(
        "/organizations/999999999/invitations",
        json={"email": "unknown@example.com"},
        headers=headers_for(admin),
    )

    assert allowed.status_code == 201
    assert denied_existing.status_code == denied_unknown.status_code == 403
    assert denied_existing.json() == denied_unknown.json() == {
        "detail": PERMISSION_DETAIL
    }


def test_other_management_permission_does_not_allow_inviting(
    client, make_user, make_organization, grant_permissions
):
    admin = make_user()
    organization = make_organization()
    grant_permissions(admin, organization, ["organizations.manage"])

    response = invite(client, admin, organization, "member@example.com")

    assert response.status_code == 403
    assert response.json() == {"detail": PERMISSION_DETAIL}


def test_invitation_creation_does_not_create_or_modify_global_identity(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    organization = make_organization()
    other_organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    existing = make_user(
        email="shared-identity@example.com",
        password="original-password",
        full_name="Existing Person",
        is_active=False,
    )
    add_member(existing, other_organization)
    original_password = existing.hashed_password

    existing_response = invite(client, admin, organization, existing.email)
    unknown_response = invite(client, admin, organization, "unknown@example.com")

    assert existing_response.status_code == unknown_response.status_code == 201
    assert set(existing_response.json()) == set(unknown_response.json())
    assert "requires_registration" not in existing_response.json()
    db.refresh(existing)
    assert existing.full_name == "Existing Person"
    assert existing.hashed_password == original_password
    assert existing.is_active is False
    assert existing.is_superuser is False
    assert membership_count(db, organization.id, existing.id) == 0
    assert get_user_by_email(db, "unknown@example.com") is None


def test_raw_token_is_returned_once_and_only_hash_is_persisted(
    client, db, make_user, make_organization, grant_permissions
):
    admin = make_user()
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])

    response = invite(client, admin, organization, "member@example.com")

    assert response.status_code == 201
    token = response.json()["token"]
    assert len(token) >= 43
    invitation = db.get(OrganizationInvitation, response.json()["id"])
    assert invitation is not None
    assert invitation.token_hash == invitation_token_hash(token)
    assert token != invitation.token_hash
    listed = client.get(
        f"/organizations/{organization.id}/invitations",
        headers=headers_for(admin),
    )
    assert listed.status_code == 200
    assert "token" not in listed.json()[0]
    assert listed.headers["Cache-Control"] == "no-store"
    assert response.headers["Cache-Control"] == "no-store"


def test_duplicate_pending_invitation_is_case_insensitive(
    client, make_user, make_organization, grant_permissions
):
    admin = make_user()
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])

    first = invite(client, admin, organization, "Person@Example.COM")
    duplicate = invite(client, admin, organization, "person@example.com")

    assert first.status_code == 201
    assert first.json()["email"] == "person@example.com"
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Invitation already pending"}


def test_pending_invitation_limit_is_enforced_per_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
):
    admin = make_user()
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    monkeypatch.setattr(
        "app.invitations.service.settings.organization_invitation_max_pending_per_organization",
        1,
    )

    first = invite(client, admin, organization, "first-limit@example.com")
    blocked = invite(client, admin, organization, "second-limit@example.com")

    assert first.status_code == 201
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Organization invitation limit reached"}


def test_concurrent_invitation_creations_cannot_exceed_pending_limit(
    engine,
    monkeypatch,
):
    suffix = unique_suffix()
    monkeypatch.setattr(
        "app.invitations.service.settings.organization_invitation_max_pending_per_organization",
        1,
    )
    with Session(engine, expire_on_commit=False) as session:
        admin = User(
            email=f"quota-admin-{suffix}@example.com",
            hashed_password=hash_password("password-123"),
            full_name="Quota Admin",
            is_active=True,
            is_superuser=True,
        )
        organization = Organization(name=f"Quota Org {suffix}")
        session.add_all([admin, organization])
        session.commit()
        admin_id = admin.id
        organization_id = organization.id

    barrier = Barrier(2)

    def create_once(index: int) -> int:
        with Session(engine) as session:
            barrier.wait()
            try:
                create_invitation(
                    session,
                    organization_id=organization_id,
                    email=f"quota-{index}-{suffix}@example.com",
                    invited_by_user_id=admin_id,
                )
                return 201
            except HTTPException as exc:
                return exc.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create_once, range(2)))

        assert sorted(results) == [201, 429]
        with Session(engine) as session:
            pending_count = session.scalar(
                select(func.count(OrganizationInvitation.id)).where(
                    OrganizationInvitation.organization_id == organization_id,
                    OrganizationInvitation.accepted_at.is_(None),
                    OrganizationInvitation.revoked_at.is_(None),
                )
            )
            assert pending_count == 1
    finally:
        with Session(engine) as session:
            session.execute(
                delete(Organization).where(Organization.id == organization_id)
            )
            session.execute(delete(User).where(User.id == admin_id))
            session.commit()


def test_invitation_listing_is_paginated(
    client, make_user, make_organization, grant_permissions
):
    admin = make_user()
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    invite(client, admin, organization, "first-page@example.com")
    invite(client, admin, organization, "second-page@example.com")

    response = client.get(
        f"/organizations/{organization.id}/invitations",
        headers=headers_for(admin),
        params={"limit": 1, "offset": 0},
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.headers["X-Total-Count"] == "2"
    assert response.headers["Cache-Control"] == "no-store"


def test_database_rejects_non_normalized_invitation_email(
    db, make_user, make_organization
):
    inviter = make_user()
    organization = make_organization()

    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(
                OrganizationInvitation(
                    organization_id=organization.id,
                    email="MixedCase@example.com",
                    token_hash="a" * 64,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    invited_by_user_id=inviter.id,
                )
            )
            db.flush()


def test_token_hash_collision_is_retried_without_overwriting_invitation(
    client,
    db,
    superuser,
    make_organization,
    monkeypatch,
):
    first_organization = make_organization()
    second_organization = make_organization()
    first = invite(client, superuser, first_organization, "first@example.com")
    first_token = first.json()["token"]
    replacement_token = "R" * 43
    generated = iter((first_token, replacement_token))
    monkeypatch.setattr(
        "app.invitations.service.secrets.token_urlsafe",
        lambda _size: next(generated),
    )

    second = invite(client, superuser, second_organization, "second@example.com")

    assert second.status_code == 201
    assert second.json()["token"] == replacement_token
    assert db.scalar(select(func.count(OrganizationInvitation.id))) == 2


def test_expired_invitation_cannot_be_previewed_or_accepted(
    client, db, make_user, make_organization, grant_permissions
):
    admin = make_user()
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    invited = make_user(email="expired@example.com")
    created = invite(client, admin, organization, invited.email).json()
    invitation = db.get(OrganizationInvitation, created["id"])
    assert invitation is not None
    invitation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    preview = client.post(
        "/auth/invitations/preview",
        json={"token": created["token"]},
    )
    accepted = accept(client, created["token"], invited)

    assert preview.status_code == accepted.status_code == 410
    assert preview.json() == accepted.json() == {"detail": UNAVAILABLE_DETAIL}
    assert membership_count(db, organization.id, invited.id) == 0


def test_acceptance_requires_matching_authenticated_identity(
    client, db, make_user, make_organization, grant_permissions
):
    admin = make_user()
    attacker = make_user(email="attacker@example.com")
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    created = invite(client, admin, organization, "new-person@example.com").json()

    unauthenticated = client.post(
        "/auth/invitations/accept",
        json={"token": created["token"]},
    )
    mismatched = accept(client, created["token"], attacker)
    registration_attempt = client.post(
        "/auth/invitations/accept",
        headers=headers_for(attacker),
        json={
            "token": created["token"],
            "full_name": "Pre-hijacked Person",
            "password": "attacker-password",
        },
    )

    assert unauthenticated.status_code == 401
    assert mismatched.status_code == 403
    assert mismatched.json() == {
        "detail": "Invitation does not match authenticated user"
    }
    assert registration_attempt.status_code == 422
    assert get_user_by_email(db, "new-person@example.com") is None


def test_acceptance_links_existing_identity_without_changing_global_fields(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    admin = make_user()
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    existing = make_user(
        email="existing-person@example.com",
        password="existing-password",
        full_name="Existing Name",
        is_active=True,
        is_superuser=True,
    )
    password_hash = existing.hashed_password
    created = invite(client, admin, organization, existing.email).json()
    preview = client.post(
        "/auth/invitations/preview",
        json={"token": created["token"]},
    )

    response = accept(client, created["token"], existing)

    assert preview.status_code == 200
    assert "requires_registration" not in preview.json()
    assert response.status_code == 200
    db.refresh(existing)
    assert existing.full_name == "Existing Name"
    assert existing.hashed_password == password_hash
    assert existing.is_active is True
    assert existing.is_superuser is True
    assert membership_count(db, organization.id, existing.id) == 1


def test_accepted_invitation_cannot_be_replayed(
    client, db, make_user, make_organization, grant_permissions
):
    admin = make_user()
    invited = make_user(email="single-use@example.com", full_name="Single Use")
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    created = invite(client, admin, organization, invited.email).json()

    first = accept(client, created["token"], invited)
    replay = accept(client, created["token"], invited)

    assert first.status_code == 200
    assert replay.status_code == 410
    assert replay.json() == {"detail": UNAVAILABLE_DETAIL}
    assert membership_count(db, organization.id, invited.id) == 1


def test_invitation_listing_and_revocation_are_tenant_isolated(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    admin_a = make_user()
    admin_b = make_user()
    organization_a = make_organization()
    organization_b = make_organization()
    grant_permissions(admin_a, organization_a, ["users.manage"])
    grant_permissions(admin_b, organization_b, ["users.manage"])
    invitation_b = invite(
        client, admin_b, organization_b, "private-b@example.com"
    ).json()

    foreign_list = client.get(
        f"/organizations/{organization_b.id}/invitations",
        headers=headers_for(admin_a),
    )
    cross_tenant_revoke = client.delete(
        f"/organizations/{organization_a.id}/invitations/{invitation_b['id']}",
        headers=headers_for(admin_a),
    )
    owner_list = client.get(
        f"/organizations/{organization_b.id}/invitations",
        headers=headers_for(admin_b),
    )

    assert foreign_list.status_code == 403
    assert foreign_list.json() == {"detail": PERMISSION_DETAIL}
    assert cross_tenant_revoke.status_code == 404
    assert owner_list.status_code == 200
    assert [item["email"] for item in owner_list.json()] == [
        "private-b@example.com"
    ]


def _seed_committed_invitations(engine, *, invitation_count: int):
    suffix = unique_suffix()
    email = f"concurrent-{suffix}@example.com"
    admin_email = f"concurrent-admin-{suffix}@example.com"
    with Session(engine, expire_on_commit=False) as session:
        admin = User(
            email=admin_email,
            hashed_password=hash_password("password-123"),
            full_name="Concurrent Admin",
            is_active=True,
            is_superuser=True,
        )
        organizations = [
            Organization(name=f"Concurrent Org {suffix} {index}")
            for index in range(invitation_count)
        ]
        invited = User(
            email=email,
            hashed_password=hash_password("password-123"),
            full_name="Concurrent User",
            is_active=True,
            is_superuser=False,
        )
        session.add_all([admin, invited, *organizations])
        session.commit()
        tokens = [
            create_invitation(
                session,
                organization_id=organization.id,
                email=email,
                invited_by_user_id=admin.id,
            )[1]
            for organization in organizations
        ]
        return (
            email,
            admin.id,
            invited.id,
            [organization.id for organization in organizations],
            tokens,
        )


def _cleanup_committed_invitations(
    engine,
    admin_id: int,
    organization_ids: list[int],
    email: str,
):
    with Session(engine) as session:
        session.execute(
            delete(Organization).where(Organization.id.in_(organization_ids))
        )
        session.execute(delete(User).where(User.email == email))
        session.execute(delete(User).where(User.id == admin_id))
        session.commit()


def test_concurrent_consumers_cannot_replay_same_token(engine):
    (
        email,
        admin_id,
        invited_id,
        organization_ids,
        tokens,
    ) = _seed_committed_invitations(
        engine, invitation_count=1
    )
    barrier = Barrier(2)

    def consume_once():
        with Session(engine, expire_on_commit=False) as session:
            invited = session.get(User, invited_id)
            assert invited is not None
            barrier.wait()
            try:
                invitation = accept_invitation(
                    session,
                    token=tokens[0],
                    current_user=invited,
                )
                return "accepted", invitation.id, invited.id
            except HTTPException as exc:
                return "rejected", exc.status_code, exc.detail

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: consume_once(), range(2)))

        assert sorted(result[0] for result in results) == ["accepted", "rejected"]
        assert next(result for result in results if result[0] == "rejected")[1:] == (
            410,
            UNAVAILABLE_DETAIL,
        )
        with Session(engine) as session:
            user = session.scalar(select(User).where(User.email == email))
            assert user is not None
            assert membership_count(session, organization_ids[0], user.id) == 1
    finally:
        _cleanup_committed_invitations(engine, admin_id, organization_ids, email)


def test_concurrent_invitations_link_same_authenticated_identity(engine):
    (
        email,
        admin_id,
        invited_id,
        organization_ids,
        tokens,
    ) = _seed_committed_invitations(
        engine, invitation_count=2
    )
    barrier = Barrier(2)

    def consume(index: int):
        with Session(engine, expire_on_commit=False) as session:
            invited = session.get(User, invited_id)
            assert invited is not None
            barrier.wait()
            invitation = accept_invitation(
                session,
                token=tokens[index],
                current_user=invited,
            )
            return invitation.id, invited.id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(consume, range(2)))

        assert len({user_id for _invitation_id, user_id in results}) == 1
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count(User.id)).where(User.email == email))
                == 1
            )
            user = session.scalar(select(User).where(User.email == email))
            assert user is not None
            assert all(
                membership_count(session, organization_id, user.id) == 1
                for organization_id in organization_ids
            )
    finally:
        _cleanup_committed_invitations(engine, admin_id, organization_ids, email)
