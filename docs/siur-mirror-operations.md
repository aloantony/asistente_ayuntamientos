# Operación del espejo cartográfico SIUR

La entrada de operador es:

```bash
python -m app.reference_layers.mirror_admin
```

Todos los resultados se emiten como JSON. Los comandos de transición exigen
una generación esperada para impedir que una decisión revisada se aplique
sobre un estado que cambió mientras tanto.

## Estado

```bash
python -m app.reference_layers.mirror_admin status \
  --provider-key siur
```

Se puede limitar el informe con `--layer-id`. Por cada capa se muestran:

- fuentes y próxima comprobación;
- última ejecución, duración y último error;
- versión activa y anterior, validación y bytes;
- integridad de la cadena de promociones;
- candidatura de rollback o reactivación;
- revisión, atestación y permiso explícito de espejo.

`size_bytes_complete=false` significa que no se conoce el tamaño total. Es el
resultado esperado para una entrega PostGIS mientras no exista una métrica
fiable del tamaño físico; no se inventa un cero.

Un servicio con catálogo `pending`, sin revisión, sin atestación o sin permiso
explícito figura como `mirror_authorized=false`. Este comando no crea ni
aprueba evidencia legal.

## Sincronización manual acotada

Para comprobar de inmediato una fuente concreta sin esperar a su programación
diaria, primero se copian del informe de estado `source_id`,
`definition_sha256` y `expected_active_generation`:

```bash
python -m app.reference_layers.mirror_admin enqueue \
  --provider-key siur \
  --source-id 321 \
  --expected-source-definition-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --expected-generation 8 \
  --check-mode full \
  --actor-user-id 7 \
  --reason "comprobación de aceptación del raster representativo" \
  --dry-run
```

Tras revisar el JSON se repite exactamente con `--apply`. El comando solo
acepta una fuente habilitada y procesable por el worker, evidencia de catálogo
y autorización vigentes, una generación y hash exactos y ninguna ejecución
abierta para la misma capa. El dry-run no inserta filas. El apply crea una sola
ejecución `manual`, conserva la próxima fecha programada y registra actor y
motivo durante toda la ejecución. Si esa fuente falla, el comando no prueba
otra automáticamente: la selección exacta solo cambia mediante una nueva
orden explícita.

## Rollback

Primero se revisa un dry-run:

```bash
python -m app.reference_layers.mirror_admin rollback \
  --provider-key siur \
  --layer-id 123 \
  --target-version-id 456 \
  --expected-generation 8 \
  --actor-user-id 7 \
  --reason "regresión confirmada en el renderizador" \
  --dry-run
```

Si el plan sigue siendo correcto, se repite exactamente con `--apply`. El
actor debe existir y estar activo. El target debe pertenecer a esa capa, haber
estado publicado previamente y conservar íntegros catálogo, ejecución,
validación y asset primario. La operación añade un evento inmutable; no
reescribe ni borra la historia.

## Recuperación tras desactivación

`reactivate` usa los mismos argumentos que `rollback`. Solo acepta una capa
desactivada y una versión que ya hubiera estado servida. También requiere un
dry-run o apply explícito:

```bash
python -m app.reference_layers.mirror_admin reactivate \
  --provider-key siur \
  --layer-id 123 \
  --target-version-id 456 \
  --expected-generation 9 \
  --actor-user-id 7 \
  --reason "recuperación posterior al mantenimiento" \
  --dry-run
```

## Retención y GC

La política es conservar indefinidamente versiones publicadas, assets,
artefactos de adquisición y evidencia de promoción. El borrado de blobs CAS
permanece deshabilitado: sin un fence de mantenimiento no se puede distinguir
con seguridad un huérfano real del breve intervalo entre publicar el blob y
confirmar su fila en PostgreSQL.

La única limpieza disponible afecta a parciales antiguos de `staging`. El
dry-run es explícito y no elimina nada:

```bash
python -m app.reference_layers.mirror_admin staging-gc --dry-run
```

Para ejecutar el plan:

```bash
python -m app.reference_layers.mirror_admin staging-gc --apply
```

La retención predeterminada se configura con
`REFERENCE_STAGING_RETENTION_SECONDS` (24 horas por defecto y nunca menos de
una hora). Se omiten ficheros activos, bloqueados, no regulares o con nombres
no canónicos. Este comando nunca recorre ni elimina blobs publicados.

El backup consistente, la verificación de hashes, el restore aislado y la
cuota comprobada de GeoWebCache se operan según
[Recuperación ante desastres del espejo SIUR](siur-disaster-recovery.md).
