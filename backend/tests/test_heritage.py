import pytest
from conftest import headers_for, unique_suffix

from app.documents.models import Document
from app.projects.models import Project


@pytest.fixture
def heritage_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


@pytest.fixture
def make_document(db):
    def _make(organization_id):
        project = Project(
            name=f"Archivo {unique_suffix()}",
            organization_id=organization_id,
        )
        db.add(project)
        db.flush()
        stored = f"{unique_suffix()}.pdf"
        document = Document(
            organization_id=organization_id,
            project_id=project.id,
            original_filename=f"escaneo-{unique_suffix()}.pdf",
            stored_filename=stored,
            storage_key=f"{organization_id}/{stored}",
            content_type="application/pdf",
            size_bytes=1024,
            checksum_sha256="c" * 64,
        )
        db.add(document)
        db.commit()
        return document

    return _make


def asset_payload(organization_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "slug": f"ermita-{suffix}",
        "name": "Ermita de San Roque",
        "kind": "building",
        "period": "siglo XVI",
    }
    payload.update(overrides)
    return payload


def archive_payload(organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "reference": f"AR-{unique_suffix()}",
        "title": "Libro de actas",
        "kind": "book",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "path",
    ["/heritage/assets?organization_id=1", "/heritage/archive?organization_id=1"],
)
def test_heritage_requires_authentication(client, path):
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
    grant_permissions(user, other, ["heritage.manage"])
    add_member(user, target)
    auth = headers_for(user)

    listed = client.get(
        "/heritage/assets", params={"organization_id": target.id}, headers=auth
    )
    created = client.post(
        "/heritage/assets", json=asset_payload(target.id), headers=auth
    )

    assert listed.status_code == 403
    assert listed.json()["detail"] == "Permission required: heritage.view"
    assert created.status_code == 403


def test_a_protected_asset_needs_the_reference_of_its_declaration(
    client,
    make_user,
    heritage_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, heritage_organization, ["heritage.manage"])
    auth = headers_for(editor)

    without_reference = client.post(
        "/heritage/assets",
        json=asset_payload(heritage_organization.id, protection_level="bic"),
        headers=auth,
    )
    declared = client.post(
        "/heritage/assets",
        json=asset_payload(
            heritage_organization.id,
            protection_level="bic",
            protection_reference="BOCyL 12/2004",
        ),
        headers=auth,
    )
    # Mucho patrimonio de un pueblo es valioso sin estar declarado.
    undeclared = client.post(
        "/heritage/assets",
        json=asset_payload(heritage_organization.id),
        headers=auth,
    )

    assert without_reference.status_code == 422
    assert declared.status_code == 201
    assert undeclared.status_code == 201
    assert undeclared.json()["protection_level"] == "none"


def test_the_period_stays_as_written(
    client,
    make_user,
    heritage_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, heritage_organization, ["heritage.manage"])

    created = client.post(
        "/heritage/assets",
        json=asset_payload(
            heritage_organization.id,
            period="finales del XIX o principios del XX",
        ),
        headers=headers_for(editor),
    )

    # Forzar un año sería inventar precisión que la fuente no tiene.
    assert created.json()["period"] == "finales del XIX o principios del XX"


def test_a_digitised_item_needs_its_document(
    client,
    make_user,
    heritage_organization,
    grant_permissions,
    make_document,
):
    editor = make_user()
    grant_permissions(editor, heritage_organization, ["heritage.manage"])
    auth = headers_for(editor)
    document = make_document(heritage_organization.id)

    promised = client.post(
        "/heritage/archive",
        json=archive_payload(
            heritage_organization.id,
            digitisation_state="digitised",
        ),
        headers=auth,
    )
    in_progress = client.post(
        "/heritage/archive",
        json=archive_payload(
            heritage_organization.id,
            digitisation_state="in_progress",
        ),
        headers=auth,
    )
    real = client.post(
        "/heritage/archive",
        json=archive_payload(
            heritage_organization.id,
            digitisation_state="digitised",
            document_id=document.id,
        ),
        headers=auth,
    )

    assert promised.status_code == 422
    # En curso sí puede no tener fichero todavía.
    assert in_progress.status_code == 201
    assert real.status_code == 201


