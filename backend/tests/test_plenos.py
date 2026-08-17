import pytest
from conftest import headers_for, unique_suffix

from app.documents.models import Document
from app.projects.models import Project


@pytest.fixture
def pleno_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


@pytest.fixture
def make_document(db):
    def _make(organization_id):
        project = Project(
            name=f"Actas {unique_suffix()}",
            organization_id=organization_id,
        )
        db.add(project)
        db.flush()
        stored = f"{unique_suffix()}.pdf"
        document = Document(
            organization_id=organization_id,
            project_id=project.id,
            original_filename=f"acta-{unique_suffix()}.pdf",
            stored_filename=stored,
            storage_key=f"{organization_id}/{stored}",
            content_type="application/pdf",
            size_bytes=4096,
            checksum_sha256="b" * 64,
        )
        db.add(document)
        db.commit()
        return document

    return _make


def create_session(client, auth, organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "kind": "ordinary",
        "held_on": "2026-03-27",
    }
    payload.update(overrides)
    response = client.post("/plenos/sessions", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


def test_plenos_require_authentication(client):
    assert client.get("/plenos/sessions?organization_id=1").status_code == 401


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
    grant_permissions(user, other, ["plenos.manage"])
    add_member(user, target)
    auth = headers_for(user)

    listed = client.get(
        "/plenos/sessions",
        params={"organization_id": target.id},
        headers=auth,
    )

    assert listed.status_code == 403
    assert listed.json()["detail"] == "Permission required: plenos.view"


def test_one_session_of_each_kind_per_day(
    client,
    make_user,
    pleno_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, pleno_organization, ["plenos.manage"])
    auth = headers_for(manager)

    create_session(client, auth, pleno_organization.id)
    duplicate = client.post(
        "/plenos/sessions",
        json={
            "organization_id": pleno_organization.id,
            "kind": "ordinary",
            "held_on": "2026-03-27",
        },
        headers=auth,
    )
    # Un extraordinario el mismo día es perfectamente posible.
    extraordinary = client.post(
        "/plenos/sessions",
        json={
            "organization_id": pleno_organization.id,
            "kind": "extraordinary",
            "held_on": "2026-03-27",
        },
        headers=auth,
    )

    assert duplicate.status_code == 409
    assert extraordinary.status_code == 201


def test_the_agenda_keeps_its_order_and_rejects_repeated_positions(
    client,
    make_user,
    pleno_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, pleno_organization, ["plenos.manage"])
    auth = headers_for(manager)
    session = create_session(client, auth, pleno_organization.id)

    for position, title in ((2, "Ordenanza de terrazas"), (1, "Acta anterior")):
        response = client.post(
            f"/plenos/sessions/{session['id']}/agenda",
            json={"position": position, "title": title},
            headers=auth,
        )
        assert response.status_code == 201, response.text

    duplicate = client.post(
        f"/plenos/sessions/{session['id']}/agenda",
        json={"position": 1, "title": "Choca"},
        headers=auth,
    )
    listed = client.get(
        "/plenos/sessions",
        params={"organization_id": pleno_organization.id},
        headers=auth,
    ).json()

    assert duplicate.status_code == 409
    assert [item["title"] for item in listed[0]["agenda_items"]] == [
        "Acta anterior",
        "Ordenanza de terrazas",
    ]


def test_an_unvoted_item_keeps_its_votes_null(
    client,
    make_user,
    pleno_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, pleno_organization, ["plenos.manage"])
    auth = headers_for(manager)
    session = create_session(client, auth, pleno_organization.id)

    informative = client.post(
        f"/plenos/sessions/{session['id']}/agenda",
        json={"position": 1, "title": "Dación de cuenta"},
        headers=auth,
    ).json()
    voted = client.post(
        f"/plenos/sessions/{session['id']}/agenda",
        json={
            "position": 2,
            "title": "Aprobación del presupuesto",
            "votes_in_favour": 5,
            "votes_against": 2,
            "abstentions": 0,
            "outcome": "Aprobado",
        },
        headers=auth,
    ).json()

    # Cero votos a favor no es lo mismo que no haberse votado.
    assert informative["votes_in_favour"] is None
    assert voted["votes_in_favour"] == 5
    assert voted["abstentions"] == 0


def test_approving_the_minutes_needs_manage_and_a_document(
    client,
    make_user,
    pleno_organization,
    grant_permissions,
    make_document,
    superuser,
):
    session = create_session(client, headers_for(superuser), pleno_organization.id)
    document = make_document(pleno_organization.id)

    editor = make_user()
    grant_permissions(editor, pleno_organization, ["plenos.view", "plenos.edit"])
    editor_auth = headers_for(editor)

    drafted = client.patch(
        f"/plenos/sessions/{session['id']}/minutes",
        json={"minutes_status": "draft", "minutes_document_id": document.id},
        headers=editor_auth,
    )
    denied_approval = client.patch(
        f"/plenos/sessions/{session['id']}/minutes",
        json={"minutes_status": "approved", "minutes_document_id": document.id},
        headers=editor_auth,
    )
    without_document = client.patch(
        f"/plenos/sessions/{session['id']}/minutes",
        json={"minutes_status": "approved"},
        headers=headers_for(superuser),
    )
    approved = client.patch(
        f"/plenos/sessions/{session['id']}/minutes",
        json={"minutes_status": "approved", "minutes_document_id": document.id},
        headers=headers_for(superuser),
    )

    assert drafted.status_code == 200
    assert denied_approval.status_code == 403
    assert denied_approval.json()["detail"] == "Permission required: plenos.manage"
    assert without_document.status_code == 422
    assert approved.status_code == 200
    assert approved.json()["minutes_status"] == "approved"


def test_the_minutes_document_must_belong_to_the_organization(
    client,
    make_organization,
    make_document,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    session = create_session(client, headers_for(superuser), target.id)
    foreign_document = make_document(other.id)

    response = client.patch(
        f"/plenos/sessions/{session['id']}/minutes",
        json={"minutes_status": "draft", "minutes_document_id": foreign_document.id},
        headers=headers_for(superuser),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "Document does not belong to the organization"
    )


def test_a_cancelled_session_produces_neither_agenda_nor_minutes(
    client,
    make_user,
    pleno_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, pleno_organization, ["plenos.manage"])
    auth = headers_for(manager)
    session = create_session(client, auth, pleno_organization.id, status="cancelled")

    agenda = client.post(
        f"/plenos/sessions/{session['id']}/agenda",
        json={"position": 1, "title": "Nunca se trató"},
        headers=auth,
    )
    minutes = client.patch(
        f"/plenos/sessions/{session['id']}/minutes",
        json={"minutes_status": "draft"},
        headers=auth,
    )

    assert agenda.status_code == 409
    assert minutes.status_code == 409


def test_sessions_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    own = create_session(client, auth, target.id)
    create_session(client, auth, other.id)

    viewer = make_user()
    grant_permissions(viewer, target, ["plenos.view"])
    listed = client.get(
        "/plenos/sessions",
        params={"organization_id": target.id},
        headers=headers_for(viewer),
    )

    assert [item["id"] for item in listed.json()] == [own["id"]]


def test_paused_organization_keeps_plenos_read_only(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    organization = make_organization(name=f"Pausada {unique_suffix()}")
    session = create_session(client, headers_for(superuser), organization.id)
    organization.status = "paused"
    db.commit()

    manager = make_user()
    grant_permissions(manager, organization, ["plenos.manage"])
    auth = headers_for(manager)

    listed = client.get(
        "/plenos/sessions",
        params={"organization_id": organization.id},
        headers=auth,
    )
    denied = client.post(
        f"/plenos/sessions/{session['id']}/agenda",
        json={"position": 1, "title": "Tardío"},
        headers=auth,
    )

    assert listed.status_code == 200
    assert denied.status_code == 409
