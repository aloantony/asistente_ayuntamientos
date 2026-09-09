import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status as http_status,
)
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.rate_limit import (
    ordinance_import_rate_limiter,
    require_rate_limit_slot,
)
from app.core.jobs import get_default_queue
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.documents.access import user_can_access_document
from app.documents.models import Document
from app.municipalities.models import Municipality
from app.ordinances.bop_burgos import (
    build_burgos_coverage_report,
    retry_failed_burgos_embeddings,
)
from app.ordinances.embeddings import EmbeddingsUnavailableError, embed_text
from app.ordinances.import_service import (
    ImportSourceError,
    create_review_report,
    dispatch_ordinance_embeddings,
    is_official_source_url,
    is_valid_official_source_definition,
    rebuild_ordinance_chunks,
    run_import_job,
)
from app.ordinances.models import (
    OfficialLegalSource,
    Ordinance,
    OrdinanceImportItem,
    OrdinanceImportJob,
    OrdinanceLegalChunk,
    OrdinanceReviewReport,
)
from app.ordinances.schemas import (
    OfficialLegalSourceCreate,
    OfficialLegalSourceRead,
    OfficialLegalSourceUpdate,
    OrdinanceComparisonRead,
    OrdinanceCoverageRead,
    OrdinanceCreate,
    OrdinanceEmbeddingDispatchRead,
    OrdinanceEmbeddingRetryRead,
    OrdinanceImportEnqueueRead,
    OrdinanceImportItemRead,
    OrdinanceImportItemReviewUpdate,
    OrdinanceImportJobCreate,
    OrdinanceImportJobDetail,
    OrdinanceImportJobRead,
    OrdinanceLegalChunkRead,
    OrdinanceListRead,
    OrdinanceRead,
    OrdinanceReviewReportRead,
    OrdinanceSearchRead,
    OrdinanceSemanticSearchResult,
    OrdinanceStatus,
    OrdinanceUpdate,
)
from app.ordinances.search import (
    DEFINITIVELY_INACTIVE_STATUSES,
    OrdinanceResultScope,
    OrdinanceSearchOptions,
    search_ordinance_chunks,
)
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/ordinances", tags=["ordinances"])

REVIEW_SENSITIVE_ORDINANCE_FIELDS = {
    "municipality_id",
    "document_id",
    "title",
    "topic",
    "subtopic",
    "ordinance_type",
    "summary",
    "source_url",
    "official_bulletin",
    "bulletin_number",
    "approval_date",
    "publication_date",
    "effective_date",
    "text_content",
    "legal_review_notes",
}


