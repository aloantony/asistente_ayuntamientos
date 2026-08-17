import re

import pytest
from conftest import headers_for, unique_suffix

from app.assets.seed import INITIAL_ASSET_TAXONOMY
from app.municipalities.models import Municipality

CODE_PATTERN = re.compile(r"^[a-z0-9]+([-_][a-z0-9]+)*$")


@pytest.fixture
def seed_organization(db, make_organization):
    suffix = unique_suffix()
    municipality = Municipality(
        name=f"Municipio {suffix}",
        province="Burgos",
        autonomous_community="Castilla y Leon",
        ine_code=f"seed-{suffix}",
    )
    db.add(municipality)
    db.commit()
    return make_organization(
        name=f"Ayuntamiento {suffix}",
        municipality_id=municipality.id,
    )


def seed(client, auth, organization_id):
    return client.post(
        "/assets/taxonomy/seed",
        json={"organization_id": organization_id},
        headers=auth,
    )


def test_every_seeded_code_satisfies_the_database_constraint():
    """La base exige códigos ASCII en minúscula; el seed no puede saltárselo."""
    for category in INITIAL_ASSET_TAXONOMY:
        assert CODE_PATTERN.fullmatch(category["code"]), category["code"]
        for type_code, _ in category["types"]:
            assert CODE_PATTERN.fullmatch(type_code), type_code


def test_every_seeded_colour_is_a_hex_triplet():
    for category in INITIAL_ASSET_TAXONOMY:
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", category["color"]), category["code"]


def test_seeding_requires_manage_in_the_target_organization(
    client,
    make_user,
    seed_organization,
    grant_permissions,
):
    creator = make_user()
    grant_permissions(creator, seed_organization, ["assets.create"])

    denied = seed(client, headers_for(creator), seed_organization.id)

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Permission required: assets.manage"


def test_the_seed_creates_the_whole_starting_taxonomy(
    client,
    make_user,
    seed_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, seed_organization, ["assets.manage"])
    auth = headers_for(manager)

    response = seed(client, auth, seed_organization.id)
    categories = client.get(
        "/assets/categories",
        params={"organization_id": seed_organization.id, "limit": 200},
        headers=auth,
    ).json()

    assert response.status_code == 201
    assert len(categories) == len(INITIAL_ASSET_TAXONOMY)
    # El orden del árbol de capas lo fija `sort_order`, no el alfabeto.
    assert [item["code"] for item in categories] == [
        entry["code"] for entry in INITIAL_ASSET_TAXONOMY
    ]
    assert categories[0]["created_by_id"] == manager.id


def test_seeding_twice_creates_nothing_new(
    client,
    make_user,
    seed_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, seed_organization, ["assets.manage"])
    auth = headers_for(manager)

    first = seed(client, auth, seed_organization.id)
    second = seed(client, auth, seed_organization.id)
    categories = client.get(
        "/assets/categories",
        params={"organization_id": seed_organization.id, "limit": 200},
        headers=auth,
    ).json()

    assert len(first.json()["created"]) > 0
    # Es idempotente: repetir la llamada no duplica ni deshace nada.
    assert second.json()["created"] == []
    assert len(categories) == len(INITIAL_ASSET_TAXONOMY)


def test_the_seed_never_overwrites_what_the_town_hall_changed(
    client,
    make_user,
    seed_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, seed_organization, ["assets.manage"])
    auth = headers_for(manager)
    seed(client, auth, seed_organization.id)

    categories = client.get(
        "/assets/categories",
        params={"organization_id": seed_organization.id, "limit": 200},
        headers=auth,
    ).json()
    water = next(item for item in categories if item["code"] == "agua")
    client.patch(
        f"/assets/categories/{water['id']}",
        json={"name": "Aguas del municipio", "color": "#123456"},
        headers=auth,
    )

    seed(client, auth, seed_organization.id)
    after = client.get(
        "/assets/categories",
        params={"organization_id": seed_organization.id, "limit": 200},
        headers=auth,
    ).json()
    water_after = next(item for item in after if item["code"] == "agua")

    # Un ayuntamiento que renombra su categoría no debe encontrársela revertida.
    assert water_after["name"] == "Aguas del municipio"
    assert water_after["color"] == "#123456"


def test_an_archived_category_stays_archived(
    client,
    make_user,
    seed_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, seed_organization, ["assets.manage"])
    auth = headers_for(manager)
    seed(client, auth, seed_organization.id)

    categories = client.get(
        "/assets/categories",
        params={"organization_id": seed_organization.id, "limit": 200},
        headers=auth,
    ).json()
    cemetery = next(item for item in categories if item["code"] == "cementerio")
    client.patch(
        f"/assets/categories/{cemetery['id']}",
        json={"status": "archived"},
        headers=auth,
    )

    seed(client, auth, seed_organization.id)
    with_archived = client.get(
        "/assets/categories",
        params={
            "organization_id": seed_organization.id,
            "include_archived": True,
            "limit": 200,
        },
        headers=auth,
    ).json()
    cemetery_after = next(
        item for item in with_archived if item["code"] == "cementerio"
    )

    # Archivar es una decisión del municipio; el seed no la revierte.
    assert cemetery_after["status"] == "archived"


def test_missing_types_are_completed_in_a_hand_made_category(
    client,
    make_user,
    seed_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, seed_organization, ["assets.manage"])
    auth = headers_for(manager)

    # La categoría existe pero fue creada a mano, sin sus tipos.
    created = client.post(
        "/assets/categories",
        json={
            "organization_id": seed_organization.id,
            "code": "agua",
            "name": "Agua",
        },
        headers=auth,
    ).json()

    seed(client, auth, seed_organization.id)
    types = client.get(
        "/assets/types",
        params={
            "organization_id": seed_organization.id,
            "category_id": created["id"],
            "limit": 200,
        },
        headers=auth,
    ).json()

    expected = {
        code
        for entry in INITIAL_ASSET_TAXONOMY
        if entry["code"] == "agua"
        for code, _ in entry["types"]
    }
    assert {item["code"] for item in types} == expected


def test_the_seed_does_not_reach_other_organizations(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    manager = make_user()
    grant_permissions(manager, target, ["assets.manage"])
    grant_permissions(manager, other, ["assets.manage"])
    auth = headers_for(manager)

    seed(client, auth, target.id)
    elsewhere = client.get(
        "/assets/categories",
        params={"organization_id": other.id, "limit": 200},
        headers=auth,
    ).json()

    assert elsewhere == []


def test_a_paused_organization_is_not_seeded(
    client,
    db,
    make_user,
    seed_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, seed_organization, ["assets.manage"])
    seed_organization.status = "paused"
    db.commit()

    response = seed(client, headers_for(manager), seed_organization.id)

    assert response.status_code == 409
