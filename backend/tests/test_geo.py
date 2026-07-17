import json

import pytest
from conftest import headers_for, unique_suffix
from sqlalchemy import insert, select

from app.assets.models import (
    MunicipalAsset,
    MunicipalAssetCategory,
    MunicipalAssetType,
)
from app.geo.geometry import build_point_geojson
from app.geo.models import EntityLocation, GeoLocation
from app.municipalities.models import Municipality
from app.projects.models import Project, project_users
from app.requirements.models import Requirement


def make_requirement(db, organization, user, title: str = "Farola rota") -> Requirement:
    requirement = Requirement(
        organization_id=organization.id,
        title=f"{title} {unique_suffix()}",
        priority="medium",
        status="draft",
        source_type="manual",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    return requirement


def make_project(db, organization, name: str = "Plan de aceras") -> Project:
    project = Project(
        organization_id=organization.id,
        name=f"{name} {unique_suffix()}",
        description="Mejoras de accesibilidad",
        status="active",
    )
    db.add(project)
    db.commit()
    return project


def location_payload(entity_type: str, entity_id: int, *, latitude: float = 42.34, longitude: float = -3.70):
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "role": "primary",
        "location": {
            "label": "Plaza Mayor",
            "latitude": latitude,
            "longitude": longitude,
            "address_text": "Plaza Mayor, 1",
        },
    }


def test_build_point_geojson_uses_lon_lat_order():
    assert json.loads(build_point_geojson(42.34, -3.70)) == {
        "type": "Point",
        "coordinates": [-3.70, 42.34],
    }


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(91, -3.7), (-91, -3.7), (42.34, 181), (42.34, -181)],
)
def test_build_point_geojson_rejects_invalid_coordinates(latitude, longitude):
    with pytest.raises(Exception):
        build_point_geojson(latitude, longitude)


def test_map_items_requires_map_view(client, db, make_user, make_organization):
    user = make_user()
    creator = make_user()
    organization = make_organization()
    requirement = make_requirement(db, organization, creator)

    response = client.get("/geo/map-items", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: map.view"
    assert requirement.id


def test_create_requirement_location_then_map_items_returns_visible_marker(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(
        user,
        organization,
        ["map.view", "map.edit", "requirements.view"],
    )
    requirement = make_requirement(db, organization, user)

    create_response = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=headers_for(user),
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["entity_type"] == "requirement"
    assert created["entity_id"] == requirement.id
    assert created["role"] == "primary"
    assert created["layer_key"] == "requirements"
    assert created["layer_label"] == "Necesidades"
    assert created["layer_color"] == "#c0603a"
    assert created["item_type"] is None
    assert created["condition_status"] is None
    assert created["detail_path"] == f"/requisitos?id={requirement.id}"
    assert json.loads(created["location"]["geometry_json"])["coordinates"] == [-3.7, 42.34]

    map_response = client.get("/geo/map-items", headers=headers_for(user))

    assert map_response.status_code == 200
    items = map_response.json()
    assert len(items) == 1
    assert items[0]["role"] == "primary"
    assert items[0]["layer_key"] == "requirements"
    assert items[0]["layer_label"] == "Necesidades"
    assert items[0]["layer_color"] == "#c0603a"
    assert items[0]["title"] == requirement.title
    assert items[0]["location"]["latitude"] == 42.34
    assert items[0]["location"]["longitude"] == -3.7


def test_map_items_hides_requirement_without_entity_access(
    client, db, make_user, make_organization, grant_permissions
):
    viewer = make_user()
    creator = make_user()
    organization = make_organization()
    grant_permissions(viewer, organization, ["map.view"])
    grant_permissions(creator, organization, ["map.view", "map.edit", "requirements.view"])
    requirement = make_requirement(db, organization, creator)
    assert client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=headers_for(creator),
    ).status_code == 201

    response = client.get("/geo/map-items", headers=headers_for(viewer))

    assert response.status_code == 200
    assert response.json() == []


def test_map_items_requires_map_view_in_entity_organization(
    client, db, make_user, make_organization, grant_permissions
):
    viewer = make_user()
    creator = make_user()
    map_only_organization = make_organization()
    entity_organization = make_organization()
    grant_permissions(viewer, map_only_organization, ["map.view"])
    grant_permissions(viewer, entity_organization, ["requirements.view"])
    grant_permissions(
        creator,
        entity_organization,
        ["map.view", "map.edit", "requirements.view"],
    )
    requirement = make_requirement(db, entity_organization, creator)
    assert client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=headers_for(creator),
    ).status_code == 201

    response = client.get("/geo/map-items", headers=headers_for(viewer))

    assert response.status_code == 200
    assert response.json() == []


