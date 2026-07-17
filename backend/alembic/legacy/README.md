# Migraciones históricas fuera del grafo activo

Este directorio conserva, sin renumerar ni reescribir, migraciones que llegaron
a ejecutarse en bases persistentes pero cuyo identificador colisionó antes de
integrarse en el grafo canónico de Alembic.

`20260716_0026_add_municipality_reference_geography.py` es la revisión
geográfica antigua. Comparte el identificador `20260716_0026` con la revisión
publicada en `main` que cambia el estado por defecto de las ordenanzas. Por eso
no puede permanecer en `alembic/versions`: Alembic no admite dos revisiones con
el mismo ID. Su contenido se conserva intacto como artefacto auditable y como
fuente exacta del DDL que usa la revisión reconciliadora `20260717_0029`. Su
SHA-256 es
`78b7dd5d0ff1b5c11f0516ad9154c922ea192157fe9267760d166d030a4ac474`.

La revisión `20260717_0029` reconoce de forma segura estos caminos históricos:

- `20260716_0026` geográfica, seguida o no de las revisiones de adjuntos;
- `20260716_0026` canónica de ordenanzas;
- bases nuevas que recorren exclusivamente el grafo canónico.

En una base con la huella geográfica antigua y versión `0026`, `0027` o `0028`,
la primera operación mutante debe ser `alembic upgrade 20260717_0029` (o
`alembic upgrade head`). El entorno de Alembic bloquea expresamente `downgrade`
y `stamp` hasta completar esa reconciliación: las revisiones canónicas antiguas
no pueden distinguir por sí solas qué `0026` se ejecutó. La huella textual se
valida sobre PostgreSQL 17, versión fijada tanto en Compose como en CI.

No se normalizan automáticamente revisiones anteriores en las que adjuntos
también usó los IDs `0026`/`0027`. Si aparecen esas huellas, hay que auditar el
esquema y hacer una recuperación manual; un `stamp` basado solo en
`alembic_version` no demuestra que el DDL sea compatible.
