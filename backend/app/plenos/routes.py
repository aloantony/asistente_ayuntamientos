from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.documents.models import Document
from app.plenos.access import (
    get_plenos_organization_for_read,
    get_plenos_organization_for_write,
    require_plenos_permission,
)
from app.plenos.models import CouncilAgendaItem, CouncilSession
from app.plenos.schemas import (
    AgendaItemCreate,
    AgendaItemRead,
    MinutesUpdate,
    SessionCreate,
    SessionDetail,
    SessionKind,
    SessionStatus,
)
from app.users.models import User

router = APIRouter(prefix="/plenos", tags=["plenos"])


@router.get("/sessions", response_model=list[SessionDetail])
def list_sessions(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        SessionStatus | None,
        Query(alias="status"),
    ] = None,
    kind: SessionKind | None = None,
) -> list[CouncilSession]:
    get_plenos_organization_for_read(db, organization_id)
    require_plenos_permission(db, current_user, organization_id, "plenos.view")

    query = (
        select(CouncilSession)
        .options(selectinload(CouncilSession.agenda_items))
        .where(CouncilSession.organization_id == organization_id)
        .order_by(CouncilSession.held_on.desc(), CouncilSession.id.desc())
    )
    if status_filter is not None:
        query = query.where(CouncilSession.status == status_filter)
    if kind is not None:
        query = query.where(CouncilSession.kind == kind)
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/sessions",
    response_model=SessionDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_session(
    payload: SessionCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CouncilSession:
    get_plenos_organization_for_write(db, payload.organization_id)
    require_plenos_permission(
        db,
        current_user,
        payload.organization_id,
        "plenos.edit",
    )

    session = CouncilSession(**payload.model_dump())
    db.add(session)
    commit_or_conflict(
        db,
        "A session of that kind already exists on that date",
    )
    return get_existing_session(db, session.id)


@router.post(
    "/sessions/{session_id}/agenda",
    response_model=AgendaItemRead,
    status_code=status.HTTP_201_CREATED,
)
def add_agenda_item(
    session_id: int,
    payload: AgendaItemCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CouncilAgendaItem:
    session = get_existing_session(db, session_id)
    get_plenos_organization_for_write(db, session.organization_id)
    require_plenos_permission(
        db,
        current_user,
        session.organization_id,
        "plenos.edit",
    )
    # Una sesión cancelada no llegó a celebrarse: no tiene orden del día que
    # completar después.
    if session.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A cancelled session takes no agenda items",
        )

    item = CouncilAgendaItem(
        **payload.model_dump(),
        session_id=session.id,
        organization_id=session.organization_id,
    )
    db.add(item)
    commit_or_conflict(db, "That agenda position is already taken")
    db.refresh(item)
    return item


@router.patch("/sessions/{session_id}/minutes", response_model=SessionDetail)
def update_minutes(
    session_id: int,
    payload: MinutesUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CouncilSession:
    session = get_existing_session(db, session_id)
    get_plenos_organization_for_write(db, session.organization_id)
    # Aprobar un acta es el acto que la convierte en el registro oficial de lo
    # acordado, así que pide `manage`; redactarla, sólo `edit`.
    require_plenos_permission(
        db,
        current_user,
        session.organization_id,
        "plenos.manage" if payload.minutes_status == "approved" else "plenos.edit",
    )
    if session.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A cancelled session produces no minutes",
        )
    if payload.minutes_document_id is not None:
        ensure_document_belongs_to_organization(
            db,
            document_id=payload.minutes_document_id,
            organization_id=session.organization_id,
        )

    session.minutes_status = payload.minutes_status
    session.minutes_document_id = payload.minutes_document_id
    commit_or_conflict(db, "Minutes could not be saved")
    return get_existing_session(db, session.id)


def get_existing_session(db: Session, session_id: int) -> CouncilSession:
    session = db.scalar(
        select(CouncilSession)
        .options(selectinload(CouncilSession.agenda_items))
        .where(CouncilSession.id == session_id)
        .execution_options(populate_existing=True)
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Council session not found",
        )
    return session


def ensure_document_belongs_to_organization(
    db: Session,
    *,
    document_id: int,
    organization_id: int,
) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )
    if document.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document does not belong to the organization",
        )
    return document


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
