#!/usr/bin/env bash
#
# Purga de la traza de seguridad por retención. Ver ADR-036 y
# docs/proteccion-datos.md.
#
# La tabla security_events es inmutable por trigger: la aplicación no puede
# modificarla ni borrarla, y eso es justo lo que se quiere (un backend
# comprometido no debe poder borrar su rastro). La purga es una tarea de
# mantenimiento que desactiva el trigger de forma explícita, borra por
# antigüedad y lo vuelve a activar en la misma transacción.
#
# Se purga porque la tabla guarda IP y correo intentado, que son datos
# personales: conservarlos indefinidamente no sería proporcionado.

set -Eeuo pipefail

# shellcheck source=ops/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

load_env

DAYS="${1:-$SECURITY_EVENT_RETENTION_DAYS}"
case "$DAYS" in
	''|*[!0-9]*) die "los días de retención deben ser un entero: $DAYS" ;;
esac

log "purgando eventos de seguridad de más de $DAYS días"

# Todo en una transacción: si el DELETE falla, el trigger se restaura solo.
psql_in_container -d "$POSTGRES_DB" <<SQL
BEGIN;
ALTER TABLE security_events DISABLE TRIGGER trg_security_events_immutable;
WITH deleted AS (
    DELETE FROM security_events
    WHERE created_at < now() - interval '$DAYS days'
    RETURNING 1
)
SELECT count(*) AS eventos_purgados FROM deleted;
ALTER TABLE security_events ENABLE TRIGGER trg_security_events_immutable;
COMMIT;
SQL

# Comprobación de que el trigger quedó activo: sin él la traza deja de ser prueba.
psql_in_container -d "$POSTGRES_DB" -t -c "
	SELECT CASE WHEN tgenabled = 'O' THEN 'trigger activo' ELSE 'TRIGGER DESACTIVADO' END
	FROM pg_trigger
	WHERE tgname = 'trg_security_events_immutable';
"

log "purga completada"
