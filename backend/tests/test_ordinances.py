"""Tests for municipalities (global reference data) and ordinances,
including the document-linking access rule."""

import io
from email.message import Message

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.municipalities.models import Municipality
from app.ordinances import import_service
from app.ordinances.bop_burgos import (
    parse_bop_burgos_search_results,
    search_bop_burgos_announcements,
)
from app.ordinances.models import OfficialLegalSource, OrdinanceLegalChunk
from app.ordinances.seed import ensure_initial_official_legal_sources
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


def test_manual_population_update_clears_official_provenance(
    client,
    db,
    superuser,
):
    municipality = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code="09137",
        population=290,
        population_reference_year=2025,
        population_source_url="https://www.ine.es/pob_xls/pobmun.zip",
        population_source_sha256="a" * 64,
        surface_km2=10.0,
        density=29.0,
    )
    db.add(municipality)
    db.commit()

    response = client.patch(
        f"/municipalities/{municipality.id}",
        headers=headers_for(superuser),
        json={"population": 300},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["population"] == 300
    assert body["population_reference_year"] is None
    assert body["population_source_url"] is None
    assert body["population_source_sha256"] is None
    assert body["density"] == 30.0


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


def test_manual_ordinance_defaults_to_pending_review_and_approval_requires_review(
    client,
    superuser,
    make_user,
    make_organization,
    grant_permissions,
):
    municipality = create_municipality(client, headers_for(superuser))
    creator = make_user()
    grant_permissions(creator, make_organization(), ["ordinances.create"])

    pending = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(municipality["id"]),
    )
    approved = client.post(
        "/ordinances",
        headers=headers_for(creator),
        json=ordinance_payload(
            municipality["id"],
            curation_status="approved",
        ),
    )

    assert pending.status_code == 201
    assert pending.json()["curation_status"] == "pending_review"
    assert approved.status_code == 403
    assert approved.json()["detail"] == "Permission required: ordinances.review"


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


def test_ordinance_reader_cannot_see_document_metadata_from_another_tenant(
    client,
    db,
    superuser,
    make_user,
    make_organization,
    grant_permissions,
):
    owner_organization = make_organization()
    project = make_project(db, owner_organization)
    document = upload_document(client, headers_for(superuser), project.id)
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(
        client,
        headers_for(superuser),
        municipality["id"],
        document_id=document["id"],
        curation_status="approved",
    )
    reader = make_user()
    grant_permissions(reader, make_organization(), ["ordinances.view"])

    detail = client.get(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(reader),
    )
    listing = client.get("/ordinances", headers=headers_for(reader))

    assert detail.status_code == 200
    assert detail.json()["document_id"] is None
    assert detail.json()["document"] is None
    listed = next(item for item in listing.json() if item["id"] == ordinance["id"])
    assert listed["document_id"] is None


def test_ordinance_reader_list_hides_unreviewed_records(
    client,
    superuser,
    make_user,
    make_organization,
    grant_permissions,
):
    headers = headers_for(superuser)
    municipality = create_municipality(client, headers)
    pending = create_ordinance(client, headers, municipality["id"])
    approved = create_ordinance(
        client,
        headers,
        municipality["id"],
        curation_status="approved",
    )
    reader = make_user()
    grant_permissions(reader, make_organization(), ["ordinances.view"])

    listing = client.get("/ordinances", headers=headers_for(reader))
    pending_filter = client.get(
        "/ordinances",
        headers=headers_for(reader),
        params={"curation_status": "pending_review"},
    )

    assert listing.status_code == 200
    listed_ids = {item["id"] for item in listing.json()}
    assert approved["id"] in listed_ids
    assert pending["id"] not in listed_ids
    assert pending_filter.status_code == 403


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


