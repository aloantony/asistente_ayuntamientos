#!/usr/bin/env bash
#
# Copia de seguridad de la base de datos y de los documentos subidos.
# Ver ADR-037 y docs/despliegue.md.
#
# NO toca los volúmenes: la base se vuelca con pg_dump y los documentos se leen
# con el volumen montado en SOLO LECTURA. Este script nunca borra datos de
# producción, solo copias antiguas dentro de BACKUP_ROOT.
#
# Riesgo residual asumido conscientemente: las copias viven en el mismo servidor.
# Un borrado accidental o un compromiso del servidor se las lleva también. Los
# snapshots del proveedor de VPS son la única red fuera de la máquina y hay que
# tenerlos activados. Para llevarlas fuera, ver la sección "costura externa".

set -Eeuo pipefail

# shellcheck source=ops/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

load_env
require_volume document_storage

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DAY_OF_WEEK="$(date -u +%u)"   # 1 = lunes
DAY_OF_MONTH="$(date -u +%d)"

install -d -m 0700 "$BACKUP_ROOT"
for tier in daily weekly monthly; do
	install -d -m 0700 "$BACKUP_ROOT/$tier"
done

# Se construye en un directorio .partial y se renombra solo al terminar bien.
# Así una ejecución interrumpida no deja una copia a medias que la rotación
# contaría como válida, desplazando a una buena.
DAILY_DIR="$BACKUP_ROOT/daily/$STAMP"
PARTIAL_DIR="$DAILY_DIR.partial"
rm -rf "$PARTIAL_DIR"
install -d -m 0700 "$PARTIAL_DIR"
trap 'rm -rf "$PARTIAL_DIR"' EXIT

# Restos de ejecuciones anteriores que murieran a medias.
find "$BACKUP_ROOT/daily" -mindepth 1 -maxdepth 1 -type d -name '*.partial' \
	! -path "$PARTIAL_DIR" -exec rm -rf {} + 2>/dev/null || true

# --- Base de datos -----------------------------------------------------------
# Formato custom (-Fc): comprimido y restaurable de forma selectiva con
# pg_restore, a diferencia de un volcado SQL plano.
log "volcando la base de datos $POSTGRES_DB"
compose exec -T \
	-e PGPASSWORD="$POSTGRES_PASSWORD" \
	postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
	>"$PARTIAL_DIR/database.dump"

[ -s "$PARTIAL_DIR/database.dump" ] || die "el volcado salió vacío"

# --- Documentos --------------------------------------------------------------
# :ro en el montaje: el contenedor auxiliar no puede escribir en el volumen ni
# por error. Se ejecuta como root solo para poder leer todo y escribir en /out.
log "empaquetando los documentos"
# El contenedor corre como root para poder leer todo el volumen, pero devuelve la
# propiedad del paquete a quien invoca el script (root en el servidor, el usuario
# de desarrollo al probarlo). Sin ese chown el fichero queda de root y ni el
# manifiesto ni la rotación podrían tocarlo.
docker run --rm \
	--user 0 \
	-v "$(volume_name document_storage):/data:ro" \
	-v "$PARTIAL_DIR:/out" \
	-e OWNER="$(id -u):$(id -g)" \
	"$HELPER_IMAGE" \
	sh -c 'tar czf /out/documents.tar.gz -C /data . \
		&& chown "$OWNER" /out/documents.tar.gz \
		&& chmod 600 /out/documents.tar.gz'

[ -s "$PARTIAL_DIR/documents.tar.gz" ] || die "el paquete de documentos salió vacío"

# --- Manifiesto --------------------------------------------------------------
# Permite detectar una copia corrupta antes de necesitarla.
(cd "$PARTIAL_DIR" && sha256sum database.dump documents.tar.gz >SHA256SUMS)
chmod 0600 "$PARTIAL_DIR/database.dump" "$PARTIAL_DIR/SHA256SUMS"

# A partir de aquí la copia está completa: se publica con su nombre definitivo.
mv "$PARTIAL_DIR" "$DAILY_DIR"
trap - EXIT

# --- Promoción semanal y mensual ---------------------------------------------
if [ "$DAY_OF_WEEK" = "1" ]; then
	cp -a "$DAILY_DIR" "$BACKUP_ROOT/weekly/$STAMP"
	log "promovida a semanal"
fi
if [ "$DAY_OF_MONTH" = "01" ]; then
	cp -a "$DAILY_DIR" "$BACKUP_ROOT/monthly/$STAMP"
	log "promovida a mensual"
fi

# --- Rotación ----------------------------------------------------------------
# Se poda por número de copias, no por antigüedad: si el servidor estuvo
# apagado una semana, no queremos quedarnos sin ninguna.
prune_tier() {
	local tier="$1" keep="$2" dir count
	dir="$BACKUP_ROOT/$tier"
	# Orden inverso por nombre: el sello temporal es ordenable lexicográficamente.
	mapfile -t entries < <(find "$dir" -mindepth 1 -maxdepth 1 -type d | sort -r)
	count=${#entries[@]}
	if [ "$count" -le "$keep" ]; then
		return 0
	fi
	for entry in "${entries[@]:$keep}"; do
		log "rotando: elimino la copia antigua $entry"
		rm -rf -- "$entry"
	done
}

prune_tier daily "$BACKUP_KEEP_DAILY"
prune_tier weekly "$BACKUP_KEEP_WEEKLY"
prune_tier monthly "$BACKUP_KEEP_MONTHLY"

log "copia completada en $DAILY_DIR"
du -sh "$DAILY_DIR"

# --- Costura externa ---------------------------------------------------------
# Deliberadamente no implementada (el usuario eligió copias en servidor +
# snapshots del proveedor). Para activarla, define BACKUP_REMOTE y añade aquí el
# cifrado y la subida, por ejemplo:
#
#   age -r "$BACKUP_AGE_RECIPIENT" -o "$DAILY_DIR/database.dump.age" \
#       "$DAILY_DIR/database.dump"
#   rclone copy "$DAILY_DIR" "$BACKUP_REMOTE/$STAMP"
#
# Sin cifrar no debe salir del servidor: contiene datos personales.
if [ -n "${BACKUP_REMOTE:-}" ]; then
	log "AVISO: BACKUP_REMOTE está definido pero la subida externa no está implementada"
fi
