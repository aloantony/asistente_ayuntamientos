"""Complete, pageable retrieval over approved ordinance chunks."""

from collections.abc import Hashable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.municipalities.models import Municipality
from app.ordinances.embeddings import vector_similarity
from app.ordinances.models import Ordinance, OrdinanceLegalChunk

OrdinanceResultScope = Literal["fragments", "ordinances", "municipalities"]

TOPIC_PREFERENCE_BOOST = 0.05
MAX_RESULT_TEXT_CHARS = 900
DEFINITIVELY_INACTIVE_STATUSES = ("repealed", "superseded", "archived")
_PGVECTOR_SESSION_KEY = "ordinance_search_pgvector_available"
_SQL_TOPIC_LITERAL_MATCH = (
    "(STRPOS(LOWER(COALESCE(o.topic, '')), LOWER(:topic_literal)) > 0 "
    "OR STRPOS(LOWER(COALESCE(o.subtopic, '')), LOWER(:topic_literal)) > 0 "
    "OR STRPOS(LOWER(COALESCE(o.title, '')), LOWER(:topic_literal)) > 0)"
)


@dataclass(frozen=True)
class OrdinanceSearchOptions:
    include_pending: bool = False
    include_inactive: bool = False
    municipality_id: int | None = None
    municipality_name: str | None = None
    autonomous_community: str | None = None
    province: str | None = None
    topic: str | None = None
    strict_topic: bool = False
    population_gte: int | None = None
    population_lt: int | None = None
    result_scope: OrdinanceResultScope = "fragments"
    limit: int = 10
    offset: int = 0


def search_ordinance_chunks(
    db: Session,
    *,
    query_vector: str,
    embedding_model: str,
    options: OrdinanceSearchOptions,
) -> dict:
    """Search every eligible chunk and return an auditable result page.

    PostgreSQL with pgvector performs the full ranking in the database. Test or
    fallback databases without the extension retain the same semantics by
    scoring the complete candidate set in Python.
    """

    _validate_options(options)
    coverage = _population_coverage(db, embedding_model, options)
    if _pgvector_available(db):
        page = _search_with_pgvector(db, query_vector, embedding_model, options)
        search_backend = "pgvector"
    else:
        page = _search_with_python(db, query_vector, embedding_model, options)
        search_backend = "python"

    total_matches = page["total_matches"]
    returned_results = page["results"]
    population_filter_applied = (
        options.population_gte is not None or options.population_lt is not None
    )
    return {
        "result_scope": options.result_scope,
        "limit": options.limit,
        "offset": options.offset,
        "returned": len(returned_results),
        "total_matches": total_matches,
        "has_more": options.offset + len(returned_results) < total_matches,
        "next_offset": (
            options.offset + len(returned_results)
            if options.offset + len(returned_results) < total_matches
            else None
        ),
        "corpus_scan_complete": True,
        "search_backend": search_backend,
        "topic_filter_mode": (
            "strict"
            if options.topic and options.strict_topic
            else "preference"
            if options.topic
            else "none"
        ),
        "legal_status_filter": {
            "include_inactive": options.include_inactive,
            "excluded_statuses": (
                []
                if options.include_inactive
                else list(DEFINITIVELY_INACTIVE_STATUSES)
            ),
        },
        "population_filter": {
            "applied": population_filter_applied,
            "gte": options.population_gte,
            "lt": options.population_lt,
            "eligible_municipalities": coverage["eligible_municipalities"],
            "municipalities_with_population": coverage[
                "municipalities_with_population"
            ],
            "municipalities_without_population": coverage[
                "municipalities_without_population"
            ],
            "coverage_complete": (
                not population_filter_applied
                or coverage["municipalities_without_population"] == 0
            ),
        },
        "eligible_chunks": coverage["eligible_chunks"],
        "results": returned_results,
    }

def _validate_options(options: OrdinanceSearchOptions) -> None:
    if options.result_scope not in {"fragments", "ordinances", "municipalities"}:
        raise ValueError("result_scope no es válido")
    if options.limit < 1:
        raise ValueError("limit debe ser mayor o igual que 1")
    if options.offset < 0:
        raise ValueError("offset debe ser mayor o igual que 0")
    for value, label in (
        (options.population_gte, "population_gte"),
        (options.population_lt, "population_lt"),
    ):
        if value is not None and value < 0:
            raise ValueError(f"{label} debe ser mayor o igual que 0")
    if (
        options.population_gte is not None
        and options.population_lt is not None
        and options.population_gte >= options.population_lt
    ):
        raise ValueError("population_gte debe ser menor que population_lt")


