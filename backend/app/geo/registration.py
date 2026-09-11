"""Atomic, retryable registration using the same domain services as the forms."""
import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assets.routes import create_asset_record
from app.assets.schemas import MunicipalAssetCreate
from app.geo.access import has_map_edit_permission, require_visible_entity
from app.geo.models import MapRegistration
from app.geo.routes import save_entity_location
from app.geo.schemas import EntityLocationCreate, MapRegistrationCreate
from app.projects.routes import create_project_record
from app.projects.schemas import ProjectCreate
from app.requirements.routes import create_requirement_record
from app.requirements.schemas import RequirementCreate
from app.users.models import User


def register_map_item(payload: MapRegistrationCreate, db: Session, user: User):
    if not has_map_edit_permission(db, user, payload.organization_id):
        raise HTTPException(403, "Permission required: map.edit")
    if payload.location.organization_id not in (None, payload.organization_id):
        raise HTTPException(409, "Location organization does not match registration")
    digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    receipt = db.scalar(select(MapRegistration).where(
        MapRegistration.actor_id == user.id, MapRegistration.request_key == payload.request_key))
    if receipt is not None:
        if receipt.payload_sha256 != digest:
            raise HTTPException(409, "This registration key was already used for different content")
        # Revalidate visibility on every replay. Never return a cached authorized response.
        require_visible_entity(db, user, receipt.entity_type, receipt.entity_id)
        return receipt.entity_type, receipt.entity_id
    try:
        if payload.entity_type == "requirement":
            entity = create_requirement_record(RequirementCreate(
                organization_id=payload.organization_id, title=payload.title,
                summary=payload.description, problem=payload.description,
                status="draft", priority="medium", source_type="manual"), db, user, commit=False)
        elif payload.entity_type == "project":
            entity = create_project_record(ProjectCreate(
                organization_id=payload.organization_id, name=payload.title,
                description=payload.description, status="active"), db, user, commit=False)
        else:
            if payload.asset_type_id is None:
                raise HTTPException(422, "Asset type is required")
            entity = create_asset_record(MunicipalAssetCreate(
                organization_id=payload.organization_id, asset_type_id=payload.asset_type_id,
                name=payload.title, description=payload.description), db, user, commit=False)
        save_entity_location(EntityLocationCreate(entity_type=payload.entity_type, entity_id=entity.id,
            location=payload.location), db, user, commit=False)
        db.add(MapRegistration(actor_id=user.id, request_key=payload.request_key,
            payload_sha256=digest, entity_type=payload.entity_type, entity_id=entity.id))
        db.commit()
        return payload.entity_type, entity.id
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Registration conflict. Retry the same request key.") from None
    except Exception:
        db.rollback()
        raise
