from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.municipalities.models import Municipality
from app.organizations.access import get_accessible_organizations_query
from app.organizations.models import Organization, organization_users
from app.organizations.schemas import (
    OrganizationCreate,
    OrganizationMembershipResponse,
    OrganizationRead,
    OrganizationUpdate,
)
from app.projects.models import Project, project_users
from app.rbac.models import Group, user_groups
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("", response_model=list[OrganizationRead])
def list_organizations(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[Organization]:
    query = get_accessible_organizations_query(current_user).options(
        selectinload(Organization.users),
        selectinload(Organization.municipality),
    )
    return list(db.scalars(query))


@router.post(
    "",
    response_model=OrganizationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_organization(
    payload: OrganizationCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Organization:
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superuser privileges required",
        )
    ensure_municipality_can_be_linked(db, payload.municipality_id)

    organization = Organization(
        name=payload.name,
        description=payload.description,
        municipality_id=payload.municipality_id,
        status=payload.status,
    )
    db.add(organization)
    db.flush()

    db.execute(
        insert(organization_users).values(
            organization_id=organization.id,
            user_id=current_user.id,
        )
    )
    db.commit()

    return get_existing_organization(db, organization.id)


@router.get("/{organization_id}", response_model=OrganizationRead)
def get_organization(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Organization:
    organization = get_existing_organization(db, organization_id)
    if not current_user.is_superuser and not any(
        user.id == current_user.id for user in organization.users
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization access denied",
        )

    return organization


@router.patch("/{organization_id}", response_model=OrganizationRead)
def update_organization(
    organization_id: int,
    payload: OrganizationUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Organization:
    organization = get_existing_organization(db, organization_id)
    require_organizations_manage(db, current_user, organization_id=organization.id)

    updates = payload.model_dump(exclude_unset=True)
    if (
        "municipality_id" in updates
        and updates["municipality_id"] != organization.municipality_id
    ):
        ensure_municipality_can_be_linked(db, updates["municipality_id"])
    for field, value in updates.items():
        setattr(organization, field, value)

    db.commit()
    return get_existing_organization(db, organization_id)


@router.post(
    "/{organization_id}/users/{user_id}",
    response_model=OrganizationMembershipResponse,
    status_code=status.HTTP_200_OK,
)
def add_user_to_organization(
    organization_id: int,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OrganizationMembershipResponse:
    ensure_organization_and_user_exist(
        db,
        organization_id=organization_id,
        user_id=user_id,
    )
    require_organizations_manage(db, current_user, organization_id=organization_id)

    exists = db.execute(
        select(organization_users).where(
            organization_users.c.organization_id == organization_id,
            organization_users.c.user_id == user_id,
        )
    ).first()
    if exists is None:
        db.execute(
            insert(organization_users).values(
                organization_id=organization_id,
                user_id=user_id,
            )
        )
        db.commit()

    return OrganizationMembershipResponse(
        organization_id=organization_id,
        user_id=user_id,
        detail="User is in organization",
    )


@router.delete(
    "/{organization_id}/users/{user_id}",
    response_model=OrganizationMembershipResponse,
)
def remove_user_from_organization(
    organization_id: int,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OrganizationMembershipResponse:
    organization, target_user = ensure_organization_and_user_exist(
        db,
        organization_id=organization_id,
        user_id=user_id,
    )
    require_organizations_manage(db, current_user, organization_id=organization.id)

    prevent_organization_lockout(db, current_user, target_user)

    group_ids = select(Group.id).where(Group.organization_id == organization.id)
    project_ids = select(Project.id).where(Project.organization_id == organization.id)

    db.execute(
        delete(user_groups).where(
            user_groups.c.user_id == target_user.id,
            user_groups.c.group_id.in_(group_ids),
        )
    )
    db.execute(
        delete(project_users).where(
            project_users.c.user_id == target_user.id,
            project_users.c.project_id.in_(project_ids),
        )
    )
    db.execute(
        delete(organization_users).where(
            organization_users.c.organization_id == organization.id,
            organization_users.c.user_id == target_user.id,
        )
    )
    db.commit()

    return OrganizationMembershipResponse(
        organization_id=organization.id,
        user_id=target_user.id,
        detail="User is not in organization",
    )


def require_organizations_manage(
    db: Session,
    current_user: User,
    *,
    organization_id: int | None = None,
) -> None:
    if has_permission(
        current_user,
        "organizations.manage",
        db,
        organization_id=organization_id,
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Permission required: organizations.manage",
    )


def ensure_organization_and_user_exist(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
) -> tuple[Organization, User]:
    organization = db.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return organization, user


def prevent_organization_lockout(
    db: Session,
    current_user: User,
    target_user: User,
) -> None:
    if target_user.id == current_user.id and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove yourself from an organization",
        )

    target_organization_count = db.scalar(
        select(func.count())
        .select_from(organization_users)
        .where(organization_users.c.user_id == target_user.id)
    )
    active_superuser_count = db.scalar(
        select(func.count(User.id)).where(
            User.is_active.is_(True),
            User.is_superuser.is_(True),
        )
    )
    if (
        target_user.is_active
        and target_user.is_superuser
        and target_organization_count is not None
        and target_organization_count <= 1
        and active_superuser_count is not None
        and active_superuser_count <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the last active superuser from all organizations",
        )


def get_existing_organization(db: Session, organization_id: int) -> Organization:
    organization = db.scalar(
        select(Organization)
        .options(
            selectinload(Organization.users),
            selectinload(Organization.municipality),
        )
        .where(Organization.id == organization_id)
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return organization


def ensure_municipality_can_be_linked(
    db: Session,
    municipality_id: int | None,
) -> None:
    if municipality_id is None:
        return

    municipality = db.get(Municipality, municipality_id)
    if municipality is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Municipality not found",
        )
    if municipality.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Municipality is archived",
        )
