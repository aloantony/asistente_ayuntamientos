from app.town_hall.schemas import SECTION_LAYOUTS
from app.town_hall.seed import INITIAL_TOWN_HALL_STRUCTURE
from tests.conftest import headers_for


def seed(client, headers, organization_id=None):
    params = {} if organization_id is None else {"organization_id": organization_id}
    return client.post("/town-hall/structure/seed", params=params, headers=headers)


def read_nav(client, headers, organization_id=None):
    params = {} if organization_id is None else {"organization_id": organization_id}
    response = client.get("/town-hall", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["nav"]


def seeded_epigraphs():
    for tab in INITIAL_TOWN_HALL_STRUCTURE:
        for epigraph in tab["epigraphs"]:
            yield tab, epigraph


def seeded_sections():
    for tab, epigraph in seeded_epigraphs():
        for key, title, layout in epigraph["sections"]:
            yield tab, epigraph, key, title, layout


def expected_tab_count():
    return len(INITIAL_TOWN_HALL_STRUCTURE)


def expected_epigraph_count():
    return sum(1 for _ in seeded_epigraphs())


def expected_section_count():
    return sum(1 for _ in seeded_sections())


def find_epigraph(nav, title):
    return next(
        epigraph
        for section in nav
        for epigraph in section["epigraphs"]
        if epigraph["title"] == title
    )


def test_every_seeded_layout_is_one_the_api_accepts():
    """Un formato inventado se guardaría y luego se leería como `text`."""
    for _, _, _, title, layout in seeded_sections():
        assert layout in SECTION_LAYOUTS, title


def test_seed_keys_are_unique():
    keys = []
    for tab in INITIAL_TOWN_HALL_STRUCTURE:
        keys.append(tab["key"])
        for epigraph in tab["epigraphs"]:
            epigraph_key = f"{tab['key']}/{epigraph['key']}"
            keys.append(epigraph_key)
            keys.extend(
                f"{epigraph_key}/{key}" for key, _, _ in epigraph["sections"]
            )

    # La clave es lo que hace idempotente al seed: repetida, dejaría de serlo.
    assert len(keys) == len(set(keys))


def test_normativa_is_left_out_on_purpose():
    """La biblioteca de ordenanzas ya cubre ese epígrafe del diseño (fase B6)."""
    titles = {title.casefold() for _, _, _, title, _ in seeded_sections()}
    titles |= {
        str(epigraph["title"]).casefold() for _, epigraph in seeded_epigraphs()
    }
    titles |= {str(tab["title"]).casefold() for tab in INITIAL_TOWN_HALL_STRUCTURE}

    assert not any("normativa" in title for title in titles)


def test_the_structure_has_the_four_levels_of_the_design():
    """Pestaña → epígrafe → apartado, y ningún nivel vacío (ADR-054)."""
    assert expected_tab_count() >= 1
    for tab in INITIAL_TOWN_HALL_STRUCTURE:
        assert tab["epigraphs"], tab["key"]
        for epigraph in tab["epigraphs"]:
            assert epigraph["sections"], epigraph["key"]


def test_the_seed_creates_the_whole_starting_structure(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento de Fuentelcésped")
    grant_permissions(user, organization, ["town_hall.edit", "town_hall.view"])
    auth = headers_for(user)

    response = seed(client, auth)
    nav = read_nav(client, auth)

    assert response.status_code == 201
    assert response.json()["organization_id"] == organization.id
    assert len(response.json()["created"]) == (
        expected_tab_count() + expected_epigraph_count() + expected_section_count()
    )
    # El orden de las pestañas, los epígrafes y los apartados es el del diseño.
    assert [tab["title"] for tab in nav] == [
        tab["title"] for tab in INITIAL_TOWN_HALL_STRUCTURE
    ]
    assert [epigraph["title"] for epigraph in nav[0]["epigraphs"]] == [
        epigraph["title"] for epigraph in INITIAL_TOWN_HALL_STRUCTURE[0]["epigraphs"]
    ]
    assert [item["title"] for item in nav[0]["epigraphs"][0]["items"]] == [
        title
        for _, title, _ in INITIAL_TOWN_HALL_STRUCTURE[0]["epigraphs"][0]["sections"]
    ]


def test_each_seeded_section_carries_its_layout(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento con formatos")
    grant_permissions(user, organization, ["town_hall.edit", "town_hall.view"])
    auth = headers_for(user)
    seed(client, auth)

    nav = read_nav(client, auth)
    layouts = {}
    for section in nav:
        for epigraph in section["epigraphs"]:
            for item in epigraph["items"]:
                content = client.get(
                    f"/town-hall/blocks/{item['id']}/content", headers=auth
                ).json()
                layouts[item["title"]] = content["layout"]

    expected = {title: layout for _, _, _, title, layout in seeded_sections()}
    # Sin esto el seed dejaría todo en `text` y la demografía no se dibujaría.
    assert layouts == expected


def test_the_seed_creates_no_municipal_content(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento sin datos")
    grant_permissions(user, organization, ["town_hall.edit", "town_hall.view"])
    auth = headers_for(user)
    seed(client, auth)

    nav = read_nav(client, auth)
    for section in nav:
        for epigraph in section["epigraphs"]:
            for item in epigraph["items"]:
                content = client.get(
                    f"/town-hall/blocks/{item['id']}/content", headers=auth
                ).json()
                # Los teléfonos y los concejales los pone el ayuntamiento.
                assert content["items"] == []


def test_seeding_twice_creates_nothing_new(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento repetido")
    grant_permissions(user, organization, ["town_hall.edit", "town_hall.view"])
    auth = headers_for(user)

    first = seed(client, auth)
    second = seed(client, auth)
    nav = read_nav(client, auth)

    assert len(first.json()["created"]) > 0
    assert second.json()["created"] == []
    assert len(nav) == expected_tab_count()
    assert (
        sum(len(section["epigraphs"]) for section in nav) == expected_epigraph_count()
    )
    assert (
        sum(
            len(epigraph["items"])
            for section in nav
            for epigraph in section["epigraphs"]
        )
        == expected_section_count()
    )


def test_a_renamed_epigraph_is_not_seeded_again(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento que renombra")
    grant_permissions(user, organization, ["town_hall.edit", "town_hall.view"])
    auth = headers_for(user)
    seed(client, auth)

    phones = find_epigraph(read_nav(client, auth), "Teléfonos de interés")
    client.patch(
        f"/town-hall/blocks/{phones['id']}",
        json={"title": "Teléfonos"},
        headers=auth,
    )

    second = seed(client, auth)
    after = read_nav(client, auth)
    titles = [
        epigraph["title"] for section in after for epigraph in section["epigraphs"]
    ]

    # La marca del seed sobrevive al renombrado: sin ella habría duplicado.
    assert second.json()["created"] == []
    assert "Teléfonos" in titles
    assert "Teléfonos de interés" not in titles
    assert len(after) == expected_tab_count()


def test_an_archived_epigraph_is_not_recreated(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento que archiva")
    grant_permissions(user, organization, ["town_hall.edit", "town_hall.view"])
    auth = headers_for(user)
    seed(client, auth)

    archive = find_epigraph(read_nav(client, auth), "Archivo municipal")
    client.patch(
        f"/town-hall/blocks/{archive['id']}",
        json={"status": "archived"},
        headers=auth,
    )

    second = seed(client, auth)
    after = read_nav(client, auth)
    titles = [
        epigraph["title"] for section in after for epigraph in section["epigraphs"]
    ]

    # Archivar es una decisión del municipio; el seed no la revierte.
    assert second.json()["created"] == []
    assert "Archivo municipal" not in titles


def test_a_hand_made_tab_is_completed_instead_of_duplicated(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento a mano")
    grant_permissions(user, organization, ["town_hall.edit", "town_hall.view"])
    auth = headers_for(user)

    tab_title = str(INITIAL_TOWN_HALL_STRUCTURE[0]["title"])
    created = client.post(
        "/town-hall/blocks",
        json={"block_type": "nav_section", "title": tab_title},
        headers=auth,
    ).json()

    seed(client, auth)
    nav = read_nav(client, auth)
    tabs = [section for section in nav if section["title"] == tab_title]

    assert len(tabs) == 1
    assert tabs[0]["id"] == created["id"]
    assert [epigraph["title"] for epigraph in tabs[0]["epigraphs"]] == [
        epigraph["title"] for epigraph in INITIAL_TOWN_HALL_STRUCTURE[0]["epigraphs"]
    ]


def test_seeding_requires_the_edit_permission(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento sólo de lectura")
    grant_permissions(user, organization, ["town_hall.view"])

    response = seed(client, headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: town_hall.edit"
    assert read_nav(client, headers_for(user)) == []


def test_the_seed_does_not_reach_another_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    target = make_organization(name="Ayuntamiento objetivo")
    other = make_organization(name="Ayuntamiento ajeno")
    grant_permissions(user, target, ["town_hall.edit", "town_hall.view"])
    grant_permissions(user, other, ["town_hall.view"])
    auth = headers_for(user)

    seed(client, auth, target.id)

    assert read_nav(client, auth, other.id) == []


def test_seeding_another_organization_requires_permission_there(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    own = make_organization(name="Ayuntamiento propio")
    other = make_organization(name="Ayuntamiento vecino")
    grant_permissions(user, own, ["town_hall.edit"])
    auth = headers_for(user)

    response = seed(client, auth, other.id)

    # Tener el permiso en la propia organización no abre la del vecino.
    assert response.status_code == 403
