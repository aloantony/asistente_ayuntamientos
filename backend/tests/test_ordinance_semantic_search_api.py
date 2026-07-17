from app.ordinances import routes as ordinance_routes
from app.ordinances.schemas import OrdinanceSemanticSearchResult


def test_semantic_search_route_forwards_autonomous_community(
    db,
    monkeypatch,
):
    captured = {}

    monkeypatch.setattr(
        ordinance_routes,
        "require_ordinance_permission",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        ordinance_routes,
        "embed_text",
        lambda query: ("[0.1,0.2]", "test-model", "ready"),
    )

    def fake_search(
        db_session,
        *,
        query_vector,
        embedding_model,
        options,
    ):
        captured["db"] = db_session
        captured["query_vector"] = query_vector
        captured["embedding_model"] = embedding_model
        captured["options"] = options
        return {"results": []}

    monkeypatch.setattr(
        ordinance_routes,
        "search_ordinance_chunks",
        fake_search,
    )

    response = ordinance_routes.semantic_search_ordinances(
        db=db,
        current_user=object(),
        q="ordenanzas de agua",
        municipality_id=None,
        municipality_name=None,
        autonomous_community="  Castilla y León  ",
        topic=None,
        include_pending=False,
        include_inactive=False,
        limit=10,
    )

    assert response == []
    assert captured["db"] is db
    assert captured["query_vector"] == "[0.1,0.2]"
    assert captured["embedding_model"] == "test-model"
    assert captured["options"].autonomous_community == "Castilla y León"


def test_semantic_search_response_schema_exposes_autonomous_community():
    result = OrdinanceSemanticSearchResult.model_validate(
        {
            "chunk_id": 1,
            "ordinance_id": 2,
            "title": "Ordenanza de abastecimiento",
            "municipality_id": 3,
            "municipality_name": "Municipio",
            "province": "Burgos",
            "autonomous_community": "Castilla y León",
            "population": 950,
            "topic": "Agua",
            "status": "active",
            "curation_status": "approved",
            "approval_date": None,
            "publication_date": None,
            "effective_date": None,
            "chunk_index": 0,
            "heading": "Artículo 1",
            "citation": "art. 1",
            "source_locator": "p. 1",
            "text": "Texto normativo",
            "text_truncated": False,
            "source_url": "https://example.invalid/ordenanza",
            "score": 0.9,
        }
    )

    assert result.autonomous_community == "Castilla y León"
    assert result.model_dump()["autonomous_community"] == "Castilla y León"