def test_create_requirement_location_requires_map_edit(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["map.view", "requirements.view"])
    requirement = make_requirement(db, organization, user)

    response = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: map.edit"


def test_create_requirement_location_rejects_invalid_latitude(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["map.edit", "requirements.view"])
    requirement = make_requirement(db, organization, user)

    response = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id, latitude=99),
        headers=headers_for(user),
    )

    assert response.status_code == 422


def test_create_requirement_location_denies_inaccessible_entity(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    creator = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["map.edit"])
    requirement = make_requirement(db, organization, creator)

    response = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Entity access denied"


def test_create_primary_location_upserts_entity_role(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["map.view", "map.edit", "requirements.view"])
    requirement = make_requirement(db, organization, user)
    auth = headers_for(user)

    first = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id, latitude=42.1, longitude=-3.1),
        headers=auth,
    )
    second = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id, latitude=42.2, longitude=-3.2),
        headers=auth,
    )
    reference_payload = location_payload(
        "requirement",
        requirement.id,
        latitude=42.3,
        longitude=-3.3,
    )
    reference_payload["role"] = "reference"
    reference = client.post(
        "/geo/entity-locations",
        json=reference_payload,
        headers=auth,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert reference.status_code == 201
    attachments = list(
        db.scalars(
            select(EntityLocation).where(
                EntityLocation.entity_type == "requirement",
                EntityLocation.entity_id == requirement.id,
                EntityLocation.role == "primary",
            )
        )
    )
    assert len(attachments) == 1
    reference_attachment = db.scalar(
        select(EntityLocation).where(
            EntityLocation.entity_type == "requirement",
            EntityLocation.entity_id == requirement.id,
            EntityLocation.role == "reference",
        )
    )
    assert reference_attachment is not None
    reference_attachment.location_id = attachments[0].location_id
    db.commit()
    assert second.json()["location"]["latitude"] == 42.2
    map_items = client.get("/geo/map-items", headers=auth).json()
    assert {item["role"] for item in map_items} == {"primary", "reference"}
    assert {
        item["location"]["longitude"]
        for item in map_items
        if item["role"] == "primary"
    } == {-3.2}
    limited = client.get(
        "/geo/map-items",
        params={
            "entity_type": "requirement",
            "entity_id": requirement.id,
            "limit": 1,
        },
        headers=auth,
    )
    assert limited.status_code == 200
    assert limited.json()[0]["role"] == "primary"


def test_project_marker_respects_project_access(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    creator = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["map.view"])
    grant_permissions(creator, organization, ["map.view", "map.edit", "projects.view_all"])
    project = make_project(db, organization)
    assert client.post(
        "/geo/entity-locations",
        json=location_payload("project", project.id),
        headers=headers_for(creator),
    ).status_code == 201

    hidden_response = client.get("/geo/map-items", headers=headers_for(user))
    assert hidden_response.status_code == 200
    assert hidden_response.json() == []

    db.execute(insert(project_users).values(project_id=project.id, user_id=user.id))
    db.commit()
    visible_response = client.get("/geo/map-items", headers=headers_for(user))

    assert visible_response.status_code == 200
    assert visible_response.json()[0]["entity_type"] == "project"
    assert visible_response.json()[0]["detail_path"] == "/proyectos"


def test_map_items_entity_type_filter(
    client, db, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(
        user,
        organization,
        ["map.view", "map.edit", "requirements.view", "projects.view_all"],
    )
    requirement = make_requirement(db, organization, user)
    project = make_project(db, organization)
    auth = headers_for(user)
    assert client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=auth,
    ).status_code == 201
    create_project = client.post(
        "/geo/entity-locations",
        json=location_payload("project", project.id, latitude=42.4),
        headers=auth,
    )
    assert create_project.status_code == 201
    assert create_project.json()["layer_key"] == "projects"
    assert create_project.json()["layer_label"] == "Proyectos"
    assert create_project.json()["layer_color"] == "#2f74d0"
    assert create_project.json()["item_type"] is None
    assert create_project.json()["condition_status"] is None

    response = client.get("/geo/map-items?entity_type=project", headers=auth)

    assert response.status_code == 200
    assert [item["entity_type"] for item in response.json()] == ["project"]
    assert response.json()[0]["layer_key"] == "projects"
    assert response.json()[0]["layer_label"] == "Proyectos"
    assert response.json()[0]["layer_color"] == "#2f74d0"


def test_superuser_can_view_map_items(client, db, superuser, make_user, make_organization, grant_permissions):
    creator = make_user()
    organization = make_organization()
    grant_permissions(creator, organization, ["map.edit", "requirements.view"])
    requirement = make_requirement(db, organization, creator)
    assert client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=headers_for(creator),
    ).status_code == 201

    response = client.get("/geo/map-items", headers=headers_for(superuser))

    assert response.status_code == 200
    assert response.json()[0]["entity_id"] == requirement.id


