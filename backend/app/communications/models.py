from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.users.models import User


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


NEWS_STATUSES = ("draft", "published", "archived")

# Un bando es un anuncio con efecto administrativo; una noticia, informativo.
NOTICE_KINDS = ("bando", "edicto", "convocatoria", "other")

NOTICE_STATUSES = ("draft", "published", "withdrawn", "expired")

NOTICE_EVENT_TYPES = ("created", "updated", "published", "withdrawn")


class MunicipalNews(TimestampMixin, Base):
    """Noticia municipal. Informativa: no produce efectos administrativos."""

    __tablename__ = "municipal_news"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in(NEWS_STATUSES)})",
            name="ck_municipal_news_status",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_municipal_news_title",
        ),
        # Publicada significa que consta desde cuándo.
        CheckConstraint(
            "(status = 'published') = (published_on is not null)",
            name="ck_municipal_news_published_on",
        ),
        UniqueConstraint(
            "organization_id",
            "slug",
            name="uq_municipal_news_org_slug",
        ),
        Index(
            "ix_municipal_news_org_status_published",
            "organization_id",
            "status",
            "published_on",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    publish_to_sede: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
        nullable=False,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")


class MunicipalNotice(TimestampMixin, Base):
    """Bando o edicto municipal, con su periodo de exposición pública.

    A diferencia de una noticia, un bando produce efectos: importa desde cuándo
    y hasta cuándo estuvo expuesto, y por eso lleva historial.
    """

    __tablename__ = "municipal_notices"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(NOTICE_KINDS)})",
            name="ck_municipal_notices_kind",
        ),
        CheckConstraint(
            f"status in ({_sql_in(NOTICE_STATUSES)})",
            name="ck_municipal_notices_status",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_municipal_notices_title",
        ),
        CheckConstraint(
            "expires_on is null"
            " or published_on is null"
            " or expires_on >= published_on",
            name="ck_municipal_notices_period",
        ),
        CheckConstraint(
            "status <> 'published' or published_on is not null",
            name="ck_municipal_notices_published_on",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_notices_id_org",
        ),
        Index(
            "ix_municipal_notices_org_status_published",
            "organization_id",
            "status",
            "published_on",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        String(20),
        default="bando",
        server_default="bando",
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    publish_to_sede: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
        nullable=False,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    events: Mapped[list["MunicipalNoticeEvent"]] = relationship(
        "MunicipalNoticeEvent",
        back_populates="notice",
        order_by="MunicipalNoticeEvent.id.desc()",
        viewonly=True,
    )


class MunicipalNoticeEvent(Base):
    """Rastro append-only de la exposición pública de un bando.

    Que un bando estuviera publicado en una fecha concreta puede tener que
    demostrarse después, así que retirarlo no borra que llegó a publicarse.
    """

    __tablename__ = "municipal_notice_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type in ({_sql_in(NOTICE_EVENT_TYPES)})",
            name="ck_municipal_notice_events_type",
        ),
        CheckConstraint(
            "from_status is null or "
            f"from_status in ({_sql_in(NOTICE_STATUSES)})",
            name="ck_municipal_notice_events_from_status",
        ),
        CheckConstraint(
            "to_status is null or "
            f"to_status in ({_sql_in(NOTICE_STATUSES)})",
            name="ck_municipal_notice_events_to_status",
        ),
        ForeignKeyConstraint(
            ["notice_id", "organization_id"],
            ["municipal_notices.id", "municipal_notices.organization_id"],
            name="fk_municipal_notice_events_notice_org",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_municipal_notice_events_notice",
            "notice_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    notice: Mapped["MunicipalNotice"] = relationship(
        "MunicipalNotice",
        back_populates="events",
        viewonly=True,
    )
