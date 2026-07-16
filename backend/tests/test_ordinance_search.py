import json

from sqlalchemy import text

from app.assistant import tools as assistant_tools
from app.assistant.prompts import build_ordinance_coverage_block
from app.municipalities.models import Municipality
from app.ordinances.models import Ordinance, OrdinanceLegalChunk
from app.ordinances import search as ordinance_search
from app.ordinances.search import OrdinanceSearchOptions, search_ordinance_chunks


def _municipality(
    db,
    *,
    name: str,
    ine_code: str | None,
    population: int | None = None,
    province: str = "Burgos",
) -> Municipality:
    municipality = Municipality(
        name=name,
        province=province,
        autonomous_community="Castilla y León",
        ine_code=ine_code,
        population=population,
    )
    db.add(municipality)
    db.flush()
    return municipality


def _ordinance(
    db,
    municipality: Municipality,
    *,
    title: str,
    topic: str,
    curation_status: str = "approved",
) -> Ordinance:
    ordinance = Ordinance(
        municipality_id=municipality.id,
        title=title,
        topic=topic,
        ordinance_type="ordinance",
        status="active",
        curation_status=curation_status,
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
    embedding: str,
    review_status: str = "approved",
) -> OrdinanceLegalChunk:
    chunk = OrdinanceLegalChunk(
        ordinance_id=ordinance.id,
        chunk_index=index,
        citation=f"art. {index + 1}",
        text=f"Contenido normativo {ordinance.title} {index}",
        review_status=review_status,
        embedding_model="test-model",
        embedding=embedding,
        embedding_status="ready",
    )
    db.add(chunk)
    return chunk


def _python_search(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.ordinances.search._pgvector_available",
        lambda db: False,
    )


def test_search_scores_best_chunk_after_the_old_500_row_cutoff(db, monkeypatch):
    _python_search(monkeypatch)
    municipality = _municipality(
        db,
        name="Villa Completa",
        ine_code="09001",
    )
    ordinance = _ordinance(
        db,
        municipality,
        title="Ordenanza de caminos",
        topic="caminos",
    )
    for index in range(500):
        _chunk(db, ordinance, index=index, embedding="[0,1]")
    target = _chunk(db, ordinance, index=500, embedding="[1,0]")
    db.flush()

    page = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(limit=1),
    )

    assert page["eligible_chunks"] == 501
    assert page["corpus_scan_complete"] is True
    assert page["total_matches"] == 1
    assert page["results"][0]["chunk_id"] == target.id


def test_municipality_scope_is_diverse_pageable_and_logically_deduplicated(
    db,
    monkeypatch,
):
    _python_search(monkeypatch)
    first = _municipality(db, name="Primero", ine_code="09011")
    second = _municipality(db, name="Segundo", ine_code="09012")
    duplicate_a = _municipality(db, name="Villa Repetida", ine_code=None)
    duplicate_b = _municipality(db, name="Villa Repetida", ine_code=None)
    for index, (municipality, embedding) in enumerate(
        [
            (first, "[1,0]"),
            (second, "[0.9,0.1]"),
            (duplicate_a, "[0.8,0.2]"),
            (duplicate_b, "[0.7,0.3]"),
        ]
    ):
        ordinance = _ordinance(
            db,
            municipality,
            title=f"Ordenanza {index}",
            topic="vías públicas",
        )
        _chunk(db, ordinance, index=0, embedding=embedding)
    db.flush()

    first_page = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(
            result_scope="municipalities",
            limit=2,
        ),
    )
    second_page = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(
            result_scope="municipalities",
            limit=2,
            offset=2,
        ),
    )

    assert first_page["total_matches"] == 3
    assert first_page["returned"] == 2
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 2
    assert second_page["total_matches"] == 3
    assert second_page["returned"] == 1
    assert second_page["has_more"] is False
    assert len(
        {
            result["municipality_name"]
            for result in first_page["results"] + second_page["results"]
        }
    ) == 3


def test_population_filter_reports_unknown_coverage_without_including_it(
    db,
    monkeypatch,
):
    _python_search(monkeypatch)
    municipalities = [
        _municipality(db, name="Pequeño", ine_code="09101", population=4999),
        _municipality(db, name="Grande", ine_code="09102", population=5000),
        _municipality(db, name="Desconocido", ine_code="09103", population=None),
    ]
    for index, municipality in enumerate(municipalities):
        ordinance = _ordinance(
            db,
            municipality,
            title=f"Ordenanza demográfica {index}",
            topic="caminos",
        )
        _chunk(db, ordinance, index=0, embedding="[1,0]")
    db.flush()

    page = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(
            population_lt=5000,
            result_scope="municipalities",
        ),
    )

    assert [result["municipality_name"] for result in page["results"]] == [
        "Pequeño"
    ]
    assert page["population_filter"] == {
        "applied": True,
        "gte": None,
        "lt": 5000,
        "eligible_municipalities": 3,
        "municipalities_with_population": 2,
        "municipalities_without_population": 1,
        "coverage_complete": False,
    }


