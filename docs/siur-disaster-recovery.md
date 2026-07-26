# Recuperación ante desastres del espejo SIUR

Este runbook cubre el estado no reconstruible del espejo local:

- el dump lógico de PostgreSQL;
- `reference_artifacts`, salvo parciales de `staging` y su lock;
- el directorio de datos de GeoServer completo, salvo el mountpoint derivado
  `gwc-cache`.

Redis y las teselas de GeoWebCache **no** son fuentes de verdad. La cola se
reconstruye desde el estado durable de PostgreSQL al reiniciar scheduler y
workers. Las teselas se regeneran bajo demanda a partir de los artefactos
locales restaurados. Compose fija `GEOWEBCACHE_CACHE_DIR` al volumen separado
`/opt/geoserver_data/gwc-cache`: quedan dentro del backup `gwc-gs.xml`,
`gwc-layers/`, `gwc/geowebcache.xml` y cualquier otra configuración persistente
del data dir. La cuota deseada está versionada en `.env.example` y se vuelve a
aplicar y releer con el comando de este documento. Esta separación sigue la
[configuración oficial de GeoWebCache integrado](https://docs.geoserver.org/3.0.x/en/user/geowebcache/config/),
que distingue `GEOWEBCACHE_CACHE_DIR` de `gwc-layers/` y
`gwc/geowebcache.xml`.

La herramienta no ofrece ninguna operación de borrado al operador. `create` y
`restore` son dry-run por defecto, nunca admiten un destino existente y dejan
cualquier directorio `.partial-*` fallido para revisión forense. `restore` solo
acepta:

- un directorio nuevo cuyo nombre empiece por `siur-drill-restore-`;
- una base realmente vacía llamada `app_drill_*`: comprueba propietario,
  sesiones, schemas, relaciones, funciones, tipos, extensiones y otros objetos;
- PostgreSQL en `127.0.0.1`, en un puerto distinto del `5432` del runtime de
  desarrollo;
- `pg_restore --single-transaction --exit-on-error`, sin `--clean`, `--create`,
  `DROP DATABASE` ni sobrescritura.

Todos los pasos de filesystem y el `restore-report.json` se preparan antes de
`pg_restore`. Si después del commit falla la publicación atómica, la herramienta
ejecuta como compensación `DROP OWNED BY CURRENT_USER CASCADE` conectada
únicamente a ese target `app_drill_*` aislado, repone el baseline estándar
`public`/`plpgsql` y vuelve a demostrar que está vacío. Nunca elimina ni recrea
la base.

La publicación final usa `renameat2(RENAME_NOREPLACE)`. Si el kernel o el
filesystem del destino no ofrece esa garantía, la herramienta falla y conserva
el parcial; nunca degrada silenciosamente a un `rename` que pueda reemplazar
otro directorio.

## Preparación única

Construir las dos imágenes operativas no inicia servicios:

```bash
docker compose --profile operations build siur-recovery gwc-ops
```

Preparar fuera del repositorio:

1. un directorio privado y existente para backups, por ejemplo
   `/srv/siur-backups`;
2. un directorio privado para secretos;
3. un fichero de modo `0600`, sin espacios, con una URL nativa de libpq:

   ```text
   postgresql://app:CONTRASEÑA@127.0.0.1:5432/app
   ```

No usar `postgresql+psycopg://` y no pasar la URL en la línea de comandos. El
programa la analiza y entrega host, puerto, usuario, base y contraseña
únicamente mediante variables privadas de libpq; ni argv, ni el manifest, ni
los errores contienen la contraseña. `pg_restore` solo expone en argv el
nombre no secreto `app_drill_*` necesario para activar la restauración directa.

## Comprobar y fijar la cuota GeoWebCache

GeoWebCache tiene la cuota deshabilitada por defecto. La configuración local
predeterminada exige un techo de 20 GiB, una reserva de 5 GiB, limpieza cada
60 segundos y política LRU. Son configurables mediante:

```text
GEOWEBCACHE_DISK_QUOTA_GIB
GEOWEBCACHE_DISK_QUOTA_MIN_FREE_GIB
GEOWEBCACHE_DISK_QUOTA_CLEANUP_SECONDS
GEOWEBCACHE_DISK_QUOTA_POLICY
```

Con GeoServer arrancado, el dry-run lee la configuración REST actual y mide el
filesystem real del volumen montado:

```bash
docker compose --profile operations run --rm -T --no-deps gwc-ops
```

El informe distingue:

- capacidad, uso y espacio libre del filesystem;
- bytes actuales de caché medidos dos veces, rechazando cambios, symlinks,
  hardlinks, ficheros sparse y tipos especiales;
- techo configurado y reserva mínima;
- margen estático `capacidad - cuota - reserva`;
- margen libre actual `libre - reserva`;
- crecimiento restante `max(cuota - caché actual, 0)` y el margen decisivo
  `libre - reserva - crecimiento restante`;
- coincidencia exacta entre estado actual y deseado.

Solo si todos los márgenes son no negativos se permite aplicar. Por ejemplo,
con caché vacía, 6 GiB libres, cuota de 20 GiB y reserva de 5 GiB se rechaza:
faltan 19 GiB para poder garantizar a la vez el crecimiento y la reserva.

```bash
docker compose --profile operations run --rm -T --no-deps gwc-ops \
  python -m app.reference_layers.gwc_quota \
  --cache-path /var/lib/geowebcache \
  --apply
```

El cliente hace `PUT /geoserver/gwc/rest/diskquota.json` y a continuación
`GET` del mismo recurso. Un estado HTTP inesperado, JSON incompleto o cualquier
diferencia de cuota, intervalo o política hace fallar la operación; no se
declara éxito por haber recibido solo el `PUT`. El contrato corresponde a la
[API oficial de cuota de GeoWebCache en GeoServer
3.0](https://docs.geoserver.org/3.0.x/en/user/geowebcache/rest/diskquota/).

Repetir el dry-run y archivar su JSON como evidencia. Después de una
restauración, ejecutar otra vez este bloque. Solo el volumen `gwc-cache` de
teselas se reconstruye; toda la configuración GWC se restaura con el data dir.

## Crear un backup consistente

### 1. Dry-run con los servicios activos

Elegir un basename nuevo `siur-backup-*`. El directorio final no debe existir:

```bash
docker compose --profile operations run --rm -T --no-deps \
  -v /srv/siur-backups:/backups \
  -v /srv/siur-secrets:/run/siur-secrets:ro \
  siur-recovery create \
  --destination /backups/siur-backup-20260726T120000Z \
  --reference-artifacts-source /sources/reference_artifacts \
  --geoserver-data-source /sources/geoserver_data \
  --database-url-file /run/siur-secrets/source-db.url
```

El dry-run no ejecuta `pg_dump` ni crea directorios. Recorre ambos árboles,
rechaza symlinks/ficheros especiales y muestra conteos, bytes, exclusiones y
hash del inventario.

### 2. Abrir una ventana de quiescencia

Adquirir primero el lease del runtime conforme a `AGENTS.md`. Detener todos los
posibles escritores y GeoServer, dejando PostgreSQL operativo:

```bash
docker compose stop \
  backend worker reference-worker reference-scheduler geoserver
docker compose ps
docker compose exec -T postgres pg_isready -U app -d app
```

No continuar si alguno de los cinco servicios sigue ejecutándose o PostgreSQL
no está sano. Registrar entonces un JSON reciente, por ejemplo
`/srv/siur-secrets/quiescence.json`, que dentro del contenedor se verá así:

```json
{
  "schema_version": 1,
  "captured_at": "2026-07-26T12:00:00Z",
  "operator": "operador-identificado",
  "reference_artifacts_source": "/sources/reference_artifacts",
  "geoserver_data_source": "/sources/geoserver_data",
  "stopped_services": [
    "backend",
    "geoserver",
    "reference-scheduler",
    "reference-worker",
    "worker"
  ],
  "postgres_running": true
}
```

La evidencia es una afirmación del operador después de comprobar `ps`; la
herramienta verifica esquema, paths exactos, lista completa, zona horaria y
antigüedad máxima de una hora. No intenta controlar Docker desde el contenedor
de backup.

### 3. Aplicar y verificar antes de reabrir

Repetir exactamente el plan, añadiendo evidencia y `--apply`:

```bash
docker compose --profile operations run --rm -T --no-deps \
  -v /srv/siur-backups:/backups \
  -v /srv/siur-secrets:/run/siur-secrets:ro \
  siur-recovery create \
  --destination /backups/siur-backup-20260726T120000Z \
  --reference-artifacts-source /sources/reference_artifacts \
  --geoserver-data-source /sources/geoserver_data \
  --database-url-file /run/siur-secrets/source-db.url \
  --quiescence-evidence /run/siur-secrets/quiescence.json \
  --apply
```

El proceso:

1. inventaría y hashea todos los ficheros antes del dump;
2. ejecuta `pg_dump` custom, serializable, sin owner ni privilegios;
3. crea dos tar sin symlinks, devices ni rutas absolutas; UID/GID, permisos y
   tiempos se guardan en el manifest y no se confían a los campos del tar;
4. vuelve a inventariar y aborta si cambió un byte o metadata;
5. conserva los bytes exactos de `quiescence-evidence.json` dentro del backup
   y enlaza su tamaño/hash desde `manifest.json`, junto con hashes del dump,
   archives, inventario por fichero, ownership y exclusiones;
6. hashea el manifest y renombra atómicamente el directorio parcial.

Verificar bytes, miembros de tar y legibilidad del dump:

```bash
docker compose --profile operations run --rm -T --no-deps \
  -v /srv/siur-backups:/backups:ro \
  siur-recovery verify \
  --backup /backups/siur-backup-20260726T120000Z
```

Solo tras obtener `"verified": true`:

```bash
docker compose start \
  geoserver reference-scheduler reference-worker worker backend
```

Liberar el lease del runtime. Una ejecución fallida no publica el nombre final:
registrar y revisar el `.partial-*`; su eventual retirada es una operación
separada con aprobación explícita, nunca una acción automática de esta
herramienta.

## Simulacro real, siempre aislado

La verificación del backup no sustituye un restore. Ejecutar periódicamente
este simulacro en un directorio temporal dedicado y un PostgreSQL separado.
Nunca usar el puerto 5432 ni los volúmenes Compose del desarrollo.

1. Construir la imagen PostgreSQL del proyecto, que contiene PostGIS y
   pgvector:

   ```bash
   docker compose build postgres
   ```

2. Arrancar un contenedor temporal en otro puerto y almacenamiento nuevo. Usar
   `POSTGRES_PASSWORD_FILE` con un secreto montado, no una contraseña en argv:

   ```bash
   docker run --rm -d \
     --name siur-drill-postgres-20260726 \
     -p 127.0.0.1:55432:5432 \
     -e POSTGRES_DB=app_drill_20260726 \
     -e POSTGRES_USER=app \
     -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password \
     -v /srv/siur-drill-db-20260726:/var/lib/postgresql/data \
     -v /srv/siur-secrets/drill-password:/run/secrets/postgres-password:ro \
     asistente-ayuntamientos-postgres:17-postgis-pgvector
   ```

3. Crear con modo `0600` `/srv/siur-secrets/drill-db.url`, apuntando únicamente
   a:

   ```text
   postgresql://app:CONTRASEÑA@127.0.0.1:55432/app_drill_20260726
   ```

4. Ejecutar primero el dry-run. No conecta a la base target ni escribe:

   ```bash
   docker compose --profile operations run --rm -T --no-deps \
     -v /srv/siur-backups:/backups:ro \
     -v /srv/siur-drills:/drills \
     -v /srv/siur-secrets:/run/siur-secrets:ro \
     siur-recovery restore \
     --backup /backups/siur-backup-20260726T120000Z \
     --destination /drills/siur-drill-restore-20260726 \
     --target-database-url-file /run/siur-secrets/drill-db.url
   ```

5. Repetir con `--apply`. El target debe seguir vacío:

   ```bash
   docker compose --profile operations run --rm -T --no-deps \
     -v /srv/siur-backups:/backups:ro \
     -v /srv/siur-drills:/drills \
     -v /srv/siur-secrets:/run/siur-secrets:ro \
     siur-recovery restore \
     --backup /backups/siur-backup-20260726T120000Z \
     --destination /drills/siur-drill-restore-20260726 \
     --target-database-url-file /run/siur-secrets/drill-db.url \
     --apply
   ```

6. Conservar `restore-report.json`, comprobar consultas de solo lectura en la
   base temporal y levantar, si se desea, otro GeoServer apuntando a
   `geoserver_data` y `reference_artifacts` restaurados. No montar esos paths
   sobre el runtime actual. El servicio operativo se ejecuta como
   `root:${REFERENCE_STORAGE_GID}` para poder restaurar los UID/GID validados
   del manifest (rango `0..2147483647`), además de permisos y tiempos, sin
   confiar en los campos de ownership del tar.

Si el informe devuelto indica `"durability_verified": false`, el directorio
final y la base ya forman una pareja completa y `restore-report.json` es
visible, pero fallaron los tres intentos de `fsync` del directorio padre. No se
debe repetir sobre la base poblada: conservar la pareja y revisar la salud del
filesystem del simulacro.

La herramienta no detiene ni elimina el contenedor/base/directorio del
simulacro. Cerrar y retirar esos recursos requiere una decisión posterior y
explícita, después de conservar la evidencia necesaria.

## Frecuencia y criterio de éxito

- Backup: después de cada promoción material y, como mínimo, una vez al día si
  hubo cambios.
- `verify`: en cada backup, antes de reabrir la ventana.
- Restore aislado: mensual y después de modificar esquema, imagen PostgreSQL,
  GeoServer o este runbook.
- Cuota GWC: en el alta, después de restaurar/actualizar GeoServer y en la
  revisión mensual de capacidad.

Un backup no es válido hasta que `verify` devuelve éxito. La recuperación no
está demostrada hasta completar un restore aislado, consultar PostgreSQL y
servir una entrega local desde las dos copias restauradas.
