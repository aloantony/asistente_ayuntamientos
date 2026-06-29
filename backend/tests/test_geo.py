import json

import pytest
from conftest import headers_for, unique_suffix
from sqlalchemy import insert, select

from app.geo.geometry import build_point_geojson
from app.geo.models import EntityLocation
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
    assert created["detail_path"] == f"/requisitos?id={requirement.id}"
    assert json.loads(created["location"]["geometry_json"])["coordinates"] == [-3.7, 42.34]

    map_response = client.get("/geo/map-items", headers=headers_for(user))

    assert map_response.status_code == 200
    items = map_response.json()
    assert len(items) == 1
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

    assert first.status_code == 201
    assert second.status_code == 201
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
    assert second.json()["location"]["latitude"] == 42.2
    assert client.get("/geo/map-items", headers=auth).json()[0]["location"]["longitude"] == -3.2


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
    assert client.post(
        "/geo/entity-locations",
        json=location_payload("project", project.id, latitude=42.4),
        headers=auth,
    ).status_code == 201

    response = client.get("/geo/map-items?entity_type=project", headers=auth)

    assert response.status_code == 200
    assert [item["entity_type"] for item in response.json()] == ["project"]


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
