from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.staff.access import (
    get_staff_organization_for_read,
    get_staff_organization_for_write,
    require_staff_permission,
)
from app.staff.models import (
    StaffAbsence,
    StaffHistoryEvent,
    StaffInvoice,
    StaffPost,
    StaffReport,
    StaffWorker,
)
from app.staff.schemas import (
    StaffAbsenceCreate,
    StaffAbsenceRead,
    StaffHistoryEventRead,
    StaffInvoiceCreate,
    StaffInvoiceRead,
    StaffPostCreate,
    StaffPostRead,
    StaffPostUpdate,
    StaffReportCreate,
    StaffReportRead,
    StaffReportType,
    StaffWorkerCreate,
    StaffWorkerRead,
    StaffWorkerStatus,
    StaffWorkerUpdate,
)
from app.users.models import User

router = APIRouter(prefix="/staff", tags=["staff"])

# Campos cuya edición no cambia el estado ni el puesto: agrupan en un único
# evento "updated" del histórico en lugar de generar uno por campo.
_TRACKED_WORKER_FIELDS = (
    "full_name",
    "email",
    "phone",
    "description",
    "schedule_summary",
    "schedule_days",
    "weekly_hours",
    "contract_type",
    "contract_start_date",
    "contract_end_date",
    "vacation_days_limit",
    "personal_days_limit",
    "bills_invoices",
)