def test_empty_patch_cannot_read_an_unreviewed_ordinance(
    client,
    superuser,
    make_user,
    make_organization,
    grant_permissions,
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(
        client,
        headers_for(superuser),
        municipality["id"],
        text_content="Texto pendiente reservado a curación.",
    )
    reader = make_user()
    grant_permissions(reader, make_organization(), ["ordinances.view"])

    response = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(reader),
        json={},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ordinance not found"


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


def test_editing_approved_legal_text_rebuilds_chunks_and_requires_new_review(
    client,
    db,
    superuser,
    make_user,
    make_organization,
    grant_permissions,
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(
        client,
        headers_for(superuser),
        municipality["id"],
        title="Ordenanza revisada",
        curation_status="approved",
        text_content=(
            "Artículo 1. Texto anterior suficientemente extenso para recuperar.\n\n"
            "Artículo 2. Segunda regla anterior de la ordenanza municipal."
        ),
    )
    original_chunks = list(
        db.scalars(
            select(OrdinanceLegalChunk).where(
                OrdinanceLegalChunk.ordinance_id == ordinance["id"]
            )
        )
    )
    assert original_chunks
    assert {chunk.review_status for chunk in original_chunks} == {"approved"}

    editor = make_user()
    grant_permissions(editor, make_organization(), ["ordinances.edit"])
    changed_text = (
        "Artículo 1. Texto nuevo que sustituye por completo el contenido anterior.\n\n"
        "Artículo 2. Segunda regla nueva pendiente de validación humana."
    )
    edited = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(editor),
        json={"text_content": changed_text},
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["curation_status"] == "needs_changes"
    rebuilt_chunks = list(
        db.scalars(
            select(OrdinanceLegalChunk)
            .where(OrdinanceLegalChunk.ordinance_id == ordinance["id"])
            .order_by(OrdinanceLegalChunk.chunk_index)
        )
    )
    assert rebuilt_chunks
    assert "Texto nuevo" in rebuilt_chunks[0].text
    assert {chunk.review_status for chunk in rebuilt_chunks} == {"pending_review"}

    reviewer = make_user()
    grant_permissions(reviewer, make_organization(), ["ordinances.review"])
    reviewed = client.patch(
        f"/ordinances/{ordinance['id']}",
        headers=headers_for(reviewer),
        json={"curation_status": "approved"},
    )

    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["curation_status"] == "approved"
    db.expire_all()
    assert {
        chunk.review_status
        for chunk in db.scalars(
            select(OrdinanceLegalChunk).where(
                OrdinanceLegalChunk.ordinance_id == ordinance["id"]
            )
        )
    } == {"approved"}


def test_manual_legal_text_queues_remote_embeddings_after_commit(
    client,
    db,
    superuser,
    monkeypatch,
):
    municipality = create_municipality(client, headers_for(superuser))
    queued: list[tuple[object, int, dict]] = []

    class FakeQueue:
        def enqueue(self, function, chunk_id, **options):
            queued.append((function, chunk_id, options))

    monkeypatch.setattr(
        import_service.settings,
        "embeddings_runtime",
        "openai_compatible",
    )
    monkeypatch.setattr(import_service, "get_default_queue", FakeQueue)

    response = client.post(
        "/ordinances",
        headers=headers_for(superuser),
        json=ordinance_payload(
            municipality["id"],
            text_content="Artículo 1. Contenido pendiente de indexación remota.",
        ),
    )

    assert response.status_code == 201, response.text
    ordinance_id = response.json()["id"]
    chunks = list(
        db.scalars(
            select(OrdinanceLegalChunk).where(
                OrdinanceLegalChunk.ordinance_id == ordinance_id
            )
        )
    )
    assert chunks
    assert len(queued) == len(chunks)
    assert {entry[0] for entry in queued} == {import_service.embed_ordinance_chunk}
    assert {entry[1] for entry in queued} == {chunk.id for chunk in chunks}
    assert all(entry[2]["job_timeout"] >= 90 for entry in queued)
    assert all(entry[2]["retry"].max == 3 for entry in queued)
    assert {chunk.embedding_status for chunk in chunks} == {"pending"}
    assert {chunk.embedding for chunk in chunks} == {None}

    queued.clear()
    retry = client.post(
        f"/ordinances/{ordinance_id}/retry-embeddings",
        headers=headers_for(superuser),
    )

    assert retry.status_code == 200, retry.text
    assert retry.json()["requested"] == len(chunks)
    assert retry.json()["queued"] == len(chunks)
    assert retry.json()["queue_failed"] == 0
    assert retry.json()["pending"] == len(chunks)
    assert len(queued) == len(chunks)


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


def test_official_source_urls_reject_unsafe_schemes_credentials_and_hosts():
    source = OfficialLegalSource(
        name="BOP seguro",
        base_url="https://bop.example.gov",
        domain="bop.example.gov",
        source_type="bop",
    )

    assert import_service.is_official_source_url(
        "https://bop.example.gov/ordenanza.pdf",
        [source],
    )
    assert import_service.is_official_source_url(
        "https://archivo.bop.example.gov/ordenanza.pdf",
        [source],
    )
    for unsafe_url in (
        "file:///etc/passwd",
        "ftp://bop.example.gov/ordenanza.pdf",
        "https://usuario@bop.example.gov/ordenanza.pdf",
        "https://bop.example.gov.evil.test/ordenanza.pdf",
        "https://bop.example.gov:22/ordenanza.pdf",
        "https://bop.example.gov/ordenanza.pdf#fragmento",
        "https://bop.example.gov\\@evil.test/ordenanza.pdf",
    ):
        assert not import_service.is_official_source_url(unsafe_url, [source])

    assert import_service.is_valid_official_source_definition(
        "https://bop.example.gov/",
        "bop.example.gov",
    )
    assert not import_service.is_valid_official_source_definition(
        "http://127.0.0.1/",
        "127.0.0.1",
    )
    assert not import_service.is_valid_official_source_definition(
        "https://localhost/",
        "localhost",
    )


def test_public_connection_rejects_private_or_mixed_dns_before_connecting(
    monkeypatch,
):
    for address in (
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "64:ff9b::7f00:1",
        "64:ff9b:1::a9fe:a9fe",
        "224.0.0.1",
        "ff02::1",
    ):
        try:
            import_service._require_public_unicast_ip(address)
        except import_service.ImportSourceError:
            pass
        else:
            raise AssertionError(f"Se aceptó la IP no pública {address}")

    socket_calls = []
    monkeypatch.setattr(
        import_service.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                import_service.socket.AF_INET,
                import_service.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            ),
            (
                import_service.socket.AF_INET,
                import_service.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 443),
            ),
        ],
    )
    monkeypatch.setattr(
        import_service.socket,
        "socket",
        lambda *_args: socket_calls.append(_args),
    )

    try:
        import_service._create_public_connection(("bop.example.gov", 443))
    except import_service.ImportSourceError:
        pass
    else:
        raise AssertionError("Se aceptó una resolución DNS pública y privada")
    assert socket_calls == []


