from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DDL,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.assets.models import MunicipalAsset
    from app.users.models import User


MAINTENANCE_ORDER_STATUSES = (
    "planned",
    "scheduled",
    "in_progress",
    "completed",
    "cancelled",
)
MAINTENANCE_ORDER_PRIORITIES = ("low", "normal", "high", "urgent")
MAINTENANCE_ORDER_TYPES = (
    "preventive",
    "corrective",
    "inspection",
    "cleaning",
    "other",
)
MAINTENANCE_EVENT_TYPES = ("created", "updated", "transition")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class MaintenanceOrder(TimestampMixin, Base):
    __tablename__ = "maintenance_orders"
    __table_args__ = (
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_maintenance_orders_title",
        ),
        CheckConstraint(
            f"maintenance_type in ({_sql_in(MAINTENANCE_ORDER_TYPES)})",
            name="ck_maintenance_orders_type",
        ),
        CheckConstraint(
            f"priority in ({_sql_in(MAINTENANCE_ORDER_PRIORITIES)})",
            name="ck_maintenance_orders_priority",
        ),
        CheckConstraint(
            f"status in ({_sql_in(MAINTENANCE_ORDER_STATUSES)})",
            name="ck_maintenance_orders_status",
        ),
        CheckConstraint(
            "estimated_minutes is null or estimated_minutes > 0",
            name="ck_maintenance_orders_estimated_minutes",
        ),
        CheckConstraint(
            "status <> 'scheduled' or scheduled_for is not null",
            name="ck_maintenance_orders_scheduled_date",
        ),
        ForeignKeyConstraint(
            ["organization_id", "municipality_id"],
            ["organizations.id", "organizations.municipality_id"],
            name="fk_maintenance_orders_organization_municipality",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["asset_id", "organization_id", "municipality_id"],
            [
                "municipal_assets.id",
                "municipal_assets.organization_id",
                "municipal_assets.municipality_id",
            ],
            name="fk_maintenance_orders_asset_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_maintenance_orders_id_org",
        ),
        Index(
            "ix_maintenance_orders_org_status_scheduled",
            "organization_id",
            "status",
            "scheduled_for",
            "id",
        ),
        Index(
            "ix_maintenance_orders_asset_status",
            "asset_id",
            "status",
            "scheduled_for",
            "id",
        ),
        Index(
            "ix_maintenance_orders_assigned_status_scheduled",
            "assigned_to_id",
            "status",
            "scheduled_for",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    municipality_id: Mapped[int] = mapped_column(
        ForeignKey("municipalities.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    asset_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    maintenance_type: Mapped[str] = mapped_column(
        String(30),
        default="other",
        server_default="other",
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        server_default="normal",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="planned",
        server_default="planned",
        nullable=False,
    )
    scheduled_for: Mapped[date | None] = mapped_column(Date, nullable=True)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assigned_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    updated_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )

    asset: Mapped["MunicipalAsset"] = relationship(
        "MunicipalAsset",
        primaryjoin=(
            "and_(MaintenanceOrder.asset_id == MunicipalAsset.id, "
            "MaintenanceOrder.organization_id == MunicipalAsset.organization_id, "
            "MaintenanceOrder.municipality_id == MunicipalAsset.municipality_id)"
        ),
        foreign_keys=[asset_id, organization_id, municipality_id],
        viewonly=True,
    )
    assigned_to: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[assigned_to_id],
    )
    created_by: Mapped["User"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
    events: Mapped[list["MaintenanceOrderEvent"]] = relationship(
        "MaintenanceOrderEvent",
        back_populates="order",
        order_by="MaintenanceOrderEvent.id",
        viewonly=True,
    )


class MaintenanceOrderEvent(Base):
    __tablename__ = "maintenance_order_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type in ({_sql_in(MAINTENANCE_EVENT_TYPES)})",
            name="ck_maintenance_order_events_type",
        ),
        CheckConstraint(
            "from_status is null or "
            f"from_status in ({_sql_in(MAINTENANCE_ORDER_STATUSES)})",
            name="ck_maintenance_order_events_from_status",
        ),
        CheckConstraint(
            "to_status is null or "
            f"to_status in ({_sql_in(MAINTENANCE_ORDER_STATUSES)})",
            name="ck_maintenance_order_events_to_status",
        ),
        ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["maintenance_orders.id", "maintenance_orders.organization_id"],
            name="fk_maintenance_order_events_order_org",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_maintenance_order_events_order_id",
            "order_id",
            "id",
        ),
        Index(
            "ix_maintenance_order_events_org_created",
            "organization_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    changed_fields: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    order: Mapped["MaintenanceOrder"] = relationship(
        "MaintenanceOrder",
        back_populates="events",
        primaryjoin=(
            "and_(MaintenanceOrderEvent.order_id == MaintenanceOrder.id, "
            "MaintenanceOrderEvent.organization_id == "
            "MaintenanceOrder.organization_id)"
        ),
        foreign_keys=[order_id, organization_id],
        viewonly=True,
    )
    actor: Mapped["User"] = relationship(
        "User",
        foreign_keys=[actor_id],
    )


_IMMUTABLE_EVENT_FUNCTION = "prevent_maintenance_order_event_mutation"
_IMMUTABLE_EVENT_TRIGGER = "trg_maintenance_order_events_immutable"

event.listen(
    MaintenanceOrderEvent.__table__,
    "after_create",
    DDL(
        f"""
        CREATE OR REPLACE FUNCTION {_IMMUTABLE_EVENT_FUNCTION}()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'maintenance order events are immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    MaintenanceOrderEvent.__table__,
    "after_create",
    DDL(
        f"""
        CREATE TRIGGER {_IMMUTABLE_EVENT_TRIGGER}
        BEFORE UPDATE OR DELETE ON maintenance_order_events
        FOR EACH ROW EXECUTE FUNCTION {_IMMUTABLE_EVENT_FUNCTION}();
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    MaintenanceOrderEvent.__table__,
    "before_drop",
    DDL(
        f"DROP TRIGGER IF EXISTS {_IMMUTABLE_EVENT_TRIGGER} "
        "ON maintenance_order_events"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    MaintenanceOrderEvent.__table__,
    "after_drop",
    DDL(
        f"DROP FUNCTION IF EXISTS {_IMMUTABLE_EVENT_FUNCTION}()"
    ).execute_if(dialect="postgresql"),
)