def make_asset_context(
    db,
    make_organization,
    *,
    organization_status="active",
    municipality_status="active",
    asset_status="active",
    condition_status="good",
    category_color="#d97706",
    description=None,
    with_location=True,
):
    suffix = unique_suffix()
    municipality = Municipality(
        name=f"Municipio {suffix}",
        province="Burgos",
        autonomous_community="Castilla y Leon",
        ine_code=f"geo-{suffix}",
        status=municipality_status,
    )
    db.add(municipality)
    db.commit()
    organization = make_organization(
        name=f"Ayuntamiento {suffix}",
        municipality_id=municipality.id,
        status=organization_status,
    )
    category = MunicipalAssetCategory(
        organization_id=organization.id,
        code=f"lighting-{suffix}",
        name="Alumbrado",
        color=category_color,
    )
    db.add(category)
    db.flush()
    asset_type = MunicipalAssetType(
        organization_id=organization.id,
        category_id=category.id,
        code=f"streetlight-{suffix}",
        name="Farola",
    )
    db.add(asset_type)
    db.flush()
    location = None
    if with_location:
        location = make_asset_geo_location(
            db,
            organization_id=organization.id,
            municipality_id=municipality.id,
            label=f"Ubicacion {suffix}",
        )
    asset = MunicipalAsset(
        organization_id=organization.id,
        municipality_id=municipality.id,
        asset_type_id=asset_type.id,
        location_id=None if location is None else location.id,
        code=f"ASSET-{suffix}",
        name=f"Farola {suffix}",
        description=description,
        status=asset_status,
        condition_status=condition_status,
    )
    db.add(asset)
    db.commit()
    return {
        "municipality": municipality,
        "organization": organization,
        "category": category,
        "asset_type": asset_type,
        "location": location,
        "asset": asset,
    }


def make_asset_geo_location(
    db,
    *,
    organization_id,
    municipality_id,
    label,
    review_status="proposed",
    latitude=42.34,
    longitude=-3.70,
):
    location = GeoLocation(
        organization_id=organization_id,
        municipality_id=municipality_id,
        label=label,
        geometry_type="point",
        geometry_json=build_point_geojson(latitude, longitude),
        latitude=latitude,
        longitude=longitude,
        source="user_provided",
        review_status=review_status,
    )
    db.add(location)
    db.flush()
    return location


