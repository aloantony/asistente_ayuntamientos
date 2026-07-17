import json

import pytest
from app.agent_office import service as agent_office_service
from app.assistant import tools as assistant_tools
from app.assistant.prompts import ANACLETO_SYSTEM_PROMPT
from app.core.config import settings
from app.municipalities.models import Municipality
from app.ordinances.catalog import (
    InvalidOrdinanceCatalogCursor,
    OrdinanceCorpusFilters,
    build_ordinance_corpus_manifest,
    decode_ordinance_catalog_cursor,
    encode_ordinance_catalog_cursor,
    list_ordinance_catalog_from_cursor,
)
from app.ordinances.models import Ordinance, OrdinanceLegalChunk
from app.ordinances.search import OrdinanceSearchOptions, search_ordinance_chunks


def _municipality(
    db,
    *,
    name: str,
    ine_code: str,
    province: str = "Burgos",
    autonomous_community: str = "Castilla y León",
    population: int | None = None,
    municipality_type: str = "municipality",
) -> Municipality:
    municipality = Municipality(
        name=name,
        ine_code=ine_code,
        province=province,
        autonomous_community=autonomous_community,
        population=population,
        municipality_type=municipality_type,
    )
    db.add(municipality)
    db.flush()
    return municipality


def _ordinance(
    db,
    municipality: Municipality,
    *,
    title: str,
    curation_status: str = "approved",
    status: str = "active",
) -> Ordinance:
    ordinance = Ordinance(
        municipality_id=municipality.id,
        title=title,
        topic="vías públicas",
        ordinance_type="ordinance",
        curation_status=curation_status,
        status=status,
        source_url=f"https://example.test/{municipality.id}/{title}",
    )
    db.add(ordinance)
    db.flush()
    return ordinance


def _chunk(
    db,
    ordinance: Ordinance,
    *,
    index: int,
    embedding: str | None = "[1,0]",
    embedding_status: str = "ready",
    review_status: str = "approved",
) -> OrdinanceLegalChunk:
    chunk = OrdinanceLegalChunk(
        ordinance_id=ordinance.id,
        chunk_index=index,
        citation=f"art. {index + 1}",
        text=f"Contenido normativo {ordinance.title} {index}",
        review_status=review_status,
        embedding_model=settings.embeddings_model,
        embedding=embedding,
        embedding_status=embedding_status,
    )
    db.add(chunk)
    return chunk


def test_manifest_distinguishes_population_catalog_and_retrieval_layers(db):
    small = _municipality(
        db,
        name="Pequeño",
        ine_code="09001",
        population=4999,
    )
    _municipality(
        db,
        name="En el límite",
        ine_code="42001",
        province="Soria",
        population=5000,
    )
    _municipality(
        db,
        name="Población desconocida",
        ine_code="24001",
        province="León",
        population=None,
    )
    no_chunks = _municipality(
        db,
        name="Sin fragmentos",
        ine_code="42002",
        province="Soria",
        population=100,
    )
    minor = _municipality(
        db,
        name="Entidad menor",
        ine_code="09002",
        population=25,
        municipality_type="minor_local_entity",
    )

    searchable = _ordinance(db, small, title="Ordenanza recuperable")
    _chunk(db, searchable, index=0)
    _chunk(
        db,
        searchable,
        index=1,
        embedding=None,
        embedding_status="failed",
    )
    _ordinance(db, no_chunks, title="Ordenanza sin fragmentos")
    _ordinance(
        db,
        small,
        title="Ordenanza pendiente",
        curation_status="pending_review",
    )
    _ordinance(db, small, title="Ordenanza derogada", status="repealed")
    _ordinance(db, minor, title="Ordenanza de entidad menor")
    db.flush()

    manifest = build_ordinance_corpus_manifest(
        db,
        embedding_model=settings.embeddings_model,
        filters=OrdinanceCorpusFilters(
            autonomous_community="Castilla y León",
            population_lt=5000,
        ),
    )

    population = manifest["population_coverage"]
    assert population == {
        "municipalities_in_geographic_scope": 4,
        "municipalities_with_population": 3,
        "municipalities_without_population": 1,
        "municipalities_without_ine_code": 0,
        "directory_backed_municipalities": 0,
        "population_provenance_municipalities": 0,
        "eligible_municipalities": 2,
        "filter_applied": True,
        "coverage_complete": False,
    }
    layers = manifest["layers"]
    assert layers["ordinance_records"] == 4
    assert layers["catalog_ordinances"] == 2
    assert layers["catalog_municipalities"] == 2
    assert layers["total_chunks"] == 2
    assert layers["ordinances_with_chunks"] == 1
    assert layers["ordinances_without_chunks"] == 1
    assert layers["searchable_chunks"] == 1
    assert layers["failed_embedding_chunks"] == 1
    assert layers["curation_statuses"] == {
        "approved": 3,
        "pending_review": 1,
    }
    assert layers["catalog_curation_statuses"] == {"approved": 2}
    assert manifest["reconciliation"]["ordinance_partition_balanced"] is True
    assert manifest["completeness"]["catalog_snapshot_complete"] is False
    assert manifest["completeness"]["complete_against_official_sources"] is False

    by_province = {row["province"]: row for row in manifest["by_province"]}
    assert by_province["Burgos"]["catalog_ordinances"] == 1
    assert by_province["Soria"]["catalog_ordinances"] == 1
    assert by_province["León"]["catalog_ordinances"] == 0
    assert by_province["León"]["municipalities_without_population"] == 1

    cursor = decode_ordinance_catalog_cursor(manifest["catalog_cursor"])
    assert cursor["snapshot_id"] == manifest["snapshot_id"]
    assert cursor["total"] == 2
    assert cursor["consumed"] == 0


