import pytest
from conftest import headers_for, unique_suffix


@pytest.fixture
def staff_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


def post_payload(organization_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "kind": "post",
        "label": f"Puesto {suffix}",
        "description": "Puesto de la plantilla municipal",
        "sort_order": 10,
    }
    payload.update(overrides)
    return payload


def worker_payload(organization_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "full_name": f"Trabajador {suffix}",
        "email": f"trabajador-{suffix}@example.com",
        "phone": "947 000 000",
        "schedule_summary": "Martes de 9 a 14",
        "schedule_days": ["tuesday"],
        "weekly_hours": "5.00",
        "contract_type": "permanent",
    }
    payload.update(overrides)
    return payload


def create_post(client, auth, organization_id, **overrides):
    response = client.post(
        "/staff/posts",
        json=post_payload(organization_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_worker(client, auth, organization_id, **overrides):
    response = client.post(
        "/staff/workers",
        json=worker_payload(organization_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "path",
    [
        "/staff/posts?organization_id=1",
        "/staff/workers?organization_id=1",
        "/staff/workers/1",
        "/staff/workers/1/absences",
        "/staff/workers/1/reports",
        "/staff/workers/1/invoices",
        "/staff/workers/1/history",
    ],
)
def test_staff_requires_authentication(client, path):
    response = client.get(path)

    assert response.status_code == 401


def test_staff_permissions_are_scoped_to_the_target_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    worker = create_worker(client, headers_for(superuser), target.id)

    user = make_user()
    grant_permissions(user, other, ["staff.manage"])
    add_member(user, target)
    auth = headers_for(user)

    list_response = client.get(
        "/staff/workers",
        params={"organization_id": target.id},
        headers=auth,
    )
    detail_response = client.get(f"/staff/workers/{worker['id']}", headers=auth)
    create_response = client.post(
        "/staff/workers",
        json=worker_payload(target.id),
        headers=auth,
    )

    assert list_response.status_code == 403
    assert list_response.json()["detail"] == "Permission required: staff.view"
    assert detail_response.status_code == 403
    assert create_response.status_code == 403
    assert create_response.json()["detail"] == "Permission required: staff.edit"


def test_view_edit_and_manage_permissions_are_separate(
    client,
    make_user,
    staff_organization,
    grant_permissions,
    superuser,
):
    super_auth = headers_for(superuser)
    worker = create_worker(client, super_auth, staff_organization.id)

    viewer = make_user()
    grant_permissions(viewer, staff_organization, ["staff.view"])
    viewer_auth = headers_for(viewer)
    assert (
        client.get(
            "/staff/workers",
            params={"organization_id": staff_organization.id},
            headers=viewer_auth,
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/staff/workers/{worker['id']}",
            json={"full_name": "No permitido"},
            headers=viewer_auth,
        ).status_code
        == 403
    )

    editor = make_user()
    grant_permissions(editor, staff_organization, ["staff.edit"])
    editor_auth = headers_for(editor)
    edited = client.patch(
        f"/staff/workers/{worker['id']}",
        json={"full_name": "Nombre corregido"},
        headers=editor_auth,
    )
    vacation = client.patch(
        f"/staff/workers/{worker['id']}",
        json={"status": "vacation"},
        headers=editor_auth,
    )
    denied_archive = client.patch(
        f"/staff/workers/{worker['id']}",
        json={"status": "archived"},
        headers=editor_auth,
    )
    denied_post = client.post(
        "/staff/posts",
        json=post_payload(staff_organization.id),
        headers=editor_auth,
    )

    assert edited.status_code == 200
    assert edited.json()["full_name"] == "Nombre corregido"
    assert vacation.status_code == 200
    assert vacation.json()["status"] == "vacation"
    assert denied_archive.status_code == 403
    assert denied_archive.json()["detail"] == "Permission required: staff.manage"
    assert denied_post.status_code == 403
    assert denied_post.json()["detail"] == "Permission required: staff.manage"

    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    archived = client.patch(
        f"/staff/workers/{worker['id']}",
        json={"status": "archived"},
        headers=headers_for(manager),
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"


def test_archived_workers_are_hidden_unless_requested(
    client,
    make_user,
    staff_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    auth = headers_for(manager)

    active = create_worker(client, auth, staff_organization.id)
    archived = create_worker(client, auth, staff_organization.id)
    client.patch(
        f"/staff/workers/{archived['id']}",
        json={"status": "archived"},
        headers=auth,
    )

    default_list = client.get(
        "/staff/workers",
        params={"organization_id": staff_organization.id},
        headers=auth,
    )
    with_archived = client.get(
        "/staff/workers",
        params={"organization_id": staff_organization.id, "include_archived": True},
        headers=auth,
    )

    assert [item["id"] for item in default_list.json()] == [active["id"]]
    assert {item["id"] for item in with_archived.json()} == {
        active["id"],
        archived["id"],
    }


def test_posts_nest_under_containers_and_reject_cycles(
    client,
    make_user,
    staff_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    auth = headers_for(manager)

    container = create_post(
        client,
        auth,
        staff_organization.id,
        kind="container",
        label="Servicios técnicos",
    )
    nested = create_post(
        client,
        auth,
        staff_organization.id,
        label="Arquitecto",
        parent_id=container["id"],
    )
    assert nested["parent_id"] == container["id"]

    denied_parent = client.post(
        "/staff/posts",
        json=post_payload(
            staff_organization.id,
            label="Colgado de un puesto",
            parent_id=nested["id"],
        ),
        headers=auth,
    )
    assert denied_parent.status_code == 409
    assert denied_parent.json()["detail"] == "Parent staff post must be a container"

    inner_container = create_post(
        client,
        auth,
        staff_organization.id,
        kind="container",
        label="Subunidad",
        parent_id=container["id"],
    )
    cycle = client.patch(
        f"/staff/posts/{container['id']}",
        json={"parent_id": inner_container["id"]},
        headers=auth,
    )
    assert cycle.status_code == 409
    assert cycle.json()["detail"] == "Parent staff post would create a cycle"

    denied_kind_change = client.patch(
        f"/staff/posts/{container['id']}",
        json={"kind": "post"},
        headers=auth,
    )
    assert denied_kind_change.status_code == 409


def test_posts_cannot_be_nested_across_organizations(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    foreign_container = create_post(
        client,
        headers_for(superuser),
        other.id,
        kind="container",
    )

    manager = make_user()
    grant_permissions(manager, target, ["staff.manage"])
    response = client.post(
        "/staff/posts",
        json=post_payload(target.id, parent_id=foreign_container["id"]),
        headers=headers_for(manager),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Parent staff post does not belong to the organization"
    )


def test_a_post_holds_at_most_one_worker(
    client,
    make_user,
    staff_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    auth = headers_for(manager)

    post = create_post(client, auth, staff_organization.id, label="Secretario")
    container = create_post(
        client,
        auth,
        staff_organization.id,
        kind="container",
        label="Servicios",
    )
    holder = create_worker(client, auth, staff_organization.id, post_id=post["id"])
    assert holder["post"]["id"] == post["id"]

    conflict = client.post(
        "/staff/workers",
        json=worker_payload(staff_organization.id, post_id=post["id"]),
        headers=auth,
    )
    container_conflict = client.post(
        "/staff/workers",
        json=worker_payload(staff_organization.id, post_id=container["id"]),
        headers=auth,
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Staff post is already occupied"
    assert container_conflict.status_code == 409
    assert container_conflict.json()["detail"] == "Staff container cannot be occupied"

    # Reasignar el puesto a quien ya lo ocupa no es un conflicto consigo mismo.
    reassigned = client.patch(
        f"/staff/workers/{holder['id']}",
        json={"post_id": post["id"]},
        headers=auth,
    )
    assert reassigned.status_code == 200


def test_workers_cannot_take_a_post_from_another_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    foreign_post = create_post(client, headers_for(superuser), other.id)

    manager = make_user()
    grant_permissions(manager, target, ["staff.manage"])
    response = client.post(
        "/staff/workers",
        json=worker_payload(target.id, post_id=foreign_post["id"]),
        headers=headers_for(manager),
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "Staff post does not belong to the organization"
    )


def test_schedule_days_are_normalised_to_week_order(
    client,
    make_user,
    staff_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    auth = headers_for(manager)

    worker = create_worker(
        client,
        auth,
        staff_organization.id,
        schedule_days=["friday", "monday", "friday", "wednesday"],
    )
    invalid = client.post(
        "/staff/workers",
        json=worker_payload(staff_organization.id, schedule_days=["lunes"]),
        headers=auth,
    )

    assert worker["schedule_days"] == ["monday", "wednesday", "friday"]
    assert invalid.status_code == 422


def test_worker_history_records_creation_status_and_field_changes(
    client,
    make_user,
    staff_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    auth = headers_for(manager)

    post = create_post(client, auth, staff_organization.id)
    worker = create_worker(client, auth, staff_organization.id)

    client.patch(
        f"/staff/workers/{worker['id']}",
        json={"status": "leave", "phone": "947 111 111"},
        headers=auth,
    )
    client.patch(
        f"/staff/workers/{worker['id']}",
        json={"post_id": post["id"]},
        headers=auth,
    )
    client.patch(
        f"/staff/workers/{worker['id']}",
        json={"status": "archived"},
        headers=auth,
    )

    history = client.get(f"/staff/workers/{worker['id']}/history", headers=auth)
    events = history.json()
    event_types = [event["event_type"] for event in events]

    assert history.status_code == 200
    # El histórico se devuelve del más reciente al más antiguo.
    assert event_types[-1] == "created"
    assert event_types[0] == "archived"
    assert "status_changed" in event_types
    assert "post_changed" in event_types
    assert any(event["changed_fields"] == ["phone"] for event in events)
    assert all(event["actor_id"] == manager.id for event in events)


def test_absences_are_scoped_to_their_worker(
    client,
    make_user,
    staff_organization,
    grant_permissions,
    superuser,
):
    editor = make_user()
    grant_permissions(editor, staff_organization, ["staff.edit"])
    auth = headers_for(editor)
    worker = create_worker(client, headers_for(superuser), staff_organization.id)
    other_worker = create_worker(
        client,
        headers_for(superuser),
        staff_organization.id,
    )

    created = client.post(
        f"/staff/workers/{worker['id']}/absences",
        json={
            "absence_type": "vacation",
            "start_date": "2026-08-01",
            "end_date": "2026-08-15",
            "reason": "Vacaciones de verano",
        },
        headers=auth,
    )
    assert created.status_code == 201
    absence = created.json()
    assert absence["organization_id"] == staff_organization.id

    inverted = client.post(
        f"/staff/workers/{worker['id']}/absences",
        json={
            "absence_type": "personal",
            "start_date": "2026-08-15",
            "end_date": "2026-08-01",
        },
        headers=auth,
    )
    assert inverted.status_code == 422

    listed = client.get(f"/staff/workers/{worker['id']}/absences", headers=auth)
    other_listed = client.get(
        f"/staff/workers/{other_worker['id']}/absences",
        headers=auth,
    )
    assert [item["id"] for item in listed.json()] == [absence["id"]]
    assert other_listed.json() == []

    # Una ausencia sólo se borra desde el trabajador al que pertenece.
    wrong_worker = client.delete(
        f"/staff/workers/{other_worker['id']}/absences/{absence['id']}",
        headers=auth,
    )
    deleted = client.delete(
        f"/staff/workers/{worker['id']}/absences/{absence['id']}",
        headers=auth,
    )
    assert wrong_worker.status_code == 404
    assert deleted.status_code == 204
    assert client.get(
        f"/staff/workers/{worker['id']}/absences",
        headers=auth,
    ).json() == []


def test_diary_entries_need_content_and_record_their_author(
    client,
    make_user,
    staff_organization,
    grant_permissions,
    superuser,
):
    editor = make_user()
    grant_permissions(editor, staff_organization, ["staff.edit"])
    auth = headers_for(editor)
    worker = create_worker(client, headers_for(superuser), staff_organization.id)

    empty = client.post(
        f"/staff/workers/{worker['id']}/reports",
        json={"report_date": "2026-08-03"},
        headers=auth,
    )
    diary = client.post(
        f"/staff/workers/{worker['id']}/reports",
        json={
            "report_date": "2026-08-03",
            "plan": "Revisar el alumbrado de la calle Mayor",
            "closing": "Dos luminarias sustituidas",
        },
        headers=auth,
    )
    report = client.post(
        f"/staff/workers/{worker['id']}/reports",
        json={
            "report_type": "report",
            "report_date": "2026-08-04",
            "incident": "Arqueta anegada en la plaza",
        },
        headers=auth,
    )

    assert empty.status_code == 422
    assert diary.status_code == 201
    assert diary.json()["report_type"] == "diary"
    assert diary.json()["author_id"] == editor.id
    assert report.status_code == 201

    only_reports = client.get(
        f"/staff/workers/{worker['id']}/reports",
        params={"report_type": "report"},
        headers=auth,
    )
    assert [item["id"] for item in only_reports.json()] == [report.json()["id"]]


def test_invoices_belong_to_external_staff_only(
    client,
    make_user,
    staff_organization,
    grant_permissions,
    superuser,
):
    super_auth = headers_for(superuser)
    payroll = create_worker(client, super_auth, staff_organization.id)
    external = create_worker(
        client,
        super_auth,
        staff_organization.id,
        contract_type="external",
        bills_invoices=True,
    )

    editor = make_user()
    grant_permissions(editor, staff_organization, ["staff.edit"])
    auth = headers_for(editor)

    denied = client.post(
        f"/staff/workers/{payroll['id']}/invoices",
        json={
            "issued_on": "2026-07-31",
            "concept": "Asistencia técnica de julio",
            "hours": "12.50",
            "amount": "625.00",
        },
        headers=auth,
    )
    accepted = client.post(
        f"/staff/workers/{external['id']}/invoices",
        json={
            "issued_on": "2026-07-31",
            "concept": "Asistencia técnica de julio",
            "hours": "12.50",
            "amount": "625.00",
        },
        headers=auth,
    )

    assert denied.status_code == 409
    assert denied.json()["detail"] == "Staff worker does not bill invoices"
    assert accepted.status_code == 201
    assert accepted.json()["organization_id"] == staff_organization.id

    listed = client.get(f"/staff/workers/{external['id']}/invoices", headers=auth)
    assert [item["id"] for item in listed.json()] == [accepted.json()["id"]]


def test_workers_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    super_auth = headers_for(superuser)
    own = create_worker(client, super_auth, target.id)
    foreign = create_worker(client, super_auth, other.id)

    manager = make_user()
    grant_permissions(manager, target, ["staff.manage"])
    auth = headers_for(manager)

    listed = client.get(
        "/staff/workers",
        params={"organization_id": target.id},
        headers=auth,
    )
    foreign_detail = client.get(f"/staff/workers/{foreign['id']}", headers=auth)
    foreign_absences = client.get(
        f"/staff/workers/{foreign['id']}/absences",
        headers=auth,
    )

    assert [item["id"] for item in listed.json()] == [own["id"]]
    assert foreign_detail.status_code == 403
    assert foreign_absences.status_code == 403


def test_paused_organization_keeps_staff_read_only(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    organization = make_organization(name=f"Pausada {unique_suffix()}")
    worker = create_worker(client, headers_for(superuser), organization.id)
    organization.status = "paused"
    db.commit()

    manager = make_user()
    grant_permissions(manager, organization, ["staff.manage"])
    auth = headers_for(manager)

    listed = client.get(
        "/staff/workers",
        params={"organization_id": organization.id},
        headers=auth,
    )
    denied_update = client.patch(
        f"/staff/workers/{worker['id']}",
        json={"full_name": "Cambio en organización pausada"},
        headers=auth,
    )
    denied_absence = client.post(
        f"/staff/workers/{worker['id']}/absences",
        json={
            "absence_type": "vacation",
            "start_date": "2026-08-01",
            "end_date": "2026-08-15",
        },
        headers=auth,
    )

    assert listed.status_code == 200
    assert denied_update.status_code == 409
    assert denied_absence.status_code == 409


def test_workers_can_be_searched_and_filtered_by_post(
    client,
    make_user,
    staff_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    auth = headers_for(manager)

    post = create_post(client, auth, staff_organization.id, label="Alguacil")
    holder = create_worker(
        client,
        auth,
        staff_organization.id,
        full_name="Marina Alonso Ruiz",
        post_id=post["id"],
    )
    create_worker(client, auth, staff_organization.id, full_name="Julio Vega Sanz")

    searched = client.get(
        "/staff/workers",
        params={"organization_id": staff_organization.id, "q": "alonso"},
        headers=auth,
    )
    by_post = client.get(
        "/staff/workers",
        params={"organization_id": staff_organization.id, "post_id": post["id"]},
        headers=auth,
    )
    everyone = client.get(
        "/staff/workers",
        params={"organization_id": staff_organization.id},
        headers=auth,
    )

    assert [item["id"] for item in searched.json()] == [holder["id"]]
    assert [item["id"] for item in by_post.json()] == [holder["id"]]
    assert everyone.headers["X-Total-Count"] == "2"


def test_unknown_worker_and_organization_are_reported_as_missing(
    client,
    make_user,
    staff_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, staff_organization, ["staff.manage"])
    auth = headers_for(manager)

    missing_worker = client.get("/staff/workers/999999", headers=auth)
    missing_organization = client.get(
        "/staff/workers",
        params={"organization_id": 999999},
        headers=auth,
    )

    assert missing_worker.status_code == 404
    assert missing_organization.status_code == 404