def test_topic_is_a_preference_unless_strictly_requested(db, monkeypatch):
    _python_search(monkeypatch)
    roads = _municipality(db, name="Caminos", ine_code="09201")
    taxes = _municipality(db, name="Tasas", ine_code="09202")
    roads_ordinance = _ordinance(
        db,
        roads,
        title="Ordenanza de caminos rurales",
        topic="caminos rurales",
    )
    taxes_ordinance = _ordinance(
        db,
        taxes,
        title="Ordenanza fiscal",
        topic="tasas",
    )
    _chunk(db, roads_ordinance, index=0, embedding="[0.999,0.04]")
    _chunk(db, taxes_ordinance, index=0, embedding="[1,0]")
    db.flush()

    preferred = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(topic="caminos rurales"),
    )
    strict = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(
            topic="caminos rurales",
            strict_topic=True,
        ),
    )

    assert preferred["topic_filter_mode"] == "preference"
    assert preferred["total_matches"] == 2
    assert preferred["results"][0]["municipality_name"] == "Caminos"
    assert strict["topic_filter_mode"] == "strict"
    assert strict["total_matches"] == 1
    assert strict["results"][0]["municipality_name"] == "Caminos"


def test_include_pending_never_returns_rejected_chunks(db, monkeypatch):
    _python_search(monkeypatch)
    municipality = _municipality(db, name="Revisión", ine_code="09301")
    ordinance = _ordinance(
        db,
        municipality,
        title="Ordenanza pendiente",
        topic="vías",
        curation_status="pending_review",
    )
    accepted = _chunk(
        db,
        ordinance,
        index=0,
        embedding="[1,0]",
        review_status="pending_review",
    )
    _chunk(
        db,
        ordinance,
        index=1,
        embedding="[1,0]",
        review_status="rejected",
    )
    db.flush()

    page = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(include_pending=True),
    )

    assert page["total_matches"] == 1
    assert page["results"][0]["chunk_id"] == accepted.id


def test_pgvector_backend_matches_python_and_preserves_total_on_empty_page(db):
    db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    for index, embedding in enumerate(("[1,0]", "[0.9,0.1]", "[0.8,0.2]")):
        municipality = _municipality(
            db,
            name=f"Vector {index}",
            ine_code=f"0940{index}",
        )
        ordinance = _ordinance(
            db,
            municipality,
            title=f"Ordenanza vector {index}",
            topic="vías públicas",
        )
        _chunk(db, ordinance, index=0, embedding=embedding)
    db.flush()
    options = OrdinanceSearchOptions(
        result_scope="municipalities",
        limit=2,
    )

    python_page = ordinance_search._search_with_python(
        db,
        "[1,0]",
        "test-model",
        options,
    )
    vector_page = ordinance_search._search_with_pgvector(
        db,
        "[1,0]",
        "test-model",
        options,
    )
    empty_page = ordinance_search._search_with_pgvector(
        db,
        "[1,0]",
        "test-model",
        OrdinanceSearchOptions(
            result_scope="municipalities",
            limit=2,
            offset=3,
        ),
    )

    assert vector_page["total_matches"] == python_page["total_matches"] == 3
    assert [item["chunk_id"] for item in vector_page["results"]] == [
        item["chunk_id"] for item in python_page["results"]
    ]
    assert empty_page == {"total_matches": 3, "results": []}


def test_ordinance_tool_payload_remains_valid_and_resumable_when_compacted():
    payload = {
        "query": "dominio público viario",
        "offset": 0,
        "returned": 20,
        "total_matches": 45,
        "has_more": True,
        "next_offset": 20,
        "results": [
            {
                "chunk_id": index,
                "municipality_name": f"Municipio {index}",
                "source_url": f"https://example.test/{index}",
                "text": "x" * 900,
            }
            for index in range(20)
        ],
    }

    serialized = assistant_tools._serialize_ordinance_search_payload(payload)
    compact = json.loads(serialized)

    assert len(serialized) < assistant_tools.MAX_ORDINANCE_TOOL_RESULT_CHARS
    assert compact["payload_truncated"] is True
    assert compact["page_candidates"] == 20
    assert 0 < compact["returned"] < 20
    assert compact["next_offset"] == compact["returned"]
    assert compact["has_more"] is True


def test_coverage_prompt_counts_the_complete_corpus(db):
    for index in range(81):
        municipality = _municipality(
            db,
            name=f"Municipio {index}",
            ine_code=f"{index + 10000:05d}",
        )
        _ordinance(
            db,
            municipality,
            title=f"Ordenanza {index}",
            topic="vías",
        )
    db.flush()

    coverage = build_ordinance_coverage_block(db)

    assert "81 ordenanzas de 81 municipios" in coverage
    assert "no es una lista parcial" in coverage
