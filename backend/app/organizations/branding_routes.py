from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.documents.models import Document
from app.organizations.branding import OrganizationBranding
from app.organizations.models import Organization
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/organizations", tags=["organizations"])


class BrandingRead(BaseModel):
    organization_id: int
    crest_document_id: int | None
    crest_alt_text: str | None
    notes: str | None

    model_config = ConfigDict(from_attributes=True)


class BrandingUpdate(BaseModel):
    crest_document_id: int | None = None
    crest_alt_text: str | None = Field(default=None, max_length=255)
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


def _get_organization(db: Session, organization_id: int) -> Organization:
    organization = db.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return organization


@router.get("/{organization_id}/branding", response_model=BrandingRead)
def get_branding(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BrandingRead:
    _get_organization(db, organization_id)
    if not has_permission(
        current_user,
        "municipalities.view",
        db,
        organization_id=organization_id,
    ) and not has_permission(
        current_user,
        "organizations.manage",
        db,
        organization_id=organization_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: municipalities.view",
        )

    branding = db.get(OrganizationBranding, organization_id)
    if branding is None:
        # Sin fila todavía, la respuesta es la identidad vacía: el escudo es
        # opcional y su ausencia no es un error.
        return BrandingRead(
            organization_id=organization_id,
            crest_document_id=None,
            crest_alt_text=None,
            notes=None,
        )
    return BrandingRead.model_validate(branding)


@router.put("/{organization_id}/branding", response_model=BrandingRead)
def update_branding(
    organization_id: int,
    payload: BrandingUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BrandingRead:
    organization = _get_organization(db, organization_id)
    if not has_permission(
        current_user,
        "organizations.manage",
        db,
        organization_id=organization_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: organizations.manage",
        )
    if organization.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization must be active to change its branding",
        )

    if payload.crest_document_id is not None:
        document = db.get(Document, payload.crest_document_id)
        if document is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document not found",
            )
        # El escudo tiene que ser un documento de la propia organización: si no,
        # bastaría conocer un id ajeno para colgar la imagen de otro municipio.
        if document.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Document does not belong to the organization",
            )

    branding = db.get(OrganizationBranding, organization_id)
    if branding is None:
        branding = OrganizationBranding(organization_id=organization_id)
        db.add(branding)
    for field, value in payload.model_dump().items():
        setattr(branding, field, value)
    db.commit()
    db.refresh(branding)
    return BrandingRead.model_validate(branding)
