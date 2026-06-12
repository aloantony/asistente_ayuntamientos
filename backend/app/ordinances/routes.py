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
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.documents.access import user_can_access_document
from app.documents.models import Document
from app.municipalities.models import Municipality
from app.ordinances.models import Ordinance
from app.ordinances.schemas import (
    OrdinanceCreate,
    OrdinanceListRead,
    OrdinanceRead,
    OrdinanceStatus,
    OrdinanceUpdate,
)
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/ordinances", tags=["ordinances"])


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
    include_archived: bool = False,
) -> list[Ordinance]:
    require_ordinance_permission(db, current_user, "ordinances.view")

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

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "",
    response_model=OrdinanceRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_ordinance(
    payload: OrdinanceCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Ordinance:
    require_ordinance_permission(db, current_user, "ordinances.create")
    if payload.status == "archived":
        require_ordinance_permission(db, current_user, "ordinances.archive")
    ensure_active_municipality(db, payload.municipality_id)
    ensure_document_access(db, current_user, payload.document_id)

    ordinance = Ordinance(
        **payload.model_dump(),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(ordinance)
    db.commit()

    return get_existing_ordinance(db, ordinance.id)


@router.get("/{ordinance_id}", response_model=OrdinanceRead)
def get_ordinance(
    ordinance_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Ordinance:
    require_ordinance_permission(db, current_user, "ordinances.view")
    return get_existing_ordinance(db, ordinance_id)


@router.patch("/{ordinance_id}", response_model=OrdinanceRead)
def update_ordinance(
    ordinance_id: int,
    payload: OrdinanceUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Ordinance:
    ordinance = get_existing_ordinance(db, ordinance_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        require_ordinance_permission(db, current_user, "ordinances.view")
        return ordinance
    reject_null_required_fields(updates)

    non_archive_updates = set(updates) - {"status"}
    if non_archive_updates or updates.get("status") != "archived":
        require_ordinance_permission(db, current_user, "ordinances.edit")
    if updates.get("status") == "archived":
        require_ordinance_permission(db, current_user, "ordinances.archive")

    requested_municipality_id = updates.get("municipality_id")
    if (
        requested_municipality_id is not None
        and requested_municipality_id != ordinance.municipality_id
    ):
        ensure_active_municipality(db, requested_municipality_id)

    if "document_id" in updates and updates["document_id"] != ordinance.document_id:
        ensure_document_access(db, current_user, updates["document_id"])

    for field, value in updates.items():
        setattr(ordinance, field, value)
    ordinance.updated_by_id = current_user.id

    db.commit()
    return get_existing_ordinance(db, ordinance_id)


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
    required_fields = {"municipality_id", "title", "topic", "ordinance_type", "status"}
    if any(field in updates and updates[field] is None for field in required_fields):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Required ordinance fields cannot be null",
        )


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