def add_context_asset(
    db,
    context,
    *,
    status="active",
    condition_status="unknown",
    location=None,
    name=None,
):
    suffix = unique_suffix()
    if location is None:
        location = make_asset_geo_location(
            db,
            organization_id=context["organization"].id,
            municipality_id=context["municipality"].id,
            label=f"Ubicacion {suffix}",
        )
    asset = MunicipalAsset(
        organization_id=context["organization"].id,
        municipality_id=context["municipality"].id,
        asset_type_id=context["asset_type"].id,
        location_id=location.id,
        code=f"ASSET-{suffix}",
        name=name or f"Activo {suffix}",
        status=status,
        condition_status=condition_status,
    )
    db.add(asset)
    db.commit()
    return asset


def asset_location_payload(
    asset_id,
    *,
    role="primary",
    organization_id=None,
    municipality_id=None,
    label="Farola geolocalizada",
    latitude=42.35,
    longitude=-3.71,
    source="manual_review",
    review_status="reviewed",
):
    location = {
        "label": label,
        "latitude": latitude,
        "longitude": longitude,
        "source": source,
        "review_status": review_status,
    }
    if organization_id is not None:
        location["organization_id"] = organization_id
    if municipality_id is not None:
        location["municipality_id"] = municipality_id
    return {
        "entity_type": "asset",
        "entity_id": asset_id,
        "role": role,
        "location": location,
    }