def test_fetch_source_validates_each_redirect_before_opening(monkeypatch):
    source = OfficialLegalSource(
        name="BOP seguro",
        base_url="https://bop.example.gov",
        domain="bop.example.gov",
        source_type="bop",
    )

    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "text/plain"}
            self._chunks = [b"texto oficial", b""]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return self._chunks.pop(0)

    class RedirectingOpener:
        def __init__(self, location):
            self.location = location
            self.calls = []

        def open(self, request, timeout):
            assert timeout == 30
            self.calls.append(request.full_url)
            if len(self.calls) == 1:
                headers = Message()
                headers["Location"] = self.location
                raise import_service.urlerror.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    headers,
                    io.BytesIO(),
                )
            return FakeResponse()

    safe_opener = RedirectingOpener("/documentos/ordenanza.txt")
    monkeypatch.setattr(import_service, "_SOURCE_OPENER", safe_opener)
    fetched = import_service._fetch_source(
        "https://bop.example.gov/inicio",
        [source],
    )
    assert fetched.content == b"texto oficial"
    assert safe_opener.calls == [
        "https://bop.example.gov/inicio",
        "https://bop.example.gov/documentos/ordenanza.txt",
    ]

    unsafe_opener = RedirectingOpener("http://127.0.0.1/admin")
    monkeypatch.setattr(import_service, "_SOURCE_OPENER", unsafe_opener)
    try:
        import_service._fetch_source(
            "https://bop.example.gov/inicio",
            [source],
        )
    except import_service.ImportSourceError:
        pass
    else:
        raise AssertionError("Se siguió una redirección fuera de fuente oficial")
    assert unsafe_opener.calls == ["https://bop.example.gov/inicio"]


