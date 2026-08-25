#!/usr/bin/env bash
#
# Restauración de una copia. Ver ADR-037 y docs/despliegue.md.
#
# Por defecto restaura a una base de PRUEBAS (app_restore_check) y NO toca
# producción: así el ensayo de restauración puede hacerse cuando se quiera. Una
# copia que nunca se ha restaurado no es una copia, es una suposición.
#
#   ops/restore.sh --list
#   ops/restore.sh --from daily/20260730T031500Z              # ensayo
#   ops/restore.sh --from daily/20260730T031500Z --production # destructivo
#
# El modo producción exige --production y una confirmación escrita, y avisa de
# que hay que parar backend y worker antes.

set -Eeuo pipefail

# shellcheck source=ops/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

load_env

SOURCE=""
TARGET_MODE="drill"
SCRATCH_DB="${SCRATCH_DB:-app_restore_check}"
RESTORE_DOCUMENTS="no"

usage() {
	sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'
	exit "${1:-0}"
}

while [ $# -gt 0 ]; do
	case "$1" in
		--list)
			find "$BACKUP_ROOT" -mindepth 2 -maxdepth 2 -type d \
				| sed "s|^$BACKUP_ROOT/||" | sort
			exit 0
			;;
		--from)
			SOURCE="${2:?--from necesita una ruta relativa a $BACKUP_ROOT}"
			shift 2
			;;
		--production)
			TARGET_MODE="production"
			shift
			;;
		--with-documents)
			RESTORE_DOCUMENTS="yes"
			shift
			;;
		-h|--help)
			usage 0
			;;
		*)
			die "argumento no reconocido: $1"
			;;
	esac
done

[ -n "$SOURCE" ] || usage 1
BACKUP_DIR="$BACKUP_ROOT/$SOURCE"
[ -d "$BACKUP_DIR" ] || die "no existe la copia $BACKUP_DIR"

# --- Integridad --------------------------------------------------------------
log "verificando el manifiesto"
(cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS) \
	|| die "la copia no cuadra con su manifiesto: NO se restaura"

if [ "$TARGET_MODE" = "drill" ]; then
	# --- Ensayo ---------------------------------------------------------------
	log "ensayo: restauro en la base de pruebas $SCRATCH_DB"
	psql_in_container -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH_DB" >/dev/null
	psql_in_container -d postgres -c "CREATE DATABASE $SCRATCH_DB" >/dev/null

	compose exec -T \
		-e PGPASSWORD="$POSTGRES_PASSWORD" \
		postgres pg_restore -U "$POSTGRES_USER" -d "$SCRATCH_DB" --no-owner \
		<"$BACKUP_DIR/database.dump" \
		|| log "AVISO: pg_restore terminó con avisos (revisa la salida)"

	log "recuento de filas en la base restaurada:"
	psql_in_container -d "$SCRATCH_DB" -c "
		SELECT relname AS tabla, n_live_tup AS filas
		FROM pg_stat_user_tables
		WHERE n_live_tup > 0
		ORDER BY n_live_tup DESC
		LIMIT 15;
	"
	log "compáralo con producción; después puedes borrar $SCRATCH_DB"
	log "ensayo completado sin tocar producción"
	exit 0
fi

# --- Restauración real -------------------------------------------------------
cat >&2 <<EOF

*** RESTAURACIÓN DESTRUCTIVA SOBRE PRODUCCIÓN ***

Base de datos : $POSTGRES_DB
Copia         : $BACKUP_DIR
Documentos    : $([ "$RESTORE_DOCUMENTS" = yes ] && echo "SE SOBRESCRIBEN" || echo "no se tocan")

Esto sustituye el contenido actual por el de la copia. Antes hay que parar los
servicios que escriben:

  docker compose --env-file $ENV_FILE -f $COMPOSE_FILE stop backend worker

EOF
printf 'Escribe exactamente RESTAURAR para continuar: ' >&2
read -r answer
[ "$answer" = "RESTAURAR" ] || die "cancelado"

running="$(compose ps --status running --services | grep -E '^(backend|worker)$' || true)"
[ -z "$running" ] || die "backend/worker siguen en marcha: párralos antes"

log "restaurando la base de datos (limpiando objetos existentes)"
compose exec -T \
	-e PGPASSWORD="$POSTGRES_PASSWORD" \
	postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
	--clean --if-exists --no-owner \
	<"$BACKUP_DIR/database.dump"

if [ "$RESTORE_DOCUMENTS" = "yes" ]; then
	require_volume document_storage
	log "restaurando los documentos"
	# --keep-newer-files no borra lo que ya haya: se añade sobre lo existente.
	# Un borrado del volumen sería irreversible y está prohibido por el proyecto.
	docker run --rm \
		--user 0 \
		-v "$(volume_name document_storage):/data" \
		-v "$BACKUP_DIR:/in:ro" \
		"$HELPER_IMAGE" \
		tar xzf /in/documents.tar.gz -C /data --keep-newer-files
	docker run --rm --user 0 \
		-v "$(volume_name document_storage):/data" \
		"$HELPER_IMAGE" chown -R 10001:10001 /data
fi

log "restauración terminada. Arranca los servicios y comprueba /api/ready:"
log "  docker compose --env-file $ENV_FILE -f $COMPOSE_FILE start backend worker"
