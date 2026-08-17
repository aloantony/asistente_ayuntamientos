import pytest
from conftest import headers_for, unique_suffix

from app.municipalities.models import Municipality
from app.ordinances.models import Ordinance
from app.sede.models import MunicipalTax


@pytest.fixture
def sede_organization(db, make_organization):
    suffix = unique_suffix()
    municipality = Municipality(
        name=f"Municipio {suffix}",
        province="Burgos",
        autonomous_community="Castilla y Leon",
        ine_code=f"sede-{suffix}",
    )
    db.add(municipality)
    db.commit()
    return make_organization(
        name=f"Ayuntamiento {suffix}",
        municipality_id=municipality.id,
    )


def publish_notice(client, auth, organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "title": f"Bando {unique_suffix()}",
        "publish_to_sede": True,
    }
    payload.update(overrides)
    notice = client.post("/communications/notices", json=payload, headers=auth).json()
    client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05"},
        headers=auth,
    )
    return notice


def test_sede_requires_authentication(client):
    assert client.get("/sede?organization_id=1").status_code == 401


def test_sede_needs_permission_in_the_target_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")

    user = make_user()
    grant_permissions(user, other, ["sede.view"])
    add_member(user, target)

    response = client.get(
        "/sede",
        params={"organization_id": target.id},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: sede.view"


def test_the_board_only_shows_what_is_actually_on_display(
    client,
    make_user,
    sede_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(
        manager,
        sede_organization,
        ["sede.view", "communications.manage"],
    )
    auth = headers_for(manager)

    published = publish_notice(client, auth, sede_organization.id)
    # Un borrador no está expuesto.
    client.post(
        "/communications/notices",
        json={
            "organization_id": sede_organization.id,
            "title": "Borrador",
            "publish_to_sede": True,
        },
        headers=auth,
    )
    # Un bando publicado pero sin marcar para la sede tampoco sale.
    publish_notice(
        client,
        auth,
        sede_organization.id,
        title="Interno",
        publish_to_sede=False,
    )
    withdrawn = publish_notice(client, auth, sede_organization.id, title="Retirado")
    client.post(
        f"/communications/notices/{withdrawn['id']}/withdraw",
        json={"reason": "Se corrige"},
        headers=auth,
    )

    sede = client.get(
        "/sede", params={"organization_id": sede_organization.id}, headers=auth
    ).json()

    board_ids = {entry["id"] for entry in sede["board"] if entry["kind"] != "noticia"}
    assert board_ids == {published["id"]}


def test_the_board_mixes_notices_and_news_newest_first(
    client,
    make_user,
    sede_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(
        manager,
        sede_organization,
        ["sede.view", "communications.manage"],
    )
    auth = headers_for(manager)

    publish_notice(client, auth, sede_organization.id, title="Bando del 5")
    client.post(
        "/communications/news",
        json={
            "organization_id": sede_organization.id,
            "slug": f"noticia-{unique_suffix()}",
            "title": "Noticia del 10",
            "status": "published",
            "published_on": "2026-08-10",
            "publish_to_sede": True,
        },
        headers=auth,
    )

    sede = client.get(
        "/sede", params={"organization_id": sede_organization.id}, headers=auth
    ).json()

    # Se leen juntos y por fecha, no separados por tipo.
    assert [entry["title"] for entry in sede["board"]] == [
        "Noticia del 10",
        "Bando del 5",
    ]


def test_only_published_contracts_reach_the_contractor_profile(
    client,
    make_user,
    sede_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(
        manager,
        sede_organization,
        ["sede.view", "administration.manage"],
    )
    auth = headers_for(manager)

    for status_value, reference in (
        ("draft", "BORRADOR"),
        ("published", "PUBLICADO"),
        ("awarded", "ADJUDICADO"),
    ):
        payload = {
            "organization_id": sede_organization.id,
            "reference": f"{reference}-{unique_suffix()}",
            "title": reference,
            "status": status_value,
        }
        if status_value == "awarded":
            payload["awarded_to"] = "Empresa"
            payload["awarded_amount"] = "1000.00"
        response = client.post(
            "/administration/contracts", json=payload, headers=auth
        )
        assert response.status_code == 201, response.text

    sede = client.get(
        "/sede", params={"organization_id": sede_organization.id}, headers=auth
    ).json()

    # Un borrador no ha salido a licitación: no es público todavía.
    assert {item["title"] for item in sede["contracts"]} == {
        "PUBLICADO",
        "ADJUDICADO",
    }


def test_only_active_and_curated_ordinances_are_published(
    client,
    db,
    make_user,
    sede_organization,
    grant_permissions,
):
    viewer = make_user()
    grant_permissions(viewer, sede_organization, ["sede.view"])

    db.add_all(
        [
            Ordinance(
                municipality_id=sede_organization.municipality_id,
                title="Vigente y revisada",
                topic="urbanismo",
                ordinance_type="ordinance",
                status="active",
                curation_status="approved",
            ),
            Ordinance(
                municipality_id=sede_organization.municipality_id,
                title="Pendiente de revisar",
                topic="urbanismo",
                ordinance_type="ordinance",
                status="active",
                curation_status="pending_review",
            ),
            Ordinance(
                municipality_id=sede_organization.municipality_id,
                title="Derogada",
                topic="urbanismo",
                ordinance_type="ordinance",
                status="repealed",
                curation_status="approved",
            ),
        ]
    )
    db.commit()

    sede = client.get(
        "/sede",
        params={"organization_id": sede_organization.id},
        headers=headers_for(viewer),
    ).json()

    # La sede no es el sitio para una ordenanza sin curar ni para una derogada.
    assert [item["title"] for item in sede["ordinances"]] == ["Vigente y revisada"]


def test_taxes_may_reference_their_ordinance_but_do_not_need_it(
    client,
    db,
    make_user,
    sede_organization,
    grant_permissions,
):
    viewer = make_user()
    grant_permissions(viewer, sede_organization, ["sede.view"])

    ordinance = Ordinance(
        municipality_id=sede_organization.municipality_id,
        title="Ordenanza fiscal del IBI",
        topic="tributos",
        ordinance_type="tax_ordinance",
        status="active",
        curation_status="approved",
    )
    db.add(ordinance)
    db.flush()
    db.add_all(
        [
            MunicipalTax(
                organization_id=sede_organization.id,
                slug="ibi",
                name="Impuesto sobre bienes inmuebles",
                kind="tax",
                rate_kind="percentage",
                rate_value="0.4000",
                ordinance_id=ordinance.id,
            ),
            # El tributo existe aunque su ordenanza no esté digitalizada.
            MunicipalTax(
                organization_id=sede_organization.id,
                slug="basuras",
                name="Tasa de basuras",
                kind="fee",
                rate_kind="tariff",
                rate_description="Según tarifa por vivienda",
            ),
        ]
    )
    db.commit()

    sede = client.get(
        "/sede",
        params={"organization_id": sede_organization.id},
        headers=headers_for(viewer),
    ).json()

    by_slug = {tax["slug"]: tax for tax in sede["taxes"]}
    assert by_slug["ibi"]["ordinance_id"] == ordinance.id
    assert by_slug["basuras"]["ordinance_id"] is None


def test_a_tax_rate_needs_either_a_number_or_a_tariff(
    db,
    sede_organization,
):
    from sqlalchemy.exc import IntegrityError

    # Un tipo porcentual sin valor no dice cuánto se paga.
    db.add(
        MunicipalTax(
            organization_id=sede_organization.id,
            slug="roto",
            name="Sin cuota",
            kind="tax",
            rate_kind="percentage",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_an_archived_organization_stops_publishing(
    client,
    db,
    make_user,
    sede_organization,
    grant_permissions,
):
    viewer = make_user()
    grant_permissions(viewer, sede_organization, ["sede.view"])
    auth = headers_for(viewer)

    available = client.get(
        "/sede", params={"organization_id": sede_organization.id}, headers=auth
    )
    sede_organization.status = "archived"
    db.commit()
    archived = client.get(
        "/sede", params={"organization_id": sede_organization.id}, headers=auth
    )

    assert available.status_code == 200
    assert archived.status_code == 409


def test_content_from_other_organizations_is_never_published(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    own = publish_notice(client, auth, target.id, title="Del objetivo")
    publish_notice(client, auth, other.id, title="De la otra")

    viewer = make_user()
    grant_permissions(viewer, target, ["sede.view"])
    sede = client.get(
        "/sede",
        params={"organization_id": target.id},
        headers=headers_for(viewer),
    ).json()

    assert [entry["id"] for entry in sede["board"]] == [own["id"]]


def test_an_unknown_organization_is_reported_as_missing(
    client,
    make_user,
    sede_organization,
    grant_permissions,
):
    viewer = make_user()
    grant_permissions(viewer, sede_organization, ["sede.view"])

    response = client.get(
        "/sede",
        params={"organization_id": 999999},
        headers=headers_for(viewer),
    )

    assert response.status_code == 404


def test_the_sede_response_is_not_cached(
    client,
    make_user,
    sede_organization,
    grant_permissions,
):
    viewer = make_user()
    grant_permissions(viewer, sede_organization, ["sede.view"])

    response = client.get(
        "/sede",
        params={"organization_id": sede_organization.id},
        headers=headers_for(viewer),
    )

    # Retirar un bando tiene que notarse de inmediato.
    assert response.headers["Cache-Control"] == "no-store"