def test_signed_cursor_enumerates_each_ordinance_once(db):
    municipality = _municipality(
        db,
        name="Catálogo",
        ine_code="09010",
        population=1000,
    )
    expected_ids = [
        _ordinance(db, municipality, title=f"Ordenanza {index:03d}").id
        for index in range(37)
    ]
    db.flush()
    manifest = build_ordinance_corpus_manifest(
        db,
        embedding_model=settings.embeddings_model,
        filters=OrdinanceCorpusFilters(province="Burgos"),
    )

    cursor = manifest["catalog_cursor"]
    found_ids: list[int] = []
    while cursor is not None:
        page = list_ordinance_catalog_from_cursor(
            db,
            embedding_model=settings.embeddings_model,
            cursor=cursor,
            limit=7,
        )
        found_ids.extend(item["ordinance_id"] for item in page["results"])
        cursor = page["next_cursor"]

    assert found_ids == expected_ids
    assert len(found_ids) == len(set(found_ids)) == 37
    assert page["complete"] is True
    assert page["has_more"] is False
    assert page["remaining"] == 0


def test_cursor_rejects_tampering_and_snapshot_drift(db):
    municipality = _municipality(
        db,
        name="Snapshot",
        ine_code="09020",
        population=1500,
    )
    _ordinance(db, municipality, title="Ordenanza inicial")
    db.flush()
    manifest = build_ordinance_corpus_manifest(
        db,
        embedding_model=settings.embeddings_model,
        filters=OrdinanceCorpusFilters(population_lt=5000),
    )
    cursor = manifest["catalog_cursor"]
    tampered = ("A" if cursor[0] != "A" else "B") + cursor[1:]

    with pytest.raises(InvalidOrdinanceCatalogCursor):
        decode_ordinance_catalog_cursor(tampered)

    _ordinance(db, municipality, title="Alta posterior al manifiesto")
    db.flush()
    with pytest.raises(ValueError, match="corpus cambió"):
        list_ordinance_catalog_from_cursor(
            db,
            embedding_model=settings.embeddings_model,
            cursor=cursor,
            limit=10,
        )


