# Mapa municipal

Actualizado: 2026-07-15.

## Propósito

El mapa municipal es una capa geográfica compartida para el trabajo del ayuntamiento. No es solo una pantalla decorativa: permite visualizar necesidades, proyectos y activos con ubicación, abrir su detalle o contexto disponible y preparar futuras capas territoriales sin acoplar coordenadas a cada módulo.

La versión actual se limita a puntos asociados a necesidades, proyectos y activos visibles para el usuario.

## Modelo de datos

La ubicación se modela en el dominio `geo`:

- `geo_locations`: ubicación geográfica reutilizable, con etiqueta, organización, municipio opcional, coordenadas de punto, GeoJSON, fuente, confianza y estado de revisión.
- `entity_locations`: vínculo entre una ubicación y una necesidad o proyecto.
- `municipal_assets.location_id`: vínculo canónico y único entre un activo y su ubicación; los activos no se duplican en `entity_locations`.

Se evita añadir columnas `latitude`/`longitude` directamente a requisitos o proyectos para que el mapa pueda crecer hacia más entidades, roles de ubicación y capas sin duplicar lógica.

## Geometría

En v1 solo se crean puntos. Cada punto guarda:

- `latitude`
- `longitude`
- `geometry_json`

`geometry_json` usa GeoJSON RFC 7946. Por tanto, las coordenadas se serializan como `[longitude, latitude]`, aunque Leaflet recibe los puntos como `[latitude, longitude]` en el frontend.

PostGIS queda aplazado: la imagen actual de desarrollo usa PostgreSQL/pgvector, no PostGIS. La lógica de geometría queda aislada en `app/geo/geometry.py` para facilitar una migración posterior a columnas espaciales si el producto lo requiere.

## API

La API inicial expone:

- `GET /geo/map-items`: devuelve marcadores visibles para el usuario autenticado.
- `POST /geo/entity-locations`: crea o actualiza la ubicación primaria, de área afectada o de referencia de una necesidad/proyecto, y ubica o reubica un activo existente con el rol primario.

`GET /geo/map-items` devuelve elementos preparados para el mapa: tipo de entidad, id, rol de la ubicación, título, subtítulo, estado, prioridad, organización, enlace de detalle y ubicación. La identidad de un marcador es `(entity_type, entity_id, role)`, por lo que una ubicación primaria y otra de referencia de la misma entidad no colisionan. `entity_id` solo se acepta junto con `entity_type`. Los filtros de visibilidad y estado se aplican antes de un único límite global; el resultado combina los tres dominios por actualización de ubicación más reciente, con desempate estable.

Para necesidades y proyectos, `POST /geo/entity-locations` hace upsert lógico por `(entity_type, entity_id, role)`, pero también crea una ubicación nueva y reasigna el vínculo. Para activos crea una ubicación nueva en cada operación y reasigna únicamente `municipal_assets.location_id`. Ninguno de los flujos modifica una ubicación previa que pueda estar compartida.

## Permisos y aislamiento

El mapa combina dos niveles de autorización:

1. Permisos de mapa:
   - `map.view`: ver mapa municipal.
   - `map.edit`: editar ubicaciones.
   - `map.import`: importar capas geográficas futuras.
   - `map.manage`: gestionar mapa municipal.
2. Visibilidad de la entidad original:
   - necesidades usan las reglas existentes de requisitos;
   - proyectos usan las reglas existentes de acceso a proyectos.
   - activos requieren además `assets.view|manage`; su reubicación exige también `assets.edit|manage`.

Una ubicación solo aparece si el usuario tiene permiso de mapa en la organización de esa entidad y además podría acceder a la entidad por sus reglas normales. En activos se ocultan ubicaciones rechazadas, organizaciones archivadas y activos archivados por defecto. La reubicación deriva organización y municipio del activo, solo admite organizaciones activas con municipio activo y registra la ubicación como aportada por el usuario y pendiente de revisión. El frontend oculta navegación y acciones según permisos, pero la garantía de seguridad reside en el backend.

## Frontend

La ruta `/mapa` monta un panel de trabajo con filtros básicos y un mapa Leaflet.

Decisiones de v1:

- Leaflet directo en componente cliente, sin React-Leaflet.
- Import dinámico de Leaflet dentro del cliente para evitar problemas SSR de Next.js.
- Marcadores propios con `divIcon`, evitando depender de assets internos de Leaflet.
- Fallback centrado en Burgos/Castilla y León si no hay puntos.
- Enlaces desde marcador:
  - necesidad: `/requisitos?id=<id>`;
  - proyecto: `/proyectos` hasta que la pantalla de proyectos tenga deep-link de selección;
  - activo: `/ayuntamiento` hasta que exista una pantalla específica de inventario.
- El menú contextual permite buscar por texto y seleccionar un activo municipal existente para ubicarlo o reubicarlo. La búsqueda consulta el listado paginado del backend, muestra el total y pide acotar el texto si existen más coincidencias que el máximo de una página. La creación y edición de su ficha siguen fuera del mapa.

## Fuera de alcance de v1

No se implementa todavía:

- geocodificación automática;
- extracción de ubicaciones por el asistente;
- integración Catastro;
- PostGIS;
- polígonos o dibujo de áreas;
- capas de redes, equipamientos o rutas;
- navegación/direcciones;
- tiles offline;
- decisiones oficiales o jurídicas basadas en ubicación.

Estas piezas deben entrar como slices posteriores cuando el mapa base esté probado con datos reales.

## Validación esperada

Para aceptar cambios del mapa v1:

```bash
python3 -m compileall -q backend/app backend/alembic
```

```bash
docker compose run --rm -T --no-deps -v "$(pwd)/backend:/app" backend sh -c \
  "pip install -q -r requirements-dev.txt && python -m pytest tests/test_geo.py tests/test_assets.py tests/test_admin_rbac.py -q"
```

```bash
cd frontend
npm run typecheck
npm run lint
npm run build
```

Si se valida en navegador con Docker Compose, reconstruir/recrear los servicios afectados antes de probar `/mapa`.