def test_the_archive_is_ordered_by_year_with_undated_items_last(
    client,
    make_user,
    heritage_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, heritage_organization, ["heritage.manage"])
    auth = headers_for(editor)

    for title, start in (("Moderno", 1950), ("Antiguo", 1712), ("Sin fechar", None)):
        payload = archive_payload(heritage_organization.id, title=title)
        if start is not None:
            payload["start_year"] = start
        response = client.post("/heritage/archive", json=payload, headers=auth)
        assert response.status_code == 201, response.text

    listed = client.get(
        "/heritage/archive",
        params={"organization_id": heritage_organization.id},
        headers=auth,
    )

    # Un archivo se recorre cronológicamente; lo sin fechar no lo encabeza.
    assert [item["title"] for item in listed.json()] == [
        "Antiguo",
        "Moderno",
        "Sin fechar",
    ]


def test_an_inverted_year_range_is_rejected(
    client,
    make_user,
    heritage_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, heritage_organization, ["heritage.manage"])

    response = client.post(
        "/heritage/archive",
        json=archive_payload(
            heritage_organization.id,
            start_year=1950,
            end_year=1900,
        ),
        headers=headers_for(editor),
    )

    assert response.status_code == 422


def test_an_archive_item_cannot_borrow_another_organizations_document(
    client,
    make_user,
    make_organization,
    grant_permissions,
    make_document,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    foreign_document = make_document(other.id)

    editor = make_user()
    grant_permissions(editor, target, ["heritage.manage"])

    response = client.post(
        "/heritage/archive",
        json=archive_payload(
            target.id,
            digitisation_state="digitised",
            document_id=foreign_document.id,
        ),
        headers=headers_for(editor),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "Document does not belong to the organization"
    )


def test_archive_items_can_be_searched_by_physical_location(
    client,
    make_user,
    heritage_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, heritage_organization, ["heritage.manage"])
    auth = headers_for(editor)

    wanted = client.post(
        "/heritage/archive",
        json=archive_payload(
            heritage_organization.id,
            title="Padrón de 1940",
            physical_location="Armario 2, balda 3",
        ),
        headers=auth,
    ).json()
    client.post(
        "/heritage/archive",
        json=archive_payload(heritage_organization.id, title="Otro"),
        headers=auth,
    )

    # Lo que más se busca en un archivo de pueblo es dónde está el papel.
    found = client.get(
        "/heritage/archive",
        params={"organization_id": heritage_organization.id, "q": "armario 2"},
        headers=auth,
    )

    assert [item["id"] for item in found.json()] == [wanted["id"]]


def test_references_and_slugs_are_unique_per_organization(
    client,
    make_organization,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    reference = f"AR-{unique_suffix()}"

    first = client.post(
        "/heritage/archive",
        json=archive_payload(target.id, reference=reference),
        headers=auth,
    )
    duplicate = client.post(
        "/heritage/archive",
        json=archive_payload(target.id, reference=reference),
        headers=auth,
    )
    elsewhere = client.post(
        "/heritage/archive",
        json=archive_payload(other.id, reference=reference),
        headers=auth,
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert elsewhere.status_code == 201


def test_heritage_from_other_organizations_is_never_listed(
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
        "/heritage/assets", json=asset_payload(target.id), headers=auth
    ).json()
    client.post("/heritage/assets", json=asset_payload(other.id), headers=auth)

    viewer = make_user()
    grant_permissions(viewer, target, ["heritage.view"])
    listed = client.get(
        "/heritage/assets",
        params={"organization_id": target.id},
        headers=headers_for(viewer),
    )

    assert [item["id"] for item in listed.json()] == [own["id"]]


def test_paused_organization_keeps_heritage_read_only(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    organization = make_organization(name=f"Pausada {unique_suffix()}")
    client.post(
        "/heritage/assets",
        json=asset_payload(organization.id),
        headers=headers_for(superuser),
    )
    organization.status = "paused"
    db.commit()

    manager = make_user()
    grant_permissions(manager, organization, ["heritage.manage"])
    auth = headers_for(manager)

    listed = client.get(
        "/heritage/assets",
        params={"organization_id": organization.id},
        headers=auth,
    )
    denied = client.post(
        "/heritage/assets", json=asset_payload(organization.id), headers=auth
    )

    assert listed.status_code == 200
    assert denied.status_code == 409
