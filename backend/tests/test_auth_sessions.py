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
