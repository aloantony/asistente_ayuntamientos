import pytest
from conftest import headers_for, unique_suffix


@pytest.fixture
def admin_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


def licence_payload(organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "reference": f"LIC-{unique_suffix()}",
        "kind": "works",
        "applicant": "Vecina de la calle Mayor",
        "requested_on": "2026-03-01",
    }
    payload.update(overrides)
    return payload


def contract_payload(organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "reference": f"CON-{unique_suffix()}",
        "title": "Reparación del alumbrado",
        "procedure_type": "minor",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "path",
    [
        "/administration/office-hours?organization_id=1",
        "/administration/procedures?organization_id=1",
        "/administration/licences?organization_id=1",
        "/administration/contracts?organization_id=1",
        "/administration/grants?organization_id=1",
        "/administration/transparency?organization_id=1",
    ],
)
def test_administration_requires_authentication(client, path):
    assert client.get(path).status_code == 401


def test_permissions_are_scoped_to_the_target_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")

    user = make_user()
    grant_permissions(user, other, ["administration.manage"])
    add_member(user, target)
    auth = headers_for(user)

    listed = client.get(
        "/administration/licences",
        params={"organization_id": target.id},
        headers=auth,
    )
    created = client.post(
        "/administration/licences",
        json=licence_payload(target.id),
        headers=auth,
    )

    assert listed.status_code == 403
    assert listed.json()["detail"] == "Permission required: administration.view"
    assert created.status_code == 403
    assert created.json()["detail"] == "Permission required: administration.edit"


def test_office_hours_are_listed_in_week_order(
    client,
    make_user,
    admin_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, admin_organization, ["administration.manage"])
    auth = headers_for(editor)

    for weekday, opens in (("friday", 540), ("monday", 540), ("wednesday", 600)):
        response = client.post(
            "/administration/office-hours",
            json={
                "organization_id": admin_organization.id,
                "office_name": "Secretaría",
                "weekday": weekday,
                "opens_at": opens,
                "closes_at": opens + 240,
            },
            headers=auth,
        )
        assert response.status_code == 201, response.text

    listed = client.get(
        "/administration/office-hours",
        params={"organization_id": admin_organization.id},
        headers=auth,
    )

    # Alfabéticamente "friday" iría primero, que no es como se lee un horario.
    assert [row["weekday"] for row in listed.json()] == [
        "monday",
        "wednesday",
        "friday",
    ]


def test_office_hours_reject_an_inverted_range(
    client,
    make_user,
    admin_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, admin_organization, ["administration.edit"])

    response = client.post(
        "/administration/office-hours",
        json={
            "organization_id": admin_organization.id,
            "office_name": "Secretaría",
            "weekday": "monday",
            "opens_at": 800,
            "closes_at": 700,
        },
        headers=headers_for(editor),
    )

    assert response.status_code == 422


def test_a_resolved_licence_needs_its_resolution_date(
    client,
    make_user,
    admin_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, admin_organization, ["administration.manage"])
    auth = headers_for(editor)

    granted_without_date = client.post(
        "/administration/licences",
        json=licence_payload(admin_organization.id, status="granted"),
        headers=auth,
    )
    pending_with_date = client.post(
        "/administration/licences",
        json=licence_payload(admin_organization.id, resolved_on="2026-04-01"),
        headers=auth,
    )
    resolved_before_request = client.post(
        "/administration/licences",
        json=licence_payload(
            admin_organization.id,
            status="denied",
            resolved_on="2026-01-01",
        ),
        headers=auth,
    )
    valid = client.post(
        "/administration/licences",
        json=licence_payload(
            admin_organization.id,
            status="granted",
            resolved_on="2026-04-01",
        ),
        headers=auth,
    )

    assert granted_without_date.status_code == 422
    assert pending_with_date.status_code == 422
    assert resolved_before_request.status_code == 422
    assert valid.status_code == 201


