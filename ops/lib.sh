# Utilidades comunes de los scripts de operación. Se incluye con `source`.
# Ver ADR-033.

# El nombre del proyecto tiene que coincidir con `name:` de
# docker-compose.prod.yml: de él dependen los nombres de los volúmenes
# (anacleto_postgres_data, anacleto_document_storage, ...).
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-anacleto}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env.production}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/docker-compose.prod.yml}"

# Imagen auxiliar para tar/chown sobre volúmenes. Fijada para no depender de un
# tag que cambie bajo los pies.
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.20}"

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

log() {
	printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# El fichero de entorno se PARSEA, no se ejecuta con `source`. Contiene valores
# sin comillas y con espacios (APP_NAME=Asistente Ayuntamientos), que al
# interpretarlos como shell revientan; y ejecutar un fichero de secretos como
# código es además innecesariamente peligroso.
declare -A ENV_VARS=()

parse_env_file() {
	local line key value
	while IFS= read -r line || [ -n "$line" ]; do
		line="${line#"${line%%[![:space:]]*}"}"   # recorta espacios iniciales
		case "$line" in
			''|'#'*) continue ;;
		esac
		line="${line#export }"
		[ "${line#*=}" != "$line" ] || continue
		key="${line%%=*}"
		value="${line#*=}"
		key="${key//[[:space:]]/}"
		case "$key" in
			''|*[!A-Za-z0-9_]*) continue ;;
		esac
		# Quita comillas envolventes si las hay.
		case "$value" in
			\"*\") value="${value#\"}"; value="${value%\"}" ;;
			\'*\') value="${value#\'}"; value="${value%\'}" ;;
		esac
		ENV_VARS["$key"]="$value"
	done <"$ENV_FILE"
}

# Precedencia: lo que ya venga exportado en el entorno gana sobre el fichero, y
# el fichero sobre el valor por defecto.
resolve() {
	local key="$1" fallback="${2-}" current="${!1-}"
	if [ -n "$current" ]; then
		printf '%s' "$current"
	elif [ -n "${ENV_VARS[$key]-}" ]; then
		printf '%s' "${ENV_VARS[$key]}"
	else
		printf '%s' "$fallback"
	fi
}

load_env() {
	[ -f "$ENV_FILE" ] || die "no existe $ENV_FILE"
	parse_env_file

	POSTGRES_DB="$(resolve POSTGRES_DB app)"
	POSTGRES_USER="$(resolve POSTGRES_USER app)"
	POSTGRES_PASSWORD="$(resolve POSTGRES_PASSWORD)"
	BACKUP_ROOT="$(resolve BACKUP_ROOT /var/backups/anacleto)"
	BACKUP_KEEP_DAILY="$(resolve BACKUP_KEEP_DAILY 7)"
	BACKUP_KEEP_WEEKLY="$(resolve BACKUP_KEEP_WEEKLY 4)"
	BACKUP_KEEP_MONTHLY="$(resolve BACKUP_KEEP_MONTHLY 3)"
	SECURITY_EVENT_RETENTION_DAYS="$(resolve SECURITY_EVENT_RETENTION_DAYS 90)"

	[ -n "$POSTGRES_PASSWORD" ] \
		|| die "POSTGRES_PASSWORD no está definida en $ENV_FILE ni en el entorno"
}

compose() {
	docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

# Ejecuta psql dentro del contenedor de Postgres. La contraseña viaja por
# variable de entorno, nunca en la línea de órdenes (sería visible en `ps`).
psql_in_container() {
	compose exec -T \
		-e PGPASSWORD="$POSTGRES_PASSWORD" \
		postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$@"
}

volume_name() {
	printf '%s_%s' "$COMPOSE_PROJECT_NAME" "$1"
}

require_volume() {
	local name
	name="$(volume_name "$1")"
	docker volume inspect "$name" >/dev/null 2>&1 \
		|| die "no existe el volumen $name (¿proyecto de Compose distinto?)"
}
