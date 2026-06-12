from conftest import headers_for


def test_login_sets_httponly_cookie_usable_for_session(client, make_user):
    make_user(email="cookie@example.com", password="password-123")

    response = client.post(
        "/auth/login",
        json={"email": "cookie@example.com", "password": "password-123"},
    )

    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()

    # The TestClient keeps the cookie jar: /auth/me works without a header.
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "cookie@example.com"


def test_logout_clears_session_cookie(client, make_user):
    make_user(email="bye@example.com", password="password-123")
    client.post(
        "/auth/login",
        json={"email": "bye@example.com", "password": "password-123"},
    )
    assert client.get("/auth/me").status_code == 200

    logout = client.post("/auth/logout")
    assert logout.status_code == 200

    assert client.get("/auth/me").status_code == 401


def test_bearer_header_still_works(client, make_user):
    user = make_user()

    response = client.get("/auth/me", headers=headers_for(user))

    assert response.status_code == 200


def test_change_password_requires_current_password(client, make_user):
    user = make_user(password="password-123")

    response = client.post(
        "/auth/change-password",
        json={"current_password": "wrong-password", "new_password": "new-password-9"},
        headers=headers_for(user),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Current password is incorrect"


def test_change_password_updates_credentials(client, make_user):
    user = make_user(email="rotate@example.com", password="password-123")

    response = client.post(
        "/auth/change-password",
        json={"current_password": "password-123", "new_password": "new-password-9"},
        headers=headers_for(user),
    )
    assert response.status_code == 200

    old_login = client.post(
        "/auth/login",
        json={"email": "rotate@example.com", "password": "password-123"},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/auth/login",
        json={"email": "rotate@example.com", "password": "new-password-9"},
    )
    assert new_login.status_code == 200


def test_login_rate_limit_returns_429(client, make_user):
    make_user(email="brute@example.com", password="password-123")

    for _ in range(10):
        response = client.post(
            "/auth/login",
            json={"email": "brute@example.com", "password": "bad-password"},
        )
        assert response.status_code == 401

    blocked = client.post(
        "/auth/login",
        json={"email": "brute@example.com", "password": "password-123"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "Too many login attempts"


def test_admin_can_reset_member_password(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    member = make_user(email="member-reset@example.com", password="password-123")
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    add_member(member, organization)

    response = client.patch(
        f"/admin/users/{member.id}",
        json={"password": "reset-password-9"},
        headers=headers_for(admin),
    )
    assert response.status_code == 200

    login = client.post(
        "/auth/login",
        json={"email": "member-reset@example.com", "password": "reset-password-9"},
    )
    assert login.status_code == 200


def test_logout_requires_authentication(client):
    response = client.post("/auth/logout")

    assert response.status_code == 401


def test_change_password_invalidates_previous_tokens(client, make_user):
    user = make_user(email="revoke@example.com", password="password-123")
    old_headers = headers_for(user)
    assert client.get("/auth/me", headers=old_headers).status_code == 200

    response = client.post(
        "/auth/change-password",
        json={"current_password": "password-123", "new_password": "new-password-9"},
        headers=old_headers,
    )
    assert response.status_code == 200
    # The response refreshes the session cookie so the user stays signed in.
    assert "access_token=" in response.headers.get("set-cookie", "")
    assert client.get("/auth/me").status_code == 200

    # Tokens issued before the change are revoked.
    assert client.get("/auth/me", headers=old_headers).status_code == 401


def test_change_password_rate_limit_returns_429(client, make_user):
    user = make_user(password="password-123")
    headers = headers_for(user)

    for _ in range(10):
        response = client.post(
            "/auth/change-password",
            json={
                "current_password": "wrong-password",
                "new_password": "new-password-9",
            },
            headers=headers,
        )
        assert response.status_code == 400

    blocked = client.post(
        "/auth/change-password",
        json={"current_password": "password-123", "new_password": "new-password-9"},
        headers=headers,
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "Too many password attempts"


def test_login_rate_limit_is_per_account(client, make_user):
    make_user(email="noisy@example.com", password="password-123")
    make_user(email="quiet@example.com", password="password-123")

    for _ in range(10):
        client.post(
            "/auth/login",
            json={"email": "noisy@example.com", "password": "bad-password"},
        )

    blocked = client.post(
        "/auth/login",
        json={"email": "noisy@example.com", "password": "password-123"},
    )
    assert blocked.status_code == 429

    # One noisy account does not lock everyone else out (clients share the
    # docker-proxy IP, so an IP-only key would be a global budget).
    other = client.post(
        "/auth/login",
        json={"email": "quiet@example.com", "password": "password-123"},
    )
    assert other.status_code == 200


def test_successful_logins_do_not_consume_rate_limit(client, make_user):
    make_user(email="busy@example.com", password="password-123")

    for _ in range(12):
        response = client.post(
            "/auth/login",
            json={"email": "busy@example.com", "password": "password-123"},
        )
        assert response.status_code == 200


def test_admin_password_reset_invalidates_member_tokens(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    member = make_user(email="locked-out@example.com", password="password-123")
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    add_member(member, organization)
    member_headers = headers_for(member)
    assert client.get("/auth/me", headers=member_headers).status_code == 200

    response = client.patch(
        f"/admin/users/{member.id}",
        json={"password": "rotated-password-9"},
        headers=headers_for(admin),
    )
    assert response.status_code == 200

    # The compromised-account case: sessions issued before the reset die.
    assert client.get("/auth/me", headers=member_headers).status_code == 401


def test_admin_resetting_own_password_keeps_session(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    admin = make_user(email="self-reset@example.com", password="password-123")
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])

    login = client.post(
        "/auth/login",
        json={"email": "self-reset@example.com", "password": "password-123"},
    )
    assert login.status_code == 200

    response = client.patch(
        f"/admin/users/{admin.id}",
        json={"password": "rotated-password-9"},
    )
    assert response.status_code == 200
    # The refreshed cookie keeps the admin signed in despite the revocation.
    assert "access_token=" in response.headers.get("set-cookie", "")
    assert client.get("/auth/me").status_code == 200


def test_admin_password_reset_preserves_whitespace(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    member = make_user(email="spaced@example.com", password="password-123")
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    add_member(member, organization)

    response = client.patch(
        f"/admin/users/{member.id}",
        json={"password": "  spaced-secret-9  "},
        headers=headers_for(admin),
    )
    assert response.status_code == 200

    # The credential is stored exactly as typed, surrounding spaces included.
    stripped = client.post(
        "/auth/login",
        json={"email": "spaced@example.com", "password": "spaced-secret-9"},
    )
    assert stripped.status_code == 401

    exact = client.post(
        "/auth/login",
        json={"email": "spaced@example.com", "password": "  spaced-secret-9  "},
    )
    assert exact.status_code == 200


def test_non_superuser_cannot_reset_superuser_password(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    admin = make_user()
    boss = make_user(is_superuser=True)
    organization = make_organization()
    grant_permissions(admin, organization, ["users.manage"])
    add_member(boss, organization)

    response = client.patch(
        f"/admin/users/{boss.id}",
        json={"password": "hijacked-password"},
        headers=headers_for(admin),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Only superusers can reset a superuser password"
