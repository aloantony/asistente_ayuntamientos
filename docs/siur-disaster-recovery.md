# Recuperación ante desastres del espejo SIUR

Este runbook cubre el estado no reconstruible del espejo local:

- el dump lógico de PostgreSQL;
- `reference_artifacts`, salvo parciales de `staging` y su lock;
- el directorio de datos de GeoServer completo; el placeholder histórico
  `gwc-cache` se exige vacío y se excluye por compatibilidad con backups
  anteriores.

El volumen `reference_transient` se excluye deliberadamente: contiene solo
descargas brutas de trabajo, nunca artefactos promovidos. Los parciales se
borran al finalizar y los huérfanos de una interrupción se purgan antes de la
siguiente adquisición.

Redis y las teselas de GeoWebCache **no** son fuentes de verdad. La cola se
reconstruye desde el estado durable de PostgreSQL al reiniciar scheduler y
workers. Las teselas se regeneran bajo demanda a partir de los artefactos
locales restaurados. Compose fija `GEOWEBCACHE_CACHE_DIR` a la ruta persistente
`/opt/geoserver_data/gwc`, dentro de `geoserver_data`: quedan en el backup
`gwc-gs.xml`, `gwc-layers/`, `gwc/geowebcache.xml`, la definición del
FileBlobStore y cualquier otra configuración persistente. Las teselas se
escriben mediante el FileBlobStore explícito `siur-tile-cache-v3`, cuyo
`baseDirectory` exacto es `/var/lib/geowebcache`, sobre un volumen distinto.
La cuota deseada está versionada en `.env.example` y se vuelve a aplicar y
releer con el comando de este documento. Esta separación sigue la
[configuración oficial de GeoWebCache integrado](https://docs.geoserver.org/3.0.x/en/user/geowebcache/config/),
que distingue `GEOWEBCACHE_CACHE_DIR` de `gwc-layers/` y
`gwc/geowebcache.xml`, y el
[FileBlobStore oficial](https://geowebcache.osgeo.org/docs/current/rest/blobstores.html),
que permite separar su `baseDirectory`.

Compose usa ahora el volumen de teselas versionado
`geowebcache_tile_cache_v3` con `nocopy`. Los anteriores
`geowebcache_tile_cache_v2` y `geowebcache_data` quedan sin montar y **no se
borran, copian ni renombran automáticamente**. Deben conservarse para rollback
e inspeccionarse fuera de línea, porque podrían mezclar configuración
persistente y teselas derivadas. El control de cuota falla si encuentra
configuración dentro de v3. La migración segura conserva la configuración
canónica en `geoserver_data`, arranca v3 vacío y deja que regenere únicamente
teselas.

La herramienta no ofrece ninguna operación de borrado al operador. `create` y
`restore` son dry-run por defecto, nunca admiten un destino existente y dejan
cualquier directorio `.partial-*` fallido para revisión forense. `restore` solo
acepta:

- un directorio nuevo cuyo nombre empiece por `siur-drill-restore-`;
- una base realmente vacía llamada `app_drill_*`: comprueba propietario,
  identidad persistida, sesiones, schemas, relaciones, funciones, tipos,
  extensiones, ACL, ajustes y etiquetas de seguridad;
- un propietario dedicado no-superusuario, miembro de `pg_monitor`, con límite
  de una conexión tanto en el rol como en la base; `plpgsql`, `postgis` y
  `vector` deben estar preinstaladas por el administrador, pertenecerle y
  coincidir exactamente en schema, versión y conjunto canónico de miembros con
  el backup;
- PostgreSQL en `127.0.0.1`, en un puerto distinto del `5432` del runtime de
  desarrollo;
- `pg_restore --single-transaction --exit-on-error`, sin `--clean`, `--create`,
  comentarios, `DROP DATABASE` ni sobrescritura.

El dump excluye formalmente `plpgsql`, `postgis` y `vector`: no intenta
recrearlas ni copiar datos internos como `spatial_ref_sys`. El manifest
conserva la identidad completa observada, las versiones requeridas y el conteo
y SHA-256 del inventario estable de miembros de cada extensión. La herramienta
compara ese inventario con el baseline fijado para `plpgsql 1.0`,
`postgis 3.6.4` y `vector 0.8.2` antes y después del dump y antes y después del
restore. Así, un objeto de aplicación añadido accidentalmente con
`ALTER EXTENSION ... ADD` provoca un aborto en vez de desaparecer por
`--exclude-extension`. Si una versión exacta no está instalada y disponible en
el servidor drill, la restauración falla antes de `pg_restore`; no actualiza ni
degrada extensiones automáticamente. Al usar `--no-comments`, los objetos de
aplicación se restauran sin metadatos `COMMENT`.

Durante un restore, los seis ficheros del backup se abren una sola vez respecto
del descriptor del directorio, se verifican sobre esos descriptores y los tres
payloads consumidos se copian a ficheros anónimos privados (`O_TMPFILE`). Tar y
`pg_restore` leen exclusivamente esos snapshots sin nombre. La identidad de la
ruta original se vuelve a comprobar antes de extraer y antes de mutar
PostgreSQL; cualquier sustitución hace abortar sin publicar el destino.

Todos los pasos de filesystem y el `restore-report.json` se preparan antes de
`pg_restore`. Si después del commit falla la publicación atómica, la herramienta
ejecuta como compensación `DROP OWNED BY CURRENT_USER CASCADE` conectada
únicamente a ese target `app_drill_*` aislado, repone el baseline estándar
`public`, conserva las tres extensiones administrativas y vuelve a demostrar
que está vacío. En la misma transacción, antes de `DROP OWNED`, comprueba nombre
de base y usuario, `system_identifier`, OID, token drill y la identidad exacta
de las extensiones. Una respuesta ambigua de `pg_restore` también se considera
potencialmente mutante. Nunca elimina ni recrea la base ni ejecuta la
compensación si cambia una identidad.

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

Con GeoServer arrancado pero antes de admitir tráfico de teselas, el dry-run
lee por REST tanto los blobstores como la cuota y mide el filesystem real de
v3:

```bash
docker compose --profile operations run --rm -T --no-deps gwc-ops
```

El informe distingue:

- capacidad, uso y espacio libre del filesystem;
- definición actual y deseada del FileBlobStore fijo
  `siur-tile-cache-v3` (`default=true`, `enabled=true`,
  `baseDirectory=/var/lib/geowebcache`, layout `DEFAULT` y tamaño de bloque
  real del filesystem);
- bytes lógicos y bloques físicos asignados, medidos dos veces y sobre el mismo
  `st_dev`, rechazando cambios, mounts anidados, symlinks, hardlinks, ficheros
  sparse, configuración legacy y tipos especiales;
- inodos usados/libres y una reserva conservadora para millones de teselas
  pequeñas;
- techo configurado y reserva mínima;
- una ampliación física conservadora de al menos el 125 % del crecimiento
  lógico restante;
- margen libre actual `libre - reserva`;
- crecimiento restante `max(cuota - caché actual, 0)` y los márgenes decisivos
  de bloques físicos e inodos;
- coincidencia exacta entre estado actual y deseado.

Solo si todos los márgenes son no negativos se permite aplicar. Por ejemplo,
con caché vacía, 6 GiB libres, cuota de 20 GiB y reserva de 5 GiB se rechaza:
se reservan 25 GiB físicos para el crecimiento más 5 GiB libres, por lo que
faltan 24 GiB, además de comprobar la reserva de inodos.

```bash
docker compose --profile operations run --rm -T --no-deps gwc-ops \
  python -m app.reference_layers.gwc_quota \
  --cache-path /var/lib/geowebcache \
  --apply
```

En `--apply`, el cliente primero lista y lee por XML todos los blobstores. Si el
identificador reservado ya existe con otra ruta, tamaño de bloque, estado o
layout, o si hay otro blobstore configurado como predeterminado, aborta sin
mutar. Si falta y no hay otro default configurado, hace el `PUT` XML oficial a
`/geoserver/gwc/rest/blobstores/siur-tile-cache-v3.xml`, vuelve a listar y
releer la representación canónica completa y solo entonces configura la cuota.
El default anónimo que GeoWebCache genera cuando no hay ninguno configurado no
forma parte de la lista REST; GeoWebCache 2.0.0 lo sustituye al añadir el
default explícito, sin reescribir otro blobstore persistido.

Después, el cliente hace `PUT /geoserver/gwc/rest/diskquota.json` y a
continuación `GET` del mismo recurso. Un estado HTTP inesperado, XML/JSON
incompleto o cualquier diferencia de blobstore, cuota, intervalo o política
hace fallar la operación; no se declara éxito por haber recibido solo un
`PUT`. El contrato de cuota corresponde a la
[API oficial de cuota de GeoWebCache en GeoServer
3.0](https://docs.geoserver.org/3.0.x/en/user/geowebcache/rest/diskquota/).

Repetir el dry-run y archivar su JSON como evidencia. Después de una
restauración, ejecutar otra vez este bloque. Solo el volumen v3 de teselas se
reconstruye; toda la configuración GWC se restaura con el data dir.

### Migración desde v2 y rollback

1. Adquirir el lease del runtime y detener escritores, frontend/backend y
   GeoServer. No ejecutar `down -v`.
2. Crear un backup verificado de `geoserver_data` y conservar intactos los
   volúmenes v2 y `geowebcache_data`.
3. Desplegar Compose: `GEOWEBCACHE_CACHE_DIR` debe resolver a
   `/opt/geoserver_data/gwc`; solo v3 debe estar montado en
   `/var/lib/geowebcache`. No montar v2 y v3 simultáneamente.
4. Arrancar GeoServer sin admitir tráfico, ejecutar primero el dry-run y
   después `--apply`. Archivar el JSON con `blob_store.verified=true`,
   `disk_quota.verified=true` y `verified=true`.
5. Generar una tesela de prueba y demostrar que aparece únicamente en v3; no
   promover el despliegue si `geowebcache.xml`, `gwc-gs.xml`, `gwc-layers` o
   metadatos de configuración aparecen en ese volumen.

Para rollback, detener de nuevo todos los escritores y GeoServer, volver al
Compose anterior y montar el volumen v2 original sin modificarlo. No copiar
v3 sobre v2 ni borrar ninguno de los dos. La configuración nueva permanece en
el backup/data dir; si la revisión anterior no la entiende, restaurar el backup
verificado de `geoserver_data` tomado en el paso 2. Tras confirmar el rollback,
mantener v3 desconectado para análisis o una nueva migración.

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
  "schema_version": 2,
  "captured_at": "2026-07-26T12:00:00Z",
  "operator": "operador-identificado",
  "reference_artifacts_source": "/sources/reference_artifacts",
  "geoserver_data_source": "/sources/geoserver_data",
  "postgres_database_name": "app",
  "postgres_database_user": "app",
  "postgres_hostname": "127.0.0.1",
  "postgres_port": 5432,
  "postgres_system_identifier": "7560000000000000000",
  "postgres_database_oid": 16384,
  "postgres_extensions": [
    {
      "name": "plpgsql",
      "schema": "pg_catalog",
      "version": "1.0",
      "owner": "app"
    },
    {
      "name": "postgis",
      "schema": "public",
      "version": "3.6.4",
      "owner": "app"
    },
    {
      "name": "vector",
      "schema": "public",
      "version": "0.8.2",
      "owner": "app"
    }
  ],
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

Obtener `postgres_system_identifier` y `postgres_database_oid` de la conexión
exacta descrita por `source-db.url`, sin copiarlos de otro entorno:

```sql
SELECT system_identifier::text,
       (SELECT oid FROM pg_database WHERE datname = current_database())
FROM pg_control_system();

SELECT e.extname, n.nspname, e.extversion,
       pg_get_userbyid(e.extowner)
FROM pg_extension e
JOIN pg_namespace n ON n.oid = e.extnamespace
ORDER BY e.extname;
```

La evidencia es una afirmación del operador después de comprobar `ps`; la
herramienta verifica esquema, paths y endpoint exactos, lista completa, zona
horaria y antigüedad máxima de una hora. En `--apply` contrasta además nombre,
usuario, `system_identifier`, OID y extensiones con PostgreSQL vivo antes del
dump y vuelve a comprobar la misma identidad después. No intenta controlar
Docker desde el contenedor de backup.

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

1. contrasta la identidad PostgreSQL viva con la evidencia, valida el inventario
   canónico de miembros de las tres extensiones e inventaría y hashea todos los
   ficheros mediante descriptores abiertos, sin seguir componentes sustituidos;
2. ejecuta `pg_dump` custom, serializable, sin owner ni privilegios, excluyendo
   las tres extensiones preinstaladas, y vuelve a comprobar toda la identidad y
   los miembros canónicos de PostgreSQL al terminar;
3. crea dos tar sin symlinks, devices ni rutas absolutas; UID/GID, permisos y
   tiempos se guardan en el manifest y no se confían a los campos del tar;
4. vuelve a inventariar y aborta si cambió un byte o metadata;
5. conserva los bytes exactos de `quiescence-evidence.json` dentro del backup
   y enlaza su tamaño/hash desde `manifest.json`, junto con hashes del dump,
   archives, inventario por fichero, ownership y exclusiones;
6. hashea el manifest y publica el directorio parcial mediante
   `renameat2(RENAME_NOREPLACE)` relativo al descriptor abierto del padre.

Verificar bytes, miembros de tar y legibilidad del dump:

```bash
docker compose --profile operations run --rm -T --no-deps \
  -v /srv/siur-backups:/backups:ro \
  siur-recovery verify \
  --backup /backups/siur-backup-20260726T120000Z
```

El verificador conserva compatibilidad de lectura con backups schema 2 y 3 ya
publicados, pero no los restaura automáticamente porque carecen del baseline
canónico de miembros de extensiones. Los backups nuevos son schema 4: conservan
la identidad PostgreSQL, versiones requeridas de extensiones, `ctime`, número
de enlaces y el inventario canónico hashado de miembros.

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

2. Arrancar un contenedor temporal en otro puerto y almacenamiento nuevo. El
   usuario de bootstrap es solo el administrador del clúster; no se usará para
   el restore. Usar `POSTGRES_PASSWORD_FILE`, no una contraseña en argv:

   ```bash
   docker run --rm -d \
     --name siur-drill-postgres-20260726 \
     -p 127.0.0.1:55432:5432 \
     -e POSTGRES_DB=postgres \
     -e POSTGRES_USER=drill_admin \
     -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password \
     -v /srv/siur-drill-db-20260726:/var/lib/postgresql/data \
     -v /srv/siur-secrets:/run/secrets:ro \
     asistente-ayuntamientos-postgres:17-postgis-pgvector
   ```

3. Preparar con modo `0600`
   `/srv/siur-secrets/drill-bootstrap.sql`. Debe crear un propietario dedicado
   no-superusuario. El token es un UUID nuevo, en minúsculas, que no se
   reutiliza entre bases:

   ```sql
   CREATE ROLE siur_drill_owner
     LOGIN PASSWORD 'CONTRASEÑA-DISTINTA'
     CONNECTION LIMIT 1
     NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
   GRANT pg_monitor TO siur_drill_owner;
   CREATE DATABASE app_drill_20260726
     WITH OWNER = siur_drill_owner CONNECTION LIMIT = 1;
   REVOKE ALL ON DATABASE app_drill_20260726 FROM PUBLIC;
   COMMENT ON DATABASE app_drill_20260726 IS
     'siur-drill-v1:8b27db31-f75c-4f5b-b417-6ae42da7491a';
   \connect app_drill_20260726 drill_admin
   CREATE EXTENSION postgis VERSION '3.6.4';
   CREATE EXTENSION vector VERSION '0.8.2';
   ```

   Aplicarlo desde el contenedor temporal:

   ```bash
   docker exec -i siur-drill-postgres-20260726 \
     psql -X -v ON_ERROR_STOP=1 -U drill_admin -d postgres \
     -f /run/secrets/drill-bootstrap.sql
   ```

   Usar las versiones declaradas por `required_extensions` en el backup, no
   copiar a ciegas las del ejemplo. Si la imagen no ofrece exactamente alguna
   de ellas, detener el simulacro y reconciliar primero la imagen. Las tres
   extensiones deben seguir perteneciendo a `drill_admin`; no cambiar su owner
   al rol drill. Consultar después la identidad no secreta sobre la base
   exacta:

   ```sql
   SELECT system_identifier::text,
          (SELECT oid FROM pg_database
           WHERE datname = current_database())
   FROM pg_control_system();

   SELECT e.extname, n.nspname, e.extversion,
          pg_get_userbyid(e.extowner)
   FROM pg_extension e
   JOIN pg_namespace n ON n.oid = e.extnamespace
   ORDER BY e.extname;
   ```

4. Crear con modo `0600` `/srv/siur-secrets/drill-db.url`, apuntando
   únicamente a:

   ```text
   postgresql://siur_drill_owner:CONTRASEÑA-DISTINTA@127.0.0.1:55432/app_drill_20260726
   ```

   Crear también con modo `0600`
   `/srv/siur-secrets/drill-identity.json`. Todos los campos son exactos; usar
   el `system_identifier`, OID y extensiones recién consultados:

   ```json
   {
     "schema_version": 2,
     "database_name": "app_drill_20260726",
     "database_user": "siur_drill_owner",
     "system_identifier": "7560000000000000000",
     "database_oid": 16392,
     "drill_token": "8b27db31-f75c-4f5b-b417-6ae42da7491a",
     "extensions": [
       {
         "name": "plpgsql",
         "schema": "pg_catalog",
         "version": "1.0",
         "owner": "drill_admin"
       },
       {
         "name": "postgis",
         "schema": "public",
         "version": "3.6.4",
         "owner": "drill_admin"
       },
       {
         "name": "vector",
         "schema": "public",
         "version": "0.8.2",
         "owner": "drill_admin"
       }
     ]
   }
   ```

   Este fichero y el comentario de la base son la atestación local de identidad
   del target. No son permisos de licencia ni condiciones de uso de los datos.

5. Ejecutar primero el dry-run. Verifica backup e identidad persistida, pero no
   conecta a la base target ni escribe:

   ```bash
   docker compose --profile operations run --rm -T --no-deps \
     -v /srv/siur-backups:/backups:ro \
     -v /srv/siur-drills:/drills \
     -v /srv/siur-secrets:/run/siur-secrets:ro \
     siur-recovery restore \
     --backup /backups/siur-backup-20260726T120000Z \
     --destination /drills/siur-drill-restore-20260726 \
     --target-database-url-file /run/siur-secrets/drill-db.url \
     --target-database-identity-file \
       /run/siur-secrets/drill-identity.json
   ```

6. Repetir con `--apply`. El target debe seguir vacío y sin ninguna otra
   conexión:

   ```bash
   docker compose --profile operations run --rm -T --no-deps \
     -v /srv/siur-backups:/backups:ro \
     -v /srv/siur-drills:/drills \
     -v /srv/siur-secrets:/run/siur-secrets:ro \
     siur-recovery restore \
     --backup /backups/siur-backup-20260726T120000Z \
     --destination /drills/siur-drill-restore-20260726 \
     --target-database-url-file /run/siur-secrets/drill-db.url \
     --target-database-identity-file \
       /run/siur-secrets/drill-identity.json \
     --apply
   ```

7. Conservar `restore-report.json`, comprobar consultas de solo lectura en la
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
