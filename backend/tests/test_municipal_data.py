import pytest
from conftest import headers_for, unique_suffix

from app.municipalities.models import Municipality


@pytest.fixture
def make_municipal_organization(db, make_organization):
    def _make(*, with_municipality=True):
        suffix = unique_suffix()
        municipality_id = None
        if with_municipality:
            municipality = Municipality(
                name=f"Municipio {suffix}",
                province="Burgos",
                autonomous_community="Castilla y Leon",
                ine_code=f"data-{suffix}",
            )
            db.add(municipality)
            db.commit()
            municipality_id = municipality.id
        return make_organization(
            name=f"Ayuntamiento {suffix}",
            municipality_id=municipality_id,
        )

    return _make


@pytest.fixture
def data_organization(make_municipal_organization):
    return make_municipal_organization()


def padron_payload(organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "reference_year": 2026,
        "population": 320,
        "men": 165,
        "women": 155,
    }
    payload.update(overrides)
    return payload


def supply_payload(organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "name": f"Sondeo {unique_suffix()}",
        "origin": "Acuífero de la vega",
        "treatment": "chlorination",
    }
    payload.update(overrides)
    return payload


def meter_payload(organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "code": f"CT-{unique_suffix()}",
        "address": "Calle Mayor 1",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "path",
    [
        "/municipal-data/padron?organization_id=1",
        "/municipal-data/climate?organization_id=1",
        "/municipal-data/households?organization_id=1",
        "/municipal-data/water/supplies?organization_id=1",
        "/municipal-data/water/meters?organization_id=1",
    ],
)
def test_municipal_data_requires_authentication(client, path):
    response = client.get(path)

    assert response.status_code == 401


def test_permissions_are_scoped_to_the_target_organization(
    client,
    make_user,
    make_municipal_organization,
    grant_permissions,
    add_member,
):
    target = make_municipal_organization()
    other = make_municipal_organization()

    user = make_user()
    grant_permissions(user, other, ["municipal_data.manage"])
    add_member(user, target)
    auth = headers_for(user)

    listed = client.get(
        "/municipal-data/padron",
        params={"organization_id": target.id},
        headers=auth,
    )
    created = client.post(
        "/municipal-data/padron",
        json=padron_payload(target.id),
        headers=auth,
    )

    assert listed.status_code == 403
    assert listed.json()["detail"] == "Permission required: municipal_data.view"
    assert created.status_code == 403
    assert created.json()["detail"] == "Permission required: municipal_data.edit"


def test_viewer_reads_the_series_but_cannot_write_it(
    client,
    make_user,
    data_organization,
    grant_permissions,
    superuser,
):
    client.post(
        "/municipal-data/padron",
        json=padron_payload(data_organization.id),
        headers=headers_for(superuser),
    )

    viewer = make_user()
    grant_permissions(viewer, data_organization, ["municipal_data.view"])
    auth = headers_for(viewer)

    listed = client.get(
        "/municipal-data/padron",
        params={"organization_id": data_organization.id},
        headers=auth,
    )
    denied = client.post(
        "/municipal-data/padron",
        json=padron_payload(data_organization.id, reference_year=2025),
        headers=auth,
    )

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert denied.status_code == 403