def _pgvector_available(db: Session) -> bool:
    cached = db.info.get(_PGVECTOR_SESSION_KEY)
    if isinstance(cached, bool):
        return cached
    available = bool(
        db.scalar(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                ")"
            )
        )
    )
    db.info[_PGVECTOR_SESSION_KEY] = available
    return available


def _base_where(
    embedding_model: str,
    options: OrdinanceSearchOptions,
    *,
    include_population: bool,
) -> tuple[list[str], dict[str, object]]:
    clauses = [
        "c.embedding_status = 'ready'",
        "c.embedding IS NOT NULL",
        "c.embedding_model = :embedding_model",
    ]
    params: dict[str, object] = {"embedding_model": embedding_model}
    if options.include_pending:
        clauses.extend(
            [
                "o.curation_status <> 'rejected'",
                "c.review_status <> 'rejected'",
            ]
        )
    else:
        clauses.extend(
            [
                "o.curation_status = 'approved'",
                "c.review_status = 'approved'",
            ]
        )
    if not options.include_inactive:
        clauses.append("o.status NOT IN ('repealed', 'superseded', 'archived')")
    if options.municipality_id is not None:
        clauses.append("o.municipality_id = :municipality_id")
        params["municipality_id"] = options.municipality_id
    if options.municipality_name:
        clauses.append("m.name ILIKE :municipality_name")
        params["municipality_name"] = options.municipality_name
    if options.autonomous_community:
        clauses.append("m.autonomous_community ILIKE :autonomous_community")
        params["autonomous_community"] = options.autonomous_community
    if options.province:
        clauses.append("m.province ILIKE :province")
        params["province"] = options.province
    if options.topic:
        params["topic_literal"] = options.topic
        if options.strict_topic:
            clauses.append(_SQL_TOPIC_LITERAL_MATCH)
    if include_population:
        if options.population_gte is not None:
            clauses.append("m.population >= :population_gte")
            params["population_gte"] = options.population_gte
        if options.population_lt is not None:
            clauses.append("m.population < :population_lt")
            params["population_lt"] = options.population_lt
    return clauses, params


def _population_coverage(
    db: Session,
    embedding_model: str,
    options: OrdinanceSearchOptions,
) -> dict[str, int]:
    clauses, params = _base_where(
        embedding_model,
        options,
        include_population=False,
    )
    statement = text(
        f"""
        SELECT
            COUNT(*) AS eligible_chunks,
            COUNT(DISTINCT m.id) AS eligible_municipalities,
            COUNT(DISTINCT m.id) FILTER (WHERE m.population IS NOT NULL)
                AS municipalities_with_population,
            COUNT(DISTINCT m.id) FILTER (WHERE m.population IS NULL)
                AS municipalities_without_population
        FROM ordinance_legal_chunks c
        JOIN ordinances o ON o.id = c.ordinance_id
        JOIN municipalities m ON m.id = o.municipality_id
        WHERE {" AND ".join(clauses)}
        """
    )
    row = db.execute(statement, params).mappings().one()
    return {key: int(row[key] or 0) for key in row.keys()}


