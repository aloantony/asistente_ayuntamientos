from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.projects.models import Project, project_groups, project_users
from app.rbac.models import Group, user_groups
from app.users.models import User


def select_visible_projects(
    current_user: User,
    *,
    organization_ids: list[int] | None = None,
    view_all_organization_ids: list[int] | None = None,
):
    query = (
        select(Project)
        .options(
            selectinload(Project.organization),
            selectinload(Project.users),
            selectinload(Project.groups),
        )
        .order_by(Project.id)
    )

    if current_user.is_superuser:
        return query

    organization_ids = organization_ids or []
    view_all_organization_ids = view_all_organization_ids or []
    if not organization_ids:
        return query.where(False)

    direct_assignment = exists().where(
        project_users.c.project_id == Project.id,
        project_users.c.user_id == current_user.id,
    )
    group_assignment = exists().where(
        project_groups.c.project_id == Project.id,
        project_groups.c.group_id == user_groups.c.group_id,
        project_groups.c.group_id == Group.id,
        Group.organization_id == Project.organization_id,
        user_groups.c.user_id == current_user.id,
    )
    visibility_filters = [direct_assignment, group_assignment]
    if view_all_organization_ids:
        visibility_filters.append(Project.organization_id.in_(view_all_organization_ids))

    return query.where(
        Project.organization_id.in_(organization_ids),
        or_(*visibility_filters),
    )


def get_project_with_memberships(db: Session, project_id: int) -> Project | None:
    return db.scalar(
        select(Project)
        .options(
            selectinload(Project.organization),
            selectinload(Project.users),
            selectinload(Project.groups),
        )
        .where(Project.id == project_id)
    )


def user_can_access_project(
    db: Session,
    current_user: User,
    project: Project,
) -> bool:
    if current_user.is_superuser:
        return True

    from app.organizations.access import user_is_organization_member
    from app.rbac.permissions import has_permission

    if not user_is_organization_member(db, current_user, project.organization_id):
        return False

    if has_permission(
        current_user,
        "projects.view_all",
        db,
        organization_id=project.organization_id,
    ):
        return True

    direct_project_id = db.scalar(
        select(project_users.c.project_id)
        .where(
            project_users.c.project_id == project.id,
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
        .join(Group, Group.id == project_groups.c.group_id)
        .where(
            project_groups.c.project_id == project.id,
            Group.organization_id == project.organization_id,
            user_groups.c.user_id == current_user.id,
        )
        .limit(1)
    )
    return group_project_id is not None
