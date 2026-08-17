import pytest
from conftest import headers_for, unique_suffix

from app.documents.models import Document
from app.projects.models import Project


@pytest.fixture
def make_document(db):
    """Todo documento cuelga de un proyecto: el escudo reutiliza esa maquinaria
    en lugar de estrenar un almacén propio con reglas de acceso paralelas."""

    def _make(organization_id):
        project = Project(
            name=f"Identidad {unique_suffix()}",
            organization_id=organization_id,
        )
        db.add(project)
        db.flush()
        stored_filename = f"{unique_suffix()}.png"
        document = Document(
            organization_id=organization_id,
            project_id=project.id,
            original_filename=f"escudo-{unique_suffix()}.png",
            stored_filename=stored_filename,
            storage_key=f"{organization_id}/{stored_filename}",
            content_type="image/png",
            size_bytes=2048,
            checksum_sha256="a" * 64,
        )
        db.add(document)
        db.commit()
        return document

    return _make


def test_branding_requires_authentication(client):
    assert client.get("/organizations/1/branding").status_code == 401


def test_missing_branding_reads_as_an_empty_identity(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization(name=f"Org {unique_suffix()}")
    viewer = make_user()
    grant_permissions(viewer, organization, ["municipalities.view"])

    response = client.get(
        f"/organizations/{organization.id}/branding",
        headers=headers_for(viewer),
    )

    # Que no haya escudo no es un error: es lo normal hasta que alguien lo sube.
    assert response.status_code == 200
    assert response.json()["crest_document_id"] is None


def test_only_managers_can_set_the_crest(
    client,
    make_user,
    make_organization,
    grant_permissions,
    make_document,
):
    organization = make_organization(name=f"Org {unique_suffix()}")
    document = make_document(organization.id)

    viewer = make_user()
    grant_permissions(viewer, organization, ["municipalities.view"])
    denied = client.put(
        f"/organizations/{organization.id}/branding",
        json={"crest_document_id": document.id},
        headers=headers_for(viewer),
    )

    manager = make_user()
    grant_permissions(manager, organization, ["organizations.manage"])
    allowed = client.put(
        f"/organizations/{organization.id}/branding",
        json={"crest_document_id": document.id, "crest_alt_text": "Escudo"},
        headers=headers_for(manager),
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["crest_document_id"] == document.id
    assert allowed.json()["crest_alt_text"] == "Escudo"


def test_the_crest_must_belong_to_the_same_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    make_document,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    foreign_document = make_document(other.id)

    manager = make_user()
    grant_permissions(manager, target, ["organizations.manage"])

    response = client.put(
        f"/organizations/{target.id}/branding",
        json={"crest_document_id": foreign_document.id},
        headers=headers_for(manager),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "Document does not belong to the organization"
    )


def test_setting_the_crest_twice_replaces_it(
    client,
    make_user,
    make_organization,
    grant_permissions,
    make_document,
):
    organization = make_organization(name=f"Org {unique_suffix()}")
    first = make_document(organization.id)
    second = make_document(organization.id)
    manager = make_user()
    grant_permissions(manager, organization, ["organizations.manage"])
    auth = headers_for(manager)

    client.put(
        f"/organizations/{organization.id}/branding",
        json={"crest_document_id": first.id},
        headers=auth,
    )
    replaced = client.put(
        f"/organizations/{organization.id}/branding",
        json={"crest_document_id": second.id},
        headers=auth,
    )
    cleared = client.put(
        f"/organizations/{organization.id}/branding",
        json={},
        headers=auth,
    )

    assert replaced.json()["crest_document_id"] == second.id
    # Un PUT vacío deja la identidad en blanco: es un reemplazo, no un parche.
    assert cleared.json()["crest_document_id"] is None


def test_unknown_document_and_organization_are_reported_as_missing(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization(name=f"Org {unique_suffix()}")
    manager = make_user()
    grant_permissions(manager, organization, ["organizations.manage"])
    auth = headers_for(manager)

    missing_document = client.put(
        f"/organizations/{organization.id}/branding",
        json={"crest_document_id": 999999},
        headers=auth,
    )
    missing_organization = client.get("/organizations/999999/branding", headers=auth)

    assert missing_document.status_code == 404
    assert missing_organization.status_code == 404
