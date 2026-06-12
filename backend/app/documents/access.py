from sqlalchemy.orm import Session

from app.documents.models import Document
from app.projects.access import user_can_access_project
from app.rbac.permissions import has_permission
from app.users.models import User


def user_can_access_document(
    db: Session,
    user: User,
    document: Document,
) -> bool:
    if user.is_superuser:
        return True

    if has_permission(
        user,
        "documents.manage",
        db,
        organization_id=document.organization_id,
    ):
        return True

    if not has_permission(
        user,
        "documents.view",
        db,
        organization_id=document.organization_id,
    ):
        return False

    return user_can_access_project(db, user, document.project)
