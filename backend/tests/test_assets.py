import json

import pytest
from conftest import headers_for, unique_suffix
from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import IntegrityError

from app.assets.models import (
    MunicipalAsset,
    MunicipalAssetCategory,
    MunicipalAssetType,
)
from app.geo.models import GeoLocation
from app.municipalities.models import Municipality
from app.organizations.models import Organization
from app.requirements.models import Requirement


@pytest.fixture
def make_municipal_organization(db, make_organization):
    def _make(*, organization_status="active", municipality_status="active"):
        suffix = unique_suffix()
        municipality = Municipality(
            name=f"Municipio {suffix}",
            province="Burgos",
            autonomous_community="Castilla y Leon",
            ine_code=f"asset-{suffix}",
            status=municipality_status,
        )
        db.add(municipality)
        db.commit()
        organization = make_organization(
            name=f"Ayuntamiento {suffix}",
            municipality_id=municipality.id,
            status=organization_status,
        )
        return organization, municipality

    return _make


@pytest.fixture
def municipal_organization(make_municipal_organization):
    return make_municipal_organization()


def category_payload(organization_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "code": f"category-{suffix}",
        "name": f"Categoria {suffix}",
        "description": "Infraestructura municipal",
        "color": "#336699",
        "sort_order": 10,
    }
    payload.update(overrides)
    return payload


def type_payload(organization_id, category_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "category_id": category_id,
        "code": f"type-{suffix}",
        "name": f"Tipo {suffix}",
        "description": "Tipo de activo municipal",
        "sort_order": 10,
    }
    payload.update(overrides)
    return payload


def asset_payload(organization_id, asset_type_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "asset_type_id": asset_type_id,
        "code": f"ASSET-{suffix}",
        "name": f"Activo {suffix}",
        "description": "Elemento del inventario municipal",
        "condition_status": "unknown",
    }
    payload.update(overrides)
    return payload


