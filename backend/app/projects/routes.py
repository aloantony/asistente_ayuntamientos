from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user, require_superuser
from app.db.session import get_db
from app.projects.access import (
    get_project_with_memberships,
    select_visible_projects,
    user_can_access_project,
)
from app.projects.models import Project, project_groups, project_users
from app.projects.schemas import ProjectCreate, ProjectRead, ProjectUpdate
from app.rbac.models import Group
from app.users.models import User

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectRead])
def list_projects(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[Project]:
    return list(db.scalars(select_visible_projects(current_user)))


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    payload: ProjectCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> Project:
    project = Project(
        name=payload.name,
        description=payload.description,
        status=payload.status,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    created_project = get_project_with_memberships(db, project.id)
    if created_project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return created_project


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Project:
    project = get_project_with_memberships(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    if not user_can_access_project(db, current_user, project_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project access denied",
        )
    return project


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> Project:
    project = get_project_with_memberships(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(project, field, value)

    db.commit()
    db.refresh(project)

    updated_project = get_project_with_memberships(db, project_id)
    if updated_project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return updated_project


@router.post(
    "/{project_id}/users/{user_id}",
    response_model=ProjectRead,
    status_code=status.HTTP_200_OK,
)
def assign_user_to_project(
    project_id: int,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> Project:
    ensure_project_and_user_exist(db, project_id=project_id, user_id=user_id)

    exists = db.execute(
        select(project_users).where(
            project_users.c.project_id == project_id,
            project_users.c.user_id == user_id,
        )
    ).first()
    if exists is None:
        db.execute(insert(project_users).values(project_id=project_id, user_id=user_id))
        touch_project(db, project_id)
        db.commit()

    return get_existing_project_with_memberships(db, project_id)


@router.delete(
    "/{project_id}/users/{user_id}",
    response_model=ProjectRead,
)
def remove_user_from_project(
    project_id: int,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> Project:
    ensure_project_and_user_exist(db, project_id=project_id, user_id=user_id)

    result = db.execute(
        delete(project_users).where(
            project_users.c.project_id == project_id,
            project_users.c.user_id == user_id,
        )
    )
    if result.rowcount:
        touch_project(db, project_id)
    db.commit()

    return get_existing_project_with_memberships(db, project_id)


@router.post(
    "/{project_id}/groups/{group_id}",
    response_model=ProjectRead,
    status_code=status.HTTP_200_OK,
)
def assign_group_to_project(
    project_id: int,
    group_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> Project:
    ensure_project_and_group_exist(db, project_id=project_id, group_id=group_id)

    exists = db.execute(
        select(project_groups).where(
            project_groups.c.project_id == project_id,
            project_groups.c.group_id == group_id,
        )
    ).first()
    if exists is None:
        db.execute(insert(project_groups).values(project_id=project_id, group_id=group_id))
        touch_project(db, project_id)
        db.commit()

    return get_existing_project_with_memberships(db, project_id)


@router.delete(
    "/{project_id}/groups/{group_id}",
    response_model=ProjectRead,
)
def remove_group_from_project(
    project_id: int,
    group_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> Project:
    ensure_project_and_group_exist(db, project_id=project_id, group_id=group_id)

    result = db.execute(
        delete(project_groups).where(
            project_groups.c.project_id == project_id,
            project_groups.c.group_id == group_id,
        )
    )
    if result.rowcount:
        touch_project(db, project_id)
    db.commit()

    return get_existing_project_with_memberships(db, project_id)


def ensure_project_and_user_exist(
    db: Session,
    *,
    project_id: int,
    user_id: int,
) -> None:
    ensure_project_exists(db, project_id)
    if db.get(User, user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )


def ensure_project_and_group_exist(
    db: Session,
    *,
    project_id: int,
    group_id: int,
) -> None:
    ensure_project_exists(db, project_id)
    if db.get(Group, group_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )


def ensure_project_exists(db: Session, project_id: int) -> None:
    if db.get(Project, project_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )


def touch_project(db: Session, project_id: int) -> None:
    db.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(updated_at=func.now())
    )


def get_existing_project_with_memberships(db: Session, project_id: int) -> Project:
    project = get_project_with_memberships(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return project
