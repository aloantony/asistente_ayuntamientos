# Inventario municipal

Actualizado: 2026-07-15.

## Propósito

El inventario municipal registra elementos físicos gestionados por cada
ayuntamiento, permite representarlos y reubicarlos en el mapa, y prepara su
conexión posterior con mantenimiento. Continúa sin una pantalla específica de
inventario.

## Modelo de datos

La taxonomía y los activos se mantienen en tres tablas:

- `municipal_asset_categories`: categorías ordenables de una organización,
  con código estable, nombre, color opcional y estado.
- `municipal_asset_types`: tipos pertenecientes a una categoría de la misma
  organización.
- `municipal_assets`: activos vinculados a una organización, al municipio
  configurado en ella, a un tipo y, opcionalmente, a una `GeoLocation`.

Las claves foráneas compuestas impiden en PostgreSQL asociar un tipo a una
categoría de otro tenant, un activo a un tipo de otra organización o una
organización, municipio y ubicación incompatibles entre sí. La API aplica las
mismas reglas antes del commit para devolver conflictos comprensibles.

Los estados disponibles son:

- categoría y tipo: `active`, `archived`;
- activo: `active`, `inactive`, `retired`, `archived`;
- estado de conservación: `good`, `fair`, `poor`, `unknown`.

No hay borrado físico en la API. Los códigos de categoría se normalizan a
minúsculas y son únicos por organización; los de tipo, por organización y
categoría; los códigos opcionales de activo, por organización.

## API

El contrato inicial expone:

- `GET|POST /assets/categories`;
- `PATCH /assets/categories/{category_id}`;
- `GET|POST /assets/types`;
- `PATCH /assets/types/{asset_type_id}`;
- `GET|POST /assets`;
- `GET|PATCH /assets/{asset_id}`.

Los listados requieren `organization_id`, aceptan la paginación común
`limit`/`offset` y publican el total en `X-Total-Count`. Los activos admiten
búsqueda textual y filtros por categoría, tipo, estado y conservación. Los
elementos archivados se excluyen por defecto y se pueden solicitar de forma
explícita.

## Permisos y aislamiento

Los permisos son tenant-scoped:

- `assets.view`;
- `assets.create`;
- `assets.edit`;
- `assets.archive`;
- `assets.manage`, que incluye todas las acciones anteriores dentro de la
  organización donde se concede.

Una organización `active` permite lectura y escritura. Una organización
`paused` conserva la lectura pero bloquea toda modificación; una organización
`archived` no expone el inventario. Las escrituras requieren también un
municipio activo vinculado a la organización.

Cuando una organización ya tiene activos, su municipio no se puede cambiar ni
eliminar mediante la API: antes hay que migrar o retirar esos datos de forma
explícita. En el modelo, una ubicación enlazada conserva mutable su etiqueta,
coordenadas y metadatos, pero no puede reasignarse a otra organización o
municipio. El flujo cartográfico no explota esa mutabilidad: reubica mediante
copy-on-write para preservar cualquier referencia compartida. Las restricciones
de PostgreSQL protegen el ámbito también ante carreras o escrituras que no pasen
por la API.

Archivar y editar son capacidades independientes. Un `PATCH` que archive y
modifique otros campos exige ambos permisos, y crear directamente un registro
archivado exige `assets.create` y `assets.archive`. Una categoría o tipo
archivado sigue disponible para consultar datos históricos, pero no puede
recibir nuevos tipos o activos.

## Integración geográfica

Un activo puede referenciar una ubicación de `geo_locations`.
Para aceptarla, `organization_id` y `municipality_id` deben coincidir con el
contexto del activo. La creación y reubicación de geometrías pertenece al
dominio `geo`; `municipal_assets.location_id` continúa siendo el único vínculo
del activo y nunca se duplica en `entity_locations`.

`assets.view` autoriza a leer la ubicación completa vinculada a los activos
visibles de esa organización, porque forma parte de su ficha de inventario.
No autoriza a listar otras entidades geográficas ni a editar la ubicación:
`GET /geo/map-items` exige simultáneamente `map.view|manage` y
`assets.view|manage` en la organización del activo. Reubicarlo exige además
edición en ambos dominios. Esta separación es deliberada y evita que el permiso
del mapa sea un requisito implícito para consultar una ficha de activo, o que
un editor cartográfico pueda modificar el inventario sin autorización.

Cada reubicación cartográfica mediante `POST /geo/entity-locations` crea una
ubicación nueva con procedencia `user_provided` y estado de revisión `proposed`,
y reasigna solo el activo. No se actualiza ni se elimina la ubicación anterior:
puede estar compartida y su eventual limpieza requiere un proceso separado y
auditable.

`location_id` es de solo lectura en los esquemas públicos del inventario. Ni
`POST /assets` ni `PATCH /assets/{id}` aceptan identificadores geográficos
arbitrarios: hacerlo permitiría convertir en visible la ubicación de una
necesidad o proyecto inaccesible del mismo tenant. El único flujo HTTP que
asigna o reubica actualmente es el dominio `geo`, con sus permisos,
copy-on-write y procedencia controlada. Una futura importación o reutilización
de ubicaciones existentes necesitará un contrato explícito de visibilidad y
auditoría; no se habilita implícitamente por conocer un id.

`/mapa` permite filtrar y abrir marcadores de activos, y seleccionar un activo
existente desde el menú contextual para ubicarlo. No crea fichas de inventario.

## Fuera de alcance

Quedan expresamente fuera de esta entrega:

- adjuntos e importaciones masivas;
- edición de ubicaciones desde una futura pantalla de inventario;
- PostGIS, polígonos y otras geometrías avanzadas;
- frontend específico de inventario.

Las órdenes y su historial auditable se implementan como dominio separado para
no mezclar la ficha del activo con el ciclo de trabajo. Su contrato, permisos y
límites se documentan en `docs/mantenimiento-municipal.md`.

Cada ampliación debe conservar RBAC, aislamiento tenant, procedencia de datos y
migraciones reversibles antes de habilitarla en producción.

## Validación esperada

```bash
python3 -m compileall -q backend/app backend/alembic
```

```bash
python -m pytest tests/test_geo.py tests/test_assets.py tests/test_migrations.py -q
```

La aceptación final debe incluir además la suite backend completa y
`npm run typecheck`, `npm run lint`, `npm run build` y validación en navegador
de `/mapa`.
