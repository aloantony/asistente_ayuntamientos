from decimal import Decimal

import pytest
from conftest import headers_for, unique_suffix


@pytest.fixture
def budget_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


def create_budget(client, auth, organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "reference_year": 2026,
        "status": "approved",
        "approved_on": "2025-12-20",
    }
    payload.update(overrides)
    response = client.post("/budgets", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


def add_line(client, auth, budget_id, **overrides):
    payload = {
        "code": f"{unique_suffix()[:6]}",
        "name": "Partida",
        "kind": "expense",
        "amount": "10000.00",
    }
    payload.update(overrides)
    response = client.post(
        f"/budgets/{budget_id}/lines", json=payload, headers=auth
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "path",
    ["/budgets?organization_id=1", "/budgets/treasury/movements?organization_id=1"],
)
def test_budgets_require_authentication(client, path):
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
    grant_permissions(user, other, ["budgets.manage"])
    add_member(user, target)
    auth = headers_for(user)

    listed = client.get("/budgets", params={"organization_id": target.id}, headers=auth)
    created = client.post(
        "/budgets",
        json={"organization_id": target.id, "reference_year": 2026},
        headers=auth,
    )

    assert listed.status_code == 403
    assert listed.json()["detail"] == "Permission required: budgets.view"
    assert created.status_code == 403


def test_leaving_draft_requires_the_approval_date(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    editor = make_user()
    grant_permissions(editor, budget_organization, ["budgets.manage"])
    auth = headers_for(editor)

    approved_without_date = client.post(
        "/budgets",
        json={
            "organization_id": budget_organization.id,
            "reference_year": 2026,
            "status": "approved",
        },
        headers=auth,
    )
    draft_with_date = client.post(
        "/budgets",
        json={
            "organization_id": budget_organization.id,
            "reference_year": 2027,
            "approved_on": "2026-12-01",
        },
        headers=auth,
    )

    assert approved_without_date.status_code == 422
    assert draft_with_date.status_code == 422


def test_one_budget_per_year_and_organization(
    client,
    make_organization,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)

    create_budget(client, auth, target.id, reference_year=2026)
    duplicate = client.post(
        "/budgets",
        json={
            "organization_id": target.id,
            "reference_year": 2026,
            "status": "approved",
            "approved_on": "2025-12-20",
        },
        headers=auth,
    )
    elsewhere = create_budget(client, auth, other.id, reference_year=2026)

    assert duplicate.status_code == 409
    assert elsewhere["reference_year"] == 2026


def test_execution_is_derived_from_lines_amendments_and_expenses(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, budget_organization, ["budgets.manage"])
    auth = headers_for(manager)
    budget = create_budget(client, auth, budget_organization.id)

    add_line(client, auth, budget["id"], kind="income", amount="50000.00")
    expense_line = add_line(client, auth, budget["id"], amount="30000.00")

    client.post(
        f"/budgets/{budget['id']}/amendments",
        json={
            "kind": "supplement",
            "status": "approved",
            "amount": "5000.00",
            "approved_on": "2026-05-01",
        },
        headers=auth,
    )
    # Una modificación en borrador es una intención: no mueve crédito.
    client.post(
        f"/budgets/{budget['id']}/amendments",
        json={"kind": "supplement", "amount": "9999.00"},
        headers=auth,
    )
    client.post(
        f"/budgets/lines/{expense_line['id']}/expenses",
        json={
            "concept": "Sustitución de luminarias",
            "amount": "12000.00",
            "incurred_on": "2026-06-15",
        },
        headers=auth,
    )

    execution = client.get(f"/budgets/{budget['id']}/execution", headers=auth).json()

    assert Decimal(execution["total_income"]) == Decimal("50000.00")
    assert Decimal(execution["total_expense"]) == Decimal("30000.00")
    assert Decimal(execution["approved_amendments"]) == Decimal("5000.00")
    assert Decimal(execution["executed_expense"]) == Decimal("12000.00")
    # 30000 + 5000 - 12000
    assert Decimal(execution["available_credit"]) == Decimal("23000.00")


def test_a_draft_budget_is_edited_not_amended(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, budget_organization, ["budgets.manage"])
    auth = headers_for(manager)
    draft = create_budget(
        client,
        auth,
        budget_organization.id,
        status="draft",
        approved_on=None,
    )

    response = client.post(
        f"/budgets/{draft['id']}/amendments",
        json={"kind": "supplement", "amount": "1000.00"},
        headers=auth,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "A draft budget is edited directly, not amended"


def test_approving_an_amendment_needs_manage(
    client,
    make_user,
    budget_organization,
    grant_permissions,
    superuser,
):
    budget = create_budget(client, headers_for(superuser), budget_organization.id)

    editor = make_user()
    grant_permissions(editor, budget_organization, ["budgets.view", "budgets.edit"])
    auth = headers_for(editor)

    draft = client.post(
        f"/budgets/{budget['id']}/amendments",
        json={"kind": "supplement", "amount": "1000.00"},
        headers=auth,
    )
    approved = client.post(
        f"/budgets/{budget['id']}/amendments",
        json={
            "kind": "supplement",
            "status": "approved",
            "amount": "1000.00",
            "approved_on": "2026-05-01",
        },
        headers=auth,
    )

    assert draft.status_code == 201
    assert approved.status_code == 403
    assert approved.json()["detail"] == "Permission required: budgets.manage"


def test_an_amendment_that_moves_nothing_is_refused(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, budget_organization, ["budgets.manage"])
    auth = headers_for(manager)
    budget = create_budget(client, auth, budget_organization.id)

    response = client.post(
        f"/budgets/{budget['id']}/amendments",
        json={"kind": "supplement", "amount": "0.00"},
        headers=auth,
    )

    assert response.status_code == 422


def test_only_expense_lines_take_expenses(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, budget_organization, ["budgets.manage"])
    auth = headers_for(manager)
    budget = create_budget(client, auth, budget_organization.id)
    income_line = add_line(client, auth, budget["id"], kind="income")

    response = client.post(
        f"/budgets/lines/{income_line['id']}/expenses",
        json={
            "concept": "Imputación imposible",
            "amount": "10.00",
            "incurred_on": "2026-06-15",
        },
        headers=auth,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Only expense lines take expenses"


def test_a_settled_budget_takes_no_new_lines(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, budget_organization, ["budgets.manage"])
    auth = headers_for(manager)
    budget = create_budget(
        client,
        auth,
        budget_organization.id,
        status="settled",
        approved_on="2025-12-20",
    )

    response = client.post(
        f"/budgets/{budget['id']}/lines",
        json={
            "code": "999",
            "name": "Tardía",
            "kind": "expense",
            "amount": "100.00",
        },
        headers=auth,
    )

    assert response.status_code == 409


def test_line_codes_are_unique_within_a_budget(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, budget_organization, ["budgets.manage"])
    auth = headers_for(manager)
    budget = create_budget(client, auth, budget_organization.id)
    add_line(client, auth, budget["id"], code="151")

    duplicate = client.post(
        f"/budgets/{budget['id']}/lines",
        json={
            "code": "151",
            "name": "Repetida",
            "kind": "expense",
            "amount": "100.00",
        },
        headers=auth,
    )

    assert duplicate.status_code == 409


def test_treasury_movements_are_independent_of_the_budget(
    client,
    make_user,
    budget_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, budget_organization, ["budgets.manage"])
    auth = headers_for(manager)

    # No hace falta presupuesto: el dinero entra y sale con su propio
    # calendario, y una factura de un año puede pagarse al siguiente.
    inflow = client.post(
        "/budgets/treasury/movements",
        json={
            "organization_id": budget_organization.id,
            "direction": "inflow",
            "concept": "Transferencia de la diputación",
            "amount": "18000.00",
            "moved_on": "2026-02-01",
        },
        headers=auth,
    )
    outflow = client.post(
        "/budgets/treasury/movements",
        json={
            "organization_id": budget_organization.id,
            "direction": "outflow",
            "concept": "Nóminas de febrero",
            "amount": "9000.00",
            "moved_on": "2026-02-28",
        },
        headers=auth,
    )
    only_outflows = client.get(
        "/budgets/treasury/movements",
        params={"organization_id": budget_organization.id, "direction": "outflow"},
        headers=auth,
    )

    assert inflow.status_code == 201
    assert outflow.status_code == 201
    assert [item["id"] for item in only_outflows.json()] == [outflow.json()["id"]]


def test_budgets_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    auth = headers_for(superuser)
    own = create_budget(client, auth, target.id)
    foreign = create_budget(client, auth, other.id)

    viewer = make_user()
    grant_permissions(viewer, target, ["budgets.view"])
    listed = client.get(
        "/budgets",
        params={"organization_id": target.id},
        headers=headers_for(viewer),
    )
    foreign_execution = client.get(
        f"/budgets/{foreign['id']}/execution",
        headers=headers_for(viewer),
    )

    assert [item["id"] for item in listed.json()] == [own["id"]]
    assert foreign_execution.status_code == 403


def test_paused_organization_keeps_budgets_read_only(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    organization = make_organization(name=f"Pausada {unique_suffix()}")
    budget = create_budget(client, headers_for(superuser), organization.id)
    organization.status = "paused"
    db.commit()

    manager = make_user()
    grant_permissions(manager, organization, ["budgets.manage"])
    auth = headers_for(manager)

    execution = client.get(f"/budgets/{budget['id']}/execution", headers=auth)
    denied = client.post(
        f"/budgets/{budget['id']}/lines",
        json={"code": "1", "name": "X", "kind": "expense", "amount": "1.00"},
        headers=auth,
    )

    assert execution.status_code == 200
    assert denied.status_code == 409
