from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.organizations.access import get_user_organization_ids
from app.organizations.models import Organization
from app.projects.models import Project
from app.rbac.permissions import has_permission
from app.requirements.models import Requirement, RequirementMessage
from app.requirements.schemas import (
    RequirementCreate,
    RequirementMessageCreate,
    RequirementMessageRead,
    RequirementRead,
    RequirementStatus,
    RequirementUpdate,
)
from app.users.models import User

router = APIRouter(prefix="/requirements", tags=["requirements"])

CONTENT_FIELDS = {
    "project_id",
    "title",
    "summary",
    "problem",
    "current_process",
    "desired_process",
    "affected_users",
    "involved_documents",
    "data_sensitivity_notes",
    "legal_notes",
    "acceptance_criteria",
    "open_questions",
    "priority",
    "source_type",
}
EDITABLE_CONTENT_STATUSES = {"draft", "submitted", "needs_clarification"}
REVIEW_STATUSES = {
    "in_review",
    "needs_clarification",
    "accepted",
    "rejected",
    "converted",
}


@router.get("", response_model=list[RequirementRead])
def list_requirements(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: int | None = None,
    project_id: int | None = None,
    status: Annotated[RequirementStatus | None, Query()] = None,
    include_archived: bool = False,
) -> list[Requirement]:
    query = select_requirements_with_summaries().order_by(
        Requirement.created_at.desc(),
        Requirement.id.desc(),
    )

    if organization_id is not None:
        query = query.where(Requirement.organization_id == organization_id)
    if project_id is not None:
        query = query.where(Requirement.project_id == project_id)
    if status is not None:
        query = query.where(Requirement.status == status)
    elif not include_archived:
        query = query.where(Requirement.status != "archived")

    if not current_user.is_superuser:
        query = query.where(build_requirement_visibility_filter(db, current_user))

    return list(db.scalars(query))


