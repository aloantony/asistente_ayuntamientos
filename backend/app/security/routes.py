from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_superuser
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.security.models import SecurityEvent
from app.security.schemas import SecurityEventRead
from app.users.models import User

# Solo superusuario: la traza cruza organizaciones (un login fallido no tiene
# organización todavía) y contiene IP y correo intentado, así que no encaja en el
# modelo de permisos por organización. Mantenerla fuera del catálogo RBAC evita
# además que un rol municipal pueda concederse acceso a sí mismo.
router = APIRouter(
    prefix="/admin/security-events",
    tags=["security"],
    dependencies=[Depends(require_superuser)],
)


@router.get("", response_model=list[SecurityEventRead])
def list_security_events(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_superuser)],
    page: Annotated[PageParams, Depends(page_params)],
    event_type: Annotated[str | None, Query(max_length=100)] = None,
    outcome: Annotated[str | None, Query(max_length=20)] = None,
) -> list[SecurityEvent]:
    query = select(SecurityEvent).order_by(
        SecurityEvent.created_at.desc(),
        SecurityEvent.id.desc(),
    )
    if event_type:
        query = query.where(SecurityEvent.event_type == event_type)
    if outcome:
        query = query.where(SecurityEvent.outcome == outcome)

    return list(db.scalars(paginate(db, query, page, response)))
