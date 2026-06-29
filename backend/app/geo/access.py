from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.organizations.models import Organization
from app.projects.access import get_project_with_memberships, user_can_access_project
from app.projects.models import Project
from app.rbac.permissions import has_permission
from app.requirements.models import Requirement
from app.requirements.routes import can_view_requirement, get_existing_requirement
from app.users.models import User


@dataclass(frozen=True)
class VisibleEntity:
    entity_type: str
    entity: Requirement | Project
    organization_id: int
    organization_name: str
    title: str
    subtitle: str | None
    status: str
    priority: str | None
    detail_path: str


def has_any_map_view_permission(db: Session, current_user: User) -> bool:
    if current_user.is_superuser:
        return True
    return has_permission(current_user, "map.view", db) or has_permission(
        current_user,
        "map.manage",
        db,
    )


def has_map_view_permission(
    db: Session,
    current_user: User,
    organization_id: int,
) -> bool:
    if current_user.is_superuser:
        return True
    return has_permission(
        current_user,
        "map.view",
        db,
        organization_id=organization_id,
    ) or has_permission(
        current_user,
        "map.manage",
        db,
        organization_id=organization_id,
    )


def has_map_edit_permission(
    db: Session,
    current_user: User,
    organization_id: int,
) -> bool:
    if current_user.is_superuser:
        return True
    return has_permission(
        current_user,
        "map.edit",
        db,
        organization_id=organization_id,
    ) or has_permission(
        current_user,
        "map.manage",
        db,
        organization_id=organization_id,
    )


def get_visible_entity(
    db: Session,
    current_user: User,
    entity_type: str,
    entity_id: int,
) -> VisibleEntity | None:
    if entity_type == "requirement":
        requirement = db.get(Requirement, entity_id)
        if requirement is None:
            return None
        if not can_view_requirement(db, current_user, requirement):
            return None
        # Reload with summary relationships for organization/project labels.
        requirement = get_existing_requirement(db, entity_id)
        return VisibleEntity(
            entity_type="requirement",
            entity=requirement,
            organization_id=requirement.organization_id,
            organization_name=requirement.organization.name,
            title=requirement.title,
            subtitle=requirement.summary,
            status=requirement.status,
            priority=requirement.priority,
            detail_path=f"/requisitos?id={requirement.id}",
        )

    if entity_type == "project":
        project = get_project_with_memberships(db, entity_id)
        if project is None:
            return None
        if not user_can_access_project(db, current_user, project):
            return None
        return VisibleEntity(
            entity_type="project",
            entity=project,
            organization_id=project.organization_id,
            organization_name=project.organization.name,
            title=project.name,
            subtitle=project.description,
            status=project.status,
            priority=None,
            detail_path="/proyectos",
        )

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Unsupported entity_type",
    )


def require_visible_entity(
    db: Session,
    current_user: User,
    entity_type: str,
    entity_id: int,
) -> VisibleEntity:
    visible = get_visible_entity(db, current_user, entity_type, entity_id)
    if visible is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entity access denied",
        )
    return visible


def derive_municipality_id(db: Session, organization_id: int) -> int | None:
    organization = db.get(Organization, organization_id)
    if organization is None:
        return None
    return organization.municipality_id
