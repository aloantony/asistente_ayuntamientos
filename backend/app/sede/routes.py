"""Sede electrónica: una vista de solo lectura sobre lo que ya se publica.

No estrena dominio salvo `municipal_taxes`. Todo lo demás —bandos, noticias,
trámites, transparencia, contratos y plenos— ya vive en administración,
comunicación y plenos, y aquí sólo se recoge lo que lleva `publish_to_sede`.
Duplicar esos datos habría creado dos verdades que envejecerían por separado.

**Sigue exigiendo autenticación.** Es la vista previa de lo que verá la
ciudadanía, no el portal público: abrirla sin sesión expondría por comodidad
datos cuya publicación real es una decisión de despliegue, no de este endpoint.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.administration.models import (
    MunicipalContract,
    MunicipalProcedure,
    TransparencyItem,
)
from app.auth.dependencies import get_current_user
from app.communications.models import MunicipalNews, MunicipalNotice
from app.db.session import get_db
from app.ordinances.models import Ordinance
from app.organizations.models import Organization
from app.plenos.models import CouncilSession
from app.rbac.permissions import has_permission
from app.sede.models import MunicipalTax
from app.users.models import User

router = APIRouter(prefix="/sede", tags=["sede"])

SECTION_LIMIT = 50


class BoardEntry(BaseModel):
    """Tablón: bandos y noticias mezclados, que es como se leen."""

    kind: str
    id: int
    title: str
    summary: str | None
    published_on: date | None
    expires_on: date | None


class SedeProcedure(BaseModel):
    id: int
    slug: str
    name: str
    description: str | None
    channel: str
    deadline_days: int | None
    fee_description: str | None

    model_config = ConfigDict(from_attributes=True)


class SedeTax(BaseModel):
    id: int
    slug: str
    name: str
    kind: str
    rate_kind: str
    rate_value: Decimal | None
    rate_description: str | None
    taxable_base: str | None
    ordinance_id: int | None

    model_config = ConfigDict(from_attributes=True)


class SedeContract(BaseModel):
    id: int
    reference: str
    title: str
    procedure_type: str
    status: str
    base_amount: Decimal | None
    awarded_amount: Decimal | None
    awarded_to: str | None
    published_on: date | None

    model_config = ConfigDict(from_attributes=True)


class SedeTransparencyItem(BaseModel):
    id: int
    area: str
    title: str
    description: str | None
    reference_period: str | None
    published_on: date | None

    model_config = ConfigDict(from_attributes=True)


class SedeSession(BaseModel):
    id: int
    kind: str
    status: str
    held_on: date
    summary: str | None
    minutes_status: str
    minutes_document_id: int | None

    model_config = ConfigDict(from_attributes=True)


class SedeOrdinance(BaseModel):
    id: int
    title: str
    topic: str
    ordinance_type: str
    status: str
    approval_date: date | None
    publication_date: date | None

    model_config = ConfigDict(from_attributes=True)


class SedeContent(BaseModel):
    organization_id: int
    board: list[BoardEntry]
    procedures: list[SedeProcedure]
    taxes: list[SedeTax]
    contracts: list[SedeContract]
    transparency: list[SedeTransparencyItem]
    sessions: list[SedeSession]
    ordinances: list[SedeOrdinance]


@router.get("", response_model=SedeContent)
def get_sede_content(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
) -> SedeContent:
    organization = db.scalar(
        select(Organization).where(Organization.id == organization_id)
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    # Una organización archivada no publica: su sede deja de responder en lugar
    # de seguir enseñando lo último que hubiera.
    if organization.status not in {"active", "paused"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization is not available for sede reads",
        )
    if not (
        has_permission(current_user, "sede.view", db, organization_id=organization_id)
        or has_permission(
            current_user,
            "municipalities.view",
            db,
            organization_id=organization_id,
        )
        or has_permission(
            current_user,
            "municipalities.manage",
            db,
            organization_id=organization_id,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: sede.view",
        )

    # Todo se sirve en una respuesta: la sede de un municipio pequeño cabe de
    # sobra, y siete peticiones para pintar siete pestañas sería derrochar.
    response.headers["Cache-Control"] = "no-store"

    notices = list(
        db.scalars(
            select(MunicipalNotice)
            .where(
                MunicipalNotice.organization_id == organization_id,
                MunicipalNotice.publish_to_sede.is_(True),
                # Sólo lo expuesto: un borrador o un bando retirado no está en
                # el tablón, aunque conste en el histórico interno.
                MunicipalNotice.status == "published",
            )
            .order_by(MunicipalNotice.published_on.desc(), MunicipalNotice.id.desc())
            .limit(SECTION_LIMIT)
        )
    )
    news = list(
        db.scalars(
            select(MunicipalNews)
            .where(
                MunicipalNews.organization_id == organization_id,
                MunicipalNews.publish_to_sede.is_(True),
                MunicipalNews.status == "published",
            )
            .order_by(MunicipalNews.published_on.desc(), MunicipalNews.id.desc())
            .limit(SECTION_LIMIT)
        )
    )
    board = [
        BoardEntry(
            kind=notice.kind,
            id=notice.id,
            title=notice.title,
            summary=notice.body,
            published_on=notice.published_on,
            expires_on=notice.expires_on,
        )
        for notice in notices
    ] + [
        BoardEntry(
            kind="noticia",
            id=item.id,
            title=item.title,
            summary=item.summary,
            published_on=item.published_on,
            expires_on=None,
        )
        for item in news
    ]
    # Lo más reciente primero, sea bando o noticia; sin fecha, al final.
    board.sort(key=lambda entry: entry.published_on or date.min, reverse=True)

    procedures = list(
        db.scalars(
            select(MunicipalProcedure)
            .where(
                MunicipalProcedure.organization_id == organization_id,
                MunicipalProcedure.publish_to_sede.is_(True),
                MunicipalProcedure.status == "active",
            )
            .order_by(MunicipalProcedure.name)
            .limit(SECTION_LIMIT)
        )
    )
    taxes = list(
        db.scalars(
            select(MunicipalTax)
            .where(
                MunicipalTax.organization_id == organization_id,
                MunicipalTax.publish_to_sede.is_(True),
            )
            .order_by(MunicipalTax.kind, MunicipalTax.name)
            .limit(SECTION_LIMIT)
        )
    )
    contracts = list(
        db.scalars(
            select(MunicipalContract)
            .where(
                MunicipalContract.organization_id == organization_id,
                # El perfil de contratante enseña lo que salió a licitación en
                # adelante; un borrador todavía no es público.
                MunicipalContract.status.in_(
                    ("published", "awarded", "executed")
                ),
            )
            .order_by(
                MunicipalContract.published_on.desc().nullslast(),
                MunicipalContract.id.desc(),
            )
            .limit(SECTION_LIMIT)
        )
    )
    transparency = list(
        db.scalars(
            select(TransparencyItem)
            .where(
                TransparencyItem.organization_id == organization_id,
                TransparencyItem.publish_to_sede.is_(True),
            )
            .order_by(TransparencyItem.area, TransparencyItem.title)
            .limit(SECTION_LIMIT)
        )
    )
    sessions = list(
        db.scalars(
            select(CouncilSession)
            .where(
                CouncilSession.organization_id == organization_id,
                CouncilSession.publish_to_sede.is_(True),
                CouncilSession.status != "cancelled",
            )
            .order_by(CouncilSession.held_on.desc(), CouncilSession.id.desc())
            .limit(SECTION_LIMIT)
        )
    )
    ordinances = list(
        db.scalars(
            select(Ordinance)
            .where(
                Ordinance.municipality_id == organization.municipality_id,
                # Sólo normativa vigente y revisada: la sede no es el sitio
                # para una ordenanza pendiente de curar.
                Ordinance.status == "active",
                Ordinance.curation_status == "approved",
            )
            .order_by(Ordinance.title)
            .limit(SECTION_LIMIT)
        )
        if organization.municipality_id is not None
        else []
    )

    return SedeContent(
        organization_id=organization_id,
        board=board,
        procedures=[SedeProcedure.model_validate(row) for row in procedures],
        taxes=[SedeTax.model_validate(row) for row in taxes],
        contracts=[SedeContract.model_validate(row) for row in contracts],
        transparency=[
            SedeTransparencyItem.model_validate(row) for row in transparency
        ],
        sessions=[SedeSession.model_validate(row) for row in sessions],
        ordinances=[SedeOrdinance.model_validate(row) for row in ordinances],
    )
