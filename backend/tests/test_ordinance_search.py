import json
from datetime import date

import pytest
from sqlalchemy import text

from app.assistant import tools as assistant_tools
from app.assistant.prompts import build_ordinance_coverage_block
from app.core.config import settings
from app.municipalities.models import Municipality
from app.ordinances.models import Ordinance, OrdinanceLegalChunk
from app.ordinances import search as ordinance_search
from app.ordinances.search import OrdinanceSearchOptions, search_ordinance_chunks
from conftest import headers_for


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
    status: str = "active",
) -> Ordinance:
    ordinance = Ordinance(
        municipality_id=municipality.id,
        title=title,
        topic=topic,
        ordinance_type="ordinance",
        status=status,
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
    embedding: str | None,
    review_status: str = "approved",
    embedding_model: str = "test-model",
) -> OrdinanceLegalChunk:
    chunk = OrdinanceLegalChunk(
        ordinance_id=ordinance.id,
        chunk_index=index,
        citation=f"art. {index + 1}",
        text=f"Contenido normativo {ordinance.title} {index}",
        review_status=review_status,
        embedding_model=embedding_model,
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


@pytest.mark.parametrize("topic_literal", ["%", "_"])
def test_topic_filter_treats_sql_wildcards_as_literals_across_backends(
    db,
    topic_literal,
):
    db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    literal_municipality = _municipality(
        db,
        name=f"Tema literal {ord(topic_literal)}",
        ine_code=f"092{ord(topic_literal):02d}",
    )
    plain_municipality = _municipality(
        db,
        name=f"Tema sin literal {ord(topic_literal)}",
        ine_code=f"093{ord(topic_literal):02d}",
    )
    literal_ordinance = _ordinance(
        db,
        literal_municipality,
        title="Ordenanza con signo literal",
        topic=f"tasa{topic_literal}especial",
    )
    plain_ordinance = _ordinance(
        db,
        plain_municipality,
        title="Ordenanza fiscal ordinaria",
        topic="tasa municipal ordinaria",
    )
    _chunk(db, literal_ordinance, index=0, embedding="[0.999,0.04]")
    _chunk(db, plain_ordinance, index=0, embedding="[1,0]")
    db.flush()
    strict_options = OrdinanceSearchOptions(
        topic=topic_literal,
        strict_topic=True,
    )
    preference_options = OrdinanceSearchOptions(topic=topic_literal)

    for backend in (
        ordinance_search._search_with_python,
        ordinance_search._search_with_pgvector,
    ):
        page = backend(db, "[1,0]", "test-model", strict_options)
        assert page["total_matches"] == 1
        assert [result["ordinance_id"] for result in page["results"]] == [
            literal_ordinance.id
        ]
        preferred_page = backend(db, "[1,0]", "test-model", preference_options)
        assert preferred_page["total_matches"] == 2
        assert preferred_page["results"][0]["ordinance_id"] == literal_ordinance.id


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


def test_legacy_semantic_search_gates_pending_and_opts_into_inactive(
    client,
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    _python_search(monkeypatch)
    monkeypatch.setattr(
        "app.ordinances.routes.embed_text",
        lambda _query: ("[1,0]", "test-model", "ready"),
    )
    municipality = _municipality(db, name="Histórico", ine_code="09309")
    pending = _ordinance(
        db,
        municipality,
        title="Ordenanza pendiente",
        topic="convivencia",
        curation_status="pending_review",
    )
    inactive = _ordinance(
        db,
        municipality,
        title="Ordenanza derogada",
        topic="convivencia",
        status="repealed",
    )
    _chunk(
        db,
        pending,
        index=0,
        embedding="[1,0]",
        review_status="pending_review",
    )
    _chunk(db, inactive, index=0, embedding="[1,0]")
    db.commit()

    comparer = make_user()
    grant_permissions(comparer, make_organization(), ["ordinances.compare"])
    denied = client.get(
        "/ordinances/semantic-search",
        headers=headers_for(comparer),
        params={"q": "convivencia", "include_pending": "true"},
    )
    current = client.get(
        "/ordinances/semantic-search",
        headers=headers_for(comparer),
        params={"q": "convivencia"},
    )
    historical = client.get(
        "/ordinances/semantic-search",
        headers=headers_for(comparer),
        params={"q": "convivencia", "include_inactive": "true"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Permission required: ordinances.review"
    assert current.status_code == 200
    assert current.json() == []
    assert historical.status_code == 200
    assert [result["ordinance_id"] for result in historical.json()] == [
        inactive.id
    ]

    reviewer = make_user()
    grant_permissions(
        reviewer,
        make_organization(),
        ["ordinances.compare", "ordinances.review"],
    )
    pending_result = client.get(
        "/ordinances/semantic-search",
        headers=headers_for(reviewer),
        params={"q": "convivencia", "include_pending": "true"},
    )

    assert pending_result.status_code == 200
    assert [result["ordinance_id"] for result in pending_result.json()] == [
        pending.id
    ]


def test_search_excludes_definitively_inactive_ordinances_by_default(
    db,
    monkeypatch,
):
    _python_search(monkeypatch)
    municipality = _municipality(db, name="Vigencia", ine_code="09302")
    ordinances = {}
    for index, status in enumerate(
        (
            "active",
            "partially_repealed",
            "unknown",
            "repealed",
            "superseded",
            "archived",
        )
    ):
        ordinance = _ordinance(
            db,
            municipality,
            title=f"Ordenanza {status}",
            topic="convivencia",
            status=status,
        )
        ordinance.approval_date = date(2024, 12, index + 1)
        ordinance.publication_date = date(2025, 1, index + 1)
        ordinance.effective_date = date(2025, 2, index + 1)
        _chunk(db, ordinance, index=0, embedding="[1,0]")
        ordinances[status] = ordinance
    db.flush()

    current = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(limit=20),
    )
    historical = search_ordinance_chunks(
        db,
        query_vector="[1,0]",
        embedding_model="test-model",
        options=OrdinanceSearchOptions(include_inactive=True, limit=20),
    )

    assert {result["status"] for result in current["results"]} == {
        "active",
        "partially_repealed",
        "unknown",
    }
    assert current["legal_status_filter"] == {
        "include_inactive": False,
        "excluded_statuses": ["repealed", "superseded", "archived"],
    }
    assert historical["total_matches"] == 6
    active_result = next(
        result
        for result in historical["results"]
        if result["ordinance_id"] == ordinances["active"].id
    )
    assert active_result["approval_date"] == date(2024, 12, 1)
    assert active_result["publication_date"] == date(2025, 1, 1)
    assert active_result["effective_date"] == date(2025, 2, 1)


def test_search_endpoint_returns_page_metadata_to_view_only_user(
    client,
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    _python_search(monkeypatch)
    monkeypatch.setattr(
        "app.ordinances.routes.embed_text",
        lambda _query: ("[1,0]", "test-model", "ready"),
    )
    municipality = _municipality(db, name="Villa Consulta", ine_code="09303")
    ordinance = _ordinance(
        db,
        municipality,
        title="Ordenanza de terrazas",
        topic="terrazas",
    )
    chunk = _chunk(db, ordinance, index=0, embedding="[1,0]")
    db.commit()
    reader = make_user()
    grant_permissions(reader, make_organization(), ["ordinances.view"])

    response = client.get(
        "/ordinances/search",
        headers=headers_for(reader),
        params={
            "q": "ocupación de vía pública",
            "municipality_name": "Villa Consulta",
            "result_scope": "ordinances",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == "ocupación de vía pública"
    assert body["result_scope"] == "ordinances"
    assert body["total_matches"] == 1
    assert body["results"][0]["chunk_id"] == chunk.id
    assert body["results"][0]["status"] == "active"
    assert body["legal_status_filter"]["include_inactive"] is False


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


def test_assistant_ordinance_tool_accepts_manage_and_propagates_sensitive_flags(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, make_organization(), ["ordinances.manage"])
    captured_options = []
    monkeypatch.setattr(
        assistant_tools,
        "embed_text",
        lambda _query: ("[1,0]", "test-model", "ready"),
    )

    def fake_search(
        _db,
        *,
        query_vector,
        embedding_model,
        options,
    ):
        assert query_vector == "[1,0]"
        assert embedding_model == "test-model"
        captured_options.append(options)
        return {
            "offset": options.offset,
            "total_matches": 0,
            "results": [],
        }

    monkeypatch.setattr(assistant_tools, "search_ordinance_chunks", fake_search)

    available = assistant_tools.get_available_tool_specs(
        db,
        manager,
        frozenset({"semantic_search_ordinances"}),
    )
    result = assistant_tools.execute_tool(
        db,
        manager,
        "semantic_search_ordinances",
        {
            "query": "ordenanza histórica pendiente",
            "include_pending": True,
            "include_inactive": True,
        },
        allowed=frozenset({"semantic_search_ordinances"}),
    )

    schema = assistant_tools.TOOL_CATALOG[
        "semantic_search_ordinances"
    ].input_schema["properties"]
    assert "include_inactive" in schema
    assert [spec.name for spec in available] == ["semantic_search_ordinances"]
    assert result.ok is True
    assert len(captured_options) == 1
    assert captured_options[0].include_pending is True
    assert captured_options[0].include_inactive is True


def test_assistant_ordinance_tool_rejects_pending_without_review_before_embedding(
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    comparer = make_user()
    grant_permissions(comparer, make_organization(), ["ordinances.compare"])
    embedding_called = False

    def unexpected_embedding(_query):
        nonlocal embedding_called
        embedding_called = True
        raise AssertionError("No debe generar embeddings sin permiso de revisión")

    monkeypatch.setattr(assistant_tools, "embed_text", unexpected_embedding)

    result = assistant_tools.execute_tool(
        db,
        comparer,
        "semantic_search_ordinances",
        {"query": "borrador", "include_pending": True},
        allowed=frozenset({"semantic_search_ordinances"}),
    )

    assert result.ok is False
    assert result.content == "Error (403): Permission required: ordinances.review"
    assert embedding_called is False


def test_coverage_prompt_counts_the_complete_corpus(db):
    for index in range(81):
        municipality = _municipality(
            db,
            name=f"Municipio {index}",
            ine_code=f"{index + 10000:05d}",
        )
        ordinance = _ordinance(
            db,
            municipality,
            title=f"Ordenanza {index}",
            topic="vías",
        )
        _chunk(
            db,
            ordinance,
            index=0,
            embedding="[1,0]",
            embedding_model=settings.embeddings_model,
        )
    unsearchable_municipality = _municipality(
        db,
        name="Municipio sin vector",
        ine_code="99999",
    )
    unsearchable_ordinance = _ordinance(
        db,
        unsearchable_municipality,
        title="Ordenanza marcada ready sin vector",
        topic="vías",
    )
    _chunk(
        db,
        unsearchable_ordinance,
        index=0,
        embedding=None,
        embedding_model=settings.embeddings_model,
    )
    db.flush()

    coverage = build_ordinance_coverage_block(db)

    assert "81 ordenanzas de 81 municipios" in coverage
    assert "no es una lista parcial" in coverage