def test_asset_map_items_require_both_permissions_and_are_tenant_scoped(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    visible_context = make_asset_context(
        db,
        make_organization,
        description=None,
        condition_status="fair",
    )
    hidden_context = make_asset_context(db, make_organization)
    viewer = make_user()
    grant_permissions(
        viewer,
        visible_context["organization"],
        ["map.view", "assets.view"],
    )
    grant_permissions(
        viewer,
        hidden_context["organization"],
        ["map.view"],
    )

    response = client.get(
        "/geo/map-items",
        params={"entity_type": "asset"},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    item = response.json()[0]
    assert item["entity_id"] == visible_context["asset"].id
    assert item["entity_type"] == "asset"
    assert item["layer_key"] == (
        f"asset-category-{visible_context['category'].id}"
    )
    assert item["layer_label"] == "Alumbrado"
    assert item["layer_color"] == "#d97706"
    assert item["item_type"] == "Farola"
    assert item["condition_status"] == "fair"
    assert item["title"] == visible_context["asset"].name
    assert item["subtitle"] == "Farola · fair"
    assert item["priority"] is None
    assert item["detail_path"] == "/ayuntamiento"


def test_asset_map_uses_fallback_color_for_category_without_color(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    context = make_asset_context(
        db,
        make_organization,
        category_color=None,
    )
    viewer = make_user()
    grant_permissions(
        viewer,
        context["organization"],
        ["map.view", "assets.view"],
    )

    response = client.get(
        "/geo/map-items",
        params={"entity_type": "asset"},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert response.json()[0]["layer_color"] == "#3caf8c"


def test_asset_map_filters_archives_and_global_limit(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    context = make_asset_context(db, make_organization)
    inactive = add_context_asset(
        db,
        context,
        status="inactive",
        condition_status="poor",
        name="Farola inactiva",
    )
    archived = add_context_asset(
        db,
        context,
        status="archived",
        name="Farola archivada",
    )
    viewer = make_user()
    grant_permissions(
        viewer,
        context["organization"],
        ["map.view", "assets.view"],
    )
    auth = headers_for(viewer)

    by_id = client.get(
        "/geo/map-items",
        params={"entity_type": "asset", "entity_id": inactive.id},
        headers=auth,
    )
    by_status = client.get(
        "/geo/map-items",
        params={"entity_type": "asset", "status": "inactive"},
        headers=auth,
    )
    default_items = client.get(
        "/geo/map-items",
        params={"entity_type": "asset"},
        headers=auth,
    )
    with_archived = client.get(
        "/geo/map-items",
        params={"entity_type": "asset", "include_archived": True},
        headers=auth,
    )
    limited = client.get(
        "/geo/map-items",
        params={"entity_type": "asset", "include_archived": True, "limit": 1},
        headers=auth,
    )
    second_page = client.get(
        "/geo/map-items",
        params={
            "entity_type": "asset",
            "include_archived": True,
            "limit": 1,
            "offset": 1,
        },
        headers=auth,
    )
    invalid_offset = client.get("/geo/map-items?offset=-1", headers=auth)
    wrong_organization = client.get(
        "/geo/map-items",
        params={"entity_type": "asset", "organization_id": 999999},
        headers=auth,
    )
    missing_type = client.get(
        "/geo/map-items",
        params={"entity_id": inactive.id},
        headers=auth,
    )

    assert {item["entity_id"] for item in by_id.json()} == {inactive.id}
    assert {item["entity_id"] for item in by_status.json()} == {inactive.id}
    assert archived.id not in {
        item["entity_id"] for item in default_items.json()
    }
    assert archived.id in {
        item["entity_id"] for item in with_archived.json()
    }
    assert len(limited.json()) == 1
    assert len(second_page.json()) == 1
    assert second_page.json()[0]["entity_id"] != limited.json()[0]["entity_id"]
    assert invalid_offset.status_code == 422
    assert wrong_organization.status_code == 200
    assert wrong_organization.json() == []
    assert missing_type.status_code == 422


def test_map_limit_is_global_and_orders_newest_location_across_domains(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    viewer = make_user()
    requirement_organization = make_organization()
    grant_permissions(
        viewer,
        requirement_organization,
        ["map.view", "map.edit", "requirements.view"],
    )
    requirement = make_requirement(db, requirement_organization, viewer)
    created_requirement = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=headers_for(viewer),
    )
    assert created_requirement.status_code == 201

    asset_context = make_asset_context(db, make_organization)
    grant_permissions(
        viewer,
        asset_context["organization"],
        ["map.view", "assets.view"],
    )

    response = client.get(
        "/geo/map-items",
        params={"limit": 1},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["entity_type"] == "asset"
    assert response.json()[0]["entity_id"] == asset_context["asset"].id


def test_asset_map_hides_rejected_locations_and_archived_organizations(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    rejected_context = make_asset_context(db, make_organization)
    rejected_context["location"].review_status = "rejected"
    archived_context = make_asset_context(
        db,
        make_organization,
        organization_status="archived",
    )
    db.commit()
    viewer = make_user()
    for context in (rejected_context, archived_context):
        grant_permissions(
            viewer,
            context["organization"],
            ["map.view", "assets.view"],
        )

    response = client.get(
        "/geo/map-items",
        params={"entity_type": "asset", "include_archived": True},
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    assert response.json() == []


def test_asset_location_requires_map_and_asset_edit_intersection(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    context = make_asset_context(db, make_organization, with_location=False)
    payload = asset_location_payload(context["asset"].id)

    view_only = make_user()
    grant_permissions(
        view_only,
        context["organization"],
        ["map.edit", "assets.view"],
    )
    edit_only = make_user()
    grant_permissions(
        edit_only,
        context["organization"],
        ["map.edit", "assets.edit"],
    )
    no_map_edit = make_user()
    grant_permissions(
        no_map_edit,
        context["organization"],
        ["assets.view", "assets.edit"],
    )

    assert client.post(
        "/geo/entity-locations",
        json=payload,
        headers=headers_for(view_only),
    ).status_code == 403
    assert client.post(
        "/geo/entity-locations",
        json=payload,
        headers=headers_for(edit_only),
    ).status_code == 403
    no_map_response = client.post(
        "/geo/entity-locations",
        json=payload,
        headers=headers_for(no_map_edit),
    )
    assert no_map_response.status_code == 403
    assert no_map_response.json()["detail"] == "Permission required: map.edit"


def test_asset_location_is_copy_on_write_and_never_creates_entity_location(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    context = make_asset_context(db, make_organization, with_location=False)
    manager = make_user()
    grant_permissions(
        manager,
        context["organization"],
        ["map.manage", "assets.manage"],
    )
    auth = headers_for(manager)

    first = client.post(
        "/geo/entity-locations",
        json=asset_location_payload(
            context["asset"].id,
            organization_id=context["organization"].id,
            municipality_id=context["municipality"].id,
            label="Ubicacion original",
        ),
        headers=auth,
    )
    assert first.status_code == 201
    assert first.json()["layer_key"] == (
        f"asset-category-{context['category'].id}"
    )
    assert first.json()["layer_label"] == "Alumbrado"
    assert first.json()["layer_color"] == "#d97706"
    assert first.json()["item_type"] == "Farola"
    assert first.json()["condition_status"] == "good"
    first_location_id = first.json()["location"]["id"]
    shared_asset = add_context_asset(
        db,
        context,
        location=db.get(GeoLocation, first_location_id),
        name="Activo que comparte ubicacion",
    )

    second = client.post(
        "/geo/entity-locations",
        json=asset_location_payload(
            context["asset"].id,
            label="Ubicacion nueva",
            latitude=42.40,
            longitude=-3.80,
        ),
        headers=auth,
    )

    assert second.status_code == 201
    assert second.json()["location"]["id"] != first_location_id
    assert second.json()["location"]["source"] == "user_provided"
    assert second.json()["location"]["review_status"] == "proposed"
    db.expire_all()
    moved_asset = db.get(MunicipalAsset, context["asset"].id)
    untouched_asset = db.get(MunicipalAsset, shared_asset.id)
    old_location = db.get(GeoLocation, first_location_id)
    assert moved_asset.location_id == second.json()["location"]["id"]
    assert moved_asset.updated_by_id == manager.id
    assert untouched_asset.location_id == first_location_id
    assert old_location.label == "Ubicacion original"
    assert db.scalar(
        select(EntityLocation.id).where(
            EntityLocation.entity_type == "asset",
            EntityLocation.entity_id == moved_asset.id,
        )
    ) is None


def test_asset_location_rejects_wrong_scope_role_and_paused_writes(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    context = make_asset_context(db, make_organization)
    other = make_asset_context(db, make_organization)
    manager = make_user()
    grant_permissions(
        manager,
        context["organization"],
        ["map.manage", "assets.manage"],
    )
    auth = headers_for(manager)

    wrong_org = client.post(
        "/geo/entity-locations",
        json=asset_location_payload(
            context["asset"].id,
            organization_id=other["organization"].id,
        ),
        headers=auth,
    )
    wrong_municipality = client.post(
        "/geo/entity-locations",
        json=asset_location_payload(
            context["asset"].id,
            municipality_id=other["municipality"].id,
        ),
        headers=auth,
    )
    wrong_role = client.post(
        "/geo/entity-locations",
        json=asset_location_payload(context["asset"].id, role="reference"),
        headers=auth,
    )

    context["organization"].status = "paused"
    db.commit()
    paused_read = client.get(
        "/geo/map-items",
        params={"entity_type": "asset"},
        headers=auth,
    )
    paused_write = client.post(
        "/geo/entity-locations",
        json=asset_location_payload(context["asset"].id),
        headers=auth,
    )

    assert wrong_org.status_code == 409
    assert wrong_municipality.status_code == 409
    assert wrong_role.status_code == 422
    assert paused_read.status_code == 200
    assert {item["entity_id"] for item in paused_read.json()} == {
        context["asset"].id
    }
    assert paused_write.status_code == 409


def test_requirement_location_update_does_not_move_shared_asset(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    context = make_asset_context(db, make_organization, with_location=False)
    user = make_user()
    grant_permissions(
        user,
        context["organization"],
        ["map.view", "map.edit", "requirements.view"],
    )
    requirement = make_requirement(db, context["organization"], user)
    auth = headers_for(user)
    first = client.post(
        "/geo/entity-locations",
        json=location_payload("requirement", requirement.id),
        headers=auth,
    )
    assert first.status_code == 201
    first_location_id = first.json()["location"]["id"]
    context["asset"].location_id = first_location_id
    db.commit()

    updated = client.post(
        "/geo/entity-locations",
        json=location_payload(
            "requirement",
            requirement.id,
            latitude=42.50,
            longitude=-3.90,
        ),
        headers=auth,
    )

    assert updated.status_code == 201
    assert updated.json()["location"]["id"] != first_location_id
    db.expire_all()
    assert db.get(MunicipalAsset, context["asset"].id).location_id == first_location_id
    assert db.get(GeoLocation, first_location_id).latitude == 42.34
