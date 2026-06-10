from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.organizations.models import Organization, organization_users
from app.users.models import User


def get_user_organization_ids(db: Session, user: User) -> list[int]:
    return list(
        db.scalars(
            select(organization_users.c.organization_id)
            .where(organization_users.c.user_id == user.id)
            .order_by(organization_users.c.organization_id)
        )
    )


def user_is_organization_member(
    db: Session,
    user: User,
    organization_id: int,
) -> bool:
    return db.scalar(
        select(
            exists().where(
                organization_users.c.organization_id == organization_id,
                organization_users.c.user_id == user.id,
            )
        )
    )


def get_accessible_organizations_query(user: User):
    query = select(Organization).order_by(Organization.id)
    if user.is_superuser:
        return query

    return query.join(
        organization_users,
        organization_users.c.organization_id == Organization.id,
    ).where(organization_users.c.user_id == user.id)
