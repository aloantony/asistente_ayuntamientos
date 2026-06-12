from conftest import headers_for, unique_suffix


def make_municipalities(client, superuser, count: int) -> list[int]:
    ids = []
    for index in range(count):
        response = client.post(
            "/municipalities",
            json={
                "name": f"Pag-{index:02d}-{unique_suffix()}",
                "province": "Test",
                "autonomous_community": "Test",
            },
            headers=headers_for(superuser),
        )
        assert response.status_code == 201
        ids.append(response.json()["id"])
    return ids


def test_municipalities_pagination_and_total_header(client, superuser):
    make_municipalities(client, superuser, 5)

    first_page = client.get(
        "/municipalities?limit=2&offset=0",
        headers=headers_for(superuser),
    )
    assert first_page.status_code == 200
    assert len(first_page.json()) == 2
    assert first_page.headers["X-Total-Count"] == "5"

    last_page = client.get(
        "/municipalities?limit=2&offset=4",
        headers=headers_for(superuser),
    )
    assert len(last_page.json()) == 1

    # Pages do not overlap.
    first_ids = {m["id"] for m in first_page.json()}
    last_ids = {m["id"] for m in last_page.json()}
    assert first_ids.isdisjoint(last_ids)


def test_pagination_limit_is_validated(client, superuser):
    response = client.get(
        "/municipalities?limit=9999",
        headers=headers_for(superuser),
    )
    assert response.status_code == 422


def test_ordinance_listing_omits_text_content(client, superuser):
    municipality_id = make_municipalities(client, superuser, 1)[0]
    created = client.post(
        "/ordinances",
        json={
            "municipality_id": municipality_id,
            "title": "Ordenanza de prueba",
            "topic": "residuos",
            "ordinance_type": "ordinance",
            "text_content": "Texto legal completo de la ordenanza.",
        },
        headers=headers_for(superuser),
    )
    assert created.status_code == 201
    ordinance_id = created.json()["id"]

    listing = client.get("/ordinances", headers=headers_for(superuser))
    assert listing.status_code == 200
    assert "X-Total-Count" in listing.headers
    listed = next(o for o in listing.json() if o["id"] == ordinance_id)
    assert "text_content" not in listed

    detail = client.get(f"/ordinances/{ordinance_id}", headers=headers_for(superuser))
    assert detail.json()["text_content"] == "Texto legal completo de la ordenanza."


def test_requirements_and_admin_users_expose_total_count(
    client,
    superuser,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["requirements.create"])

    created = client.post(
        "/requirements",
        json={"organization_id": organization.id, "title": "Paginado"},
        headers=headers_for(user),
    )
    assert created.status_code == 201

    requirements = client.get("/requirements", headers=headers_for(superuser))
    assert requirements.status_code == 200
    assert int(requirements.headers["X-Total-Count"]) >= 1

    users = client.get("/admin/users?limit=1", headers=headers_for(superuser))
    assert users.status_code == 200
    assert len(users.json()) == 1
    assert int(users.headers["X-Total-Count"]) >= 2