def test_create_official_source_rejects_private_ip_definition(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    importer = make_user()
    grant_permissions(importer, make_organization(), ["ordinances.import"])

    response = client.post(
        "/ordinances/official-sources",
        headers=headers_for(importer),
        json={
            "name": "Fuente interna",
            "base_url": "http://127.0.0.1/",
            "domain": "127.0.0.1",
            "source_type": "other",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid official legal source definition"


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

    def fake_fetch(_url, _sources):
        return import_service.FetchedSource(
            content=ordinance_text.encode("utf-8"),
            content_type="text/plain",
        )

    def unavailable_discovery(*_args, **_kwargs):
        raise import_service.ImportSourceError("Búsqueda complementaria no disponible")

    monkeypatch.setattr(import_service, "_fetch_source", fake_fetch)
    monkeypatch.setattr(
        import_service,
        "_discover_candidates",
        unavailable_discovery,
    )

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

    rerun_pending = client.post(
        f"/ordinances/import-jobs/{created.json()['id']}/run-inline",
        headers=headers,
    )
    assert rerun_pending.status_code == 200, rerun_pending.text
    pending_item = rerun_pending.json()["items"][0]
    assert pending_item["id"] == item["id"]
    assert pending_item["status"] == "pending_review"
    assert pending_item["ordinance_id"] == item["ordinance_id"]

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

    rerun_approved = client.post(
        f"/ordinances/import-jobs/{created.json()['id']}/run-inline",
        headers=headers,
    )
    assert rerun_approved.status_code == 200, rerun_approved.text
    approved_item = rerun_approved.json()["items"][0]
    assert approved_item["id"] == item["id"]
    assert approved_item["status"] == "approved"
    assert approved_item["ordinance"]["curation_status"] == "approved"

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


def test_import_item_creation_is_atomic_and_retryable(
    db,
    monkeypatch,
    superuser,
):
    municipality = import_service.Municipality(
        name=f"Municipio atómico {unique_suffix()}",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    source = OfficialLegalSource(
        name=f"Fuente atómica {unique_suffix()}",
        base_url="https://atomic.example.gov/",
        domain=f"atomic-{unique_suffix()}.example.gov",
        source_type="municipal",
    )
    db.add_all([municipality, source])
    db.flush()
    source.base_url = f"https://{source.domain}/"
    source_url = f"https://{source.domain}/ordenanza.txt"
    job = import_service.OrdinanceImportJob(
        title="Importación atómica",
        municipality_ids_json=f"[{municipality.id}]",
        official_source_ids_json=f"[{source.id}]",
        source_urls_json=(
            '[{"url": "'
            + source_url
            + f'", "municipality_id": {municipality.id}, '
            + f'"official_source_id": {source.id}}}]'
        ),
        review_criteria="Fuente y articulado revisables.",
        created_by_id=superuser.id,
    )
    db.add(job)
    db.commit()

    legal_text = (
        f"Ordenanza municipal de {municipality.name}.\n\n"
        "Artículo 1. Objeto y ámbito de aplicación suficientemente descritos.\n\n"
        "Artículo 2. Obligaciones y condiciones municipales aplicables."
    )
    monkeypatch.setattr(
        import_service,
        "_fetch_source",
        lambda *_args: import_service.FetchedSource(
            content=legal_text.encode(),
            content_type="text/plain",
        ),
    )
    original_create_report = import_service.create_review_report
    monkeypatch.setattr(
        import_service,
        "create_review_report",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("fallo de informe")),
    )

    import_service.run_import_job(job.id, db=db)

    db.expire_all()
    item = db.scalar(
        select(import_service.OrdinanceImportItem).where(
            import_service.OrdinanceImportItem.job_id == job.id
        )
    )
    assert item.status == "failed"
    assert item.ordinance_id is None
    assert db.scalar(
        select(import_service.Ordinance).where(
            import_service.Ordinance.source_url == source_url
        )
    ) is None

    monkeypatch.setattr(
        import_service,
        "create_review_report",
        original_create_report,
    )
    import_service.run_import_job(job.id, db=db)

    db.expire_all()
    item = db.get(import_service.OrdinanceImportItem, item.id)
    assert item.status == "pending_review"
    assert item.ordinance_id is not None
    assert db.scalars(
        select(OrdinanceLegalChunk).where(
            OrdinanceLegalChunk.ordinance_id == item.ordinance_id
        )
    ).first() is not None


def test_duplicate_import_item_cannot_review_the_original_ordinance(
    client,
    db,
    superuser,
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(client, headers_for(superuser), municipality["id"])
    job = import_service.OrdinanceImportJob(
        title="Duplicado no revisable",
        municipality_ids_json="[]",
        official_source_ids_json="[]",
        source_urls_json="[]",
        review_criteria="No aplica.",
        created_by_id=superuser.id,
    )
    db.add(job)
    db.flush()
    duplicate = import_service.OrdinanceImportItem(
        job_id=job.id,
        municipality_id=municipality["id"],
        ordinance_id=ordinance["id"],
        source_url="https://example.gov/duplicada.pdf",
        status="duplicate",
    )
    db.add(duplicate)
    db.commit()

    response = client.patch(
        f"/ordinances/import-items/{duplicate.id}/review",
        headers=headers_for(superuser),
        json={"decision": "approve"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Import item is not pending review"


def test_editing_imported_ordinance_reopens_owner_not_later_duplicate(
    client,
    db,
    superuser,
):
    municipality = create_municipality(client, headers_for(superuser))
    ordinance = create_ordinance(
        client,
        headers_for(superuser),
        municipality["id"],
        curation_status="approved",
        text_content="Artículo 1. Texto importado original y aprobado.",
    )
    owner_job = import_service.OrdinanceImportJob(
        title="Importación propietaria",
        municipality_ids_json=f"[{municipality['id']}]",
        official_source_ids_json="[]",
        source_urls_json="[]",
        review_criteria="Revisión jurídica humana.",
        created_by_id=superuser.id,
    )
    duplicate_job = import_service.OrdinanceImportJob(
        title="Importación duplicada posterior",
        municipality_ids_json=f"[{municipality['id']}]",
        official_source_ids_json="[]",
        source_urls_json="[]",
        review_criteria="Detectar duplicados.",
        created_by_id=superuser.id,
    )
    db.add_all([owner_job, duplicate_job])
    db.flush()
    model = db.get(import_service.Ordinance, ordinance["id"])
    model.import_job_id = owner_job.id
    owner_item = import_service.OrdinanceImportItem(
        job_id=owner_job.id,
        municipality_id=municipality["id"],
        ordinance_id=model.id,
        source_url="https://example.gov/original.pdf",
        status="approved",
    )
    db.add(owner_item)
    db.flush()
    import_service.create_review_report(db, model, owner_item)
    db.flush()
    original_report = db.scalar(
        select(import_service.OrdinanceReviewReport).where(
            import_service.OrdinanceReviewReport.import_item_id == owner_item.id
        )
    )
    original_report.status = "human_approved"
    original_checklist = original_report.checklist_json
    duplicate_item = import_service.OrdinanceImportItem(
        job_id=duplicate_job.id,
        municipality_id=municipality["id"],
        ordinance_id=model.id,
        source_url="https://example.gov/duplicada.pdf",
        status="duplicate",
    )
    db.add(duplicate_item)
    db.commit()

    edited = client.patch(
        f"/ordinances/{model.id}",
        headers=headers_for(superuser),
        json={
            "text_content": (
                "Artículo 1. Texto importado modificado que exige nueva revisión."
            )
        },
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["curation_status"] == "needs_changes"
    db.expire_all()
    assert db.get(import_service.OrdinanceImportItem, owner_item.id).status == (
        "pending_review"
    )
    assert db.get(import_service.OrdinanceImportItem, duplicate_item.id).status == (
        "duplicate"
    )
    reports = list(
        db.scalars(
            select(import_service.OrdinanceReviewReport)
            .where(
                import_service.OrdinanceReviewReport.import_item_id == owner_item.id
            )
            .order_by(import_service.OrdinanceReviewReport.id)
        )
    )
    assert len(reports) == 2
    assert reports[0].id == original_report.id
    assert reports[0].status == "superseded"
    assert reports[0].checklist_json == original_checklist
    assert reports[1].status == "agent_reviewed"
    assert reports[1].checklist_json != original_checklist
    assert {
        chunk.import_item_id
        for chunk in db.scalars(
            select(OrdinanceLegalChunk).where(
                OrdinanceLegalChunk.ordinance_id == model.id
            )
        )
    } == {owner_item.id}

    requested_changes = client.patch(
        f"/ordinances/import-items/{owner_item.id}/review",
        headers=headers_for(superuser),
        json={"decision": "needs_changes"},
    )
    assert requested_changes.status_code == 200, requested_changes.text
    assert requested_changes.json()["status"] == "pending_review"
    assert requested_changes.json()["ordinance"]["curation_status"] == (
        "needs_changes"
    )

    blocked = client.patch(
        f"/ordinances/import-items/{owner_item.id}/review",
        headers=headers_for(superuser),
        json={"decision": "approve"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Import item has no current review report"

    revised = client.patch(
        f"/ordinances/{model.id}",
        headers=headers_for(superuser),
        json={"title": "Ordenanza importada corregida tras revisión"},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["curation_status"] == "pending_review"
    db.expire_all()
    reports = list(
        db.scalars(
            select(import_service.OrdinanceReviewReport)
            .where(
                import_service.OrdinanceReviewReport.import_item_id == owner_item.id
            )
            .order_by(import_service.OrdinanceReviewReport.id)
        )
    )
    assert len(reports) == 3
    assert [report.status for report in reports] == [
        "superseded",
        "superseded",
        "agent_reviewed",
    ]

    reviewed = client.patch(
        f"/ordinances/import-items/{owner_item.id}/review",
        headers=headers_for(superuser),
        json={"decision": "approve"},
    )

    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["ordinance"]["curation_status"] == "approved"
    db.expire_all()
    assert db.get(import_service.OrdinanceReviewReport, reports[0].id).status == (
        "superseded"
    )
    assert db.get(import_service.OrdinanceReviewReport, reports[1].id).status == (
        "superseded"
    )
    assert db.get(import_service.OrdinanceReviewReport, reports[2].id).status == (
        "human_approved"
    )


def test_comparison_excludes_definitively_inactive_ordinances_by_default(
    client,
    superuser,
):
    headers = headers_for(superuser)
    municipality = create_municipality(client, headers)
    for status in (
        "active",
        "partially_repealed",
        "unknown",
        "repealed",
        "superseded",
        "archived",
    ):
        create_ordinance(
            client,
            headers,
            municipality["id"],
            title=f"Ordenanza {status}",
            topic="vigencia",
            status=status,
            curation_status="approved",
        )

    current = client.get(
        "/ordinances/comparison",
        headers=headers,
        params={"municipality_ids": municipality["id"], "topic": "vigencia"},
    )
    historical = client.get(
        "/ordinances/comparison",
        headers=headers,
        params={
            "municipality_ids": municipality["id"],
            "topic": "vigencia",
            "include_inactive": "true",
        },
    )

    assert current.status_code == 200
    assert current.json()["include_inactive"] is False
    assert {
        entry["status"]
        for row in current.json()["rows"]
        for entry in row["entries"]
    } == {"active", "partially_repealed", "unknown"}
    assert historical.status_code == 200
    assert historical.json()["include_inactive"] is True
    assert sum(len(row["entries"]) for row in historical.json()["rows"]) == 6


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


def test_search_bop_burgos_uses_required_injected_fetcher():
    requested_urls = []

    def fetch_content(url: str) -> bytes:
        requested_urls.append(url)
        return BOP_BURGOS_SEARCH_HTML.encode()

    results = search_bop_burgos_announcements(
        "basuras Hoyales de Roa",
        year=2025,
        limit=1,
        fetch_content=fetch_content,
    )

    assert len(results) == 1
    assert requested_urls == [
        "http://bopbur.diputaciondeburgos.es/busqueda?"
        "keys=basuras+Hoyales+de+Roa&field_bop_anio_numero%5Bvalue%5D%5Bdate%5D=2025"
    ]


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

    def fake_search(query, *, fetch_content, limit):
        assert "Hoyales de Roa" in query
        assert callable(fetch_content)
        assert limit > 0
        return parse_bop_burgos_search_results(BOP_BURGOS_SEARCH_HTML)

    monkeypatch.setattr(import_service, "search_bop_burgos_announcements", fake_search)

    candidates = import_service._discover_candidates(job, [source], [municipality_model])

    assert len(candidates) == 1
    assert candidates[0].municipality_id == municipality_model.id
    assert candidates[0].official_source_id == source.id
    assert candidates[0].url.endswith("bopbur-2025-177-anuncio-202504362.pdf")


def test_import_job_uses_configured_web_search_provider(monkeypatch):
    municipality_model = import_service.Municipality(
        id=101,
        name="Aranda de Duero",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    source = OfficialLegalSource(
        id=202,
        name="Sede municipal",
        base_url="https://sede.example.org/",
        domain="sede.example.org",
        source_type="municipal",
    )
    job = import_service.OrdinanceImportJob(
        title="Búsqueda municipal",
        topic="terrazas",
        search_query="ordenanza",
        municipality_ids_json="[]",
        official_source_ids_json="[]",
        source_urls_json="[]",
        review_criteria="Fuente oficial.",
        created_by_id=1,
    )
    calls = []

    class FakeWebSearchClient:
        enabled = True

        def search(self, *, query, limit):
            calls.append((query, limit))
            return [
                {
                    "title": "Ordenanza de terrazas",
                    "url": "https://sede.example.org/ordenanza.pdf",
                    "snippet": "Texto oficial.",
                    "published_at": None,
                }
            ]

    monkeypatch.setattr(
        import_service,
        "web_search_client",
        FakeWebSearchClient(),
    )

    candidates = import_service._discover_candidates(
        job,
        [source],
        [municipality_model],
    )

    assert calls
    assert "Aranda de Duero" in calls[0][0]
    assert "site:sede.example.org" in calls[0][0]
    assert candidates == [
        import_service.SourceCandidate(
            url="https://sede.example.org/ordenanza.pdf",
            municipality_id=101,
            official_source_id=202,
            title="Ordenanza de terrazas",
        )
    ]


def test_import_job_preserves_invalid_web_query_cause(monkeypatch):
    municipality_model = import_service.Municipality(
        id=101,
        name="Aranda de Duero",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    source = OfficialLegalSource(
        id=202,
        name="Sede municipal",
        base_url="https://sede.example.org/",
        domain="sede.example.org",
        source_type="municipal",
    )
    job = import_service.OrdinanceImportJob(
        title="Búsqueda municipal inválida",
        topic="terrazas",
        search_query="x" * 401,
        municipality_ids_json="[]",
        official_source_ids_json="[]",
        source_urls_json="[]",
        review_criteria="Fuente oficial.",
        created_by_id=1,
    )
    monkeypatch.setattr(import_service.settings, "web_search_provider", "brave")
    monkeypatch.setattr(
        import_service.settings,
        "brave_search_api_key",
        "brave-secret",
    )
    monkeypatch.setattr(
        import_service.settings,
        "brave_search_storage_rights_confirmed",
        True,
    )

    candidates = import_service._discover_candidates(
        job,
        [source],
        [municipality_model],
    )

    assert candidates == []
    assert job.error_message == (
        "Consulta de búsqueda no válida: query no puede superar 400 caracteres"
    )


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


def test_import_service_splits_every_chunk_to_the_configured_limit(monkeypatch):
    monkeypatch.setattr(import_service.settings, "ordinance_chunk_chars", 20)

    chunks = import_service._split_chunks(
        "Párrafo breve.\n\n" + "contenido-sin-espacios" * 4
    )

    assert len(chunks) > 1
    assert "contenido-sin-espacios" * 4 in "".join(chunks).replace("\n", "")
    assert all(0 < len(chunk) <= 20 for chunk in chunks)


def test_rebuild_chunks_rejects_overflow_without_deleting_existing_chunks(
    client,
    db,
    superuser,
    monkeypatch,
):
    headers = headers_for(superuser)
    municipality = create_municipality(client, headers)
    ordinance = create_ordinance(client, headers, municipality["id"])
    model = db.get(import_service.Ordinance, ordinance["id"])
    existing = OrdinanceLegalChunk(
        ordinance_id=model.id,
        chunk_index=0,
        text="Texto anterior",
        review_status="approved",
        embedding_status="ready",
        embedding_model="local_hash",
        embedding="[0.1]",
    )
    db.add(existing)
    db.commit()
    model.text_content = "A" * 25
    monkeypatch.setattr(import_service.settings, "ordinance_chunk_chars", 10)
    monkeypatch.setattr(import_service.settings, "ordinance_import_max_chunks", 2)

    try:
        import_service.rebuild_ordinance_chunks(db, model)
    except import_service.ImportSourceError as error:
        assert "máximo de fragmentos" in str(error)
    else:
        raise AssertionError("Se truncó silenciosamente una ordenanza sobredimensionada")

    db.rollback()
    assert db.get(OrdinanceLegalChunk, existing.id).text == "Texto anterior"


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


def test_retry_burgos_remote_embeddings_enqueues_bounded_chunk_jobs(
    client,
    db,
    superuser,
    monkeypatch,
):
    headers = headers_for(superuser)
    municipality = create_municipality(
        client,
        headers,
        name="Briviesca",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    ordinance = create_ordinance(
        client,
        headers,
        municipality["id"],
        title="Ordenanza fiscal remota",
        topic="tributos",
        curation_status="approved",
    )
    chunk = OrdinanceLegalChunk(
        ordinance_id=ordinance["id"],
        chunk_index=0,
        text="Artículo 1. Regulación tributaria municipal.",
        review_status="approved",
        embedding_model="remote-model",
        embedding_status="failed",
    )
    db.add(chunk)
    db.commit()
    queued = []

    class FakeQueue:
        def enqueue(self, function, chunk_id, **options):
            queued.append((function, chunk_id, options))

    monkeypatch.setattr(
        import_service.settings,
        "embeddings_runtime",
        "openai_compatible",
    )
    monkeypatch.setattr(import_service, "get_default_queue", FakeQueue)

    response = client.post(
        "/ordinances/coverage/burgos/retry-embeddings",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["retried"] == 1
    assert response.json()["queued"] == 1
    assert response.json()["restored"] == 0
    assert response.json()["failed"] == 1
    assert queued[0][0] is import_service.embed_ordinance_chunk
    assert queued[0][1] == chunk.id
    assert queued[0][2]["job_timeout"] >= 90
