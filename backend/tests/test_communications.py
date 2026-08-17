import pytest
from conftest import headers_for, unique_suffix


@pytest.fixture
def comms_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


def notice_payload(organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "kind": "bando",
        "title": f"Bando {unique_suffix()}",
        "body": "Corte de agua por obras en la calle Mayor",
    }
    payload.update(overrides)
    return payload


def create_notice(client, auth, organization_id, **overrides):
    response = client.post(
        "/communications/notices",
        json=notice_payload(organization_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "path",
    [
        "/communications/news?organization_id=1",
        "/communications/notices?organization_id=1",
    ],
)
def test_communications_require_authentication(client, path):
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
    grant_permissions(user, other, ["communications.manage"])
    add_member(user, target)
    auth = headers_for(user)

    listed = client.get(
        "/communications/notices",
        params={"organization_id": target.id},
        headers=auth,
    )
    created = client.post(
        "/communications/notices",
        json=notice_payload(target.id),
        headers=auth,
    )

    assert listed.status_code == 403
    assert listed.json()["detail"] == "Permission required: communications.view"
    assert created.status_code == 403


def test_a_notice_is_born_as_a_draft(
    client,
    make_user,
    comms_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, comms_organization, ["communications.manage"])
    notice = create_notice(client, headers_for(editor), comms_organization.id)

    # Publicar es una transición con permiso propio, no un campo del alta.
    assert notice["status"] == "draft"
    assert notice["published_on"] is None
    assert [event["event_type"] for event in notice["events"]] == ["created"]


def test_publishing_needs_its_own_permission(
    client,
    make_user,
    comms_organization,
    grant_permissions,
    superuser,
):
    notice = create_notice(client, headers_for(superuser), comms_organization.id)

    editor = make_user()
    grant_permissions(
        editor,
        comms_organization,
        ["communications.view", "communications.edit"],
    )
    denied = client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05"},
        headers=headers_for(editor),
    )

    publisher = make_user()
    grant_permissions(
        publisher,
        comms_organization,
        ["communications.view", "communications.publish"],
    )
    allowed = client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05"},
        headers=headers_for(publisher),
    )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Permission required: communications.publish"
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "published"
    assert allowed.json()["published_on"] == "2026-08-05"


def test_withdrawing_keeps_the_record_that_it_was_published(
    client,
    make_user,
    comms_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, comms_organization, ["communications.manage"])
    auth = headers_for(manager)
    notice = create_notice(client, auth, comms_organization.id)

    client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05", "expires_on": "2026-08-20"},
        headers=auth,
    )
    withdrawn = client.post(
        f"/communications/notices/{notice['id']}/withdraw",
        json={"reason": "Se corrige la fecha del corte"},
        headers=auth,
    )

    body = withdrawn.json()
    assert withdrawn.status_code == 200
    assert body["status"] == "withdrawn"
    # Que el bando llegó a estar expuesto ese día puede tener que demostrarse.
    assert body["published_on"] == "2026-08-05"
    event_types = [event["event_type"] for event in body["events"]]
    assert event_types[0] == "withdrawn"
    assert event_types[-1] == "created"
    assert body["events"][0]["note"] == "Se corrige la fecha del corte"


def test_withdrawing_demands_a_reason(
    client,
    make_user,
    comms_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, comms_organization, ["communications.manage"])
    auth = headers_for(manager)
    notice = create_notice(client, auth, comms_organization.id)
    client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05"},
        headers=auth,
    )

    response = client.post(
        f"/communications/notices/{notice['id']}/withdraw",
        json={"reason": "   "},
        headers=auth,
    )

    assert response.status_code == 422


def test_the_publication_graph_refuses_impossible_moves(
    client,
    make_user,
    comms_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, comms_organization, ["communications.manage"])
    auth = headers_for(manager)
    notice = create_notice(client, auth, comms_organization.id)

    draft_withdraw = client.post(
        f"/communications/notices/{notice['id']}/withdraw",
        json={"reason": "Todavía no está expuesto"},
        headers=auth,
    )
    client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05"},
        headers=auth,
    )
    double_publish = client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-06"},
        headers=auth,
    )
    client.post(
        f"/communications/notices/{notice['id']}/withdraw",
        json={"reason": "Retirado"},
        headers=auth,
    )
    republish = client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-07"},
        headers=auth,
    )

    assert draft_withdraw.status_code == 409
    assert double_publish.status_code == 409
    # Reexponer lo retirado sería reescribir la historia: se publica uno nuevo.
    assert republish.status_code == 409


def test_an_expiry_before_publication_is_rejected(
    client,
    make_user,
    comms_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, comms_organization, ["communications.manage"])
    auth = headers_for(manager)
    notice = create_notice(client, auth, comms_organization.id)

    response = client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05", "expires_on": "2026-08-01"},
        headers=auth,
    )

    assert response.status_code == 422


def test_published_news_must_say_since_when(
    client,
    make_user,
    comms_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, comms_organization, ["communications.manage"])
    auth = headers_for(editor)

    without_date = client.post(
        "/communications/news",
        json={
            "organization_id": comms_organization.id,
            "slug": f"fiestas-{unique_suffix()}",
            "title": "Programa de fiestas",
            "status": "published",
        },
        headers=auth,
    )
    draft_with_date = client.post(
        "/communications/news",
        json={
            "organization_id": comms_organization.id,
            "slug": f"borrador-{unique_suffix()}",
            "title": "Borrador",
            "published_on": "2026-08-05",
        },
        headers=auth,
    )
    valid = client.post(
        "/communications/news",
        json={
            "organization_id": comms_organization.id,
            "slug": f"valida-{unique_suffix()}",
            "title": "Programa de fiestas",
            "status": "published",
            "published_on": "2026-08-05",
        },
        headers=auth,
    )

    assert without_date.status_code == 422
    assert draft_with_date.status_code == 422
    assert valid.status_code == 201


def test_news_slugs_are_unique_per_organization(
    client,
    make_organization,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    slug = f"fiestas-{unique_suffix()}"

    first = client.post(
        "/communications/news",
        json={"organization_id": target.id, "slug": slug, "title": "Fiestas"},
        headers=auth,
    )
    duplicate = client.post(
        "/communications/news",
        json={"organization_id": target.id, "slug": slug, "title": "Fiestas"},
        headers=auth,
    )
    elsewhere = client.post(
        "/communications/news",
        json={"organization_id": other.id, "slug": slug, "title": "Fiestas"},
        headers=auth,
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert elsewhere.status_code == 201


def test_notices_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    own = create_notice(client, auth, target.id)
    create_notice(client, auth, other.id)

    viewer = make_user()
    grant_permissions(viewer, target, ["communications.view"])
    listed = client.get(
        "/communications/notices",
        params={"organization_id": target.id},
        headers=headers_for(viewer),
    )

    assert [item["id"] for item in listed.json()] == [own["id"]]


def test_paused_organization_keeps_communications_read_only(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    organization = make_organization(name=f"Pausada {unique_suffix()}")
    notice = create_notice(client, headers_for(superuser), organization.id)
    organization.status = "paused"
    db.commit()

    manager = make_user()
    grant_permissions(manager, organization, ["communications.manage"])
    auth = headers_for(manager)

    listed = client.get(
        "/communications/notices",
        params={"organization_id": organization.id},
        headers=auth,
    )
    denied = client.post(
        f"/communications/notices/{notice['id']}/publish",
        json={"published_on": "2026-08-05"},
        headers=auth,
    )

    assert listed.status_code == 200
    assert denied.status_code == 409