@router.get("/coverage/cyl")
def get_cyl_coverage(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from app.ordinances.coverage import build_cyl_coverage
    require_ordinance_permission(db, current_user, "ordinances.view")
    return build_cyl_coverage(db)


@router.get("", response_model=list[OrdinanceListRead])
def list_ordinances(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    q: str | None = None,
    municipality_id: int | None = None,
    province: str | None = None,
    autonomous_community: str | None = None,
    topic: str | None = None,
    status_filter: Annotated[
        OrdinanceStatus | None,
        Query(alias="status"),
    ] = None,
    curation_status: str | None = None,
    include_archived: bool = False,
) -> list[OrdinanceListRead]:
    require_ordinance_permission(db, current_user, "ordinances.view")
    staff_read = has_ordinance_staff_read(db, current_user)
    if curation_status and curation_status != "approved" and not staff_read:
        require_ordinance_staff_read(db, current_user)

    query = select_ordinances_with_summaries().order_by(
        Ordinance.publication_date.desc().nullslast(),
        Ordinance.approval_date.desc().nullslast(),
        Ordinance.id.desc(),
    )
    if q:
        search_text = f"%{q.strip()}%"
        query = query.where(
            or_(
                Ordinance.title.ilike(search_text),
                Ordinance.topic.ilike(search_text),
                Ordinance.subtopic.ilike(search_text),
                Ordinance.summary.ilike(search_text),
                Ordinance.official_bulletin.ilike(search_text),
                Ordinance.bulletin_number.ilike(search_text),
                Municipality.name.ilike(search_text),
                Municipality.province.ilike(search_text),
            )
        )
    if municipality_id is not None:
        query = query.where(Ordinance.municipality_id == municipality_id)
    if province:
        query = query.where(Municipality.province.ilike(province.strip()))
    if autonomous_community:
        query = query.where(
            Municipality.autonomous_community.ilike(autonomous_community.strip())
        )
    if topic:
        query = query.where(Ordinance.topic.ilike(topic.strip()))
    if status_filter is not None:
        query = query.where(Ordinance.status == status_filter)
    elif not include_archived:
        query = query.where(Ordinance.status != "archived")
    if curation_status:
        query = query.where(Ordinance.curation_status == curation_status)
    elif not staff_read:
        query = query.where(Ordinance.curation_status == "approved")

    return [
        serialize_ordinance_list_item(ordinance)
        for ordinance in db.scalars(paginate(db, query, page, response))
    ]


@router.post(
    "",
    response_model=OrdinanceRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_ordinance(
    payload: OrdinanceCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OrdinanceRead:
    require_ordinance_permission(db, current_user, "ordinances.create")
    if payload.status == "archived":
        require_ordinance_permission(db, current_user, "ordinances.archive")
    elif payload.status != "unknown":
        require_ordinance_permission(db, current_user, "ordinances.review")
    if payload.curation_status in {"approved", "rejected"}:
        require_ordinance_permission(db, current_user, "ordinances.review")
    ensure_active_municipality(db, payload.municipality_id)
    ensure_document_access(db, current_user, payload.document_id)

    ordinance = Ordinance(
        **payload.model_dump(),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(ordinance)
    db.flush()
    if ordinance.text_content:
        try:
            rebuild_ordinance_chunks(
                db,
                ordinance,
                review_status=(
                    "approved"
                    if ordinance.curation_status == "approved"
                    else "rejected"
                    if ordinance.curation_status == "rejected"
                    else "pending_review"
                ),
                generate_embeddings=False,
            )
        except ImportSourceError as error:
            db.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
    db.commit()
    dispatch_ordinance_embeddings(db, ordinance.id)

    return serialize_ordinance_detail(
        db,
        current_user,
        get_existing_ordinance(db, ordinance.id),
    )


@router.get(
    "/coverage/burgos",
    response_model=OrdinanceCoverageRead,
)
def get_burgos_coverage(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.view")
    return build_burgos_coverage_report(db)


@router.post(
    "/coverage/burgos/retry-embeddings",
    response_model=OrdinanceEmbeddingRetryRead,
)
def retry_burgos_failed_embeddings(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.import")
    if settings.embeddings_runtime != "openai_compatible":
        return {"queued": 0, **retry_failed_burgos_embeddings(db)}

    chunks = list(
        db.scalars(
            select(OrdinanceLegalChunk)
            .join(OrdinanceLegalChunk.ordinance)
            .join(Ordinance.municipality)
            .options(
                selectinload(OrdinanceLegalChunk.ordinance).selectinload(
                    Ordinance.municipality
                )
            )
            .where(
                Municipality.province.ilike("Burgos"),
                OrdinanceLegalChunk.embedding_status == "failed",
            )
            .order_by(OrdinanceLegalChunk.id)
        )
    )
    queued = 0
    for ordinance_id in dict.fromkeys(chunk.ordinance_id for chunk in chunks):
        queued += dispatch_ordinance_embeddings(
            db,
            ordinance_id,
            embedding_statuses=("failed",),
        )["queued"]
    return {
        "province": "Burgos",
        "retried": len(chunks),
        "queued": queued,
        "restored": 0,
        # They remain failed until their idempotent worker succeeds.
        "failed": len(chunks),
        "still_failed": [
            {
                "chunk_id": chunk.id,
                "ordinance_id": chunk.ordinance_id,
                "municipality_name": chunk.ordinance.municipality.name,
                "citation": chunk.citation,
                "embedding_status": chunk.embedding_status,
            }
            for chunk in chunks
        ],
    }


@router.get(
    "/official-sources",
    response_model=list[OfficialLegalSourceRead],
)
def list_official_sources(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[OfficialLegalSource]:
    require_ordinance_permission(db, current_user, "ordinances.import")
    query = select(OfficialLegalSource).order_by(OfficialLegalSource.name)
    if not include_archived:
        query = query.where(OfficialLegalSource.status == "active")
    return list(db.scalars(query))


@router.post(
    "/official-sources",
    response_model=OfficialLegalSourceRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_official_source(
    payload: OfficialLegalSourceCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OfficialLegalSource:
    require_ordinance_permission(db, current_user, "ordinances.import")
    if not is_valid_official_source_definition(payload.base_url, payload.domain):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid official legal source definition",
        )
    source_data = payload.model_dump()
    source_data["domain"] = payload.domain.strip().lower().rstrip(".")
    source = OfficialLegalSource(**source_data)
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.patch(
    "/official-sources/{source_id}",
    response_model=OfficialLegalSourceRead,
)
def update_official_source(
    source_id: int,
    payload: OfficialLegalSourceUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OfficialLegalSource:
    require_ordinance_permission(db, current_user, "ordinances.import")
    source = db.get(OfficialLegalSource, source_id)
    if source is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Official legal source not found",
        )
    updates = payload.model_dump(exclude_unset=True)
    base_url = str(updates.get("base_url", source.base_url))
    domain = str(updates.get("domain", source.domain))
    if not is_valid_official_source_definition(base_url, domain):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid official legal source definition",
        )
    if "domain" in updates:
        updates["domain"] = domain.strip().lower().rstrip(".")
    for field, value in updates.items():
        setattr(source, field, value)
    db.commit()
    db.refresh(source)
    return source


@router.post(
    "/import-jobs",
    response_model=OrdinanceImportJobRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_import_job(
    payload: OrdinanceImportJobCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.import")
    validate_import_job_payload(db, payload)
    job = OrdinanceImportJob(
        title=payload.title,
        description=payload.description,
        topic=payload.topic,
        subtopic=payload.subtopic,
        search_query=payload.search_query,
        municipality_ids_json=json.dumps(payload.municipality_ids),
        official_source_ids_json=json.dumps(payload.official_source_ids),
        source_urls_json=json.dumps(
            [source.model_dump() for source in payload.source_urls],
            ensure_ascii=False,
        ),
        review_criteria=payload.review_criteria,
        created_by_id=current_user.id,
    )
    db.add(job)
    db.commit()
    return serialize_import_job(job)


@router.get(
    "/import-jobs",
    response_model=list[OrdinanceImportJobRead],
)
def list_import_jobs(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[dict]:
    require_ordinance_permission(db, current_user, "ordinances.import")
    query = (
        select(OrdinanceImportJob)
        .options(selectinload(OrdinanceImportJob.items))
        .order_by(OrdinanceImportJob.created_at.desc(), OrdinanceImportJob.id.desc())
    )
    if status_filter:
        query = query.where(OrdinanceImportJob.status == status_filter)
    return [serialize_import_job(job) for job in db.scalars(query)]


@router.get(
    "/import-jobs/{job_id}",
    response_model=OrdinanceImportJobDetail,
)
def get_import_job(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.import")
    return serialize_import_job_detail(
        db,
        current_user,
        get_existing_import_job(db, job_id),
    )


@router.post(
    "/import-jobs/{job_id}/enqueue",
    response_model=OrdinanceImportEnqueueRead,
)
def enqueue_import_job(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OrdinanceImportEnqueueRead:
    require_ordinance_permission(db, current_user, "ordinances.import")
    # Cada importación descarga y trocea un boletín oficial completo y encola
    # trabajos de embeddings: es la operación más cara del sistema (ADR-036).
    require_rate_limit_slot(
        ordinance_import_rate_limiter,
        str(current_user.id),
        detail="Too many ordinance imports",
    )
    job = get_existing_import_job(db, job_id)
    if job.status in {"running", "queued"}:
        return OrdinanceImportEnqueueRead(
            job_id=job.id,
            status=job.status,
            queue_job_id=None,
        )
    job.status = "queued"
    job.error_message = None
    db.commit()
    try:
        queue_job = get_default_queue().enqueue(run_import_job, job.id)
    except Exception as error:
        job.status = "failed"
        job.error_message = str(error)[:2000]
        db.commit()
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Import queue is unavailable",
        ) from error
    return OrdinanceImportEnqueueRead(
        job_id=job.id,
        status="queued",
        queue_job_id=queue_job.id,
    )


@router.post(
    "/import-jobs/{job_id}/run-inline",
    response_model=OrdinanceImportJobDetail,
)
def run_import_job_inline(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.import")
    get_existing_import_job(db, job_id)
    run_import_job(job_id, db=db)
    return serialize_import_job_detail(
        db,
        current_user,
        get_existing_import_job(db, job_id),
    )


@router.patch(
    "/import-items/{item_id}/review",
    response_model=OrdinanceImportItemRead,
)
def review_import_item(
    item_id: int,
    payload: OrdinanceImportItemReviewUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.review")
    item = get_existing_import_item(db, item_id)
    if item.ordinance is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Import item has no ordinance",
        )
    if item.status != "pending_review":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Import item is not pending review",
        )
    if item.ordinance.import_job_id != item.job_id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Import item does not own this ordinance review",
        )
    report = latest_review_report(item)
    if report is None or report.status != "agent_reviewed":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Import item has no current review report",
        )
    now = datetime.now(UTC)
    if payload.decision == "approve":
        item.status = "approved"
        item.ordinance.curation_status = "approved"
        for chunk in item.ordinance.legal_chunks:
            chunk.review_status = "approved"
        report_status = "human_approved"
    elif payload.decision == "needs_changes":
        item.status = "pending_review"
        item.ordinance.curation_status = "needs_changes"
        for chunk in item.ordinance.legal_chunks:
            chunk.review_status = "pending_review"
        report_status = "superseded"
    else:
        item.status = "rejected"
        item.ordinance.curation_status = "rejected"
        for chunk in item.ordinance.legal_chunks:
            chunk.review_status = "rejected"
        report_status = "human_rejected"

    report.status = report_status
    report.reviewed_by_agent = False
    report.reviewed_by_id = current_user.id
    report.reviewed_at = now
    if payload.notes:
        report.doubts = payload.notes
    item.ordinance.updated_by_id = current_user.id
    db.commit()
    return serialize_import_item(
        db,
        current_user,
        get_existing_import_item(db, item_id),
    )


@router.get(
    "/comparison",
    response_model=OrdinanceComparisonRead,
)
def compare_ordinances(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    municipality_ids: Annotated[list[int], Query()],
    topic: str | None = None,
    include_pending: bool = False,
    include_inactive: bool = False,
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.compare")
    municipality_ids = list(dict.fromkeys(municipality_ids))
    if not municipality_ids or len(municipality_ids) > 20:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Comparison needs between 1 and 20 municipalities",
        )
    if include_pending:
        require_ordinance_permission(db, current_user, "ordinances.review")
    municipalities_by_id = {
        municipality.id: municipality
        for municipality in db.scalars(
            select(Municipality).where(Municipality.id.in_(municipality_ids))
        )
    }
    if len(municipalities_by_id) != len(municipality_ids):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Municipality not found",
        )
    query = (
        select_ordinances_with_summaries()
        .where(Ordinance.municipality_id.in_(municipality_ids))
        .order_by(Ordinance.topic, Ordinance.subtopic, Municipality.name)
    )
    if topic:
        query = query.where(Ordinance.topic.ilike(f"%{topic.strip()}%"))
    if include_pending:
        query = query.where(Ordinance.curation_status != "rejected")
    else:
        query = query.where(Ordinance.curation_status == "approved")
    if not include_inactive:
        query = query.where(
            Ordinance.status.not_in(DEFINITIVELY_INACTIVE_STATUSES)
        )

    rows: dict[tuple[str, str | None], list[dict]] = {}
    for ordinance in db.scalars(query):
        key = (ordinance.topic, ordinance.subtopic)
        rows.setdefault(key, []).append(
            {
                "municipality_id": ordinance.municipality_id,
                "municipality_name": ordinance.municipality.name,
                "ordinance_id": ordinance.id,
                "title": ordinance.title,
                "topic": ordinance.topic,
                "subtopic": ordinance.subtopic,
                "status": ordinance.status,
                "curation_status": ordinance.curation_status,
                "approval_date": ordinance.approval_date,
                "publication_date": ordinance.publication_date,
                "effective_date": ordinance.effective_date,
                "source_url": ordinance.source_url,
                "summary": ordinance.summary,
                "confidence_score": ordinance.confidence_score,
            }
        )
    return {
        "municipality_ids": municipality_ids,
        "municipalities": [
            {
                "id": municipality_id,
                "name": municipalities_by_id[municipality_id].name,
                "province": municipalities_by_id[municipality_id].province,
            }
            for municipality_id in municipality_ids
        ],
        "include_pending": include_pending,
        "include_inactive": include_inactive,
        "rows": [
            {"topic": key[0], "subtopic": key[1], "entries": entries}
            for key, entries in rows.items()
        ],
    }


@router.get(
    "/semantic-search",
    response_model=list[OrdinanceSemanticSearchResult],
)
def semantic_search_ordinances(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    q: Annotated[str, Query(min_length=1, max_length=400)],
    municipality_id: Annotated[int | None, Query(ge=1)] = None,
    municipality_name: Annotated[str | None, Query(max_length=255)] = None,
    autonomous_community: Annotated[str | None, Query(max_length=255)] = None,
    topic: Annotated[str | None, Query(max_length=255)] = None,
    include_pending: bool = False,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[dict]:
    require_ordinance_permission(db, current_user, "ordinances.compare")
    if include_pending:
        require_ordinance_permission(db, current_user, "ordinances.review")
    normalized_query = q.strip()
    if not normalized_query:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="q cannot be empty",
        )
    query_vector, embedding_model, status = embed_text(normalized_query)
    if status != "ready" or query_vector is None:
        return []
    page = search_ordinance_chunks(
        db,
        query_vector=query_vector,
        embedding_model=embedding_model,
        options=OrdinanceSearchOptions(
            municipality_id=municipality_id,
            municipality_name=(municipality_name or "").strip() or None,
            autonomous_community=(autonomous_community or "").strip() or None,
            topic=(topic or "").strip() or None,
            strict_topic=bool(topic),
            include_pending=include_pending,
            include_inactive=include_inactive,
            limit=limit,
        ),
    )
    return page["results"]


@router.get(
    "/search",
    response_model=OrdinanceSearchRead,
)
def search_ordinances(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    q: Annotated[str, Query(min_length=2, max_length=500)],
    municipality_id: int | None = None,
    municipality_name: str | None = None,
    province: str | None = None,
    topic: str | None = None,
    strict_topic: bool = False,
    population_gte: Annotated[int | None, Query(ge=0)] = None,
    population_lt: Annotated[int | None, Query(ge=0)] = None,
    result_scope: OrdinanceResultScope = "fragments",
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=50)] = 12,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Search the reviewed corpus for the user-facing ordinance library."""

    require_ordinance_permission(db, current_user, "ordinances.view")
    query_text = q.strip()
    if len(query_text) < 2:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Ordinance search query is too short",
        )
    if (
        population_gte is not None
        and population_lt is not None
        and population_gte >= population_lt
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="population_gte must be lower than population_lt",
        )
    try:
        query_vector, embedding_model, embedding_status = embed_text(query_text)
    except EmbeddingsUnavailableError as error:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ordinance semantic search is unavailable",
        ) from error
    if embedding_status != "ready" or query_vector is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ordinance semantic search is unavailable",
        )

    page = search_ordinance_chunks(
        db,
        query_vector=query_vector,
        embedding_model=embedding_model,
        options=OrdinanceSearchOptions(
            municipality_id=municipality_id,
            municipality_name=(municipality_name or "").strip() or None,
            province=(province or "").strip() or None,
            topic=(topic or "").strip() or None,
            strict_topic=strict_topic,
            population_gte=population_gte,
            population_lt=population_lt,
            result_scope=result_scope,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        ),
    )
    return {"query": query_text, **page}


@router.get(
    "/{ordinance_id}/chunks",
    response_model=list[OrdinanceLegalChunkRead],
)
def list_ordinance_chunks(
    ordinance_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_unreviewed: bool = False,
    include_inactive: bool = False,
) -> list[OrdinanceLegalChunk]:
    require_ordinance_permission(db, current_user, "ordinances.view")
    ordinance = get_existing_ordinance(db, ordinance_id)
    enforce_ordinance_read_policy(
        db,
        current_user,
        ordinance,
        include_unreviewed=include_unreviewed,
        include_inactive=include_inactive,
    )
    if include_unreviewed:
        require_ordinance_permission(db, current_user, "ordinances.review")
    query = select(OrdinanceLegalChunk).where(
        OrdinanceLegalChunk.ordinance_id == ordinance_id
    )
    if not include_unreviewed:
        query = query.where(OrdinanceLegalChunk.review_status == "approved")
    return list(
        db.scalars(
            query.order_by(OrdinanceLegalChunk.chunk_index)
        )
    )


@router.post(
    "/{ordinance_id}/retry-embeddings",
    response_model=OrdinanceEmbeddingDispatchRead,
)
def retry_ordinance_embeddings(
    ordinance_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_ordinance_permission(db, current_user, "ordinances.import")
    get_existing_ordinance(db, ordinance_id)
    dispatch = dispatch_ordinance_embeddings(db, ordinance_id)
    statuses = list(
        db.scalars(
            select(OrdinanceLegalChunk.embedding_status).where(
                OrdinanceLegalChunk.ordinance_id == ordinance_id
            )
        )
    )
    return {
        "ordinance_id": ordinance_id,
        **dispatch,
        "ready": statuses.count("ready"),
        "pending": statuses.count("pending"),
        "failed": statuses.count("failed"),
        "disabled": statuses.count("disabled"),
    }


@router.get("/{ordinance_id}", response_model=OrdinanceRead)
def get_ordinance(
    ordinance_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_unreviewed: bool = False,
    include_inactive: bool = False,
) -> OrdinanceRead:
    require_ordinance_permission(db, current_user, "ordinances.view")
    ordinance = get_existing_ordinance(db, ordinance_id)
    enforce_ordinance_read_policy(
        db,
        current_user,
        ordinance,
        include_unreviewed=include_unreviewed,
        include_inactive=include_inactive,
    )
    return serialize_ordinance_detail(
        db,
        current_user,
        ordinance,
    )


@router.patch("/{ordinance_id}", response_model=OrdinanceRead)
def update_ordinance(
    ordinance_id: int,
    payload: OrdinanceUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OrdinanceRead:
    ordinance = get_existing_ordinance(db, ordinance_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        require_ordinance_permission(db, current_user, "ordinances.view")
        enforce_ordinance_read_policy(
            db,
            current_user,
            ordinance,
            include_unreviewed=has_ordinance_staff_read(db, current_user),
            include_inactive=has_ordinance_staff_read(db, current_user),
        )
        return serialize_ordinance_detail(db, current_user, ordinance)
    reject_null_required_fields(updates)

    updates = {
        field: value
        for field, value in updates.items()
        if getattr(ordinance, field) != value
    }
    if not updates:
        require_ordinance_permission(db, current_user, "ordinances.view")
        enforce_ordinance_read_policy(
            db,
            current_user,
            ordinance,
            include_unreviewed=has_ordinance_staff_read(db, current_user),
            include_inactive=has_ordinance_staff_read(db, current_user),
        )
        return serialize_ordinance_detail(db, current_user, ordinance)

    content_updates = set(updates) - {"status", "curation_status"}
    if content_updates:
        require_ordinance_permission(db, current_user, "ordinances.edit")
    if updates.get("status") == "archived":
        require_ordinance_permission(db, current_user, "ordinances.archive")
    elif "status" in updates:
        require_ordinance_permission(db, current_user, "ordinances.review")
    if "curation_status" in updates:
        require_ordinance_permission(db, current_user, "ordinances.review")
    sensitive_content_changed = bool(
        set(updates).intersection(REVIEW_SENSITIVE_ORDINANCE_FIELDS)
    )
    if (
        sensitive_content_changed
        and updates.get("curation_status") == "approved"
    ):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Changed ordinance content requires a separate review",
        )

    import_item = db.scalar(
        select(OrdinanceImportItem)
        .options(selectinload(OrdinanceImportItem.review_reports))
        .where(
            OrdinanceImportItem.ordinance_id == ordinance.id,
            OrdinanceImportItem.job_id == ordinance.import_job_id,
            OrdinanceImportItem.status != "duplicate",
        )
        .order_by(OrdinanceImportItem.id.desc())
        .limit(1)
    )
    if import_item is not None and "curation_status" in updates:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Imported ordinance review must use the import item endpoint",
        )

    requested_municipality_id = updates.get("municipality_id")
    if (
        requested_municipality_id is not None
        and requested_municipality_id != ordinance.municipality_id
    ):
        ensure_active_municipality(db, requested_municipality_id)

    if "document_id" in updates and updates["document_id"] != ordinance.document_id:
        ensure_document_access(db, current_user, updates["document_id"])

    previous_curation_status = ordinance.curation_status
    for field, value in updates.items():
        setattr(ordinance, field, value)

    text_changed = "text_content" in updates
    if text_changed:
        try:
            rebuild_ordinance_chunks(
                db,
                ordinance,
                import_item=import_item,
                generate_embeddings=False,
            )
        except ImportSourceError as error:
            db.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
    elif "source_url" in updates:
        for chunk in ordinance.legal_chunks:
            chunk.source_url = ordinance.source_url
    if sensitive_content_changed and "curation_status" not in updates:
        ordinance.curation_status = (
            "needs_changes"
            if previous_curation_status == "approved"
            else "pending_review"
        )

    if sensitive_content_changed or "curation_status" in updates:
        chunk_review_status = {
            "approved": "approved",
            "rejected": "rejected",
            "pending_review": "pending_review",
            "needs_changes": "pending_review",
        }[ordinance.curation_status]
        for chunk in ordinance.legal_chunks:
            chunk.review_status = chunk_review_status
    if sensitive_content_changed and import_item is not None:
        import_item.status = "pending_review"
        if text_changed:
            import_item.raw_text = ordinance.text_content
        report = latest_review_report(import_item)
        if report is not None:
            report.status = "superseded"
        create_review_report(db, ordinance, import_item)
    ordinance.updated_by_id = current_user.id

    db.commit()
    if text_changed:
        dispatch_ordinance_embeddings(db, ordinance.id)
    return serialize_ordinance_detail(
        db,
        current_user,
        get_existing_ordinance(db, ordinance_id),
    )


def serialize_ordinance_list_item(ordinance: Ordinance) -> OrdinanceListRead:
    """Lists never expose tenant document identifiers from the global corpus."""

    return OrdinanceListRead.model_validate(ordinance).model_copy(
        update={"document_id": None}
    )


def serialize_ordinance_detail(
    db: Session,
    current_user: User,
    ordinance: Ordinance,
) -> OrdinanceRead:
    detail = OrdinanceRead.model_validate(ordinance)
    if ordinance.document is None or user_can_access_document(
        db,
        current_user,
        ordinance.document,
    ):
        return detail
    return detail.model_copy(update={"document_id": None, "document": None})


def select_ordinances_with_summaries():
    return (
        select(Ordinance)
        .join(Ordinance.municipality)
        .options(
            selectinload(Ordinance.municipality),
            selectinload(Ordinance.document),
        )
    )


def get_existing_ordinance(db: Session, ordinance_id: int) -> Ordinance:
    ordinance = db.scalar(
        select_ordinances_with_summaries()
        .where(Ordinance.id == ordinance_id)
        # Repopulate relationships when re-reading after a commit in the same
        # request (e.g. PATCH that links a document); with expire_on_commit
        # False the identity map would otherwise return stale relations.
        .execution_options(populate_existing=True)
    )
    if ordinance is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Ordinance not found",
        )
    return ordinance


def ensure_active_municipality(db: Session, municipality_id: int) -> Municipality:
    municipality = db.get(Municipality, municipality_id)
    if municipality is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Municipality not found",
        )
    if municipality.status == "archived":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Municipality is archived",
        )

    return municipality


def ensure_document_access(
    db: Session,
    current_user: User,
    document_id: int | None,
) -> None:
    if document_id is None:
        return

    document = db.scalar(
        select(Document)
        .options(selectinload(Document.project))
        .where(Document.id == document_id)
    )
    # 404 for both missing and inaccessible documents so ordinance editors
    # cannot probe other organizations' document ids.
    if document is None or not user_can_access_document(db, current_user, document):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )


def reject_null_required_fields(updates: dict[str, object]) -> None:
    required_fields = {
        "municipality_id",
        "title",
        "topic",
        "ordinance_type",
        "status",
        "curation_status",
    }
    if any(field in updates and updates[field] is None for field in required_fields):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Required ordinance fields cannot be null",
        )


def validate_import_job_payload(
    db: Session,
    payload: OrdinanceImportJobCreate,
) -> None:
    if not payload.source_urls and not (
        payload.search_query and payload.municipality_ids
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Import job needs seed URLs or search query with municipalities",
        )
    if payload.municipality_ids:
        existing = set(
            db.scalars(
                select(Municipality.id).where(
                    Municipality.id.in_(payload.municipality_ids),
                    Municipality.status == "active",
                )
            )
        )
        missing = set(payload.municipality_ids) - existing
        if missing:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Municipality not found",
            )
    source_query = select(OfficialLegalSource).where(
        OfficialLegalSource.status == "active"
    )
    if payload.official_source_ids:
        source_query = source_query.where(
            OfficialLegalSource.id.in_(payload.official_source_ids)
        )
    sources = list(db.scalars(source_query))
    if not sources:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Import job needs active official sources",
        )
    if payload.source_urls:
        for source_input in payload.source_urls:
            if not url_allowed(source_input.url, sources):
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Import source URL is not official",
                )


def url_allowed(url: str, sources: list[OfficialLegalSource]) -> bool:
    return is_official_source_url(url, sources)


def get_existing_import_job(db: Session, job_id: int) -> OrdinanceImportJob:
    job = db.scalar(
        select(OrdinanceImportJob)
        .options(
            selectinload(OrdinanceImportJob.items)
            .selectinload(OrdinanceImportItem.ordinance)
            .selectinload(Ordinance.municipality),
            selectinload(OrdinanceImportJob.items)
            .selectinload(OrdinanceImportItem.review_reports),
            selectinload(OrdinanceImportJob.items)
            .selectinload(OrdinanceImportItem.ordinance)
            .selectinload(Ordinance.legal_chunks),
        )
        .where(OrdinanceImportJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Ordinance import job not found",
        )
    return job


def get_existing_import_item(db: Session, item_id: int) -> OrdinanceImportItem:
    item = db.scalar(
        select(OrdinanceImportItem)
        .options(
            selectinload(OrdinanceImportItem.ordinance).selectinload(
                Ordinance.municipality
            ),
            selectinload(OrdinanceImportItem.ordinance).selectinload(
                Ordinance.legal_chunks
            ),
            selectinload(OrdinanceImportItem.review_reports),
        )
        .where(OrdinanceImportItem.id == item_id)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Ordinance import item not found",
        )
    return item


def serialize_import_job(job: OrdinanceImportJob) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "description": job.description,
        "topic": job.topic,
        "subtopic": job.subtopic,
        "search_query": job.search_query,
        "municipality_ids": parse_json_list(job.municipality_ids_json),
        "official_source_ids": parse_json_list(job.official_source_ids_json),
        "source_urls": parse_json_list(job.source_urls_json),
        "review_criteria": job.review_criteria,
        "source_policy": job.source_policy,
        "status": job.status,
        "created_by_id": job.created_by_id,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error_message": job.error_message,
        "item_count": len(job.items),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def serialize_import_job_detail(
    db: Session,
    current_user: User,
    job: OrdinanceImportJob,
) -> dict:
    data = serialize_import_job(job)
    data["items"] = [
        serialize_import_item(db, current_user, item) for item in job.items
    ]
    return data


def serialize_import_item(
    db: Session,
    current_user: User,
    item: OrdinanceImportItem,
) -> dict:
    return {
        "id": item.id,
        "job_id": item.job_id,
        "municipality_id": item.municipality_id,
        "official_source_id": item.official_source_id,
        "ordinance_id": item.ordinance_id,
        "source_url": item.source_url,
        "source_title": item.source_title,
        "status": item.status,
        "source_hash": item.source_hash,
        "extracted_metadata": parse_json_object(item.extracted_metadata_json),
        "confidence_score": item.confidence_score,
        "error_message": item.error_message,
        "ordinance": (
            serialize_ordinance_detail(db, current_user, item.ordinance)
            if item.ordinance is not None
            else None
        ),
        "review_reports": [
            serialize_review_report(report)
            for report in sorted(
                item.review_reports,
                key=lambda report: report.id,
                reverse=True,
            )
        ],
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def serialize_review_report(report: OrdinanceReviewReport) -> dict:
    return {
        "id": report.id,
        "ordinance_id": report.ordinance_id,
        "import_item_id": report.import_item_id,
        "status": report.status,
        "proposed_decision": report.proposed_decision,
        "confidence_score": report.confidence_score,
        "checklist": parse_json_list(report.checklist_json),
        "summary": report.summary,
        "doubts": report.doubts,
        "reviewed_by_agent": report.reviewed_by_agent,
        "reviewed_by_id": report.reviewed_by_id,
        "reviewed_at": report.reviewed_at,
        "created_at": report.created_at,
        "updated_at": report.updated_at,
    }


def latest_review_report(
    item: OrdinanceImportItem,
) -> OrdinanceReviewReport | None:
    if not item.review_reports:
        return None
    return max(item.review_reports, key=lambda report: report.id)


def parse_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def parse_json_object(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def require_ordinance_permission(
    db: Session,
    current_user: User,
    permission_code: str,
) -> None:
    if current_user.is_superuser:
        return

    if has_permission(current_user, "ordinances.manage", db):
        return

    if has_permission(current_user, permission_code, db):
        return

    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )


def require_ordinance_staff_read(db: Session, current_user: User) -> None:
    if has_ordinance_staff_read(db, current_user):
        return
    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="Permission required: ordinances.review or ordinances.edit",
    )


def has_ordinance_staff_read(db: Session, current_user: User) -> bool:
    return current_user.is_superuser or any(
        has_permission(current_user, permission_code, db)
        for permission_code in (
            "ordinances.manage",
            "ordinances.edit",
            "ordinances.review",
        )
    )


def enforce_ordinance_read_policy(
    db: Session,
    current_user: User,
    ordinance: Ordinance,
    *,
    include_unreviewed: bool,
    include_inactive: bool,
) -> None:
    if ordinance.curation_status != "approved":
        if not include_unreviewed:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Ordinance not found",
            )
        require_ordinance_staff_read(db, current_user)
    if (
        ordinance.status in DEFINITIVELY_INACTIVE_STATUSES
        and not include_inactive
    ):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Ordinance not found",
        )
