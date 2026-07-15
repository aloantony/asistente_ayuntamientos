from fastapi import HTTPException, status
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.maintenance.models import MaintenanceOrder, MaintenanceOrderEvent
from app.organizations.models import organization_users
from app.users.models import User

OPEN_MAINTENANCE_STATUSES = ("planned", "scheduled", "in_progress")
MAINTENANCE_USER_LOCK_NAMESPACE = 1_298_121_844


def lock_maintenance_users(db: Session, *user_ids: int | None) -> None:
    """Serialize assignment and user lifecycle changes in deterministic order."""
    for user_id in sorted({value for value in user_ids if value is not None}):
        db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :user_id)"),
            {
                "namespace": MAINTENANCE_USER_LOCK_NAMESPACE,
                "user_id": user_id,
            },
        )


def get_active_organization_assignee(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    lock_already_held: bool = False,
) -> User:
    if not lock_already_held:
        lock_maintenance_users(db, user_id)
    membership = db.execute(
        select(organization_users.c.user_id)
        .where(
            organization_users.c.organization_id == organization_id,
            organization_users.c.user_id == user_id,
        )
        .with_for_update()
    ).first()
    if membership is None:
        raise hidden_resource()
    user = db.scalar(
        select(User)
        .where(User.id == user_id, User.is_active.is_(True))
        .with_for_update()
    )
    if user is None:
        raise hidden_resource()
    return user


def ensure_membership_has_no_open_assignments(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
) -> None:
    lock_maintenance_users(db, user_id)
    db.execute(
        select(organization_users.c.user_id)
        .where(
            organization_users.c.organization_id == organization_id,
            organization_users.c.user_id == user_id,
        )
        .with_for_update()
    ).first()
    open_order_id = db.scalar(
        select(MaintenanceOrder.id)
        .where(
            MaintenanceOrder.organization_id == organization_id,
            MaintenanceOrder.assigned_to_id == user_id,
            MaintenanceOrder.status.in_(OPEN_MAINTENANCE_STATUSES),
        )
        .limit(1)
    )
    if open_order_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User has open assigned maintenance orders",
        )


def ensure_user_has_no_open_assignments(db: Session, user_id: int) -> None:
    lock_maintenance_users(db, user_id)
    open_order_id = db.scalar(
        select(MaintenanceOrder.id)
        .where(
            MaintenanceOrder.assigned_to_id == user_id,
            MaintenanceOrder.status.in_(OPEN_MAINTENANCE_STATUSES),
        )
        .limit(1)
    )
    if open_order_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User has open assigned maintenance orders",
        )


def ensure_user_has_no_maintenance_history(db: Session, user_id: int) -> None:
    lock_maintenance_users(db, user_id)
    order_reference = db.scalar(
        select(MaintenanceOrder.id)
        .where(
            or_(
                MaintenanceOrder.assigned_to_id == user_id,
                MaintenanceOrder.created_by_id == user_id,
                MaintenanceOrder.updated_by_id == user_id,
            )
        )
        .limit(1)
    )
    event_reference = db.scalar(
        select(MaintenanceOrderEvent.id)
        .where(MaintenanceOrderEvent.actor_id == user_id)
        .limit(1)
    )
    if order_reference is not None or event_reference is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User has maintenance history that must be preserved",
        )


def hidden_resource() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Resource not found",
    )
