from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.projects.models import Project, project_groups, project_users
from app.rbac.models import user_groups
from app.users.models import User


def select_visible_projects(current_user: User):
    query = (
        select(Project)
        .options(selectinload(Project.users), selectinload(Project.groups))
        .order_by(Project.id)
    )

    if current_user.is_superuser:
        return query

    direct_assignment = exists().where(
        project_users.c.project_id == Project.id,
        project_users.c.user_id == current_user.id,
    )
    group_assignment = exists().where(
        project_groups.c.project_id == Project.id,
        project_groups.c.group_id == user_groups.c.group_id,
        user_groups.c.user_id == current_user.id,
    )

    return query.where(or_(direct_assignment, group_assignment))


def get_project_with_memberships(db: Session, project_id: int) -> Project | None:
    return db.scalar(
        select(Project)
        .options(selectinload(Project.users), selectinload(Project.groups))
        .where(Project.id == project_id)
    )


def user_can_access_project(db: Session, current_user: User, project_id: int) -> bool:
    if current_user.is_superuser:
        return True

    direct_project_id = db.scalar(
        select(project_users.c.project_id)
        .where(
            project_users.c.project_id == project_id,
            project_users.c.user_id == current_user.id,
        )
        .limit(1)
    )
    if direct_project_id is not None:
        return True

    group_project_id = db.scalar(
        select(project_groups.c.project_id)
        .join(
            user_groups,
            project_groups.c.group_id == user_groups.c.group_id,
        )
        .where(
            project_groups.c.project_id == project_id,
            user_groups.c.user_id == current_user.id,
        )
        .limit(1)
    )
    return group_project_id is not None