def test_padron_series_is_unique_per_year_and_ordered_backwards(
    client,
    make_user,
    data_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(
        editor,
        data_organization,
        ["municipal_data.view", "municipal_data.edit"],
    )
    auth = headers_for(editor)

    for year in (2024, 2026, 2025):
        response = client.post(
            "/municipal-data/padron",
            json=padron_payload(data_organization.id, reference_year=year),
            headers=auth,
        )
        assert response.status_code == 201, response.text

    duplicate = client.post(
        "/municipal-data/padron",
        json=padron_payload(data_organization.id, reference_year=2026),
        headers=auth,
    )
    listed = client.get(
        "/municipal-data/padron",
        params={"organization_id": data_organization.id},
        headers=auth,
    )

    assert duplicate.status_code == 409
    assert [item["reference_year"] for item in listed.json()] == [2026, 2025, 2024]


def test_padron_breakdown_cannot_exceed_the_total(
    client,
    make_user,
    data_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, data_organization, ["municipal_data.edit"])

    response = client.post(
        "/municipal-data/padron",
        json=padron_payload(
            data_organization.id,
            population=100,
            men=60,
            women=60,
        ),
        headers=headers_for(editor),
    )

    assert response.status_code == 422


def test_climate_holds_annual_and_monthly_rows_for_the_same_year(
    client,
    make_user,
    data_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(
        editor,
        data_organization,
        ["municipal_data.view", "municipal_data.edit"],
    )
    auth = headers_for(editor)

    annual = client.post(
        "/municipal-data/climate",
        json={
            "organization_id": data_organization.id,
            "reference_year": 2026,
            "avg_temperature_c": "12.40",
            "precipitation_mm": "480.00",
        },
        headers=auth,
    )
    monthly = client.post(
        "/municipal-data/climate",
        json={
            "organization_id": data_organization.id,
            "reference_year": 2026,
            "reference_month": 7,
            "avg_temperature_c": "22.10",
            "min_temperature_c": "11.00",
            "max_temperature_c": "33.50",
        },
        headers=auth,
    )
    inverted = client.post(
        "/municipal-data/climate",
        json={
            "organization_id": data_organization.id,
            "reference_year": 2025,
            "min_temperature_c": "30.00",
            "max_temperature_c": "10.00",
        },
        headers=auth,
    )
    duplicate_month = client.post(
        "/municipal-data/climate",
        json={
            "organization_id": data_organization.id,
            "reference_year": 2026,
            "reference_month": 7,
        },
        headers=auth,
    )

    assert annual.status_code == 201
    assert annual.json()["reference_month"] is None
    assert monthly.status_code == 201
    assert inverted.status_code == 422
    assert duplicate_month.status_code == 409

    only_2026 = client.get(
        "/municipal-data/climate",
        params={"organization_id": data_organization.id, "reference_year": 2026},
        headers=auth,
    )
    assert len(only_2026.json()) == 2


def test_water_meters_need_a_municipality_and_a_unique_code(
    client,
    make_user,
    make_municipal_organization,
    grant_permissions,
):
    organization = make_municipal_organization()
    without_municipality = make_municipal_organization(with_municipality=False)

    editor = make_user()
    grant_permissions(
        editor,
        organization,
        ["municipal_data.view", "municipal_data.edit"],
    )
    grant_permissions(
        editor,
        without_municipality,
        ["municipal_data.view", "municipal_data.edit"],
    )
    auth = headers_for(editor)

    payload = meter_payload(organization.id)
    created = client.post("/municipal-data/water/meters", json=payload, headers=auth)
    duplicate = client.post("/municipal-data/water/meters", json=payload, headers=auth)
    orphan = client.post(
        "/municipal-data/water/meters",
        json=meter_payload(without_municipality.id),
        headers=auth,
    )

    assert created.status_code == 201
    assert created.json()["municipality_id"] == organization.municipality_id
    assert duplicate.status_code == 409
    assert orphan.status_code == 409
    assert (
        orphan.json()["detail"]
        == "Organization must have a municipality to register water meters"
    )


def test_a_meter_cannot_hang_from_another_organizations_supply(
    client,
    make_user,
    make_municipal_organization,
    grant_permissions,
    superuser,
):
    target = make_municipal_organization()
    other = make_municipal_organization()
    foreign_supply = client.post(
        "/municipal-data/water/supplies",
        json=supply_payload(other.id),
        headers=headers_for(superuser),
    ).json()

    editor = make_user()
    grant_permissions(editor, target, ["municipal_data.manage"])

    response = client.post(
        "/municipal-data/water/meters",
        json=meter_payload(target.id, supply_id=foreign_supply["id"]),
        headers=headers_for(editor),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Water supply does not belong to the organization"
    )


def test_readings_are_one_per_meter_and_day(
    client,
    make_user,
    data_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, data_organization, ["municipal_data.manage"])
    auth = headers_for(editor)
    meter = client.post(
        "/municipal-data/water/meters",
        json=meter_payload(data_organization.id),
        headers=auth,
    ).json()

    first = client.post(
        f"/municipal-data/water/meters/{meter['id']}/readings",
        json={"read_on": "2026-07-01", "reading_m3": "1200.500"},
        headers=auth,
    )
    same_day = client.post(
        f"/municipal-data/water/meters/{meter['id']}/readings",
        json={"read_on": "2026-07-01", "reading_m3": "1300.000"},
        headers=auth,
    )
    later = client.post(
        f"/municipal-data/water/meters/{meter['id']}/readings",
        json={"read_on": "2026-08-01", "reading_m3": "1290.000"},
        headers=auth,
    )
    listed = client.get(
        f"/municipal-data/water/meters/{meter['id']}/readings",
        headers=auth,
    )

    assert first.status_code == 201
    assert same_day.status_code == 409
    assert later.status_code == 201
    # De la más reciente hacia atrás.
    assert [item["read_on"] for item in listed.json()] == ["2026-08-01", "2026-07-01"]


def test_removed_meters_stop_taking_readings(
    client,
    make_user,
    data_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, data_organization, ["municipal_data.manage"])
    auth = headers_for(editor)
    meter = client.post(
        "/municipal-data/water/meters",
        json=meter_payload(data_organization.id, status="removed"),
        headers=auth,
    ).json()

    response = client.post(
        f"/municipal-data/water/meters/{meter['id']}/readings",
        json={"read_on": "2026-07-01", "reading_m3": "10.000"},
        headers=auth,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Removed water meters cannot take readings"


def test_series_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_municipal_organization,
    grant_permissions,
    superuser,
):
    target = make_municipal_organization()
    other = make_municipal_organization()
    super_auth = headers_for(superuser)
    client.post(
        "/municipal-data/padron",
        json=padron_payload(target.id, population=100, men=50, women=50),
        headers=super_auth,
    )
    client.post(
        "/municipal-data/padron",
        json=padron_payload(other.id, population=999, men=500, women=499),
        headers=super_auth,
    )

    viewer = make_user()
    grant_permissions(viewer, target, ["municipal_data.view"])
    listed = client.get(
        "/municipal-data/padron",
        params={"organization_id": target.id},
        headers=headers_for(viewer),
    )

    assert [item["population"] for item in listed.json()] == [100]


def test_paused_organization_keeps_the_series_read_only(
    client,
    db,
    make_user,
    make_municipal_organization,
    grant_permissions,
    superuser,
):
    organization = make_municipal_organization()
    client.post(
        "/municipal-data/padron",
        json=padron_payload(organization.id),
        headers=headers_for(superuser),
    )
    organization.status = "paused"
    db.commit()

    editor = make_user()
    grant_permissions(editor, organization, ["municipal_data.manage"])
    auth = headers_for(editor)

    listed = client.get(
        "/municipal-data/padron",
        params={"organization_id": organization.id},
        headers=auth,
    )
    denied = client.post(
        "/municipal-data/padron",
        json=padron_payload(organization.id, reference_year=2025),
        headers=auth,
    )

    assert listed.status_code == 200
    assert denied.status_code == 409


def test_unknown_meter_and_organization_are_reported_as_missing(
    client,
    make_user,
    data_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, data_organization, ["municipal_data.manage"])
    auth = headers_for(editor)

    missing_meter = client.get(
        "/municipal-data/water/meters/999999/readings",
        headers=auth,
    )
    missing_organization = client.get(
        "/municipal-data/padron",
        params={"organization_id": 999999},
        headers=auth,
    )

    assert missing_meter.status_code == 404
    assert missing_organization.status_code == 404
