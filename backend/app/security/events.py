"""Registro de eventos de seguridad.

El registro nunca debe tumbar la petición que audita: si la traza falla, se anota
en el log y la operación sigue. Al revés (perder la operación por no poder
auditarla) sería peor para un piloto de dos usuarios.
"""

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.security.models import SecurityEvent

logger = logging.getLogger(__name__)

# Tipos de evento registrados. Como constantes para que un error tipográfico en
# una llamada se vea en la revisión y no cree un tipo nuevo en silencio.
LOGIN_SUCCEEDED = "auth.login.succeeded"
LOGIN_FAILED = "auth.login.failed"
LOGIN_BLOCKED = "auth.login.blocked"
LOGIN_INACTIVE = "auth.login.inactive"
LOGOUT = "auth.logout"
PASSWORD_CHANGED = "auth.password.changed"
PASSWORD_CHANGE_FAILED = "auth.password.change_failed"
BOOTSTRAP_ADMIN_CREATED = "auth.bootstrap_admin.created"
BOOTSTRAP_ADMIN_REJECTED = "auth.bootstrap_admin.rejected"
ADMIN_PASSWORD_RESET = "admin.user.password_reset"
SUPERUSER_GRANTED = "admin.user.superuser_granted"
SUPERUSER_REVOKED = "admin.user.superuser_revoked"
USER_DELETED = "admin.user.deleted"
DOCUMENT_UPLOADED = "documents.uploaded"
DOCUMENT_DOWNLOADED = "documents.downloaded"
DOCUMENT_ARCHIVED = "documents.archived"
ORGANIZATION_CREATED = "organizations.created"


def client_ip(request: Request | None) -> str | None:
    """IP del cliente tal y como la ve la aplicación.

    Detrás del proxy inverso esto solo es la IP real si uvicorn corre con
    `--proxy-headers` y `--forwarded-allow-ips` apuntando al proxy. Ver ADR-035.
    """
    if request is None or request.client is None:
        return None
    return request.client.host


def record_security_event(
    db: Session,
    *,
    event_type: str,
    outcome: str = "success",
    request: Request | None = None,
    user_id: int | None = None,
    actor_label: str | None = None,
    organization_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: str | None = None,
    commit: bool = False,
) -> None:
    """Anexa un evento dentro de un savepoint.

    Se usa `begin_nested` para que un fallo al auditar no deje la sesión rota ni
    arrastre los cambios de negocio, y para que el harness de tests (que envuelve
    cada test en un savepoint) siga funcionando.

    `commit=True` es para las rutas que no escriben nada más —el login, por
    ejemplo, no toca la base—: sin él el evento se perdería al cerrar la sesión.
    Las rutas que ya hacen `commit` no deben pasarlo, para no consolidar antes de
    tiempo cambios de negocio a medias.
    """
    user_agent = None
    if request is not None:
        # Truncado al ancho de la columna: es una cabecera que controla el
        # cliente y puede llegar arbitrariamente larga.
        user_agent = (request.headers.get("user-agent") or None)
        if user_agent is not None:
            user_agent = user_agent[:255]

    entry = SecurityEvent(
        event_type=event_type,
        outcome=outcome,
        user_id=user_id,
        actor_label=actor_label[:320] if actor_label else None,
        organization_id=organization_id,
        client_ip=client_ip(request),
        user_agent=user_agent,
        target_type=target_type,
        target_id=target_id,
        detail=detail[:500] if detail else None,
    )

    try:
        with db.begin_nested():
            db.add(entry)
        if commit:
            db.commit()
    except SQLAlchemyError:
        logger.warning(
            "Could not record security event: type=%s outcome=%s",
            event_type,
            outcome,
            exc_info=True,
        )
