import pytest

from app.town_hall import weather
from tests.conftest import headers_for


def create_section(client, headers, title, organization_id=None):
    params = {} if organization_id is None else {"organization_id": organization_id}
    response = client.post(
        "/town-hall/blocks",
        params=params,
        json={"block_type": "nav_section", "title": title},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_item(client, headers, parent_id, title, organization_id=None):
    params = {} if organization_id is None else {"organization_id": organization_id}
    response = client.post(
        "/town-hall/blocks",
        params=params,
        json={
            "block_type": "nav_item",
            "parent_id": parent_id,
            "title": title,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_read_returns_empty_shell_for_a_fresh_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization(name="Ayuntamiento de Fuentelcésped")
    grant_permissions(user, organization, ["town_hall.view"])

    response = client.get("/town-hall", headers=headers_for(user))

    assert response.status_code == 200
    payload = response.json()
    assert payload["organization_id"] == organization.id
    assert payload["organization_name"] == "Ayuntamiento de Fuentelcésped"
    assert payload["nav"] == []
    assert payload["profile"] == {
        "display_name": None,
        "weather_enabled": False,
        "weather_location": None,
        "has_shield": False,
    }


def test_read_requires_the_view_permission(
    client,
    make_user,
    make_organization,
    add_member,
):
    user = make_user()
    organization = make_organization()
    add_member(user, organization)

    response = client.get("/town-hall", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: town_hall.view"


def test_editing_requires_the_edit_permission(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view"])
    headers = headers_for(user)

    blocks = client.post(
        "/town-hall/blocks",
        json={"block_type": "nav_section", "title": "Información"},
        headers=headers,
    )
    profile = client.patch(
        "/town-hall/profile",
        json={"display_name": "Fuentelcésped"},
        headers=headers,
    )

    assert blocks.status_code == 403
    assert profile.status_code == 403
    assert blocks.json()["detail"] == "Permission required: town_hall.edit"


def test_manage_permission_grants_view_and_edit(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.manage"])
    headers = headers_for(user)

    assert client.get("/town-hall", headers=headers).status_code == 200
    create_section(client, headers, "Información")


def test_profile_update_persists_and_creates_the_row_once(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    first = client.patch(
        "/town-hall/profile",
        json={"display_name": "Fuentelcésped", "weather_enabled": True},
        headers=headers,
    )
    second = client.patch(
        "/town-hall/profile",
        json={"weather_location": "Fuentelcésped, Burgos"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    # El segundo PATCH no borra lo que no viaja en el cuerpo.
    assert second.json() == {
        "display_name": "Fuentelcésped",
        "weather_enabled": True,
        "weather_location": "Fuentelcésped, Burgos",
        "has_shield": False,
    }
    assert client.get("/town-hall", headers=headers).json()["profile"] == second.json()


def test_nav_tree_is_nested_and_ordered_by_position(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    first_section = create_section(client, headers, "Información")
    second_section = create_section(client, headers, "Administración")
    item_a = create_item(client, headers, first_section["id"], "Historia")
    item_b = create_item(client, headers, first_section["id"], "Fiestas")

    assert first_section["position"] == 0
    assert second_section["position"] == 1
    assert item_a["position"] == 0
    assert item_b["position"] == 1

    nav = client.get("/town-hall", headers=headers).json()["nav"]
    assert [section["title"] for section in nav] == ["Información", "Administración"]
    assert [item["title"] for item in nav[0]["items"]] == ["Historia", "Fiestas"]
    assert nav[1]["items"] == []


def test_reorder_moves_items_between_sections(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    first_section = create_section(client, headers, "Información")
    second_section = create_section(client, headers, "Administración")
    item = create_item(client, headers, first_section["id"], "Historia")

    response = client.post(
        "/town-hall/blocks/reorder",
        json={
            "placements": [
                {"id": second_section["id"], "position": 0},
                {"id": first_section["id"], "position": 1},
                {"id": item["id"], "parent_id": second_section["id"], "position": 0},
            ]
        },
        headers=headers,
    )

    assert response.status_code == 200
    nav = client.get("/town-hall", headers=headers).json()["nav"]
    assert [section["title"] for section in nav] == ["Administración", "Información"]
    assert [item["title"] for item in nav[0]["items"]] == ["Historia"]
    assert nav[1]["items"] == []


def test_archiving_a_section_hides_its_items(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Información")
    create_item(client, headers, section["id"], "Historia")

    response = client.patch(
        f"/town-hall/blocks/{section['id']}",
        json={"status": "archived"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    assert client.get("/town-hall", headers=headers).json()["nav"] == []


def test_rename_updates_the_title(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Informacion")
    response = client.patch(
        f"/town-hall/blocks/{section['id']}",
        json={"title": "Información general"},
        headers=headers,
    )

    assert response.status_code == 200
    nav = client.get("/town-hall", headers=headers).json()["nav"]
    assert nav[0]["title"] == "Información general"


def test_navigation_items_require_a_section_of_the_same_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    other_user = make_user()
    organization = make_organization()
    other_organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    grant_permissions(
        other_user,
        other_organization,
        ["town_hall.view", "town_hall.edit"],
    )
    foreign_section = create_section(client, headers_for(other_user), "Ajena")
    headers = headers_for(user)

    orphan = client.post(
        "/town-hall/blocks",
        json={"block_type": "nav_item", "title": "Historia"},
        headers=headers,
    )
    foreign = client.post(
        "/town-hall/blocks",
        json={
            "block_type": "nav_item",
            "parent_id": foreign_section["id"],
            "title": "Historia",
        },
        headers=headers,
    )
    parented_section = client.post(
        "/town-hall/blocks",
        json={
            "block_type": "nav_section",
            "parent_id": foreign_section["id"],
            "title": "Anidada",
        },
        headers=headers,
    )

    assert orphan.status_code == 422
    assert foreign.status_code == 422
    assert parented_section.status_code == 422


def test_blocks_are_isolated_between_organizations(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    other_user = make_user()
    organization = make_organization()
    other_organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    grant_permissions(
        other_user,
        other_organization,
        ["town_hall.view", "town_hall.edit"],
    )

    own_section = create_section(client, headers_for(user), "Propia")
    foreign_section = create_section(client, headers_for(other_user), "Ajena")
    headers = headers_for(user)

    nav = client.get("/town-hall", headers=headers).json()["nav"]
    rename = client.patch(
        f"/town-hall/blocks/{foreign_section['id']}",
        json={"title": "Secuestrada"},
        headers=headers,
    )
    reorder = client.post(
        "/town-hall/blocks/reorder",
        json={"placements": [{"id": foreign_section["id"], "position": 5}]},
        headers=headers,
    )

    assert [section["id"] for section in nav] == [own_section["id"]]
    assert rename.status_code == 403
    assert reorder.status_code == 404


def test_reading_another_organization_requires_permission_there(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    other_organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])

    response = client.get(
        "/town-hall",
        params={"organization_id": other_organization.id},
        headers=headers_for(user),
    )

    assert response.status_code == 403


def test_superuser_must_name_the_organization(
    client,
    superuser,
    make_organization,
):
    organization = make_organization()
    headers = headers_for(superuser)

    without_organization = client.get("/town-hall", headers=headers)
    with_organization = client.get(
        "/town-hall",
        params={"organization_id": organization.id},
        headers=headers,
    )

    assert without_organization.status_code == 400
    assert without_organization.json()["detail"] == "organization_id is required"
    assert with_organization.status_code == 200


def test_town_hall_requires_authentication(client):
    assert client.get("/town-hall").status_code == 401


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def test_shield_round_trip(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    assert client.get("/town-hall/shield", headers=headers).status_code == 404

    upload = client.post(
        "/town-hall/shield",
        files={"file": ("escudo.png", PNG_BYTES, "image/png")},
        headers=headers,
    )
    download = client.get("/town-hall/shield", headers=headers)

    assert upload.status_code == 200
    assert upload.json()["has_shield"] is True
    assert download.status_code == 200
    assert download.content == PNG_BYTES
    assert download.headers["content-type"].startswith("image/png")
    assert client.get("/town-hall", headers=headers).json()["profile"][
        "has_shield"
    ] is True


def test_shield_is_served_as_an_attachment(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    """Un SVG servido inline se ejecutaría en el origen de la API (ADR-032)."""
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    upload = client.post(
        "/town-hall/shield",
        # Nombre original hostil: no debe llegar a la cabecera de la descarga.
        files={"file": ('x";evil.html', svg, "image/svg+xml")},
        headers=headers,
    )
    download = client.get("/town-hall/shield", headers=headers)

    assert upload.status_code == 200
    assert download.status_code == 200
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "evil.html" not in disposition
    assert 'filename="escudo.svg"' in disposition


def test_shield_upload_requires_the_edit_permission(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view"])

    response = client.post(
        "/town-hall/shield",
        files={"file": ("escudo.png", PNG_BYTES, "image/png")},
        headers=headers_for(user),
    )

    assert response.status_code == 403


def test_shield_rejects_other_content_types_and_empty_files(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    pdf = client.post(
        "/town-hall/shield",
        files={"file": ("escudo.pdf", b"%PDF-1.4", "application/pdf")},
        headers=headers,
    )
    empty = client.post(
        "/town-hall/shield",
        files={"file": ("escudo.png", b"", "image/png")},
        headers=headers,
    )

    assert pdf.status_code == 415
    assert empty.status_code == 400
    assert client.get("/town-hall/shield", headers=headers).status_code == 404


def test_shield_is_isolated_between_organizations(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    other_user = make_user()
    organization = make_organization()
    other_organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    grant_permissions(
        other_user,
        other_organization,
        ["town_hall.view", "town_hall.edit"],
    )
    client.post(
        "/town-hall/shield",
        files={"file": ("escudo.png", PNG_BYTES, "image/png")},
        headers=headers_for(other_user),
    )
    headers = headers_for(user)

    own = client.get("/town-hall/shield", headers=headers)
    foreign = client.get(
        "/town-hall/shield",
        params={"organization_id": other_organization.id},
        headers=headers,
    )

    assert own.status_code == 404
    assert foreign.status_code == 403


@pytest.fixture(autouse=True)
def clear_weather_cache():
    """La caché de temperatura es de proceso: no debe cruzarse entre tests."""
    weather._temperature_cache.clear()
    yield
    weather._temperature_cache.clear()


def enable_weather(client, headers, location="Fuentelcésped, Burgos"):
    response = client.patch(
        "/town-hall/profile",
        json={"weather_enabled": True, "weather_location": location},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_weather_is_absent_until_the_block_is_enabled(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])

    response = client.get("/town-hall/weather", headers=headers_for(user))

    assert response.status_code == 404
    assert response.json()["detail"] == "Weather block is disabled"


def test_weather_geocodes_once_and_then_caches(
    client,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)
    enable_weather(client, headers)

    geocoded: list[str] = []
    measured: list[weather.Coordinates] = []

    def fake_geocode(place_name):
        geocoded.append(place_name)
        return weather.Coordinates(latitude=41.65, longitude=-3.66)

    def fake_fetch(coordinates):
        measured.append(coordinates)
        return 12.4

    monkeypatch.setattr("app.town_hall.routes.weather.geocode", fake_geocode)
    monkeypatch.setattr("app.town_hall.routes.weather.fetch_temperature", fake_fetch)

    first = client.get("/town-hall/weather", headers=headers)
    second = client.get("/town-hall/weather", headers=headers)

    assert first.status_code == 200
    assert first.json() == {
        "temperature_celsius": 12.4,
        "location": "Fuentelcésped, Burgos",
    }
    assert second.json() == first.json()
    # Una sola llamada externa de cada tipo: la segunda lectura sale de la caché.
    assert geocoded == ["Fuentelcésped, Burgos"]
    assert len(measured) == 1

    # Las coordenadas quedan guardadas, así que ya no se vuelve a geocodificar.
    weather.forget_cached_temperature(organization.id)
    client.get("/town-hall/weather", headers=headers)
    assert geocoded == ["Fuentelcésped, Burgos"]
    assert len(measured) == 2


def test_changing_the_location_forgets_the_coordinates(
    client,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)
    enable_weather(client, headers)

    geocoded: list[str] = []
    monkeypatch.setattr(
        "app.town_hall.routes.weather.geocode",
        lambda place_name: (
            geocoded.append(place_name),
            weather.Coordinates(latitude=41.65, longitude=-3.66),
        )[1],
    )
    monkeypatch.setattr(
        "app.town_hall.routes.weather.fetch_temperature",
        lambda coordinates: 12.4,
    )

    client.get("/town-hall/weather", headers=headers)
    enable_weather(client, headers, location="Aranda de Duero, Burgos")
    response = client.get("/town-hall/weather", headers=headers)

    assert response.status_code == 200
    assert response.json()["location"] == "Aranda de Duero, Burgos"
    assert geocoded == ["Fuentelcésped, Burgos", "Aranda de Duero, Burgos"]


def test_weather_reports_the_provider_being_unavailable(
    client,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)
    enable_weather(client, headers)

    def fail(*_args, **_kwargs):
        raise weather.WeatherUnavailableError("Weather provider unavailable")

    monkeypatch.setattr("app.town_hall.routes.weather.geocode", fail)

    response = client.get("/town-hall/weather", headers=headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "Weather provider unavailable"


def test_weather_requires_the_view_permission(
    client,
    make_user,
    make_organization,
    add_member,
):
    user = make_user()
    organization = make_organization()
    add_member(user, organization)

    response = client.get("/town-hall/weather", headers=headers_for(user))

    assert response.status_code == 403


def test_geocoding_drops_the_province_suffix():
    # Open-Meteo no entiende «Municipio, Provincia»: se consulta solo el
    # topónimo, aunque la etiqueta que ve el usuario conserve la provincia.
    assert weather.normalize_place_name("Fuentelcésped, Burgos") == "Fuentelcésped"
    assert weather.normalize_place_name("  Aranda de Duero  ") == "Aranda de Duero"
    assert weather.normalize_place_name(", Burgos") == ""


def create_content_item(client, headers, parent_id, title):
    response = client.post(
        "/town-hall/blocks",
        json={"block_type": "item", "parent_id": parent_id, "title": title},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_content_items_hang_from_a_navigation_item(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Información general")
    apartado = create_item(client, headers, section["id"], "Historia")
    first = create_content_item(client, headers, apartado["id"], "Orígenes")
    create_content_item(client, headers, apartado["id"], "Siglo XX")

    client.patch(
        f"/town-hall/blocks/{first['id']}",
        json={"body": "  Fundado en el siglo XII.  "},
        headers=headers,
    )
    content = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    )

    assert content.status_code == 200
    payload = content.json()
    assert payload["title"] == "Historia"
    assert payload["parent_title"] == "Información general"
    assert [item["title"] for item in payload["items"]] == ["Orígenes", "Siglo XX"]
    # El cuerpo se guarda recortado.
    assert payload["items"][0]["body"] == "Fundado en el siglo XII."
    assert payload["items"][1]["body"] is None


def test_content_hierarchy_is_enforced(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Información general")
    apartado = create_item(client, headers, section["id"], "Historia")

    # Un elemento no puede colgar del epígrafe, solo del apartado.
    on_section = client.post(
        "/town-hall/blocks",
        json={"block_type": "item", "parent_id": section["id"], "title": "x"},
        headers=headers,
    )
    orphan = client.post(
        "/town-hall/blocks",
        json={"block_type": "item", "title": "x"},
        headers=headers,
    )
    # Un apartado tampoco puede colgar de un elemento.
    leaf = create_content_item(client, headers, apartado["id"], "Orígenes")
    under_leaf = client.post(
        "/town-hall/blocks",
        json={"block_type": "nav_item", "parent_id": leaf["id"], "title": "x"},
        headers=headers,
    )

    assert on_section.status_code == 422
    assert orphan.status_code == 422
    assert under_leaf.status_code == 422


def test_only_content_items_carry_a_body(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Información general")

    response = client.patch(
        f"/town-hall/blocks/{section['id']}",
        json={"body": "texto"},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Only content items carry a body"


def test_archiving_cascades_through_the_whole_branch(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Información general")
    apartado = create_item(client, headers, section["id"], "Historia")
    create_content_item(client, headers, apartado["id"], "Orígenes")

    client.patch(
        f"/town-hall/blocks/{section['id']}",
        json={"status": "archived"},
        headers=headers,
    )

    assert client.get("/town-hall", headers=headers).json()["nav"] == []
    # El apartado archivado ya no expone contenido.
    assert (
        client.get(
            f"/town-hall/blocks/{apartado['id']}/content",
            headers=headers,
        ).status_code
        == 404
    )


def test_content_is_isolated_between_organizations(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    other_user = make_user()
    organization = make_organization()
    other_organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    grant_permissions(
        other_user,
        other_organization,
        ["town_hall.view", "town_hall.edit"],
    )
    foreign_section = create_section(client, headers_for(other_user), "Ajena")
    foreign_item = create_item(
        client,
        headers_for(other_user),
        foreign_section["id"],
        "Historia",
    )

    response = client.get(
        f"/town-hall/blocks/{foreign_item['id']}/content",
        headers=headers_for(user),
    )

    assert response.status_code == 403


def test_sections_carry_a_layout(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Teléfonos")
    apartado = create_item(client, headers, section["id"], "Servicios")

    default_layout = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    ).json()["layout"]

    client.patch(
        f"/town-hall/blocks/{apartado['id']}",
        json={"layout": "contacts"},
        headers=headers,
    )
    content = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    ).json()

    assert default_layout == "text"
    assert content["layout"] == "contacts"


def test_layout_survives_a_rename(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Teléfonos")
    apartado = create_item(client, headers, section["id"], "Servicios")
    client.patch(
        f"/town-hall/blocks/{apartado['id']}",
        json={"layout": "contacts"},
        headers=headers,
    )
    client.patch(
        f"/town-hall/blocks/{apartado['id']}",
        json={"title": "Servicios de emergencia"},
        headers=headers,
    )

    content = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    ).json()

    assert content["title"] == "Servicios de emergencia"
    assert content["layout"] == "contacts"


def test_only_sections_carry_a_layout(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Teléfonos")

    response = client.patch(
        f"/town-hall/blocks/{section['id']}",
        json={"layout": "contacts"},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Only sections carry a layout"


def test_unknown_layout_is_rejected_and_broken_data_falls_back(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    from app.town_hall.models import MunicipalBlock

    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Teléfonos")
    apartado = create_item(client, headers, section["id"], "Servicios")

    rejected = client.patch(
        f"/town-hall/blocks/{apartado['id']}",
        json={"layout": "carrusel"},
        headers=headers,
    )

    # Un data_json corrupto no debe romper la pantalla: se cae a `text`.
    stored = db.get(MunicipalBlock, apartado["id"])
    stored.data_json = "{no es json"
    db.commit()
    content = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    ).json()

    assert rejected.status_code == 422
    assert content["layout"] == "text"


def test_people_sections_carry_free_fields(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Corporación")
    apartado = create_item(client, headers, section["id"], "Pleno")
    client.patch(
        f"/town-hall/blocks/{apartado['id']}",
        json={"layout": "people"},
        headers=headers,
    )
    persona = create_content_item(client, headers, apartado["id"], "Ana Ruiz")
    client.patch(
        f"/town-hall/blocks/{persona['id']}",
        json={
            "fields": [
                {"label": "Cargo", "value": "  Alcaldesa  "},
                {"label": "Partido", "value": "Independiente"},
            ]
        },
        headers=headers,
    )

    content = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    ).json()

    assert content["layout"] == "people"
    assert content["items"][0]["title"] == "Ana Ruiz"
    assert content["items"][0]["fields"] == [
        {"label": "Cargo", "value": "Alcaldesa"},
        {"label": "Partido", "value": "Independiente"},
    ]


def test_fields_replace_wholesale_and_survive_a_rename(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Corporación")
    apartado = create_item(client, headers, section["id"], "Pleno")
    persona = create_content_item(client, headers, apartado["id"], "Ana Ruiz")
    client.patch(
        f"/town-hall/blocks/{persona['id']}",
        json={"fields": [{"label": "Cargo", "value": "Alcaldesa"}]},
        headers=headers,
    )
    client.patch(
        f"/town-hall/blocks/{persona['id']}",
        json={"title": "Ana Ruiz Pérez"},
        headers=headers,
    )
    # Enviar la lista completa es lo que sustituye: así se borra un campo.
    client.patch(
        f"/town-hall/blocks/{persona['id']}",
        json={"fields": []},
        headers=headers,
    )

    content = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    ).json()

    assert content["items"][0]["title"] == "Ana Ruiz Pérez"
    assert content["items"][0]["fields"] == []


def test_only_content_items_carry_fields(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Corporación")
    apartado = create_item(client, headers, section["id"], "Pleno")

    response = client.patch(
        f"/town-hall/blocks/{apartado['id']}",
        json={"fields": [{"label": "Cargo", "value": "Alcaldesa"}]},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Only content items carry fields"


def test_malformed_fields_are_dropped_not_served(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    from app.town_hall.models import MunicipalBlock

    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Corporación")
    apartado = create_item(client, headers, section["id"], "Pleno")
    persona = create_content_item(client, headers, apartado["id"], "Ana Ruiz")

    # Un campo sin etiqueta, uno que no es objeto y uno correcto.
    stored = db.get(MunicipalBlock, persona["id"])
    stored.data_json = (
        '{"fields": [{"label": "", "value": "x"}, "suelto",'
        ' {"label": "Cargo", "value": "Alcaldesa"}]}'
    )
    db.commit()

    content = client.get(
        f"/town-hall/blocks/{apartado['id']}/content",
        headers=headers,
    ).json()

    assert content["items"][0]["fields"] == [
        {"label": "Cargo", "value": "Alcaldesa"}
    ]


def test_too_many_fields_are_rejected(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    section = create_section(client, headers, "Corporación")
    apartado = create_item(client, headers, section["id"], "Pleno")
    persona = create_content_item(client, headers, apartado["id"], "Ana Ruiz")

    response = client.patch(
        f"/town-hall/blocks/{persona['id']}",
        json={
            "fields": [
                {"label": f"Campo {index}", "value": "x"} for index in range(21)
            ]
        },
        headers=headers,
    )

    assert response.status_code == 422


JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32


def make_archive_section(client, headers):
    section = create_section(client, headers, "Archivo")
    apartado = create_item(client, headers, section["id"], "Fototeca")
    client.patch(
        f"/town-hall/blocks/{apartado['id']}",
        json={"layout": "files"},
        headers=headers,
    )
    return apartado


def test_attachment_round_trip(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    apartado = make_archive_section(client, headers)
    foto = create_content_item(client, headers, apartado["id"], "Plaza Mayor, 1965")

    upload = client.post(
        f"/town-hall/blocks/{foto['id']}/attachments",
        files={"file": ("plaza.jpg", JPEG_BYTES, "image/jpeg")},
        headers=headers,
    )
    download = client.get(
        f"/town-hall/blocks/{foto['id']}/attachments/0",
        headers=headers,
    )

    assert upload.status_code == 200
    attachment = upload.json()["items"][0]["attachments"][0]
    assert attachment["name"] == "plaza.jpg"
    assert attachment["content_type"] == "image/jpeg"
    assert attachment["size_bytes"] == len(JPEG_BYTES)
    # La clave de almacenamiento nunca sale al cliente.
    assert "storage_key" not in attachment
    assert download.status_code == 200
    assert download.content == JPEG_BYTES
    # Siempre como descarga, nunca inline (ADR-032).
    assert "attachment" in download.headers["content-disposition"]


def test_attachments_reject_other_content_types(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    apartado = make_archive_section(client, headers)
    foto = create_content_item(client, headers, apartado["id"], "Plaza Mayor")

    response = client.post(
        f"/town-hall/blocks/{foto['id']}/attachments",
        files={"file": ("virus.exe", b"MZ", "application/x-msdownload")},
        headers=headers,
    )

    assert response.status_code == 415


def test_deleting_an_attachment_removes_it(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    apartado = make_archive_section(client, headers)
    foto = create_content_item(client, headers, apartado["id"], "Plaza Mayor")
    for name in ("una.jpg", "otra.jpg"):
        client.post(
            f"/town-hall/blocks/{foto['id']}/attachments",
            files={"file": (name, JPEG_BYTES, "image/jpeg")},
            headers=headers,
        )

    deleted = client.delete(
        f"/town-hall/blocks/{foto['id']}/attachments/0",
        headers=headers,
    )

    assert deleted.status_code == 200
    remaining = deleted.json()["items"][0]["attachments"]
    assert [entry["name"] for entry in remaining] == ["otra.jpg"]
    # El índice se recalcula: el que queda pasa a ser el 0.
    assert remaining[0]["index"] == 0
    assert (
        client.get(
            f"/town-hall/blocks/{foto['id']}/attachments/1",
            headers=headers,
        ).status_code
        == 404
    )


def test_attachments_require_the_edit_permission_and_own_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    reader = make_user()
    other_user = make_user()
    organization = make_organization()
    other_organization = make_organization()
    grant_permissions(reader, organization, ["town_hall.view"])
    grant_permissions(
        other_user,
        other_organization,
        ["town_hall.view", "town_hall.edit"],
    )
    foreign = make_archive_section(client, headers_for(other_user))
    foreign_item = create_content_item(
        client,
        headers_for(other_user),
        foreign["id"],
        "Ajena",
    )

    without_edit = client.post(
        f"/town-hall/blocks/{foreign_item['id']}/attachments",
        files={"file": ("x.jpg", JPEG_BYTES, "image/jpeg")},
        headers=headers_for(reader),
    )
    foreign_read = client.get(
        f"/town-hall/blocks/{foreign_item['id']}/attachments/0",
        headers=headers_for(reader),
    )

    assert without_edit.status_code == 403
    assert foreign_read.status_code == 403


def test_attachments_only_hang_from_content_items(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["town_hall.view", "town_hall.edit"])
    headers = headers_for(user)

    apartado = make_archive_section(client, headers)

    response = client.post(
        f"/town-hall/blocks/{apartado['id']}/attachments",
        files={"file": ("x.jpg", JPEG_BYTES, "image/jpeg")},
        headers=headers,
    )

    assert response.status_code == 404
