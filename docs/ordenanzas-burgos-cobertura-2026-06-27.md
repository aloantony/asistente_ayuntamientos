# Reporte de cobertura Sprint 1 — Ordenanzas Burgos

Fecha de ejecución: 2026-06-27

## Alcance

Este reporte mide el corpus local disponible para el MVP de ordenanzas Burgos y valida que el conector determinista BOPBUR puede descubrir e importar anuncios oficiales desde `bopbur.diputaciondeburgos.es`.

No representa cobertura completa de la provincia de Burgos ni de Castilla y León. Es un checkpoint operativo para decidir el siguiente lote de carga.

## Lote Sprint 1 importado

Se importaron y aprobaron técnicamente para recuperación demo tres nuevos anuncios oficiales BOPBUR. La aprobación sigue siendo técnica/producto para demo; requiere validación jurídica humana antes de uso oficial.

| Municipio | CVE | Tema | Chunks ready/approved | PDF oficial |
|---|---|---|---:|---|
| Belorado | BOPBUR-2025-06332 | Varias ordenanzas y reglamentos | 97 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-245/bopbur-2025-245-anuncio-202506332.pdf |
| Cascajares de la Sierra | BOPBUR-2025-06143 | Limpieza y vallado de solares e inmuebles | 24 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-241/bopbur-2025-241-anuncio-202506143.pdf |
| Cascajares de la Sierra | BOPBUR-2025-06142 | Aprovechamiento de leñas de hogar de montes municipales | 22 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-241/bopbur-2025-241-anuncio-202506142.pdf |

Se dejó fuera del lote la Mancomunidad de Oña-Bureba-Caderechas-Valdivielso hasta decidir explícitamente si el MVP municipal debe incluir entidades supramunicipales.

## Cobertura local en base de datos

El reporte de cobertura agrupa por nombre de municipio para evitar que altas duplicadas locales distorsionen la cobertura lógica.

Resultado tras el lote:

| Métrica | Valor |
|---|---:|
| Provincia | Burgos |
| Municipios lógicos Burgos en DB local | 5 |
| Municipios con ordenanzas aprobadas | 5 |
| Municipios listos para asistente | 5 |
| Ordenanzas totales | 6 |
| Ordenanzas aprobadas | 6 |
| Chunks totales | 146 |
| Chunks `ready` | 146 |
| Chunks `approved` | 146 |
| Chunks con embedding fallido | 0 |
| Fallos de importación detectados | 0 |

Municipios listos para el asistente:

| Municipio | Ordenanzas aprobadas | Chunks ready | Chunks approved |
|---|---:|---:|---:|
| Belorado | 1 | 97 | 97 |
| Cascajares de la Sierra | 2 | 46 | 46 |
| Condado de Treviño | 1 | 1 | 1 |
| Hoyales de Roa | 1 | 1 | 1 |
| Quintanar de la Sierra | 1 | 1 | 1 |

## Smoke semántico

Se ejecutaron búsquedas internas sobre chunks aprobados y listos:

| Consulta | Resultado top |
|---|---|
| `¿Qué dice Belorado sobre la tasa de suministro de agua?` | Belorado — modificación de diversas ordenanzas y reglamentos |
| `limpieza y vallado de solares en Cascajares` | Cascajares de la Sierra — limpieza y vallado de solares e inmuebles |
| `aprovechamiento de leñas de hogar` | Cascajares de la Sierra — aprovechamiento de leñas de hogar de montes municipales |

## Smoke real del conector BOPBUR

Consulta oficial ejecutada:

```text
ordenanza Ayuntamiento basuras, año 2025, límite 5
```

Resultados oficiales devueltos por `search_bop_burgos_announcements`:

| Entidad | CVE | PDF oficial |
|---|---|---|
| Ayuntamiento de Belorado | BOPBUR-2025-06332 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-245/bopbur-2025-245-anuncio-202506332.pdf |
| Mancomunidad de Oña-Bureba-Caderechas-Valdivielso | BOPBUR-2025-06138 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-243/bopbur-2025-243-anuncio-202506138.pdf |
| Ayuntamiento de Cascajares de la Sierra | BOPBUR-2025-06143 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-241/bopbur-2025-241-anuncio-202506143.pdf |
| Ayuntamiento de Cascajares de la Sierra | BOPBUR-2025-06142 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-241/bopbur-2025-241-anuncio-202506142.pdf |
| Diputación Provincial de Burgos | BOPBUR-2025-06100 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-239/bopbur-2025-239-anuncio-202506100.pdf |

## Lectura go/no-go

Estado actual: **GO demo controlada ampliada** para cinco municipios lógicos cargados.

Motivos:

- Hay fuente oficial BOPBUR sembrada.
- Hay 6 ordenanzas reales aprobadas para recuperación demo.
- Todos los chunks del corpus demo están `approved` y `ready`.
- No hay fallos de embeddings ni fallos de importación pendientes en la base local.
- El conector BOPBUR descubre nuevos candidatos reales desde fuente oficial y el importador ya cargó el primer lote Sprint 1.

Limitaciones:

- La cobertura real provincial sigue siendo baja frente al total de municipios de Burgos.
- Algunos anuncios futuros pueden requerir OCR o revisión manual si los PDFs no tienen texto extraíble.
- El anuncio de Belorado contiene varias ordenanzas/reglamentos en un único PDF; para una experiencia jurídica más fina conviene separar subordenanzas o mejorar el modelo de vigencia.
- Las entidades supramunicipales quedan fuera hasta decisión de producto.

## Siguiente lote recomendado

1. Ejecutar búsquedas BOPBUR por temas demo adicionales: `agua`, `residuos`, `terrazas`, `ruido`, `IBI`, `convivencia`.
2. Importar solo ayuntamientos, dejando mancomunidades/diputación fuera salvo decisión explícita.
3. Tras cada lote, ejecutar:
   - `GET /ordinances/coverage/burgos`
   - `POST /ordinances/coverage/burgos/retry-embeddings` si aparecen chunks con `embedding_status='failed'`
   - smoke de `semantic_search_ordinances` para residuos/agua/tasas/convivencia.
