"""Tests for municipalities (global reference data) and ordinances,
including the document-linking access rule."""

import io

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.ordinances import import_service
from app.ordinances.bop_burgos import (
    BopBurgosAnnouncement,
    is_municipal_bopbur_announcement,
    is_normative_bopbur_announcement,
    natural_municipality_name,
    parse_bop_burgos_search_results,
)
from app.ordinances.burgos_full_coverage import _FiscalOrdinanceTableParser
from app.ordinances.models import OfficialLegalSource, OrdinanceLegalChunk
from app.ordinances.seed import ensure_initial_official_legal_sources
from app.ordinances.soria_coverage import (
    _extract_candidate_sections,
    _normalize_key as normalize_soria_key,
    NORMATIVE_RE,
)
from app.projects.models import Project, project_users
from conftest import headers_for, unique_suffix


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def municipality_payload(**overrides) -> dict:
    payload = {
        "name": f"Municipio {unique_suffix()}",
        "province": "Madrid",
        "autonomous_community": "Comunidad de Madrid",
    }
    payload.update(overrides)
    return payload


def create_municipality(client, headers, **overrides) -> dict:
    response = client.post(
        "/municipalities",
        headers=headers,
        json=municipality_payload(**overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def ordinance_payload(municipality_id: int, **overrides) -> dict:
    payload = {
        "municipality_id": municipality_id,
        "title": f"Ordenanza {unique_suffix()}",
        "topic": f"tema-{unique_suffix()}",
        "ordinance_type": "ordinance",
    }
    payload.update(overrides)
    return payload


def create_ordinance(client, headers, municipality_id: int, **overrides) -> dict:
    response = client.post(
        "/ordinances",
        headers=headers,
        json=ordinance_payload(municipality_id, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_project(db: Session, organization) -> Project:
    project = Project(
        name=f"Project {unique_suffix()}",
        organization_id=organization.id,
    )
    db.add(project)
    db.commit()
    return project


def add_project_member(db: Session, project: Project, user) -> None:
    db.execute(
        insert(project_users).values(project_id=project.id, user_id=user.id)
    )
    db.commit()


def upload_document(client, headers, project_id: int) -> dict:
    """Upload a text/plain document (allowed type) and return its JSON body."""
    response = client.post(
        f"/projects/{project_id}/documents",
        headers=headers,
        files={
            "file": (
                "ordenanza.txt",
                io.BytesIO(b"texto consolidado de la ordenanza"),
                "text/plain",
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


BOP_BURGOS_SEARCH_HTML = """
<div class="views-row views-row-1">
  <div class="bopbur-top-boletin"><h1 class="title-numberdate">
    <span class="title-number"><a href="/bopbur-2025-177">núm. 177</a></span>
    <span class="title-date"><a href="/bopbur-2025-177">viernes, 19 de septiembre de 2025</a></span>
  </h1></div>
  <ul class="bopbur-categorias-anuncios"><li><h2>III. Administración Local</h2>
    <ul><li><h3>Ayuntamiento de Hoyales de Roa</h3>
      <ul><li id="bopbur-anuncio-202504362" class="bopbur-anuncio bopbur-anuncio-level-2">
        <p>Aprobación definitiva de la modificación parcial de la ordenanza fiscal reguladora de la tasa por recogida de basuras domiciliarias o residuos sólidos urbanos</p>
        <p>C.V.E.: BOPBUR-2025-04362 texto con ordenanza y basuras.</p>
        <p class="bopbur-filefield-file"><a href="http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-177/bopbur-2025-177-anuncio-202504362.pdf" type="application/pdf">Anuncio 202504362 (BOPBUR-2025-04362 - 116,65 KB)</a></p>
      </li></ul>
    </li></ul>
  </li></ul>
</div>
"""


# ---------------------------------------------------------------------------
# 1. POST /municipalities permissions
# ---------------------------------------------------------------------------


def test_create_municipality_without_permission_returns_403(
    client, make_user, make_organization, add_member
):
    user = make_user()
    add_member(user, make_organization())

    response = client.post(
        "/municipalities",
        headers=headers_for(user),
        json=municipality_payload(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: municipalities.create"


def test_create_municipality_with_permission_granted_in_any_org_returns_201(
    client, make_user, make_organization, grant_permissions
):
    # Reference data is global: a grant in any organization is enough.
    user = make_user()
    grant_permissions(user, make_organization(), ["municipalities.create"])

    response = client.post(
        "/municipalities",
        headers=headers_for(user),
        json=municipality_payload(),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "active"


# ---------------------------------------------------------------------------
# 2. Density is computed server-side
# ---------------------------------------------------------------------------


def test_create_municipality_computes_density_overriding_client_value(
    client, superuser
):
    body = create_municipality(
        client,
        headers_for(superuser),
        population=10000,
        surface_km2=40.0,
        density=1.0,  # client-supplied value must be ignored
    )

    assert body["density"] == 10000 / 40.0


# ---------------------------------------------------------------------------
# 3. Archiving a municipality needs municipalities.archive
# ---------------------------------------------------------------------------


def test_archive_municipality_with_only_edit_permission_returns_403(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    editor = make_user()
    grant_permissions(editor, make_organization(), ["municipalities.edit"])

    response = client.patch(
        f"/municipalities/{municipality['id']}",
        headers=headers_for(editor),
        json={"status": "archived"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: municipalities.archive"


def test_archive_municipality_with_archive_permission_succeeds(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    archiver = make_user()
    grant_permissions(archiver, make_organization(), ["municipalities.archive"])

    response = client.patch(
        f"/municipalities/{municipality['id']}",
        headers=headers_for(archiver),
        json={"status": "archived"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


# ---------------------------------------------------------------------------
# 4. Listing hides archived municipalities unless include_archived=true
# ---------------------------------------------------------------------------


def test_list_municipalities_hides_archived_by_default(client, superuser):
    headers = headers_for(superuser)
    active = create_municipality(client, headers)
    archived = create_municipality(client, headers, status="archived")

    response = client.get("/municipalities", headers=headers)

    assert response.status_code == 200
    listed_ids = [m["id"] for m in response.json()]
    assert active["id"] in listed_ids
    assert archived["id"] not in listed_ids


def test_list_municipalities_shows_archived_with_include_archived(client, superuser):
    headers = headers_for(superuser)
    active = create_municipality(client, headers)
    archived = create_municipality(client, headers, status="archived")

    response = client.get(
        "/municipalities",
        headers=headers,
        params={"include_archived": "true"},
    )

    assert response.status_code == 200
    listed_ids = [m["id"] for m in response.json()]
    assert active["id"] in listed_ids
    assert archived["id"] in listed_ids


# ---------------------------------------------------------------------------
# 5. POST /ordinances: permission + municipality validation
# ---------------------------------------------------------------------------


def test_create_ordinance_without_permission_returns_403(
    client, superuser, make_user, make_organization, add_member
):
    municipality = create_municipality(client, headers_for(superuser))
    user = make_user()
    add_member(user, make_organization())

    response = client.post(
        "/ordinances",
        headers=headers_for(user),
        json=ordinance_payload(municipality["id"]),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.create"


def test_create_ordinance_for_archived_municipality_returns_409(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(
        client, headers_for(superuser), status="archived"
    )
    creator = make_user()
    grant_permissions(creator, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(municipality["id"]),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Municipality is archived"


def test_create_ordinance_for_nonexistent_municipality_returns_404(
    client, make_user, make_organization, grant_permissions
):
    creator = make_user()
    grant_permissions(creator, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(99999999),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Municipality not found"


def test_create_ordinance_with_status_archived_requires_archive_permission(
    client, superuser, make_user, make_organization, grant_permissions
):
    # Discovered behavior: creating directly in archived status also needs
    # ordinances.archive, not just ordinances.create.
    municipality = create_municipality(client, headers_for(superuser))
    creator = make_user()
    grant_permissions(creator, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(municipality["id"], status="archived"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.archive"


# ---------------------------------------------------------------------------
# 6-7. Document linking on create
# ---------------------------------------------------------------------------


def test_create_ordinance_with_inaccessible_document_returns_404(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    # Document lives in org A; the creator only belongs to org B, so the
    # document must be hidden behind a 404 even though it exists.
    org_a = make_organization()
    project = make_project(db, org_a)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))

    outsider = make_user()
    grant_permissions(outsider, make_organization(), ["ordinances.create"])

    response = client.post(
        "/ordinances",
        headers=headers_for(outsider),
        json=ordinance_payload(municipality["id"], document_id=document["id"]),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"


def test_create_ordinance_with_accessible_document_embeds_summary(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))

    member = make_user()
    grant_permissions(member, organization, ["ordinances.create", "documents.view"])
    add_project_member(db, project, member)

    response = client.post(
        "/ordinances",
        headers=headers_for(member),
        json=ordinance_payload(municipality["id"], document_id=document["id"]),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["document_id"] == document["id"]
    assert body["document"]["id"] == document["id"]
    assert body["document"]["original_filename"] == "ordenanza.txt"
    assert body["document"]["content_type"] == "text/plain"
    assert body["document"]["status"] == "active"


def test_superuser_can_create_ordinance_with_document(
    client, db, superuser, make_organization
):
    organization = make_organization()
    project = make_project(db, organization)
    headers = headers_for(superuser)
    document = upload_document(client, headers, project.id)
    municipality = create_municipality(client, headers)

    body = create_ordinance(
        client, headers, municipality["id"], document_id=document["id"]
    )

    assert body["document"]["id"] == document["id"]


# ---------------------------------------------------------------------------
# 8. Document linking on PATCH
# ---------------------------------------------------------------------------


def test_patch_ordinance_with_inaccessible_document_returns_404(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    org_a = make_organization()
    project = make_project(db, org_a)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])

    outsider = make_user()
    grant_permissions(outsider, make_organization(), ["ordinances.edit"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(outsider),
        json={"document_id": document["id"]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"


def test_patch_ordinance_with_accessible_document_embeds_summary(
    client, db, superuser, make_user, make_organization, grant_permissions
):
    organization = make_organization()
    project = make_project(db, organization)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])

    member = make_user()
    grant_permissions(member, organization, ["ordinances.edit", "documents.view"])
    add_project_member(db, project, member)

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(member),
        json={"document_id": document["id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == document["id"]
    # Specified behavior: like on create, the response embeds the summary of
    # the newly linked document. The implementation returns a stale value
    # because the already-loaded `document` relationship is not refreshed
    # after commit (expire_on_commit=False + identity map reuse).
    assert body["document"] is not None, (
        "PATCH response should embed the newly linked document summary"
    )
    assert body["document"]["id"] == document["id"]
    assert body["document"]["original_filename"] == "ordenanza.txt"


# ---------------------------------------------------------------------------
# 9. PATCH /ordinances permissions: archive vs content edits
# ---------------------------------------------------------------------------


def test_archive_ordinance_with_only_edit_permission_returns_403(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    editor = make_user()
    grant_permissions(editor, make_organization(), ["ordinances.edit"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(editor),
        json={"status": "archived"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.archive"


def test_archive_ordinance_with_archive_permission_succeeds(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    archiver = make_user()
    grant_permissions(archiver, make_organization(), ["ordinances.archive"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(archiver),
        json={"status": "archived"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"


def test_edit_ordinance_content_without_edit_permission_returns_403(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    archiver = make_user()
    grant_permissions(archiver, make_organization(), ["ordinances.archive"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(archiver),
        json={"title": "Título modificado"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: ordinances.edit"


def test_edit_ordinance_content_with_edit_permission_succeeds(
    client, superuser, make_user, make_organization, grant_permissions
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    editor = make_user()
    grant_permissions(editor, make_organization(), ["ordinances.edit"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(editor),
        json={"title": "Ordenanza reguladora actualizada"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Ordenanza reguladora actualizada"
    assert body["updated_by_id"] == editor.id


# ---------------------------------------------------------------------------
# 10. GET /ordinances filters
# ---------------------------------------------------------------------------


def test_list_ordinances_q_filters_by_title(client, superuser):
    headers = headers_for(superuser)
    municipality = create_municipality(client, headers)
    token = unique_suffix()
    match = create_ordinance(
        client, headers, municipality["id"], title=f"Ruidos {token}"
    )
    other = create_ordinance(client, headers, municipality["id"])

    response = client.get("/ordinances", headers=headers, params={"q": token})

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids


def test_list_ordinances_q_filters_by_topic(client, superuser):
    headers = headers_for(superuser)
    municipality = create_municipality(client, headers)
    token = unique_suffix()
    match = create_ordinance(
        client, headers, municipality["id"], topic=f"residuos-{token}"
    )
    other = create_ordinance(client, headers, municipality["id"])

    response = client.get("/ordinances", headers=headers, params={"q": token})

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids


def test_list_ordinances_q_filters_by_municipality_name(client, superuser):
    headers = headers_for(superuser)
    token = unique_suffix()
    named = create_municipality(client, headers, name=f"Villanueva {token}")
    other_muni = create_municipality(client, headers)
    match = create_ordinance(client, headers, named["id"])
    other = create_ordinance(client, headers, other_muni["id"])

    response = client.get("/ordinances", headers=headers, params={"q": token})

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids


def test_list_ordinances_filters_by_municipality_id(client, superuser):
    headers = headers_for(superuser)
    muni_1 = create_municipality(client, headers)
    muni_2 = create_municipality(client, headers)
    match = create_ordinance(client, headers, muni_1["id"])
    other = create_ordinance(client, headers, muni_2["id"])

    response = client.get(
        "/ordinances",
        headers=headers,
        params={"municipality_id": muni_1["id"]},
    )

    assert response.status_code == 200
    listed_ids = [o["id"] for o in response.json()]
    assert match["id"] in listed_ids
    assert other["id"] not in listed_ids


# ---------------------------------------------------------------------------
# 11. Import jobs, review, comparison and semantic chunks
# ---------------------------------------------------------------------------


def create_official_source(client, headers, **overrides) -> dict:
    payload = {
        "name": f"Fuente {unique_suffix()}",
        "base_url": "https://bop.example.gov",
        "domain": "bop.example.gov",
        "source_type": "bop",
    }
    payload.update(overrides)
    response = client.post(
        "/ordinances/official-sources",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_seed_initial_official_sources_includes_bop_burgos(db):
    created = ensure_initial_official_legal_sources(db)

    source = db.scalar(
        select(OfficialLegalSource).where(
            OfficialLegalSource.domain == "bopbur.diputaciondeburgos.es"
        )
    )

    assert "bopbur.diputaciondeburgos.es" in created
    assert source is not None
    assert source.name == "Boletín Oficial de la Provincia de Burgos"
    assert source.source_type == "bop"
    assert source.status == "active"


def test_import_job_rejects_non_official_seed_url(
    client, make_user, make_organization, grant_permissions
):
    user = make_user()
    grant_permissions(user, make_organization(), ["ordinances.import"])
    headers = headers_for(user)
    create_official_source(client, headers, domain="bop.example.gov")

    response = client.post(
        "/ordinances/import-jobs",
        headers=headers,
        json={
            "title": "Importación no oficial",
            "topic": "residuos",
            "source_urls": [{"url": "https://noticias.example.com/ordenanza.pdf"}],
            "review_criteria": "Solo fuentes oficiales.",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Import source URL is not official"


def test_import_job_run_creates_pending_ordinance_and_review_report(
    client, monkeypatch, superuser, make_user, make_organization, grant_permissions
):
    user = make_user()
    organization = make_organization()
    grant_permissions(
        user,
        organization,
        ["ordinances.import", "ordinances.review", "ordinances.compare"],
    )
    headers = headers_for(user)
    municipality = create_municipality(
        client,
        headers_for(superuser),
        name="Villa Importada",
        province="Madrid",
        autonomous_community="Comunidad de Madrid",
    )
    source = create_official_source(client, headers, domain="bop.example.gov")

    ordinance_text = (
        "Ordenanza municipal reguladora de residuos de Villa Importada.\n\n"
        "Artículo 1. Objeto. Esta ordenanza regula la recogida de residuos.\n\n"
        "Artículo 2. Obligaciones. La ciudadanía deberá cumplir los horarios.\n\n"
        "Publicado el 12/05/2026 en el boletín oficial."
    )

    def fake_fetch(_url):
        return import_service.FetchedSource(
            content=ordinance_text.encode("utf-8"),
            content_type="text/plain",
        )

    monkeypatch.setattr(import_service, "_fetch_source", fake_fetch)

    created = client.post(
        "/ordinances/import-jobs",
        headers=headers,
        json={
            "title": "Residuos piloto",
            "topic": "residuos",
            "municipality_ids": [municipality["id"]],
            "official_source_ids": [source["id"]],
            "source_urls": [
                {
                    "url": "https://bop.example.gov/anuncio/ordenanza-residuos.txt",
                    "municipality_id": municipality["id"],
                    "official_source_id": source["id"],
                }
            ],
            "review_criteria": "Validar municipio, fuente oficial y articulado.",
        },
    )
    assert created.status_code == 201, created.text

    detail = client.post(
        f"/ordinances/import-jobs/{created.json()['id']}/run-inline",
        headers=headers,
    )

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "completed"
    item = body["items"][0]
    assert item["status"] == "pending_review"
    assert item["ordinance"]["curation_status"] == "pending_review"
    assert item["review_reports"][0]["proposed_decision"] == "approve"
    assert item["review_reports"][0]["checklist"]

    comparison_hidden = client.get(
        "/ordinances/comparison",
        headers=headers,
        params={"municipality_ids": municipality["id"], "topic": "residuos"},
    )
    assert comparison_hidden.status_code == 200
    assert comparison_hidden.json()["rows"] == []

    approved = client.patch(
        f"/ordinances/import-items/{item['id']}/review",
        headers=headers,
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["ordinance"]["curation_status"] == "approved"

    comparison = client.get(
        "/ordinances/comparison",
        headers=headers,
        params={"municipality_ids": municipality["id"], "topic": "residuos"},
    )
    assert comparison.status_code == 200
    rows = comparison.json()["rows"]
    assert rows[0]["entries"][0]["municipality_name"] == "Villa Importada"

    semantic = client.get(
        "/ordinances/semantic-search",
        headers=headers,
        params={"q": "recogida de residuos", "municipality_id": municipality["id"]},
    )
    assert semantic.status_code == 200
    assert semantic.json()[0]["ordinance_id"] == item["ordinance_id"]

    semantic_by_name_and_topic = client.get(
        "/ordinances/semantic-search",
        headers=headers,
        params={
            "q": "recogida de residuos",
            "municipality_name": "Villa Importada",
            "topic": "residuos",
        },
    )
    assert semantic_by_name_and_topic.status_code == 200
    assert semantic_by_name_and_topic.json()[0]["ordinance_id"] == item["ordinance_id"]


def test_parse_bop_burgos_search_results_extracts_official_pdf_metadata():
    results = parse_bop_burgos_search_results(BOP_BURGOS_SEARCH_HTML)

    assert len(results) == 1
    result = results[0]
    assert result.entity == "Ayuntamiento de Hoyales de Roa"
    assert result.bulletin_number == "núm. 177"
    assert result.bulletin_date == "viernes, 19 de septiembre de 2025"
    assert result.cve == "BOPBUR-2025-04362"
    assert result.pdf_url.endswith("bopbur-2025-177-anuncio-202504362.pdf")
    assert "recogida de basuras" in result.title


def test_bop_burgos_filters_municipal_normative_announcements():
    matching = BopBurgosAnnouncement(
        title="Aprobación definitiva de la ordenanza fiscal reguladora de la tasa",
        entity="Ayuntamiento de La Horra",
        bulletin_number="núm. 1",
        bulletin_date="viernes, 1 de enero de 2025",
        cve="BOPBUR-2025-00001",
        pdf_url="http://bopbur.diputaciondeburgos.es/demo.pdf",
    )
    wrong_municipality = BopBurgosAnnouncement(
        title=matching.title,
        entity="Ayuntamiento de Roa",
        bulletin_number=matching.bulletin_number,
        bulletin_date=matching.bulletin_date,
        cve=matching.cve,
        pdf_url=matching.pdf_url,
    )
    supramunicipal = BopBurgosAnnouncement(
        title=matching.title,
        entity="Diputación Provincial de Burgos",
        bulletin_number=matching.bulletin_number,
        bulletin_date=matching.bulletin_date,
        cve=matching.cve,
        pdf_url=matching.pdf_url,
    )
    non_normative = BopBurgosAnnouncement(
        title="Convocatoria para la contratación de personal laboral",
        entity=matching.entity,
        bulletin_number=matching.bulletin_number,
        bulletin_date=matching.bulletin_date,
        cve=matching.cve,
        pdf_url=matching.pdf_url,
    )

    assert natural_municipality_name("Horra, La") == "La Horra"
    assert is_municipal_bopbur_announcement(matching, "Horra, La") is True
    assert is_normative_bopbur_announcement(matching) is True
    assert is_municipal_bopbur_announcement(wrong_municipality, "Horra, La") is False
    assert is_municipal_bopbur_announcement(supramunicipal, "Burgos") is False
    assert is_normative_bopbur_announcement(non_normative) is False


def test_diputacion_fiscal_table_parser_extracts_pdf_rows():
    parser = _FiscalOrdinanceTableParser()

    parser.feed(
        """
        <table><tbody>
          <tr><th>AYUNTAMIENTO</th><th>IBI</th><th>IAE</th><th>IVTM</th></tr>
          <tr>
            <td>ABAJAS</td>
            <td><a href="/sites/default/files/ORD_ABAJAS_IBI_2004.pdf" title="BOP 08/01/2004">X</a></td>
            <td>X</td>
            <td><a href="/sites/default/files/ORD_ABAJAS_IVTM_2004.pdf" title="BOP 08/01/2004">X</a></td>
          </tr>
        </tbody></table>
        """
    )

    assert parser.rows == [
        (
            "ABAJAS",
            "IBI",
            "BOP 08/01/2004",
            "/sites/default/files/ORD_ABAJAS_IBI_2004.pdf",
        ),
        (
            "ABAJAS",
            "IVTM",
            "BOP 08/01/2004",
            "/sites/default/files/ORD_ABAJAS_IVTM_2004.pdf",
        ),
    ]


def test_soria_bop_parser_maps_municipal_normative_documents():
    html = """
    <p class="fec-f1">- AYUNTAMIENTOS </p>
    <p class="fec-f1">- ÁGREDA </p>
    <p class="fec-f1"> - Modificación Ordenanza nº 21 </p>
    <a href="/index.php/mod.documentos/mem.descargar/fichero.documentos_2135_80d4f7e0%232E%23pdf">Descargar</a>
    <p class="fec-f1">- DIPUTACIÓN PROVINCIAL DE SORIA </p>
    <p class="fec-f1"> - Ordenanza provincial </p>
    <a href="/index.php/mod.documentos/mem.descargar/fichero.documentos_999_aaaa%232E%23pdf">Descargar</a>
    """

    assert _extract_candidate_sections(html) == [
        (
            "ÁGREDA",
            "Modificación Ordenanza nº 21",
            "http://bop.dipsoria.es/index.php/mod.documentos/mem.descargar/fichero.documentos_2135_80d4f7e0%232E%23pdf",
        )
    ]


def test_soria_bop_filters_generic_definitive_approval_titles():
    assert NORMATIVE_RE.search("Aprobación definitiva modificación presupuesto") is None
    assert NORMATIVE_RE.search("Aprobación definitiva Ordenanza de ICIO") is not None
    assert normalize_soria_key("Burgo de Osma-Ciudad de Osma") == "burgo osma ciudad osma"


def test_import_job_discovers_candidates_with_bop_burgos_connector(
    db,
    monkeypatch,
    superuser,
):
    municipality_model = import_service.Municipality(
        name="Hoyales de Roa",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    source = OfficialLegalSource(
        name="Boletín Oficial de la Provincia de Burgos",
        base_url="http://bopbur.diputaciondeburgos.es/",
        domain="bopbur.diputaciondeburgos.es",
        source_type="bop",
    )
    db.add_all([municipality_model, source])
    db.commit()

    job = import_service.OrdinanceImportJob(
        title="Búsqueda BOPBUR",
        topic="residuos",
        search_query="basuras",
        municipality_ids_json="[]",
        official_source_ids_json="[]",
        source_urls_json="[]",
        review_criteria="Fuente oficial BOPBUR.",
        created_by_id=superuser.id,
    )

    def fake_search(query, *, limit):
        assert "Hoyales de Roa" in query
        assert limit > 0
        return parse_bop_burgos_search_results(BOP_BURGOS_SEARCH_HTML)

    monkeypatch.setattr(import_service, "search_bop_burgos_announcements", fake_search)

    candidates = import_service._discover_candidates(job, [source], [municipality_model])

    assert len(candidates) == 1
    assert candidates[0].municipality_id == municipality_model.id
    assert candidates[0].official_source_id == source.id
    assert candidates[0].url.endswith("bopbur-2025-177-anuncio-202504362.pdf")


def test_import_service_splits_chunks_by_articles():
    chunks = import_service._split_chunks(
        "Preámbulo de la ordenanza.\n\n"
        "Artículo 1. Objeto. Regula la tasa de basuras.\n\n"
        "Artículo 2. Personas obligadas. Define los sujetos pasivos.\n\n"
        "Disposición final. Entrada en vigor."
    )

    assert len(chunks) == 3
    assert chunks[0].startswith("Preámbulo")
    assert "Artículo 1" in chunks[0]
    assert chunks[1].startswith("Artículo 2")
    assert chunks[2].startswith("Disposición final")


def test_burgos_coverage_endpoint_reports_ready_municipalities(
    client,
    db,
    superuser,
):
    headers = headers_for(superuser)
    municipality = create_municipality(
        client,
        headers,
        name="Hoyales de Roa",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    ordinance = create_ordinance(
        client,
        headers,
        municipality["id"],
        title="Ordenanza fiscal de basuras",
        topic="residuos",
        curation_status="approved",
    )
    db.add(
        OrdinanceLegalChunk(
            ordinance_id=ordinance["id"],
            chunk_index=0,
            heading="Artículo 1. Objeto",
            citation="Artículo 1",
            text="Artículo 1. Objeto. Regula la recogida de basuras.",
            source_url="http://bopbur.diputaciondeburgos.es/demo.pdf",
            source_locator="articulo-1",
            review_status="approved",
            embedding_model="local_hash",
            embedding="[0.1, 0.2]",
            embedding_status="ready",
        )
    )
    db.commit()

    response = client.get("/ordinances/coverage/burgos", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["province"] == "Burgos"
    assert body["municipalities_ready_for_assistant"] == 1
    assert body["chunks_ready"] == 1
    assert body["import_failures_total"] == 0
    assert body["import_failures"] == []
    assert body["municipalities"][0]["ready_for_assistant"] is True


def test_burgos_coverage_groups_duplicate_municipality_names(
    client,
    db,
    superuser,
):
    headers = headers_for(superuser)
    first = create_municipality(
        client,
        headers,
        name="Cascajares de la Sierra",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    second = create_municipality(
        client,
        headers,
        name="Cascajares de la Sierra",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    for municipality, title, index in (
        (first, "Ordenanza de solares", 0),
        (second, "Ordenanza de leñas", 1),
    ):
        ordinance = create_ordinance(
            client,
            headers,
            municipality["id"],
            title=title,
            topic="servicios municipales",
            curation_status="approved",
        )
        db.add(
            OrdinanceLegalChunk(
                ordinance_id=ordinance["id"],
                chunk_index=0,
                heading="Artículo 1. Objeto",
                citation="Artículo 1",
                text=f"Artículo 1. Objeto {index}.",
                source_url="http://bopbur.diputaciondeburgos.es/demo.pdf",
                source_locator=f"articulo-{index}",
                review_status="approved",
                embedding_model="local_hash",
                embedding="[0.1, 0.2]",
                embedding_status="ready",
            )
        )
    db.commit()

    response = client.get("/ordinances/coverage/burgos", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["municipalities_total"] == 1
    assert body["ordinances_approved"] == 2
    assert body["chunks_ready"] == 2
    assert body["municipalities"][0]["municipality_id"] == first["id"]
    assert body["municipalities"][0]["municipality_name"] == "Cascajares de la Sierra"
    assert body["municipalities"][0]["ordinances_approved"] == 2


def test_burgos_coverage_reports_failed_imports_requiring_manual_review(
    client,
    db,
    superuser,
):
    headers = headers_for(superuser)
    municipality = create_municipality(
        client,
        headers,
        name="Cascajares de la Sierra",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    job = import_service.OrdinanceImportJob(
        title="Fallo OCR BOPBUR",
        municipality_ids_json="[]",
        official_source_ids_json="[]",
        source_urls_json="[]",
        review_criteria="Fuente oficial BOPBUR.",
        created_by_id=superuser.id,
    )
    db.add(job)
    db.flush()
    db.add(
        import_service.OrdinanceImportItem(
            job_id=job.id,
            municipality_id=municipality["id"],
            source_url="http://bopbur.diputaciondeburgos.es/demo-escaneado.pdf",
            status="failed",
            error_message="El PDF no tiene texto extraíble; requiere OCR o revisión manual.",
        )
    )
    db.commit()

    response = client.get("/ordinances/coverage/burgos", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["import_failures_total"] == 1
    assert body["import_failures"][0]["municipality_name"] == "Cascajares de la Sierra"
    assert body["import_failures"][0]["requires_manual_review"] is True


def test_retry_burgos_failed_embeddings_restores_ready_chunk(
    client,
    db,
    superuser,
):
    headers = headers_for(superuser)
    municipality = create_municipality(
        client,
        headers,
        name="Belorado",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    ordinance = create_ordinance(
        client,
        headers,
        municipality["id"],
        title="Ordenanza fiscal de agua",
        topic="agua",
        curation_status="approved",
    )
    chunk = OrdinanceLegalChunk(
        ordinance_id=ordinance["id"],
        chunk_index=0,
        heading="Artículo 1. Objeto",
        citation="Artículo 1",
        text="Artículo 1. Objeto. Regula el suministro de agua potable.",
        source_url="http://bopbur.diputaciondeburgos.es/demo.pdf",
        source_locator="articulo-1",
        review_status="approved",
        embedding_model="local_hash",
        embedding=None,
        embedding_status="failed",
    )
    db.add(chunk)
    db.commit()

    response = client.post("/ordinances/coverage/burgos/retry-embeddings", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["retried"] == 1
    assert body["restored"] == 1
    assert body["failed"] == 0
    db.refresh(chunk)
    assert chunk.embedding_status == "ready"
    assert chunk.embedding is not None