def _search_with_pgvector(
    db: Session,
    query_vector: str,
    embedding_model: str,
    options: OrdinanceSearchOptions,
) -> dict:
    clauses, params = _base_where(
        embedding_model,
        options,
        include_population=True,
    )
    params.update(
        {
            "query_vector": query_vector,
            "limit": options.limit,
            "offset": options.offset,
            "topic_boost": TOPIC_PREFERENCE_BOOST,
        }
    )
    topic_boost = "0.0"
    if options.topic and not options.strict_topic:
        topic_boost = (
            f"CASE WHEN {_SQL_TOPIC_LITERAL_MATCH} "
            "THEN :topic_boost ELSE 0.0 END"
        )
    partition_column = {
        "fragments": "chunk_id",
        "ordinances": "ordinance_id",
        "municipalities": "municipality_scope_key",
    }[options.result_scope]
    statement = text(
        f"""
        WITH scored AS (
            SELECT
                c.id AS chunk_id,
                c.ordinance_id,
                m.id AS municipality_id,
                COALESCE(
                    NULLIF(m.ine_code, ''),
                    lower(m.name) || '|' || lower(m.province)
                ) AS municipality_scope_key,
                1 - (c.embedding::vector <=> CAST(:query_vector AS vector))
                    AS similarity,
                1 - (c.embedding::vector <=> CAST(:query_vector AS vector))
                    + {topic_boost} AS score
            FROM ordinance_legal_chunks c
            JOIN ordinances o ON o.id = c.ordinance_id
            JOIN municipalities m ON m.id = o.municipality_id
            WHERE {" AND ".join(clauses)}
        ),
        relevant AS (
            SELECT * FROM scored WHERE similarity > 0
        ),
        ranked AS (
            SELECT
                relevant.*,
                ROW_NUMBER() OVER (
                    PARTITION BY {partition_column}
                    ORDER BY score DESC, chunk_id
                ) AS scope_rank
            FROM relevant
        ),
        selected AS (
            SELECT * FROM ranked WHERE scope_rank = 1
        ),
        page AS (
            SELECT selected.*, COUNT(*) OVER () AS total_matches
            FROM selected
            ORDER BY score DESC, chunk_id
            LIMIT :limit OFFSET :offset
        )
        SELECT
            page.*,
            o.title,
            o.topic,
            o.status,
            o.curation_status,
            o.approval_date,
            o.publication_date,
            o.effective_date,
            m.name AS municipality_name,
            m.province,
            m.autonomous_community,
            m.population,
            c.chunk_index,
            c.heading,
            c.citation,
            c.source_locator,
            c.text,
            COALESCE(c.source_url, o.source_url) AS source_url
        FROM page
        JOIN ordinance_legal_chunks c ON c.id = page.chunk_id
        JOIN ordinances o ON o.id = page.ordinance_id
        JOIN municipalities m ON m.id = page.municipality_id
        ORDER BY page.score DESC, page.chunk_id
        """
    )
    rows = list(db.execute(statement, params).mappings())
    if rows:
        total_matches = int(rows[0]["total_matches"])
    elif options.offset:
        first_page_params = {**params, "limit": 1, "offset": 0}
        first_row = db.execute(statement, first_page_params).mappings().first()
        total_matches = int(first_row["total_matches"]) if first_row else 0
    else:
        total_matches = 0
    return {
        "total_matches": total_matches,
        "results": [_serialize_mapping(row) for row in rows],
    }


def _search_with_python(
    db: Session,
    query_vector: str,
    embedding_model: str,
    options: OrdinanceSearchOptions,
) -> dict:
    query = (
        select(OrdinanceLegalChunk)
        .join(OrdinanceLegalChunk.ordinance)
        .join(Ordinance.municipality)
        .where(
            OrdinanceLegalChunk.embedding_status == "ready",
            OrdinanceLegalChunk.embedding.is_not(None),
            OrdinanceLegalChunk.embedding_model == embedding_model,
        )
        .options(
            selectinload(OrdinanceLegalChunk.ordinance).selectinload(
                Ordinance.municipality
            )
        )
    )
    if options.include_pending:
        query = query.where(
            Ordinance.curation_status != "rejected",
            OrdinanceLegalChunk.review_status != "rejected",
        )
    else:
        query = query.where(
            Ordinance.curation_status == "approved",
            OrdinanceLegalChunk.review_status == "approved",
        )
    if not options.include_inactive:
        query = query.where(
            Ordinance.status.not_in(DEFINITIVELY_INACTIVE_STATUSES)
        )
    if options.municipality_id is not None:
        query = query.where(Ordinance.municipality_id == options.municipality_id)
    if options.municipality_name:
        query = query.where(Municipality.name.ilike(options.municipality_name))
    if options.autonomous_community:
        query = query.where(
            Municipality.autonomous_community.ilike(
                options.autonomous_community
            )
        )
    if options.province:
        query = query.where(Municipality.province.ilike(options.province))
    chunks = list(db.scalars(query))
    if options.topic and options.strict_topic:
        chunks = [
            chunk for chunk in chunks if _python_topic_matches(chunk, options.topic)
        ]
    population_filtered = [
        chunk for chunk in chunks if _population_matches(chunk, options)
    ]
    scored: list[tuple[float, OrdinanceLegalChunk]] = []
    for chunk in population_filtered:
        similarity = vector_similarity(query_vector, chunk.embedding)
        if similarity <= 0:
            continue
        score = similarity + _python_topic_boost(chunk, options)
        scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1].id))

    seen: set[Hashable] = set()
    selected: list[tuple[float, OrdinanceLegalChunk]] = []
    for score, chunk in scored:
        scope_id = _scope_id(chunk, options.result_scope)
        if scope_id in seen:
            continue
        seen.add(scope_id)
        selected.append((score, chunk))
    page = selected[options.offset : options.offset + options.limit]
    return {
        "total_matches": len(selected),
        "results": [_serialize_chunk(score, chunk) for score, chunk in page],
    }


