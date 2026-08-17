import pytest
from conftest import headers_for, unique_suffix


@pytest.fixture
def government_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


def member_payload(organization_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "level": "concejalia",
        "full_name": f"Concejal {suffix}",
        "role_title": "Concejal de Urbanismo",
        "political_group": "Grupo municipal",
        "email": f"concejal-{suffix}@example.com",
        "phone": "947 000 000",
        "sort_order": 10,
    }
    payload.update(overrides)
    return payload


def create_member(client, auth, organization_id, **overrides):
    response = client.post(
        "/government/members",
        json=member_payload(organization_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "path",
    [
        "/government/members?organization_id=1",
        "/government/members/1",
    ],
)
def test_government_requires_authentication(client, path):
    response = client.get(path)

    assert response.status_code == 401


def test_government_permissions_are_scoped_to_the_target_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    member = create_member(client, headers_for(superuser), target.id)

    user = make_user()
    # El permiso vive en otra organización: dentro de `target` no vale de nada,
    # aunque la persona sea miembro de las dos.
    grant_permissions(user, other, ["government.manage"])
    add_member(user, target)
    auth = headers_for(user)

    list_response = client.get(
        "/government/members",
        params={"organization_id": target.id},
        headers=auth,
    )
    detail_response = client.get(f"/government/members/{member['id']}", headers=auth)
    create_response = client.post(
        "/government/members",
        json=member_payload(target.id),
        headers=auth,
    )

    assert list_response.status_code == 403
    assert list_response.json()["detail"] == "Permission required: government.view"
    assert detail_response.status_code == 403
    assert create_response.status_code == 403
    assert create_response.json()["detail"] == "Permission required: government.manage"


def test_viewer_can_read_but_not_write_the_corporation(
    client,
    make_user,
    government_organization,
    grant_permissions,
    superuser,
):
    member = create_member(
        client,
        headers_for(superuser),
        government_organization.id,
    )

    viewer = make_user()
    grant_permissions(viewer, government_organization, ["government.view"])
    auth = headers_for(viewer)

    listed = client.get(
        "/government/members",
        params={"organization_id": government_organization.id},
        headers=auth,
    )
    detail = client.get(f"/government/members/{member['id']}", headers=auth)
    denied_create = client.post(
        "/government/members",
        json=member_payload(government_organization.id),
        headers=auth,
    )
    denied_update = client.patch(
        f"/government/members/{member['id']}",
        json={"full_name": "No permitido"},
        headers=auth,
    )

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [member["id"]]
    assert detail.status_code == 200
    assert denied_create.status_code == 403
    assert denied_update.status_code == 403
    assert denied_update.json()["detail"] == "Permission required: government.manage"


def test_manager_creates_updates_and_archives_a_member(
    client,
    make_user,
    government_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, government_organization, ["government.manage"])
    auth = headers_for(manager)

    member = create_member(
        client,
        auth,
        government_organization.id,
        level="alcaldia",
        role_title="Alcalde",
        term_start_date="2023-06-17",
        sort_order=0,
    )

    assert member["level"] == "alcaldia"
    assert member["status"] == "active"
    assert member["created_by_id"] == manager.id
    assert member["updated_by_id"] == manager.id

    updated = client.patch(
        f"/government/members/{member['id']}",
        json={"political_group": "Independiente", "term_end_date": "2027-06-17"},
        headers=auth,
    )
    assert updated.status_code == 200
    assert updated.json()["political_group"] == "Independiente"
    assert updated.json()["term_end_date"] == "2027-06-17"

    archived = client.patch(
        f"/government/members/{member['id']}",
        json={"status": "archived"},
        headers=auth,
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    default_list = client.get(
        "/government/members",
        params={"organization_id": government_organization.id},
        headers=auth,
    )
    with_archived = client.get(
        "/government/members",
        params={"organization_id": government_organization.id, "include_archived": True},
        headers=auth,
    )
    assert default_list.json() == []
    assert [item["id"] for item in with_archived.json()] == [member["id"]]


def test_members_are_listed_in_protocol_order_and_filtered_by_level(
    client,
    make_user,
    government_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, government_organization, ["government.manage"])
    auth = headers_for(manager)

    mayor = create_member(
        client,
        auth,
        government_organization.id,
        level="alcaldia",
        role_title="Alcalde",
        sort_order=0,
    )
    deputy = create_member(
        client,
        auth,
        government_organization.id,
        level="tenencia",
        role_title="Primer teniente de alcalde",
        sort_order=1,
    )
    councillor = create_member(
        client,
        auth,
        government_organization.id,
        level="concejalia",
        sort_order=2,
    )

    ordered = client.get(
        "/government/members",
        params={"organization_id": government_organization.id},
        headers=auth,
    )
    only_councillors = client.get(
        "/government/members",
        params={
            "organization_id": government_organization.id,
            "level": "concejalia",
        },
        headers=auth,
    )

    assert [item["id"] for item in ordered.json()] == [
        mayor["id"],
        deputy["id"],
        councillor["id"],
    ]
    assert ordered.headers["X-Total-Count"] == "3"
    assert [item["id"] for item in only_councillors.json()] == [councillor["id"]]


def test_members_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    super_auth = headers_for(superuser)
    own = create_member(client, super_auth, target.id)
    foreign = create_member(client, super_auth, other.id)

    manager = make_user()
    grant_permissions(manager, target, ["government.manage"])
    listed = client.get(
        "/government/members",
        params={"organization_id": target.id},
        headers=headers_for(manager),
    )
    foreign_detail = client.get(
        f"/government/members/{foreign['id']}",
        headers=headers_for(manager),
    )

    assert [item["id"] for item in listed.json()] == [own["id"]]
    assert foreign_detail.status_code == 403


def test_inverted_term_dates_are_rejected_on_create_and_patch(
    client,
    make_user,
    government_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, government_organization, ["government.manage"])
    auth = headers_for(manager)

    invalid_create = client.post(
        "/government/members",
        json=member_payload(
            government_organization.id,
            term_start_date="2027-01-01",
            term_end_date="2026-01-01",
        ),
        headers=auth,
    )
    assert invalid_create.status_code == 422

    member = create_member(
        client,
        auth,
        government_organization.id,
        term_start_date="2023-06-17",
    )
    # El parche sólo trae la fecha final: el rango se valida contra la inicial
    # que ya está guardada, no contra un campo ausente.
    invalid_patch = client.patch(
        f"/government/members/{member['id']}",
        json={"term_end_date": "2020-01-01"},
        headers=auth,
    )
    assert invalid_patch.status_code == 422


def test_paused_organization_is_read_only_and_archived_is_unavailable(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    paused = make_organization(name=f"Pausada {unique_suffix()}")
    member = create_member(client, headers_for(superuser), paused.id)
    # La corporación se registra con la organización activa y luego se pausa:
    # crear directamente en pausa sería imposible, que es justo lo que valida
    # la segunda mitad del test.
    paused.status = "paused"
    db.commit()
    paused_manager = make_user()
    grant_permissions(paused_manager, paused, ["government.manage"])
    paused_auth = headers_for(paused_manager)

    listed = client.get(
        "/government/members",
        params={"organization_id": paused.id},
        headers=paused_auth,
    )
    denied_update = client.patch(
        f"/government/members/{member['id']}",
        json={"full_name": "Cambio en organización pausada"},
        headers=paused_auth,
    )

    archived_organization = make_organization(
        name=f"Archivada {unique_suffix()}",
        status="archived",
    )
    archived_manager = make_user()
    grant_permissions(archived_manager, archived_organization, ["government.manage"])
    archived_read = client.get(
        "/government/members",
        params={"organization_id": archived_organization.id},
        headers=headers_for(archived_manager),
    )

    assert listed.status_code == 200
    assert denied_update.status_code == 409
    assert archived_read.status_code == 409


def test_unknown_member_and_organization_are_reported_as_missing(
    client,
    make_user,
    government_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, government_organization, ["government.manage"])
    auth = headers_for(manager)

    missing_member = client.get("/government/members/999999", headers=auth)
    missing_organization = client.get(
        "/government/members",
        params={"organization_id": 999999},
        headers=auth,
    )

    assert missing_member.status_code == 404
    assert missing_organization.status_code == 404


def test_empty_patch_needs_only_view_permission(
    client,
    make_user,
    government_organization,
    grant_permissions,
    superuser,
):
    member = create_member(
        client,
        headers_for(superuser),
        government_organization.id,
    )
    viewer = make_user()
    grant_permissions(viewer, government_organization, ["government.view"])

    response = client.patch(
        f"/government/members/{member['id']}",
        json={},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert response.json()["id"] == member["id"]