def create_category(client, auth, organization_id, **overrides):
    response = client.post(
        "/assets/categories",
        json=category_payload(organization_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_type(client, auth, organization_id, category_id, **overrides):
    response = client.post(
        "/assets/types",
        json=type_payload(organization_id, category_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_asset(client, auth, organization_id, asset_type_id, **overrides):
    response = client.post(
        "/assets",
        json=asset_payload(organization_id, asset_type_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_hierarchy(client, auth, organization_id):
    category = create_category(client, auth, organization_id)
    asset_type = create_type(client, auth, organization_id, category["id"])
    return category, asset_type


def make_location(db, organization_id, municipality_id, *, label):
    location = GeoLocation(
        organization_id=organization_id,
        municipality_id=municipality_id,
        label=label,
        geometry_type="point",
        geometry_json=json.dumps(
            {"type": "Point", "coordinates": [-3.7, 42.34]}
        ),
        latitude=42.34,
        longitude=-3.7,
        source="manual_review",
        review_status="reviewed",
    )
    db.add(location)
    db.commit()
    return location


def entity_location_payload(
    requirement_id,
    organization_id,
    municipality_id,
    *,
    label="Plaza Mayor",
    latitude=42.34,
    longitude=-3.7,
):
    return {
        "entity_type": "requirement",
        "entity_id": requirement_id,
        "role": "primary",
        "location": {
            "organization_id": organization_id,
            "municipality_id": municipality_id,
            "label": label,
            "latitude": latitude,
            "longitude": longitude,
            "source": "manual_review",
            "review_status": "reviewed",
        },
    }


@pytest.mark.parametrize(
    "path",
    [
        "/assets/categories?organization_id=1",
        "/assets/types?organization_id=1",
        "/assets?organization_id=1",
    ],
)
def test_asset_inventory_requires_authentication(client, path):
    response = client.get(path)

    assert response.status_code == 401


def test_asset_permissions_are_scoped_to_the_target_organization(
    client,
    make_user,
    make_municipal_organization,
    grant_permissions,
    add_member,
    superuser,
):
    target, _ = make_municipal_organization()
    other, _ = make_municipal_organization()
    category, asset_type = create_hierarchy(
        client, headers_for(superuser), target.id
    )
    asset = create_asset(client, headers_for(superuser), target.id, asset_type["id"])

    user = make_user()
    grant_permissions(user, other, ["assets.manage"])
    add_member(user, target)
    auth = headers_for(user)

    list_response = client.get(
        "/assets", params={"organization_id": target.id}, headers=auth
    )
    detail_response = client.get(f"/assets/{asset['id']}", headers=auth)
    create_response = client.post(
        "/assets/categories",
        json=category_payload(target.id),
        headers=auth,
    )

    assert list_response.status_code == 403
    assert list_response.json()["detail"] == "Permission required: assets.view"
    assert detail_response.status_code == 403
    assert create_response.status_code == 403
    assert create_response.json()["detail"] == "Permission required: assets.create"
    assert category["organization_id"] == target.id


def test_view_create_edit_and_archive_permissions_are_separate(
    client,
    make_user,
    municipal_organization,
    grant_permissions,
    superuser,
):
    organization, _ = municipal_organization
    super_auth = headers_for(superuser)
    _, asset_type = create_hierarchy(client, super_auth, organization.id)
    asset = create_asset(client, super_auth, organization.id, asset_type["id"])

    viewer = make_user()
    grant_permissions(viewer, organization, ["assets.view"])
    viewer_auth = headers_for(viewer)
    assert (
        client.get(
            "/assets",
            params={"organization_id": organization.id},
            headers=viewer_auth,
        ).status_code
        == 200
    )
    assert client.get(f"/assets/{asset['id']}", headers=viewer_auth).status_code == 200
    assert (
        client.post(
            "/assets/categories",
            json=category_payload(organization.id),
            headers=viewer_auth,
        ).status_code
        == 403
    )

    creator = make_user()
    grant_permissions(creator, organization, ["assets.create"])
    created_category = client.post(
        "/assets/categories",
        json=category_payload(organization.id),
        headers=headers_for(creator),
    )
    assert created_category.status_code == 201
    assert (
        client.patch(
            f"/assets/categories/{created_category.json()['id']}",
            json={"name": "No permitido"},
            headers=headers_for(creator),
        ).status_code
        == 403
    )

    editor = make_user()
    grant_permissions(editor, organization, ["assets.edit"])
    edited = client.patch(
        f"/assets/{asset['id']}",
        json={"name": "Farola editada"},
        headers=headers_for(editor),
    )
    denied_archive = client.patch(
        f"/assets/{asset['id']}",
        json={"status": "archived"},
        headers=headers_for(editor),
    )
    assert edited.status_code == 200
    assert edited.json()["name"] == "Farola editada"
    assert denied_archive.status_code == 403
    assert denied_archive.json()["detail"] == "Permission required: assets.archive"

    archiver = make_user()
    grant_permissions(archiver, organization, ["assets.archive"])
    denied_mixed_archive = client.patch(
        f"/assets/{asset['id']}",
        json={"status": "archived", "name": "Cambio encubierto"},
        headers=headers_for(archiver),
    )
    archived = client.patch(
        f"/assets/{asset['id']}",
        json={"status": "archived"},
        headers=headers_for(archiver),
    )
    denied_edit = client.patch(
        f"/assets/{asset['id']}",
        json={"name": "Tampoco permitido"},
        headers=headers_for(archiver),
    )
    assert denied_mixed_archive.status_code == 403
    assert denied_mixed_archive.json()["detail"] == "Permission required: assets.edit"
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert denied_edit.status_code == 403
    assert denied_edit.json()["detail"] == "Permission required: assets.edit"


def test_manage_can_create_hierarchy_and_asset_derives_municipality(
    client,
    make_user,
    municipal_organization,
    grant_permissions,
):
    organization, municipality = municipal_organization
    manager = make_user()
    grant_permissions(manager, organization, ["assets.manage"])
    auth = headers_for(manager)

    category, asset_type = create_hierarchy(client, auth, organization.id)
    asset = create_asset(
        client,
        auth,
        organization.id,
        asset_type["id"],
        name="Farola Plaza Mayor",
        condition_status="good",
        material="Acero",
        dimensions="8 m",
        installed_on="2020-06-15",
        last_inspected_on="2026-07-01",
        notes="Revisada",
    )

    assert category["created_by_id"] == manager.id
    assert asset_type["category"]["id"] == category["id"]
    assert asset["organization_id"] == organization.id
    assert asset["municipality_id"] == municipality.id
    assert asset["asset_type"]["id"] == asset_type["id"]
    assert asset["asset_type"]["category"]["id"] == category["id"]
    assert asset["location"] is None
    assert asset["created_by_id"] == manager.id

    updated = client.patch(
        f"/assets/{asset['id']}",
        json={"condition_status": "fair", "notes": "Seguimiento anual"},
        headers=auth,
    )
    assert updated.status_code == 200
    assert updated.json()["condition_status"] == "fair"
    assert updated.json()["updated_by_id"] == manager.id


def test_creator_cannot_create_archived_inventory_without_archive_permission(
    client,
    make_user,
    municipal_organization,
    grant_permissions,
):
    organization, _ = municipal_organization
    creator = make_user()
    grant_permissions(creator, organization, ["assets.create"])
    auth = headers_for(creator)

    denied_category = client.post(
        "/assets/categories",
        json=category_payload(organization.id, status="archived"),
        headers=auth,
    )
    category = create_category(client, auth, organization.id)
    denied_type = client.post(
        "/assets/types",
        json=type_payload(
            organization.id,
            category["id"],
            status="archived",
        ),
        headers=auth,
    )
    asset_type = create_type(client, auth, organization.id, category["id"])
    denied_asset = client.post(
        "/assets",
        json=asset_payload(
            organization.id,
            asset_type["id"],
            status="archived",
        ),
        headers=auth,
    )

    for response in (denied_category, denied_type, denied_asset):
        assert response.status_code == 403
        assert response.json()["detail"] == "Permission required: assets.archive"


def test_archived_taxonomy_cannot_receive_new_active_children(
    client,
    make_user,
    municipal_organization,
    grant_permissions,
):
    organization, _ = municipal_organization
    manager = make_user()
    grant_permissions(manager, organization, ["assets.manage"])
    auth = headers_for(manager)

    archived_category = create_category(client, auth, organization.id)
    archive_category = client.patch(
        f"/assets/categories/{archived_category['id']}",
        json={"status": "archived"},
        headers=auth,
    )
    assert archive_category.status_code == 200
    type_under_archived_category = client.post(
        "/assets/types",
        json=type_payload(organization.id, archived_category["id"]),
        headers=auth,
    )

    active_category = create_category(client, auth, organization.id)
    archived_type = create_type(
        client,
        auth,
        organization.id,
        active_category["id"],
    )
    archive_type = client.patch(
        f"/assets/types/{archived_type['id']}",
        json={"status": "archived"},
        headers=auth,
    )
    assert archive_type.status_code == 200
    asset_with_archived_type = client.post(
        "/assets",
        json=asset_payload(organization.id, archived_type["id"]),
        headers=auth,
    )

    category_archived_after_type = create_category(
        client,
        auth,
        organization.id,
    )
    active_type = create_type(
        client,
        auth,
        organization.id,
        category_archived_after_type["id"],
    )
    archive_parent = client.patch(
        f"/assets/categories/{category_archived_after_type['id']}",
        json={"status": "archived"},
        headers=auth,
    )
    assert archive_parent.status_code == 200
    asset_with_archived_category = client.post(
        "/assets",
        json=asset_payload(organization.id, active_type["id"]),
        headers=auth,
    )

    assert type_under_archived_category.status_code == 409
    assert asset_with_archived_type.status_code == 409
    assert asset_with_archived_category.status_code == 409


def test_type_and_asset_reject_taxonomy_from_another_organization(
    client,
    superuser,
    make_municipal_organization,
):
    first, _ = make_municipal_organization()
    second, _ = make_municipal_organization()
    auth = headers_for(superuser)
    first_category, first_type = create_hierarchy(client, auth, first.id)

    cross_category = client.post(
        "/assets/types",
        json=type_payload(second.id, first_category["id"]),
        headers=auth,
    )
    cross_type = client.post(
        "/assets",
        json=asset_payload(second.id, first_type["id"]),
        headers=auth,
    )

    assert cross_category.status_code == 409
    assert cross_type.status_code == 409


def test_asset_api_rejects_direct_location_assignment_and_preserves_visibility(
    client,
    db,
    superuser,
    make_user,
    make_municipal_organization,
    grant_permissions,
):
    organization, municipality = make_municipal_organization()
    super_auth = headers_for(superuser)
    _, asset_type = create_hierarchy(client, super_auth, organization.id)
    asset = create_asset(client, super_auth, organization.id, asset_type["id"])
    owner = make_user()
    grant_permissions(
        owner,
        organization,
        ["map.view", "map.edit", "requirements.view"],
    )
    requirement = Requirement(
        organization_id=organization.id,
        title=f"Ubicacion privada {unique_suffix()}",
        priority="medium",
        status="draft",
        source_type="manual",
        created_by_id=owner.id,
    )
    db.add(requirement)
    db.commit()
    created_location = client.post(
        "/geo/entity-locations",
        json=entity_location_payload(
            requirement.id,
            organization.id,
            municipality.id,
        ),
        headers=headers_for(owner),
    )
    assert created_location.status_code == 201
    location_id = created_location.json()["location"]["id"]

    attacker = make_user()
    grant_permissions(
        attacker,
        organization,
        ["assets.view", "assets.create", "assets.edit", "map.view", "map.edit"],
    )
    attacker_auth = headers_for(attacker)
    hidden_map = client.get("/geo/map-items", headers=attacker_auth)
    direct_create = client.post(
        "/assets",
        json=asset_payload(
            organization.id,
            asset_type["id"],
            location_id=location_id,
        ),
        headers=attacker_auth,
    )
    direct_update = client.patch(
        f"/assets/{asset['id']}",
        json={"location_id": location_id},
        headers=attacker_auth,
    )

    assert hidden_map.status_code == 200
    assert hidden_map.json() == []
    for response in (direct_create, direct_update):
        assert response.status_code == 422
        assert any(
            error["type"] == "extra_forbidden"
            and error["loc"][-1] == "location_id"
            for error in response.json()["detail"]
        )
    db.expire_all()
    assert db.get(MunicipalAsset, asset["id"]).location_id is None


def test_organization_municipality_cannot_change_or_clear_while_assets_exist(
    client,
    db,
    superuser,
    make_municipal_organization,
):
    organization, municipality = make_municipal_organization()
    _, other_municipality = make_municipal_organization()
    auth = headers_for(superuser)
    _, asset_type = create_hierarchy(client, auth, organization.id)
    create_asset(client, auth, organization.id, asset_type["id"])

    change_response = client.patch(
        f"/organizations/{organization.id}",
        json={"municipality_id": other_municipality.id},
        headers=auth,
    )
    clear_response = client.patch(
        f"/organizations/{organization.id}",
        json={"municipality_id": None},
        headers=auth,
    )

    for response in (change_response, clear_response):
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "Organization municipality cannot change while assets exist"
        )
    db.expire_all()
    assert db.get(Organization, organization.id).municipality_id == municipality.id


def test_geo_api_uses_copy_on_write_for_location_referenced_by_asset(
    client,
    db,
    make_user,
    make_municipal_organization,
    grant_permissions,
):
    organization, municipality = make_municipal_organization()
    _, other_municipality = make_municipal_organization()
    user = make_user()
    grant_permissions(
        user,
        organization,
        ["assets.manage", "map.edit", "requirements.view"],
    )
    auth = headers_for(user)
    requirement = Requirement(
        organization_id=organization.id,
        title=f"Ubicar inventario {unique_suffix()}",
        priority="medium",
        status="draft",
        source_type="manual",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    created_location = client.post(
        "/geo/entity-locations",
        json=entity_location_payload(
            requirement.id,
            organization.id,
            municipality.id,
        ),
        headers=auth,
    )
    assert created_location.status_code == 201
    location_id = created_location.json()["location"]["id"]
    _, asset_type = create_hierarchy(client, auth, organization.id)
    asset = create_asset(client, auth, organization.id, asset_type["id"])
    stored_asset = db.get(MunicipalAsset, asset["id"])
    stored_asset.location_id = location_id
    db.commit()

    moved_to_other_municipality = client.post(
        "/geo/entity-locations",
        json=entity_location_payload(
            requirement.id,
            organization.id,
            other_municipality.id,
            label="Intento de reasignacion",
        ),
        headers=auth,
    )
    allowed_same_scope = client.post(
        "/geo/entity-locations",
        json=entity_location_payload(
            requirement.id,
            organization.id,
            municipality.id,
            label="Plaza Mayor actualizada",
            latitude=42.35,
        ),
        headers=auth,
    )

    assert moved_to_other_municipality.status_code == 201
    moved_location_id = moved_to_other_municipality.json()["location"]["id"]
    assert moved_location_id != location_id
    assert (
        moved_to_other_municipality.json()["location"]["municipality_id"]
        == other_municipality.id
    )
    assert allowed_same_scope.status_code == 201
    assert allowed_same_scope.json()["location"]["id"] not in {
        location_id,
        moved_location_id,
    }
    assert allowed_same_scope.json()["location"]["label"] == "Plaza Mayor actualizada"
    asset_detail = client.get(f"/assets/{asset['id']}", headers=auth)
    assert asset_detail.status_code == 200
    assert asset_detail.json()["location_id"] == location_id
    assert asset_detail.json()["location"]["municipality_id"] == municipality.id
    assert asset_detail.json()["location"]["label"] == "Plaza Mayor"
    assert asset_detail.json()["location"]["latitude"] == 42.34


def test_database_constraints_reject_incompatible_asset_scope(
    client,
    db,
    superuser,
    make_municipal_organization,
):
    organization, municipality = make_municipal_organization()
    other_organization, other_municipality = make_municipal_organization()
    auth = headers_for(superuser)
    _, asset_type = create_hierarchy(client, auth, organization.id)
    other_org_location = make_location(
        db,
        other_organization.id,
        other_municipality.id,
        label="Otra organizacion",
    )
    other_municipality_location = make_location(
        db,
        organization.id,
        other_municipality.id,
        label="Otro municipio",
    )

    def assert_direct_insert_rejected(**overrides):
        values = {
            "organization_id": organization.id,
            "municipality_id": municipality.id,
            "asset_type_id": asset_type["id"],
            "code": f"DIRECT-{unique_suffix()}",
            "name": f"Activo directo {unique_suffix()}",
            **overrides,
        }
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                db.execute(insert(MunicipalAsset).values(**values))
                db.execute(
                    text(
                        "SET CONSTRAINTS "
                        "fk_municipal_assets_location_tenant IMMEDIATE"
                    )
                )

    assert_direct_insert_rejected(
        organization_id=other_organization.id,
        municipality_id=other_municipality.id,
    )
    assert_direct_insert_rejected(municipality_id=other_municipality.id)
    assert_direct_insert_rejected(location_id=other_org_location.id)
    assert_direct_insert_rejected(location_id=other_municipality_location.id)


def test_deleting_location_keeps_asset_and_clears_location_id(
    client,
    db,
    superuser,
    municipal_organization,
):
    organization, municipality = municipal_organization
    auth = headers_for(superuser)
    _, asset_type = create_hierarchy(client, auth, organization.id)
    location = make_location(
        db,
        organization.id,
        municipality.id,
        label="Ubicacion eliminable",
    )
    asset = create_asset(client, auth, organization.id, asset_type["id"])
    stored_asset = db.get(MunicipalAsset, asset["id"])
    stored_asset.location_id = location.id
    db.commit()

    db.execute(delete(GeoLocation).where(GeoLocation.id == location.id))
    db.commit()
    db.expire_all()

    remaining = db.get(MunicipalAsset, asset["id"])
    assert remaining is not None
    assert remaining.location_id is None
    assert remaining.organization_id == organization.id
    assert remaining.municipality_id == municipality.id


def test_deleting_organization_cascades_complete_asset_hierarchy(
    client,
    db,
    superuser,
    municipal_organization,
):
    organization, municipality = municipal_organization
    auth = headers_for(superuser)
    category, asset_type = create_hierarchy(client, auth, organization.id)
    asset = create_asset(client, auth, organization.id, asset_type["id"])

    db.execute(delete(Organization).where(Organization.id == organization.id))
    db.commit()

    assert db.scalar(
        select(MunicipalAssetCategory.id).where(
            MunicipalAssetCategory.id == category["id"]
        )
    ) is None
    assert db.scalar(
        select(MunicipalAssetType.id).where(MunicipalAssetType.id == asset_type["id"])
    ) is None
    assert db.scalar(
        select(MunicipalAsset.id).where(MunicipalAsset.id == asset["id"])
    ) is None
    assert db.get(Municipality, municipality.id) is not None


def test_listing_is_tenant_scoped(
    client,
    make_user,
    make_municipal_organization,
    grant_permissions,
    add_member,
    superuser,
):
    first, _ = make_municipal_organization()
    second, _ = make_municipal_organization()
    super_auth = headers_for(superuser)
    _, first_type = create_hierarchy(client, super_auth, first.id)
    _, second_type = create_hierarchy(client, super_auth, second.id)
    first_asset = create_asset(client, super_auth, first.id, first_type["id"])
    second_asset = create_asset(client, super_auth, second.id, second_type["id"])

    viewer = make_user()
    grant_permissions(viewer, first, ["assets.view"])
    add_member(viewer, second)
    auth = headers_for(viewer)

    visible = client.get(
        "/assets", params={"organization_id": first.id}, headers=auth
    )
    denied = client.get(
        "/assets", params={"organization_id": second.id}, headers=auth
    )

    assert visible.status_code == 200
    assert {item["id"] for item in visible.json()} == {first_asset["id"]}
    assert {item["organization_id"] for item in visible.json()} == {first.id}
    assert second_asset["id"] not in {item["id"] for item in visible.json()}
    assert denied.status_code == 403


def test_paused_organization_allows_reads_but_rejects_writes(
    client,
    db,
    make_user,
    municipal_organization,
    grant_permissions,
):
    organization, _ = municipal_organization
    manager = make_user()
    grant_permissions(manager, organization, ["assets.manage"])
    auth = headers_for(manager)
    category, asset_type = create_hierarchy(client, auth, organization.id)
    asset = create_asset(client, auth, organization.id, asset_type["id"])

    organization.status = "paused"
    db.commit()

    for path in (
        "/assets/categories",
        "/assets/types",
        "/assets",
    ):
        response = client.get(
            path,
            params={"organization_id": organization.id},
            headers=auth,
        )
        assert response.status_code == 200, response.text
    assert client.get(f"/assets/{asset['id']}", headers=auth).status_code == 200

    create_response = client.post(
        "/assets/categories",
        json=category_payload(organization.id),
        headers=auth,
    )
    update_response = client.patch(
        f"/assets/{asset['id']}",
        json={"name": "No se debe modificar"},
        headers=auth,
    )

    assert create_response.status_code == 409
    assert update_response.status_code == 409
    assert category["organization_id"] == organization.id


def test_asset_filters_total_and_pagination(
    client,
    make_user,
    municipal_organization,
    grant_permissions,
):
    organization, _ = municipal_organization
    manager = make_user()
    grant_permissions(manager, organization, ["assets.manage"])
    auth = headers_for(manager)
    first_category = create_category(
        client, auth, organization.id, code=f"lighting-{unique_suffix()}"
    )
    first_type = create_type(
        client,
        auth,
        organization.id,
        first_category["id"],
        code=f"streetlight-{unique_suffix()}",
    )
    second_category = create_category(
        client, auth, organization.id, code=f"furniture-{unique_suffix()}"
    )
    second_type = create_type(
        client,
        auth,
        organization.id,
        second_category["id"],
        code=f"bench-{unique_suffix()}",
    )
    first = create_asset(
        client,
        auth,
        organization.id,
        first_type["id"],
        name="Farola Norte",
        code=f"LIGHT-{unique_suffix()}",
        condition_status="good",
    )
    second = create_asset(
        client,
        auth,
        organization.id,
        second_type["id"],
        name="Banco Sur",
        code=f"BENCH-{unique_suffix()}",
        status="inactive",
        condition_status="fair",
    )
    archived = create_asset(
        client,
        auth,
        organization.id,
        first_type["id"],
        name="Farola Retirada",
        code=f"OLD-{unique_suffix()}",
        status="archived",
        condition_status="poor",
    )

    def filtered_ids(**params):
        response = client.get(
            "/assets",
            params={"organization_id": organization.id, **params},
            headers=auth,
        )
        assert response.status_code == 200, response.text
        return response, {item["id"] for item in response.json()}

    _, q_ids = filtered_ids(q="Farola Norte")
    _, status_ids = filtered_ids(status="inactive")
    _, condition_ids = filtered_ids(condition_status="good")
    _, type_ids = filtered_ids(asset_type_id=first_type["id"])
    _, category_ids = filtered_ids(category_id=second_category["id"])
    all_response, all_ids = filtered_ids(include_archived="true")

    assert q_ids == {first["id"]}
    assert status_ids == {second["id"]}
    assert condition_ids == {first["id"]}
    assert type_ids == {first["id"]}
    assert category_ids == {second["id"]}
    assert all_ids == {first["id"], second["id"], archived["id"]}
    assert all_response.headers["X-Total-Count"] == "3"

    first_page, first_page_ids = filtered_ids(limit=1, offset=0)
    second_page, second_page_ids = filtered_ids(limit=1, offset=1)
    assert first_page.headers["X-Total-Count"] == "2"
    assert second_page.headers["X-Total-Count"] == "2"
    assert len(first_page_ids) == len(second_page_ids) == 1
    assert first_page_ids.isdisjoint(second_page_ids)


def test_duplicate_codes_return_409_within_an_organization(
    client,
    superuser,
    municipal_organization,
):
    organization, _ = municipal_organization
    auth = headers_for(superuser)
    category_code = f"roads-{unique_suffix()}"
    category = create_category(
        client, auth, organization.id, code=category_code
    )
    duplicate_category = client.post(
        "/assets/categories",
        json=category_payload(organization.id, code=category_code),
        headers=auth,
    )

    type_code = f"sign-{unique_suffix()}"
    asset_type = create_type(
        client,
        auth,
        organization.id,
        category["id"],
        code=type_code,
    )
    duplicate_type = client.post(
        "/assets/types",
        json=type_payload(organization.id, category["id"], code=type_code),
        headers=auth,
    )

    asset_code = f"SIGN-{unique_suffix()}"
    create_asset(
        client,
        auth,
        organization.id,
        asset_type["id"],
        code=asset_code,
    )
    duplicate_asset = client.post(
        "/assets",
        json=asset_payload(
            organization.id,
            asset_type["id"],
            code=asset_code,
        ),
        headers=auth,
    )

    assert duplicate_category.status_code == 409
    assert duplicate_type.status_code == 409
    assert duplicate_asset.status_code == 409