def _population_matches(
    chunk: OrdinanceLegalChunk,
    options: OrdinanceSearchOptions,
) -> bool:
    population = chunk.ordinance.municipality.population
    if options.population_gte is None and options.population_lt is None:
        return True
    if population is None:
        return False
    if options.population_gte is not None and population < options.population_gte:
        return False
    return options.population_lt is None or population < options.population_lt


def _python_topic_boost(
    chunk: OrdinanceLegalChunk,
    options: OrdinanceSearchOptions,
) -> float:
    if not options.topic or options.strict_topic:
        return 0.0
    return (
        TOPIC_PREFERENCE_BOOST
        if _python_topic_matches(chunk, options.topic)
        else 0.0
    )


def _python_topic_matches(chunk: OrdinanceLegalChunk, topic: str) -> bool:
    topic_literal = topic.casefold()
    ordinance = chunk.ordinance
    values = (ordinance.topic, ordinance.subtopic, ordinance.title)
    return any(topic_literal in (value or "").casefold() for value in values)


def _scope_id(
    chunk: OrdinanceLegalChunk,
    result_scope: OrdinanceResultScope,
) -> Hashable:
    if result_scope == "municipalities":
        municipality = chunk.ordinance.municipality
        return municipality.ine_code or (
            municipality.name.casefold(),
            municipality.province.casefold(),
        )
    if result_scope == "ordinances":
        return chunk.ordinance_id
    return chunk.id


def _serialize_chunk(score: float, chunk: OrdinanceLegalChunk) -> dict:
    ordinance = chunk.ordinance
    municipality = ordinance.municipality
    return _serialize_result(
        chunk_id=chunk.id,
        ordinance_id=chunk.ordinance_id,
        title=ordinance.title,
        municipality_id=ordinance.municipality_id,
        municipality_name=municipality.name,
        province=municipality.province,
        autonomous_community=municipality.autonomous_community,
        population=municipality.population,
        topic=ordinance.topic,
        status=ordinance.status,
        curation_status=ordinance.curation_status,
        approval_date=ordinance.approval_date,
        publication_date=ordinance.publication_date,
        effective_date=ordinance.effective_date,
        chunk_index=chunk.chunk_index,
        heading=chunk.heading,
        citation=chunk.citation,
        source_locator=chunk.source_locator,
        chunk_text=chunk.text,
        source_url=chunk.source_url or ordinance.source_url,
        score=score,
    )


def _serialize_mapping(row) -> dict:
    return _serialize_result(
        chunk_id=row["chunk_id"],
        ordinance_id=row["ordinance_id"],
        title=row["title"],
        municipality_id=row["municipality_id"],
        municipality_name=row["municipality_name"],
        province=row["province"],
        autonomous_community=row["autonomous_community"],
        population=row["population"],
        topic=row["topic"],
        status=row["status"],
        curation_status=row["curation_status"],
        approval_date=row["approval_date"],
        publication_date=row["publication_date"],
        effective_date=row["effective_date"],
        chunk_index=row["chunk_index"],
        heading=row["heading"],
        citation=row["citation"],
        source_locator=row["source_locator"],
        chunk_text=row["text"],
        source_url=row["source_url"],
        score=float(row["score"]),
    )


def _serialize_result(
    *,
    chunk_id: int,
    ordinance_id: int,
    title: str,
    municipality_id: int,
    municipality_name: str,
    province: str,
    autonomous_community: str,
    population: int | None,
    topic: str,
    status: str,
    curation_status: str,
    approval_date: date | None,
    publication_date: date | None,
    effective_date: date | None,
    chunk_index: int,
    heading: str | None,
    citation: str | None,
    source_locator: str | None,
    chunk_text: str,
    source_url: str | None,
    score: float,
) -> dict:
    text_truncated = len(chunk_text) > MAX_RESULT_TEXT_CHARS
    return {
        "chunk_id": chunk_id,
        "ordinance_id": ordinance_id,
        "title": title,
        "municipality_id": municipality_id,
        "municipality_name": municipality_name,
        "province": province,
        "autonomous_community": autonomous_community,
        "population": population,
        "topic": topic,
        "status": status,
        "curation_status": curation_status,
        "approval_date": approval_date,
        "publication_date": publication_date,
        "effective_date": effective_date,
        "chunk_index": chunk_index,
        "heading": heading,
        "citation": citation,
        "source_locator": source_locator,
        "text": (
            f"{chunk_text[:MAX_RESULT_TEXT_CHARS].rstrip()}…"
            if text_truncated
            else chunk_text
        ),
        "text_truncated": text_truncated,
        "source_url": source_url,
        "score": round(score, 4),
    }