@router.post(
    "",
    response_model=RequirementRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_requirement(
    payload: RequirementCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Requirement:
    ensure_organization_exists(db, payload.organization_id)
    require_requirement_permission(
        db,
        current_user,
        payload.organization_id,
        "requirements.create",
    )
    reviewed_by_id: int | None = None
    if payload.status == "archived":
        require_requirement_permission(
            db,
            current_user,
            payload.organization_id,
            "requirements.archive",
        )
    elif payload.status in REVIEW_STATUSES:
        require_requirement_permission(
            db,
            current_user,
            payload.organization_id,
            "requirements.review",
        )
        reviewed_by_id = current_user.id

    ensure_project_matches_organization(
        db,
        project_id=payload.project_id,
        organization_id=payload.organization_id,
    )

    requirement = Requirement(
        organization_id=payload.organization_id,
        project_id=payload.project_id,
        title=payload.title,
        summary=payload.summary,
        problem=payload.problem,
        current_process=payload.current_process,
        desired_process=payload.desired_process,
        affected_users=payload.affected_users,
        involved_documents=payload.involved_documents,
        data_sensitivity_notes=payload.data_sensitivity_notes,
        legal_notes=payload.legal_notes,
        acceptance_criteria=payload.acceptance_criteria,
        open_questions=payload.open_questions,
        priority=payload.priority,
        status=payload.status,
        source_type=payload.source_type,
        created_by_id=current_user.id,
        reviewed_by_id=reviewed_by_id,
    )
    db.add(requirement)
    db.commit()

    return get_existing_requirement(db, requirement.id)


@router.get("/{requirement_id}", response_model=RequirementRead)
def get_requirement(
    requirement_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Requirement:
    requirement = get_existing_requirement(db, requirement_id)
    require_requirement_view(db, current_user, requirement)
    return requirement


@router.patch("/{requirement_id}", response_model=RequirementRead)
def update_requirement(
    requirement_id: int,
    payload: RequirementUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Requirement:
    requirement = get_existing_requirement(db, requirement_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        require_requirement_view(db, current_user, requirement)
        return requirement

    requested_status = updates.get("status")
    content_updates = {key: value for key, value in updates.items() if key in CONTENT_FIELDS}

    if requested_status == "archived":
        require_requirement_permission(
            db,
            current_user,
            requirement.organization_id,
            "requirements.archive",
        )
    elif requested_status in REVIEW_STATUSES:
        require_requirement_permission(
            db,
            current_user,
            requirement.organization_id,
            "requirements.review",
        )
        requirement.reviewed_by_id = current_user.id
    elif requested_status is not None:
        require_requirement_content_edit(db, current_user, requirement)

    if content_updates:
        require_requirement_content_edit(db, current_user, requirement)

    requested_project_id = updates.pop("project_id", None)
    if "project_id" in content_updates:
        ensure_project_matches_organization(
            db,
            project_id=requested_project_id,
            organization_id=requirement.organization_id,
        )
        requirement.project_id = requested_project_id

    for field, value in updates.items():
        setattr(requirement, field, value)

    db.commit()
    return get_existing_requirement(db, requirement_id)


@router.get(
    "/{requirement_id}/messages",
    response_model=list[RequirementMessageRead],
)
def list_requirement_messages(
    requirement_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[RequirementMessage]:
    requirement = get_existing_requirement(db, requirement_id)
    require_requirement_view(db, current_user, requirement)

    return list(
        db.scalars(
            select(RequirementMessage)
            .options(selectinload(RequirementMessage.author))
            .where(RequirementMessage.requirement_id == requirement.id)
            .order_by(RequirementMessage.created_at, RequirementMessage.id)
        )
    )


@router.post(
    "/{requirement_id}/messages",
    response_model=RequirementMessageRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_requirement_message(
    requirement_id: int,
    payload: RequirementMessageCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> RequirementMessage:
    requirement = get_existing_requirement(db, requirement_id)
    require_requirement_view(db, current_user, requirement)

    message = RequirementMessage(
        requirement_id=requirement.id,
        author_id=current_user.id,
        body=payload.body,
        message_type=payload.message_type,
    )
    db.add(message)
    db.commit()

    return get_existing_requirement_message(db, message.id)


def select_requirements_with_summaries():
    return select(Requirement).options(
        selectinload(Requirement.organization),
        selectinload(Requirement.project),
        selectinload(Requirement.created_by),
        selectinload(Requirement.reviewed_by),
    )


def get_existing_requirement(db: Session, requirement_id: int) -> Requirement:
    requirement = db.scalar(
        select_requirements_with_summaries().where(Requirement.id == requirement_id)
    )
    if requirement is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Requirement not found",
        )

    return requirement


def get_existing_requirement_message(
    db: Session,
    message_id: int,
) -> RequirementMessage:
    message = db.scalar(
        select(RequirementMessage)
        .options(selectinload(RequirementMessage.author))
        .where(RequirementMessage.id == message_id)
    )
    if message is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Requirement message not found",
        )

    return message


def ensure_organization_exists(db: Session, organization_id: int) -> None:
    if db.get(Organization, organization_id) is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )


def ensure_project_matches_organization(
    db: Session,
    *,
    project_id: int | None,
    organization_id: int,
) -> None:
    if project_id is None:
        return

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    if project.organization_id != organization_id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Project does not belong to the requirement organization",
        )


def build_requirement_visibility_filter(db: Session, current_user: User):
    organization_ids = get_user_organization_ids(db, current_user)
    view_organization_ids = [
        organization_id
        for organization_id in organization_ids
        if any(
            has_permission(current_user, permission_code, db, organization_id=organization_id)
            for permission_code in ("requirements.view", "requirements.manage")
        )
    ]
    create_organization_ids = [
        organization_id
        for organization_id in organization_ids
        if has_permission(
            current_user,
            "requirements.create",
            db,
            organization_id=organization_id,
        )
    ]

    visibility_filters = []
    if view_organization_ids:
        visibility_filters.append(Requirement.organization_id.in_(view_organization_ids))
    if create_organization_ids:
        visibility_filters.append(
            and_(
                Requirement.organization_id.in_(create_organization_ids),
                Requirement.created_by_id == current_user.id,
            )
        )

    if not visibility_filters:
        return False

    return or_(*visibility_filters)


def can_view_requirement(
    db: Session,
    current_user: User,
    requirement: Requirement,
) -> bool:
    if current_user.is_superuser:
        return True

    if any(
        has_permission(
            current_user,
            permission_code,
            db,
            organization_id=requirement.organization_id,
        )
        for permission_code in ("requirements.view", "requirements.manage")
    ):
        return True

    return (
        requirement.created_by_id == current_user.id
        and has_permission(
            current_user,
            "requirements.create",
            db,
            organization_id=requirement.organization_id,
        )
    )


def require_requirement_view(
    db: Session,
    current_user: User,
    requirement: Requirement,
) -> None:
    if can_view_requirement(db, current_user, requirement):
        return

    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="Requirement access denied",
    )


def require_requirement_permission(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
) -> None:
    if current_user.is_superuser:
        return

    if has_permission(
        current_user,
        "requirements.manage",
        db,
        organization_id=organization_id,
    ):
        return

    if has_permission(
        current_user,
        permission_code,
        db,
        organization_id=organization_id,
    ):
        return

    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )


def require_requirement_content_edit(
    db: Session,
    current_user: User,
    requirement: Requirement,
) -> None:
    if current_user.is_superuser:
        return

    if has_permission(
        current_user,
        "requirements.manage",
        db,
        organization_id=requirement.organization_id,
    ):
        return

    if requirement.status not in EDITABLE_CONTENT_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Requirement status does not allow content edits",
        )

    if has_permission(
        current_user,
        "requirements.edit",
        db,
        organization_id=requirement.organization_id,
    ):
        return

    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="Permission required: requirements.edit",
    )