def test_catalog_tools_are_local_read_only_and_gate_pending(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    municipality = _municipality(
        db,
        name="Herramienta",
        ine_code="09030",
        population=900,
    )
    _ordinance(db, municipality, title="Ordenanza visible")
    _ordinance(
        db,
        municipality,
        title="Ordenanza pendiente",
        curation_status="pending_review",
    )
    comparer = make_user()
    grant_permissions(
        comparer,
        make_organization(),
        ["ordinances.compare"],
    )

    monkeypatch.setattr(
        assistant_tools,
        "embed_text_supervised",
        lambda *_args, **_kwargs: pytest.fail(
            "El inventario no debe generar embeddings"
        ),
    )
    available = assistant_tools.get_available_tool_specs(
        db,
        comparer,
        frozenset(
            {
                "get_ordinance_corpus_manifest",
                "list_ordinance_catalog",
            }
        ),
    )
    assert [spec.name for spec in available] == [
        "get_ordinance_corpus_manifest",
        "list_ordinance_catalog",
    ]
    assert all(spec.read_only for spec in available)
    manifest_result = assistant_tools.execute_tool(
        db,
        comparer,
        "get_ordinance_corpus_manifest",
        {
            "autonomous_community": "Castilla y León",
            "population_lt": 5000,
        },
        allowed=frozenset({"get_ordinance_corpus_manifest"}),
    )
    assert manifest_result.ok is True
    manifest = json.loads(manifest_result.content)
    catalog_result = assistant_tools.execute_tool(
        db,
        comparer,
        "list_ordinance_catalog",
        {"cursor": manifest["catalog_cursor"], "limit": 10},
        allowed=frozenset({"list_ordinance_catalog"}),
    )
    assert catalog_result.ok is True
    assert json.loads(catalog_result.content)["total_catalog_ordinances"] == 1

    pending_manifest = build_ordinance_corpus_manifest(
        db,
        embedding_model=settings.embeddings_model,
        filters=OrdinanceCorpusFilters(include_pending=True),
    )
    denied_manifest = assistant_tools.execute_tool(
        db,
        comparer,
        "get_ordinance_corpus_manifest",
        {"include_pending": True},
        allowed=frozenset({"get_ordinance_corpus_manifest"}),
    )
    denied_catalog = assistant_tools.execute_tool(
        db,
        comparer,
        "list_ordinance_catalog",
        {"cursor": pending_manifest["catalog_cursor"]},
        allowed=frozenset({"list_ordinance_catalog"}),
    )
    assert denied_manifest.content == (
        "Error (403): Permission required: ordinances.review"
    )
    assert denied_catalog.content == (
        "Error (403): Permission required: ordinances.review"
    )


def test_catalog_payload_compaction_preserves_signed_continuation():
    filters = OrdinanceCorpusFilters(province="Burgos")
    cursor = encode_ordinance_catalog_cursor(
        filters=filters,
        snapshot_id="a" * 64,
        total=20,
        after_id=0,
        consumed=0,
    )
    payload = {
        "cursor": cursor,
        "snapshot_id": "a" * 64,
        "returned": 10,
        "total_catalog_ordinances": 20,
        "has_more": True,
        "next_cursor": None,
        "results": [
            {
                "ordinance_id": index + 1,
                "title": "x" * 2500,
            }
            for index in range(10)
        ],
    }

    serialized = assistant_tools._serialize_ordinance_catalog_payload(payload)
    compact = json.loads(serialized)
    continuation = decode_ordinance_catalog_cursor(compact["next_cursor"])

    assert len(serialized) < assistant_tools.MAX_ORDINANCE_TOOL_RESULT_CHARS
    assert compact["payload_truncated"] is True
    assert 0 < compact["returned"] < 10
    assert continuation["after_id"] == compact["results"][-1]["ordinance_id"]
    assert continuation["consumed"] == compact["returned"]


def test_semantic_search_supports_autonomous_community_filter(db, monkeypatch):
    monkeypatch.setattr(
        "app.ordinances.search._pgvector_available",
        lambda _db: False,
    )
    castile = _municipality(
        db,
        name="Castilla",
        ine_code="09040",
        population=500,
    )
    rioja = _municipality(
        db,
        name="Rioja",
        ine_code="26001",
        province="La Rioja",
        autonomous_community="La Rioja",
        population=500,
    )
    castile_ordinance = _ordinance(
        db,
        castile,
        title="Ordenanza castellana",
    )
    rioja_ordinance = _ordinance(db, rioja, title="Ordenanza riojana")
    _chunk(db, castile_ordinance, index=0)
    _chunk(db, rioja_ordinance, index=0)
    db.flush()

    page = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model=settings.embeddings_model,
        options=OrdinanceSearchOptions(
            autonomous_community="Castilla y León",
        ),
    )

    assert [item["ordinance_id"] for item in page["results"]] == [castile_ordinance.id]
    assert page["results"][0]["autonomous_community"] == "Castilla y León"


def test_prompt_separates_inventory_from_semantic_retrieval():
    assert (
        "La búsqueda semántica nunca demuestra que se haya enumerado todo el corpus"
        in ANACLETO_SYSTEM_PROMPT
    )
    assert "get_ordinance_corpus_manifest" in ANACLETO_SYSTEM_PROMPT
    assert "complete_against_official_sources=false" in ANACLETO_SYSTEM_PROMPT


def test_agent_office_routes_inventory_as_local_ordinance_reads():
    ordinance_tools = agent_office_service.OFFICE_AGENTS["ordinances"].tool_names

    assert {
        "get_ordinance_corpus_manifest",
        "list_ordinance_catalog",
        "semantic_search_ordinances",
    } <= ordinance_tools
    assert (
        agent_office_service.ACTION_TO_DEPARTMENT["get_ordinance_corpus_manifest"]
        == "ordinances"
    )
    assert "list_ordinance_catalog" in agent_office_service.TOOL_ACTIONS
    assert (
        "get_ordinance_corpus_manifest"
        not in agent_office_service.EXTERNAL_READ_ACTIONS
    )
