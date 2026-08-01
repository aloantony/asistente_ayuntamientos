from datetime import datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SECURITY_EVENT_OUTCOMES = ("success", "failure", "blocked")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class SecurityEvent(Base):
    """Traza de auditoría de seguridad, sólo de anexado.

    El proyecto no registraba ni un login correcto ni uno fallido, ni un 429, ni
    una concesión de superusuario: sin esto no hay forma de detectar una campaña
    de fuerza bruta ni de reconstruir un incidente, y para el RGPD el registro de
    accesos a documentos es lo que hace defendible el piloto con datos reales.

    Es inmutable a nivel de base de datos con el mismo patrón que
    `maintenance_order_events`: una traza que la propia aplicación pudiera
    reescribir no sirve como prueba.

    `detail` guarda solo metadatos. Nunca contenido de conversaciones, consultas
    ni ficheros: eso lo prohíbe la regla dura del proyecto. Ver ADR-036.
    """

    __tablename__ = "security_events"
    __table_args__ = (
        CheckConstraint(
            f"outcome in ({_sql_in(SECURITY_EVENT_OUTCOMES)})",
            name="ck_security_events_outcome",
        ),
        Index("ix_security_events_created_at", "created_at", "id"),
        Index("ix_security_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    # SET NULL: borrar un usuario anonimiza su traza en lugar de bloquear el
    # borrado, coherente con el resto del esquema.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # Identificador intentado. Imprescindible para investigar un login fallido,
    # donde no existe fila de usuario. Es dato personal: retención limitada y
    # purga periódica documentadas en docs/proteccion-datos.md.
    actor_label: Mapped[str | None] = mapped_column(String(320), nullable=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


_IMMUTABLE_FUNCTION = "prevent_security_event_mutation"
_IMMUTABLE_TRIGGER = "trg_security_events_immutable"

# Inmutabilidad completa: UPDATE y DELETE. Que la aplicación no pueda borrar es
# justo el objetivo — un backend comprometido no debe poder borrar su rastro. La
# purga por retención es una tarea de mantenimiento que desactiva el trigger de
# forma explícita como dueño de la base (ops/purge_security_events.sh).
event.listen(
    SecurityEvent.__table__,
    "after_create",
    DDL(
        f"""
        CREATE OR REPLACE FUNCTION {_IMMUTABLE_FUNCTION}()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'security events are immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    SecurityEvent.__table__,
    "after_create",
    DDL(
        f"""
        CREATE TRIGGER {_IMMUTABLE_TRIGGER}
        BEFORE UPDATE OR DELETE ON security_events
        FOR EACH ROW EXECUTE FUNCTION {_IMMUTABLE_FUNCTION}();
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    SecurityEvent.__table__,
    "before_drop",
    DDL(
        f"DROP TRIGGER IF EXISTS {_IMMUTABLE_TRIGGER} ON security_events"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    SecurityEvent.__table__,
    "after_drop",
    DDL(f"DROP FUNCTION IF EXISTS {_IMMUTABLE_FUNCTION}()").execute_if(
        dialect="postgresql"
    ),
)
