# Mapa municipal

Actualizado: 2026-06-28.

## Propósito

El mapa municipal es una capa geográfica compartida para el trabajo del ayuntamiento. No es solo una pantalla decorativa: permite visualizar necesidades y proyectos con ubicación, abrir su ficha operativa y preparar futuras capas territoriales sin acoplar coordenadas a cada módulo.

La primera versión se limita a puntos asociados a necesidades y proyectos visibles para el usuario.

## Modelo de datos

La ubicación se modela en el dominio `geo`:

- `geo_locations`: ubicación geográfica reutilizable, con etiqueta, organización, municipio opcional, coordenadas de punto, GeoJSON, fuente, confianza y estado de revisión.
- `entity_locations`: vínculo entre una ubicación y una entidad de negocio. En v1 solo se admiten `requirement` y `project`.

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
- `POST /geo/entity-locations`: crea o actualiza la ubicación primaria, de área afectada o de referencia de una necesidad/proyecto.

`GET /geo/map-items` devuelve elementos preparados para el mapa: tipo de entidad, id, título, subtítulo, estado, prioridad, organización, enlace de detalle y ubicación.

`POST /geo/entity-locations` hace upsert por `(entity_type, entity_id, role)` para que actualizar la ubicación primaria no cree marcadores duplicados.

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

Una ubicación solo aparece si el usuario tiene permiso de mapa en la organización de esa entidad y además podría acceder a la necesidad/proyecto por sus reglas normales. El frontend oculta navegación y acciones según permisos, pero la garantía de seguridad reside en el backend.

## Frontend

La ruta `/mapa` monta un panel de trabajo con filtros básicos y un mapa Leaflet.

Decisiones de v1:

- Leaflet directo en componente cliente, sin React-Leaflet.
- Import dinámico de Leaflet dentro del cliente para evitar problemas SSR de Next.js.
- Marcadores propios con `divIcon`, evitando depender de assets internos de Leaflet.
- Fallback centrado en Burgos/Castilla y León si no hay puntos.
- Enlaces desde marcador:
  - necesidad: `/requisitos?id=<id>`;
  - proyecto: `/proyectos` hasta que la pantalla de proyectos tenga deep-link de selección.

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
  "pip install -q -r requirements-dev.txt && python -m pytest tests/test_geo.py tests/test_admin_rbac.py -q"
```

```bash
cd frontend
npm run build
```

Si se valida en navegador con Docker Compose, reconstruir/recrear los servicios afectados antes de probar `/mapa`.
