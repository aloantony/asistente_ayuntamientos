from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.core.transactions import flush_or_conflict
from sqlalchemy.orm import Session, selectinload

from app.assets.access import (
    get_asset_organization_for_read,
    get_asset_organization_for_write,
    require_asset_permission,
)
from app.assets.models import MunicipalAsset
from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.maintenance.access import (
    require_maintenance_manager,
    require_maintenance_permission,
    require_maintenance_response_permissions,
)
from app.maintenance.models import MaintenanceOrder, MaintenanceOrderEvent
from app.maintenance.guards import (
    get_active_organization_assignee,
    lock_maintenance_users,
)
from app.maintenance.schemas import (
    MaintenanceOrderCreate,
    MaintenanceOrderDetail,
    MaintenanceOrderRead,
    MaintenanceOrderTransition,
    MaintenanceOrderUpdate,
    MaintenancePriority,
    MaintenanceStatus,
    MaintenanceType,
)
from app.organizations.models import Organization
from app.users.models import User

router = APIRouter(prefix="/maintenance/orders", tags=["maintenance"])

OPEN_STATUSES = ("planned", "scheduled", "in_progress")
TERMINAL_STATUSES = {"completed", "cancelled"}
TRANSITIONS: dict[str, set[str]] = {
    "planned": {"scheduled", "in_progress", "cancelled"},
    "scheduled": {"planned", "in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": {"planned"},
    "cancelled": {"planned"},
}


@router.get("", response_model=list[MaintenanceOrderRead])
def list_maintenance_orders(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    asset_id: int | None = None,
    status_filter: Annotated[
        MaintenanceStatus | None,
        Query(alias="status"),
    ] = None,
    priority: MaintenancePriority | None = None,
    maintenance_type: MaintenanceType | None = None,
    assigned_to_id: int | None = None,
    scheduled_from: date | None = None,
    scheduled_to: date | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_closed: bool = False,
) -> list[MaintenanceOrder]:
    if scheduled_from and scheduled_to and scheduled_from > scheduled_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scheduled_from cannot be after scheduled_to",
        )
    require_maintenance_response_permissions(
        db,
        current_user,
        organization_id,
        "maintenance.view",
    )
    try:
        get_asset_organization_for_read(db, organization_id)
    except HTTPException as error:
        raise hidden_order() from error

    query = select_orders().where(
        MaintenanceOrder.organization_id == organization_id
    )
    if asset_id is not None:
        query = query.where(MaintenanceOrder.asset_id == asset_id)
    if status_filter is not None:
        query = query.where(MaintenanceOrder.status == status_filter)
    elif not include_closed:
        query = query.where(MaintenanceOrder.status.in_(OPEN_STATUSES))
    if priority is not None:
        query = query.where(MaintenanceOrder.priority == priority)
    if maintenance_type is not None:
        query = query.where(
            MaintenanceOrder.maintenance_type == maintenance_type
        )
    if assigned_to_id is not None:
        query = query.where(MaintenanceOrder.assigned_to_id == assigned_to_id)
    if scheduled_from is not None:
        query = query.where(
            MaintenanceOrder.scheduled_for >= scheduled_from
        )
    if scheduled_to is not None:
        query = query.where(MaintenanceOrder.scheduled_for <= scheduled_to)
    if q and q.strip():
        search_text = f"%{q.strip()}%"
        query = query.join(MunicipalAsset, MaintenanceOrder.asset).where(
            or_(
                MaintenanceOrder.title.ilike(search_text),
                MaintenanceOrder.description.ilike(search_text),
                MunicipalAsset.code.ilike(search_text),
                MunicipalAsset.name.ilike(search_text),
            )
        )

    query = query.order_by(
        MaintenanceOrder.scheduled_for.asc().nulls_last(),
        MaintenanceOrder.id.desc(),
    )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "",
    response_model=MaintenanceOrderDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_maintenance_order(
    payload: MaintenanceOrderCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MaintenanceOrder:
    result = create_maintenance_order_record(payload, db, current_user)
    commit_or_conflict(db, "Municipal operation could not be saved")
    return result


def create_maintenance_order_record(
    payload: MaintenanceOrderCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MaintenanceOrder:
    asset = get_existing_asset(db, payload.asset_id)
    require_visible_resource_permissions(
        db,
        current_user,
        asset.organization_id,
        "maintenance.create",
        hide_missing_maintenance=False,
    )
    get_visible_organization_for_write(db, asset.organization_id)

    asset = get_locked_asset(db, payload.asset_id)
    if asset.status in {"retired", "archived"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Retired or archived assets cannot receive maintenance orders",
        )
    lock_maintenance_users(db, current_user.id, payload.assigned_to_id)
    if payload.assigned_to_id is not None:
        validate_assignee(
            db,
            organization_id=asset.organization_id,
            user_id=payload.assigned_to_id,
            lock_already_held=True,
        )
    order_status = "scheduled" if payload.scheduled_for else "planned"
    values = payload.model_dump()
    values.pop("asset_id")
    order = MaintenanceOrder(
        asset_id=asset.id,
        organization_id=asset.organization_id,
        municipality_id=asset.municipality_id,
        status=order_status,
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
        **values,
    )
    db.add(order)
    flush_or_conflict(db)
    db.add(
        MaintenanceOrderEvent(
            order_id=order.id,
            organization_id=order.organization_id,
            event_type="created",
            to_status=order.status,
            changed_fields=list(CREATION_AUDIT_FIELDS),
            actor_id=current_user.id,
        )
    )
    flush_or_conflict(db)
    return get_existing_order(db, order.id, include_events=True)


@router.get("/{order_id}", response_model=MaintenanceOrderDetail)
def get_maintenance_order(
    order_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MaintenanceOrder:
    order = get_existing_order(db, order_id, include_events=True)
    require_visible_resource_permissions(
        db, current_user, order.organization_id, "maintenance.view"
    )
    try:
        get_asset_organization_for_read(db, order.organization_id)
    except HTTPException as error:
        raise hidden_order() from error
    return order


@router.patch("/{order_id}", response_model=MaintenanceOrderDetail)
def update_maintenance_order(
    order_id: int,
    payload: MaintenanceOrderUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MaintenanceOrder:
    result = update_maintenance_order_record(order_id, payload, db, current_user)
    commit_or_conflict(db, "Municipal operation could not be saved")
    return result


def update_maintenance_order_record(
    order_id: int,
    payload: MaintenanceOrderUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MaintenanceOrder:
    existing = get_existing_order(db, order_id)
    required_permission = (
        "maintenance.manage"
        if existing.status in TERMINAL_STATUSES
        else "maintenance.edit"
    )
    require_visible_resource_permissions(
        db,
        current_user,
        existing.organization_id,
        required_permission,
        hide_missing_maintenance=False,
    )
    if existing.status in TERMINAL_STATUSES:
        require_maintenance_manager(db, current_user, existing.organization_id)
    get_visible_organization_for_write(db, existing.organization_id)
    updates = payload.model_dump(exclude_unset=True)
    get_locked_asset(db, existing.asset_id)
    order = get_locked_order(db, order_id)
    if order.status in TERMINAL_STATUSES:
        require_visible_resource_permissions(
            db,
            current_user,
            order.organization_id,
            "maintenance.manage",
            hide_missing_maintenance=False,
        )
        require_maintenance_manager(db, current_user, order.organization_id)
    lock_maintenance_users(
        db,
        current_user.id,
        updates.get("assigned_to_id"),
    )
    if "assigned_to_id" in updates and updates["assigned_to_id"] is not None:
        validate_assignee(
            db,
            organization_id=existing.organization_id,
            user_id=updates["assigned_to_id"],
            lock_already_held=True,
        )
    if (
        order.status == "planned"
        and updates.get("scheduled_for") is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Use the scheduling transition to set a planned order date",
        )
    if (
        order.status == "scheduled"
        and updates.get("scheduled_for", order.scheduled_for) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scheduled orders must keep a scheduled date",
        )
    changes = changed_values(order, updates)
    if not changes:
        return get_existing_order(db, order.id, include_events=True)

    for field_name, value in updates.items():
        setattr(order, field_name, value)
    order.updated_by_id = current_user.id
    db.add(
        MaintenanceOrderEvent(
            order_id=order.id,
            organization_id=order.organization_id,
            event_type="updated",
            changed_fields=sorted(changes),
            actor_id=current_user.id,
        )
    )
    flush_or_conflict(db)
    return get_existing_order(db, order.id, include_events=True)


@router.post(
    "/{order_id}/transition",
    response_model=MaintenanceOrderDetail,
)
def transition_maintenance_order(
    order_id: int,
    payload: MaintenanceOrderTransition,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MaintenanceOrder:
    result = transition_maintenance_order_record(order_id, payload, db, current_user)
    commit_or_conflict(db, "Municipal operation could not be saved")
    return result


def transition_maintenance_order_record(
    order_id: int,
    payload: MaintenanceOrderTransition,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MaintenanceOrder:
    existing = get_existing_order(db, order_id)
    needs_manager = (
        payload.status == "cancelled"
        or existing.status in TERMINAL_STATUSES
    )
    required_permission = (
        "maintenance.manage"
        if needs_manager
        else (
            "maintenance.complete"
            if payload.status == "completed"
            else "maintenance.edit"
        )
    )
    require_visible_resource_permissions(
        db,
        current_user,
        existing.organization_id,
        required_permission,
        hide_missing_maintenance=False,
    )
    if needs_manager:
        require_maintenance_manager(
            db,
            current_user,
            existing.organization_id,
        )
    if needs_manager and payload.note is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A reason is required to cancel or reopen an order",
        )
    get_visible_organization_for_write(db, existing.organization_id)

    asset = get_locked_asset(db, existing.asset_id)
    order = get_locked_order(db, order_id)
    if order.status in TERMINAL_STATUSES and not needs_manager:
        require_visible_resource_permissions(
            db,
            current_user,
            order.organization_id,
            "maintenance.manage",
            hide_missing_maintenance=False,
        )
        require_maintenance_manager(db, current_user, order.organization_id)
        needs_manager = True
        if payload.note is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A reason is required to cancel or reopen an order",
            )
    lock_maintenance_users(db, current_user.id, order.assigned_to_id)
    target_status = payload.status
    if target_status == order.status or target_status not in TRANSITIONS[order.status]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invalid maintenance transition: {order.status} -> {target_status}",
        )
    if payload.scheduled_for is not None and target_status != "scheduled":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scheduled_for is only accepted when scheduling an order",
        )
    scheduled_for = payload.scheduled_for or order.scheduled_for
    if target_status == "scheduled" and scheduled_for is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A scheduled date is required before scheduling an order",
        )
    is_reopening = (
        order.status in TERMINAL_STATUSES and target_status == "planned"
    )
    if is_reopening and asset.status in {"retired", "archived"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Retired or archived assets cannot reopen maintenance orders",
        )
    if (
        target_status in {"in_progress", "completed"} or is_reopening
    ) and order.assigned_to_id:
        validate_assignee(
            db,
            organization_id=order.organization_id,
            user_id=order.assigned_to_id,
            lock_already_held=True,
        )

    previous_status = order.status
    order.status = target_status
    changed_fields = ["status"]
    if target_status == "scheduled" and payload.scheduled_for is not None:
        if order.scheduled_for != payload.scheduled_for:
            changed_fields.append("scheduled_for")
        order.scheduled_for = payload.scheduled_for
    elif target_status == "planned" and order.scheduled_for is not None:
        order.scheduled_for = None
        changed_fields.append("scheduled_for")
    order.updated_by_id = current_user.id
    db.add(
        MaintenanceOrderEvent(
            order_id=order.id,
            organization_id=order.organization_id,
            event_type="transition",
            from_status=previous_status,
            to_status=target_status,
            changed_fields=changed_fields,
            note=payload.note,
            actor_id=current_user.id,
        )
    )
    flush_or_conflict(db)
    return get_existing_order(db, order.id, include_events=True)


def select_orders(*, include_events: bool = False):
    options = [
        selectinload(MaintenanceOrder.asset),
        selectinload(MaintenanceOrder.assigned_to),
    ]
    if include_events:
        options.append(selectinload(MaintenanceOrder.events))
    return (
        select(MaintenanceOrder)
        .options(*options)
        .execution_options(populate_existing=True)
    )


def get_existing_asset(db: Session, asset_id: int) -> MunicipalAsset:
    asset = db.get(MunicipalAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Municipal asset not found",
        )
    return asset


def get_locked_asset(db: Session, asset_id: int) -> MunicipalAsset:
    asset = db.scalar(
        select(MunicipalAsset)
        .where(MunicipalAsset.id == asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Municipal asset not found",
        )
    return asset


def get_existing_order(
    db: Session,
    order_id: int,
    *,
    include_events: bool = False,
) -> MaintenanceOrder:
    order = db.scalar(
        select_orders(include_events=include_events).where(
            MaintenanceOrder.id == order_id
        )
    )
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Maintenance order not found",
        )
    return order


def get_locked_order(db: Session, order_id: int) -> MaintenanceOrder:
    order = db.scalar(
        select(MaintenanceOrder)
        .where(MaintenanceOrder.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Maintenance order not found",
        )
    return order


def validate_assignee(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    lock_already_held: bool = False,
) -> User:
    return get_active_organization_assignee(
        db,
        organization_id=organization_id,
        user_id=user_id,
        lock_already_held=lock_already_held,
    )


def changed_values(
    order: MaintenanceOrder,
    updates: dict[str, object],
) -> set[str]:
    return {
        field_name
        for field_name, value in updates.items()
        if getattr(order, field_name) != value
    }


CREATION_AUDIT_FIELDS = [
    "asset_id",
    "title",
    "description",
    "maintenance_type",
    "priority",
    "status",
    "scheduled_for",
    "estimated_minutes",
    "assigned_to_id",
]


def require_visible_resource_permissions(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
    *,
    hide_missing_maintenance: bool = True,
) -> None:
    try:
        require_asset_permission(
            db, current_user, organization_id, "assets.view"
        )
    except HTTPException as error:
        raise hidden_order() from error
    try:
        require_maintenance_permission(
            db, current_user, organization_id, permission_code
        )
    except HTTPException as error:
        if hide_missing_maintenance:
            raise hidden_order() from error
        raise


def hidden_order() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Maintenance order not found",
    )


def get_visible_organization_for_write(
    db: Session,
    organization_id: int,
) -> Organization:
    try:
        get_asset_organization_for_read(db, organization_id)
    except HTTPException as error:
        raise hidden_order() from error
    return get_asset_organization_for_write(db, organization_id)


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
