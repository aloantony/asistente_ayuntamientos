from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.communications.access import (
    get_communications_organization_for_read,
    get_communications_organization_for_write,
    require_communications_permission,
)
from app.communications.models import (
    MunicipalNews,
    MunicipalNotice,
    MunicipalNoticeEvent,
)
from app.communications.schemas import (
    NewsCreate,
    NewsRead,
    NewsStatus,
    NoticeCreate,
    NoticeDetail,
    NoticeKind,
    NoticePublish,
    NoticeStatus,
    NoticeWithdraw,
)
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.users.models import User

router = APIRouter(prefix="/communications", tags=["communications"])


@router.get("/news", response_model=list[NewsRead])
def list_news(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        NewsStatus | None,
        Query(alias="status"),
    ] = None,
) -> list[MunicipalNews]:
    get_communications_organization_for_read(db, organization_id)
    require_communications_permission(
        db,
        current_user,
        organization_id,
        "communications.view",
    )

    query = (
        select(MunicipalNews)
        .where(MunicipalNews.organization_id == organization_id)
        .order_by(
            MunicipalNews.published_on.desc().nullslast(),
            MunicipalNews.id.desc(),
        )
    )
    if status_filter is not None:
        query = query.where(MunicipalNews.status == status_filter)
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/news",
    response_model=NewsRead,
    status_code=status.HTTP_201_CREATED,
)
def create_news(
    payload: NewsCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalNews:
    get_communications_organization_for_write(db, payload.organization_id)
    require_communications_permission(
        db,
        current_user,
        payload.organization_id,
        "communications.edit",
    )

    news = MunicipalNews(**payload.model_dump(), created_by_id=current_user.id)
    db.add(news)
    commit_or_conflict(db, "A news item with that slug already exists")
    db.refresh(news)
    return news


@router.get("/notices", response_model=list[NoticeDetail])
def list_notices(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        NoticeStatus | None,
        Query(alias="status"),
    ] = None,
    kind: NoticeKind | None = None,
) -> list[MunicipalNotice]:
    get_communications_organization_for_read(db, organization_id)
    require_communications_permission(
        db,
        current_user,
        organization_id,
        "communications.view",
    )

    query = (
        select(MunicipalNotice)
        .options(selectinload(MunicipalNotice.events))
        .where(MunicipalNotice.organization_id == organization_id)
        .order_by(
            MunicipalNotice.published_on.desc().nullslast(),
            MunicipalNotice.id.desc(),
        )
    )
    if status_filter is not None:
        query = query.where(MunicipalNotice.status == status_filter)
    if kind is not None:
        query = query.where(MunicipalNotice.kind == kind)
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/notices",
    response_model=NoticeDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_notice(
    payload: NoticeCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalNotice:
    get_communications_organization_for_write(db, payload.organization_id)
    require_communications_permission(
        db,
        current_user,
        payload.organization_id,
        "communications.edit",
    )

    notice = MunicipalNotice(**payload.model_dump(), created_by_id=current_user.id)
    db.add(notice)
    commit_or_conflict(db, "Notice could not be saved")
    db.refresh(notice)
    record_event(
        db,
        notice=notice,
        event_type="created",
        actor_id=current_user.id,
        to_status=notice.status,
    )
    return get_existing_notice(db, notice.id)


@router.post("/notices/{notice_id}/publish", response_model=NoticeDetail)
def publish_notice(
    notice_id: int,
    payload: NoticePublish,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalNotice:
    notice = get_existing_notice(db, notice_id)
    get_communications_organization_for_write(db, notice.organization_id)
    # Publicar y retirar tienen permiso propio: exponer un bando produce
    # efectos, y no es lo mismo que redactarlo.
    require_communications_permission(
        db,
        current_user,
        notice.organization_id,
        "communications.publish",
    )
    if notice.status == "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Notice is already published",
        )
    if notice.status == "withdrawn":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A withdrawn notice cannot be published again",
        )

    previous_status = notice.status
    notice.status = "published"
    notice.published_on = payload.published_on
    if payload.expires_on is not None:
        notice.expires_on = payload.expires_on
    commit_or_conflict(db, "Notice could not be published")
    db.refresh(notice)
    record_event(
        db,
        notice=notice,
        event_type="published",
        actor_id=current_user.id,
        from_status=previous_status,
        to_status="published",
    )
    return get_existing_notice(db, notice.id)


@router.post("/notices/{notice_id}/withdraw", response_model=NoticeDetail)
def withdraw_notice(
    notice_id: int,
    payload: NoticeWithdraw,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalNotice:
    notice = get_existing_notice(db, notice_id)
    get_communications_organization_for_write(db, notice.organization_id)
    require_communications_permission(
        db,
        current_user,
        notice.organization_id,
        "communications.publish",
    )
    if notice.status != "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a published notice can be withdrawn",
        )

    notice.status = "withdrawn"
    # `published_on` no se borra: que el bando llegó a estar expuesto en esa
    # fecha puede tener que demostrarse después.
    commit_or_conflict(db, "Notice could not be withdrawn")
    db.refresh(notice)
    record_event(
        db,
        notice=notice,
        event_type="withdrawn",
        actor_id=current_user.id,
        from_status="published",
        to_status="withdrawn",
        note=payload.reason,
    )
    return get_existing_notice(db, notice.id)


def get_existing_notice(db: Session, notice_id: int) -> MunicipalNotice:
    notice = db.scalar(
        select(MunicipalNotice)
        .options(selectinload(MunicipalNotice.events))
        .where(MunicipalNotice.id == notice_id)
        .execution_options(populate_existing=True)
    )
    if notice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notice not found",
        )
    return notice


def record_event(
    db: Session,
    *,
    notice: MunicipalNotice,
    event_type: str,
    actor_id: int | None,
    from_status: str | None = None,
    to_status: str | None = None,
    note: str | None = None,
) -> None:
    db.add(
        MunicipalNoticeEvent(
            notice_id=notice.id,
            organization_id=notice.organization_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            note=note,
            actor_id=actor_id,
        )
    )
    db.commit()


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