@router.get("/posts", response_model=list[StaffPostRead])
def list_staff_posts(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    parent_id: int | None = None,
) -> list[StaffPost]:
    get_staff_organization_for_read(db, organization_id)
    require_staff_permission(db, current_user, organization_id, "staff.view")

    query = (
        select(StaffPost)
        .where(StaffPost.organization_id == organization_id)
        .order_by(StaffPost.sort_order, StaffPost.label, StaffPost.id)
    )
    if parent_id is not None:
        query = query.where(StaffPost.parent_id == parent_id)

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/posts",
    response_model=StaffPostRead,
    status_code=status.HTTP_201_CREATED,
)
def create_staff_post(
    payload: StaffPostCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffPost:
    get_staff_organization_for_write(db, payload.organization_id)
    require_staff_permission(
        db,
        current_user,
        payload.organization_id,
        "staff.manage",
    )
    if payload.parent_id is not None:
        ensure_parent_post_is_valid(
            db,
            parent_id=payload.parent_id,
            organization_id=payload.organization_id,
        )

    post = StaffPost(**payload.model_dump())
    db.add(post)
    commit_or_conflict(db, "Staff post could not be saved")
    db.refresh(post)
    return post


@router.patch("/posts/{post_id}", response_model=StaffPostRead)
def update_staff_post(
    post_id: int,
    payload: StaffPostUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffPost:
    post = get_existing_staff_post(db, post_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        get_staff_organization_for_read(db, post.organization_id)
        require_staff_permission(
            db,
            current_user,
            post.organization_id,
            "staff.view",
        )
        return post

    get_staff_organization_for_write(db, post.organization_id)
    require_staff_permission(db, current_user, post.organization_id, "staff.manage")

    if updates.get("parent_id") is not None:
        ensure_parent_post_is_valid(
            db,
            parent_id=updates["parent_id"],
            organization_id=post.organization_id,
            child_id=post.id,
        )
    if updates.get("kind") == "post" and post.kind == "container":
        # Un contenedor con puestos dentro no puede convertirse en puesto: sus
        # hijos quedarían colgando de algo ocupable.
        has_children = db.scalar(
            select(StaffPost.id).where(StaffPost.parent_id == post.id).limit(1)
        )
        if has_children is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Staff container with nested posts cannot become a post",
            )

    for field, value in updates.items():
        setattr(post, field, value)
    commit_or_conflict(db, "Staff post could not be saved")
    db.refresh(post)
    return post


@router.get("/workers", response_model=list[StaffWorkerRead])
def list_staff_workers(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    post_id: int | None = None,
    status_filter: Annotated[
        StaffWorkerStatus | None,
        Query(alias="status"),
    ] = None,
    include_archived: bool = False,
) -> list[StaffWorker]:
    get_staff_organization_for_read(db, organization_id)
    require_staff_permission(db, current_user, organization_id, "staff.view")

    query = (
        select_workers()
        .where(StaffWorker.organization_id == organization_id)
        .order_by(StaffWorker.full_name, StaffWorker.id)
    )
    if q and q.strip():
        search_text = f"%{q.strip()}%"
        query = query.where(
            or_(
                StaffWorker.full_name.ilike(search_text),
                StaffWorker.email.ilike(search_text),
                StaffWorker.description.ilike(search_text),
            )
        )
    if post_id is not None:
        query = query.where(StaffWorker.post_id == post_id)
    if status_filter is not None:
        query = query.where(StaffWorker.status == status_filter)
    elif not include_archived:
        query = query.where(StaffWorker.status != "archived")

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/workers",
    response_model=StaffWorkerRead,
    status_code=status.HTTP_201_CREATED,
)
def create_staff_worker(
    payload: StaffWorkerCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffWorker:
    get_staff_organization_for_write(db, payload.organization_id)
    require_staff_permission(
        db,
        current_user,
        payload.organization_id,
        "staff.edit",
    )
    if payload.status == "archived":
        require_staff_permission(
            db,
            current_user,
            payload.organization_id,
            "staff.manage",
        )
    if payload.post_id is not None:
        ensure_post_is_assignable(
            db,
            post_id=payload.post_id,
            organization_id=payload.organization_id,
        )

    worker = StaffWorker(
        **payload.model_dump(),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(worker)
    commit_or_conflict(db, "Staff post is already occupied")
    db.refresh(worker)
    record_history_event(
        db,
        worker=worker,
        event_type="created",
        actor_id=current_user.id,
        to_status=worker.status,
    )
    return get_existing_staff_worker(db, worker.id)


@router.get("/workers/{worker_id}", response_model=StaffWorkerRead)
def get_staff_worker(
    worker_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffWorker:
    worker = get_existing_staff_worker(db, worker_id)
    get_staff_organization_for_read(db, worker.organization_id)
    require_staff_permission(db, current_user, worker.organization_id, "staff.view")
    return worker


@router.patch("/workers/{worker_id}", response_model=StaffWorkerRead)
def update_staff_worker(
    worker_id: int,
    payload: StaffWorkerUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffWorker:
    worker = get_existing_staff_worker(db, worker_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        get_staff_organization_for_read(db, worker.organization_id)
        require_staff_permission(
            db,
            current_user,
            worker.organization_id,
            "staff.view",
        )
        return worker

    get_staff_organization_for_write(db, worker.organization_id)
    require_worker_update_permissions(
        db,
        current_user,
        organization_id=worker.organization_id,
        updates=updates,
    )
    ensure_contract_range(
        updates.get("contract_start_date", worker.contract_start_date),
        updates.get("contract_end_date", worker.contract_end_date),
    )
    if updates.get("post_id") is not None:
        ensure_post_is_assignable(
            db,
            post_id=updates["post_id"],
            organization_id=worker.organization_id,
            worker_id=worker.id,
        )

    previous_status = worker.status
    previous_post_id = worker.post_id
    changed_fields = [
        field
        for field in _TRACKED_WORKER_FIELDS
        if field in updates and updates[field] != getattr(worker, field)
    ]

    for field, value in updates.items():
        setattr(worker, field, value)
    worker.updated_by_id = current_user.id
    commit_or_conflict(db, "Staff post is already occupied")
    db.refresh(worker)

    if worker.status != previous_status:
        record_history_event(
            db,
            worker=worker,
            event_type="archived" if worker.status == "archived" else "status_changed",
            actor_id=current_user.id,
            from_status=previous_status,
            to_status=worker.status,
        )
    if worker.post_id != previous_post_id:
        record_history_event(
            db,
            worker=worker,
            event_type="post_changed",
            actor_id=current_user.id,
        )
    if changed_fields:
        record_history_event(
            db,
            worker=worker,
            event_type="updated",
            actor_id=current_user.id,
            changed_fields=changed_fields,
        )

    return get_existing_staff_worker(db, worker.id)


@router.get(
    "/workers/{worker_id}/absences",
    response_model=list[StaffAbsenceRead],
)
def list_staff_absences(
    worker_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[StaffAbsence]:
    worker = get_readable_worker(db, worker_id, current_user)

    query = (
        select(StaffAbsence)
        .where(StaffAbsence.worker_id == worker.id)
        .order_by(StaffAbsence.start_date.desc(), StaffAbsence.id.desc())
    )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/workers/{worker_id}/absences",
    response_model=StaffAbsenceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_staff_absence(
    worker_id: int,
    payload: StaffAbsenceCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffAbsence:
    worker = get_writable_worker(db, worker_id, current_user)

    absence = StaffAbsence(
        **payload.model_dump(),
        worker_id=worker.id,
        organization_id=worker.organization_id,
    )
    db.add(absence)
    commit_or_conflict(db, "Staff absence could not be saved")
    db.refresh(absence)
    return absence


@router.delete(
    "/workers/{worker_id}/absences/{absence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_staff_absence(
    worker_id: int,
    absence_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    worker = get_writable_worker(db, worker_id, current_user)
    absence = db.get(StaffAbsence, absence_id)
    if absence is None or absence.worker_id != worker.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Staff absence not found",
        )

    db.delete(absence)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/workers/{worker_id}/reports",
    response_model=list[StaffReportRead],
)
def list_staff_reports(
    worker_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    report_type: StaffReportType | None = None,
) -> list[StaffReport]:
    worker = get_readable_worker(db, worker_id, current_user)

    query = (
        select(StaffReport)
        .where(StaffReport.worker_id == worker.id)
        .order_by(StaffReport.report_date.desc(), StaffReport.id.desc())
    )
    if report_type is not None:
        query = query.where(StaffReport.report_type == report_type)

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/workers/{worker_id}/reports",
    response_model=StaffReportRead,
    status_code=status.HTTP_201_CREATED,
)
def create_staff_report(
    worker_id: int,
    payload: StaffReportCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffReport:
    worker = get_writable_worker(db, worker_id, current_user)

    report = StaffReport(
        **payload.model_dump(),
        worker_id=worker.id,
        organization_id=worker.organization_id,
        author_id=current_user.id,
    )
    db.add(report)
    commit_or_conflict(db, "Staff report could not be saved")
    db.refresh(report)
    return report


@router.get(
    "/workers/{worker_id}/invoices",
    response_model=list[StaffInvoiceRead],
)
def list_staff_invoices(
    worker_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[StaffInvoice]:
    worker = get_readable_worker(db, worker_id, current_user)

    query = (
        select(StaffInvoice)
        .where(StaffInvoice.worker_id == worker.id)
        .order_by(StaffInvoice.issued_on.desc(), StaffInvoice.id.desc())
    )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/workers/{worker_id}/invoices",
    response_model=StaffInvoiceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_staff_invoice(
    worker_id: int,
    payload: StaffInvoiceCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StaffInvoice:
    worker = get_writable_worker(db, worker_id, current_user)
    if not worker.bills_invoices:
        # Facturar es propio del personal externo; en alguien de nómina sería
        # casi siempre un error de captura.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Staff worker does not bill invoices",
        )

    invoice = StaffInvoice(
        **payload.model_dump(),
        worker_id=worker.id,
        organization_id=worker.organization_id,
    )
    db.add(invoice)
    commit_or_conflict(db, "Staff invoice could not be saved")
    db.refresh(invoice)
    return invoice


@router.get(
    "/workers/{worker_id}/history",
    response_model=list[StaffHistoryEventRead],
)
def list_staff_history(
    worker_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[StaffHistoryEvent]:
    worker = get_readable_worker(db, worker_id, current_user)

    query = (
        select(StaffHistoryEvent)
        .where(StaffHistoryEvent.worker_id == worker.id)
        .order_by(StaffHistoryEvent.id.desc())
    )
    return list(db.scalars(paginate(db, query, page, response)))


def select_workers():
    return (
        select(StaffWorker)
        .options(selectinload(StaffWorker.post))
        .execution_options(populate_existing=True)
    )


def get_existing_staff_post(db: Session, post_id: int) -> StaffPost:
    post = db.get(StaffPost, post_id)
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Staff post not found",
        )
    return post


def get_existing_staff_worker(db: Session, worker_id: int) -> StaffWorker:
    worker = db.scalar(select_workers().where(StaffWorker.id == worker_id))
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Staff worker not found",
        )
    return worker


def get_readable_worker(
    db: Session,
    worker_id: int,
    current_user: User,
) -> StaffWorker:
    worker = get_existing_staff_worker(db, worker_id)
    get_staff_organization_for_read(db, worker.organization_id)
    require_staff_permission(db, current_user, worker.organization_id, "staff.view")
    return worker


def get_writable_worker(
    db: Session,
    worker_id: int,
    current_user: User,
) -> StaffWorker:
    worker = get_existing_staff_worker(db, worker_id)
    get_staff_organization_for_write(db, worker.organization_id)
    require_staff_permission(db, current_user, worker.organization_id, "staff.edit")
    return worker


def ensure_parent_post_is_valid(
    db: Session,
    *,
    parent_id: int,
    organization_id: int,
    child_id: int | None = None,
) -> StaffPost:
    parent = get_existing_staff_post(db, parent_id)
    if parent.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parent staff post does not belong to the organization",
        )
    if parent.kind != "container":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parent staff post must be a container",
        )
    if child_id is not None:
        ensure_no_post_cycle(db, parent=parent, child_id=child_id)
    return parent


def ensure_no_post_cycle(db: Session, *, parent: StaffPost, child_id: int) -> None:
    """Rechaza colgar un puesto de uno de sus propios descendientes.

    La base sólo impide que un puesto sea su propio padre; los ciclos más
    largos hay que cerrarlos aquí, o el árbol de la plantilla dejaría de poder
    recorrerse.
    """
    seen: set[int] = set()
    current: StaffPost | None = parent
    while current is not None:
        if current.id == child_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Parent staff post would create a cycle",
            )
        if current.id in seen or current.parent_id is None:
            return
        seen.add(current.id)
        current = db.get(StaffPost, current.parent_id)


def ensure_post_is_assignable(
    db: Session,
    *,
    post_id: int,
    organization_id: int,
    worker_id: int | None = None,
) -> StaffPost:
    post = get_existing_staff_post(db, post_id)
    if post.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Staff post does not belong to the organization",
        )
    if post.kind != "post":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Staff container cannot be occupied",
        )

    occupant_id = db.scalar(
        select(StaffWorker.id).where(StaffWorker.post_id == post.id).limit(1)
    )
    if occupant_id is not None and occupant_id != worker_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Staff post is already occupied",
        )
    return post


def ensure_contract_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="contract_end_date cannot be earlier than contract_start_date",
        )


def require_worker_update_permissions(
    db: Session,
    current_user: User,
    *,
    organization_id: int,
    updates: dict[str, object],
) -> None:
    if set(updates) - {"status"}:
        require_staff_permission(db, current_user, organization_id, "staff.edit")
    if updates.get("status") == "archived":
        require_staff_permission(db, current_user, organization_id, "staff.manage")
    elif "status" in updates:
        require_staff_permission(db, current_user, organization_id, "staff.edit")


def record_history_event(
    db: Session,
    *,
    worker: StaffWorker,
    event_type: str,
    actor_id: int | None,
    from_status: str | None = None,
    to_status: str | None = None,
    changed_fields: list[str] | None = None,
) -> None:
    event = StaffHistoryEvent(
        worker_id=worker.id,
        organization_id=worker.organization_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        changed_fields=changed_fields or [],
        actor_id=actor_id,
    )
    db.add(event)
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