def test_licence_references_are_unique_per_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    reference = f"LIC-{unique_suffix()}"

    first = client.post(
        "/administration/licences",
        json=licence_payload(target.id, reference=reference),
        headers=auth,
    )
    duplicate = client.post(
        "/administration/licences",
        json=licence_payload(target.id, reference=reference),
        headers=auth,
    )
    # La misma referencia en otro ayuntamiento es legítima: los expedientes se
    # numeran por municipio.
    elsewhere = client.post(
        "/administration/licences",
        json=licence_payload(other.id, reference=reference),
        headers=auth,
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert elsewhere.status_code == 201


def test_an_awarded_contract_needs_who_and_how_much(
    client,
    make_user,
    admin_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, admin_organization, ["administration.manage"])
    auth = headers_for(editor)

    incomplete = client.post(
        "/administration/contracts",
        json=contract_payload(admin_organization.id, status="awarded"),
        headers=auth,
    )
    complete = client.post(
        "/administration/contracts",
        json=contract_payload(
            admin_organization.id,
            status="awarded",
            awarded_to="Electricidad del Duero",
            awarded_amount="12500.00",
        ),
        headers=auth,
    )

    assert incomplete.status_code == 422
    assert complete.status_code == 201


def test_transparency_items_can_be_filtered_by_area(
    client,
    make_user,
    admin_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, admin_organization, ["administration.manage"])
    auth = headers_for(editor)

    for area, title in (("economic", "Presupuesto 2026"), ("contracts", "Contratos")):
        response = client.post(
            "/administration/transparency",
            json={
                "organization_id": admin_organization.id,
                "area": area,
                "title": title,
            },
            headers=auth,
        )
        assert response.status_code == 201, response.text

    filtered = client.get(
        "/administration/transparency",
        params={"organization_id": admin_organization.id, "area": "economic"},
        headers=auth,
    )

    assert [item["title"] for item in filtered.json()] == ["Presupuesto 2026"]


def test_procedures_hide_the_archived_ones_by_default(
    client,
    make_user,
    admin_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, admin_organization, ["administration.manage"])
    auth = headers_for(editor)

    client.post(
        "/administration/procedures",
        json={
            "organization_id": admin_organization.id,
            "slug": "empadronamiento",
            "name": "Alta en el padrón",
        },
        headers=auth,
    )
    client.post(
        "/administration/procedures",
        json={
            "organization_id": admin_organization.id,
            "slug": "licencia-antigua",
            "name": "Trámite retirado",
            "status": "archived",
        },
        headers=auth,
    )

    default = client.get(
        "/administration/procedures",
        params={"organization_id": admin_organization.id},
        headers=auth,
    )
    with_archived = client.get(
        "/administration/procedures",
        params={"organization_id": admin_organization.id, "include_archived": True},
        headers=auth,
    )

    assert [item["slug"] for item in default.json()] == ["empadronamiento"]
    assert len(with_archived.json()) == 2


def test_records_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    own = client.post(
        "/administration/licences",
        json=licence_payload(target.id),
        headers=auth,
    ).json()
    client.post(
        "/administration/licences",
        json=licence_payload(other.id),
        headers=auth,
    )

    viewer = make_user()
    grant_permissions(viewer, target, ["administration.view"])
    listed = client.get(
        "/administration/licences",
        params={"organization_id": target.id},
        headers=headers_for(viewer),
    )

    assert [item["id"] for item in listed.json()] == [own["id"]]


def test_paused_organization_keeps_administration_read_only(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    organization = make_organization(name=f"Pausada {unique_suffix()}")
    client.post(
        "/administration/licences",
        json=licence_payload(organization.id),
        headers=headers_for(superuser),
    )
    organization.status = "paused"
    db.commit()

    manager = make_user()
    grant_permissions(manager, organization, ["administration.manage"])
    auth = headers_for(manager)

    listed = client.get(
        "/administration/licences",
        params={"organization_id": organization.id},
        headers=auth,
    )
    denied = client.post(
        "/administration/licences",
        json=licence_payload(organization.id),
        headers=auth,
    )

    assert listed.status_code == 200
    assert denied.status_code == 409
