# Reporte de cobertura Sprint 1 — Ordenanzas Burgos

Fecha de ejecución: 2026-06-27

## Alcance

Este reporte mide el corpus local disponible para el MVP de ordenanzas Burgos y valida que el conector determinista BOPBUR puede descubrir e importar anuncios oficiales desde `bopbur.diputaciondeburgos.es`.

No representa cobertura completa de la provincia de Burgos ni de Castilla y León. Es un checkpoint operativo para decidir los siguientes lotes de carga.

## Lotes Sprint 1 importados

Se importaron y aprobaron técnicamente para recuperación demo anuncios oficiales BOPBUR de ayuntamientos. La aprobación sigue siendo técnica/producto para demo; requiere validación jurídica humana antes de uso oficial.

### Lote inicial

| Municipio | CVE | Tema | Chunks ready/approved | PDF oficial |
|---|---|---|---:|---|
| Belorado | BOPBUR-2025-06332 | Varias ordenanzas y reglamentos | 97 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-245/bopbur-2025-245-anuncio-202506332.pdf |
| Cascajares de la Sierra | BOPBUR-2025-06143 | Limpieza y vallado de solares e inmuebles | 24 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-241/bopbur-2025-241-anuncio-202506143.pdf |
| Cascajares de la Sierra | BOPBUR-2025-06142 | Aprovechamiento de leñas de hogar de montes municipales | 22 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-241/bopbur-2025-241-anuncio-202506142.pdf |

### Lote temático agua/residuos/terrazas/convivencia

| Municipio | CVE | Tema | Chunks ready/approved | PDF oficial |
|---|---|---|---:|---|
| Ibeas de Juarros | BOPBUR-2025-06501 | Agua potable y saneamiento | 1 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-245/bopbur-2025-245-anuncio-202506501.pdf |
| Medina de Pomar | BOPBUR-2025-06455 | Dominio público y tasas de redes | 35 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-245/bopbur-2025-245-anuncio-202506455.pdf |
| Estépar | BOPBUR-2025-06226 | Conservación de caminos rurales | 14 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-244/bopbur-2025-244-anuncio-202506226.pdf |
| Burgos | BOPBUR-2025-05968 | Terrazas de hostelería | 200 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-236/bopbur-2025-236-anuncio-202505968.pdf |
| Burgos | BOPBUR-2026-00782 | Tasa por ocupación del dominio público con terrazas | 30 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2026-041/bopbur-2026-041-anuncio-202600782.pdf |
| Miranda de Ebro | BOPBUR-2025-06257 | Modificación de ordenanzas fiscales | 1 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-241/bopbur-2025-241-anuncio-202506257.pdf |
| Arlanzón | BOPBUR-2025-04798 | Tenencia y protección de animales | 54 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-192/bopbur-2025-192-anuncio-202504798.pdf |
| Quemada | BOPBUR-2025-05899 | Vertidos de aguas residuales | 54 | http://bopbur.diputaciondeburgos.es/sites/default/files/private/publicado/bopbur-2025-234/bopbur-2025-234-anuncio-202505899.pdf |

Se dejaron fuera del lote las mancomunidades/diputación y anuncios no normativos o solo provisionales, salvo el documento BOPBUR 2026 enlazado por la importación de terrazas de Burgos, que se aprobó técnicamente porque es fuente oficial y materia directamente relacionada.

## Cobertura local en base de datos

El reporte de cobertura agrupa por nombre de municipio para evitar que altas duplicadas locales distorsionen la cobertura lógica.

Resultado tras los lotes:

| Métrica | Valor |
|---|---:|
| Provincia | Burgos |
| Municipios lógicos Burgos en DB local | 12 |
| Municipios con ordenanzas aprobadas | 12 |
| Municipios listos para asistente | 12 |
| Ordenanzas totales | 14 |
| Ordenanzas aprobadas | 14 |
| Chunks totales | 535 |
| Chunks `ready` | 535 |
| Chunks `approved` | 535 |
| Chunks con embedding fallido | 0 |
| Fallos de importación detectados | 0 |

Municipios listos para el asistente:

| Municipio | Ordenanzas aprobadas | Chunks ready | Chunks approved |
|---|---:|---:|---:|
| Arlanzón | 1 | 54 | 54 |
| Belorado | 1 | 97 | 97 |
| Burgos | 2 | 230 | 230 |
| Cascajares de la Sierra | 2 | 46 | 46 |
| Condado de Treviño | 1 | 1 | 1 |
| Estépar | 1 | 14 | 14 |
| Hoyales de Roa | 1 | 1 | 1 |
| Ibeas de Juarros | 1 | 1 | 1 |
| Medina de Pomar | 1 | 35 | 35 |
| Miranda de Ebro | 1 | 1 | 1 |
| Quemada | 1 | 54 | 54 |
| Quintanar de la Sierra | 1 | 1 | 1 |

## Smoke semántico

Se ejecutaron búsquedas internas sobre chunks aprobados y listos:

| Consulta | Resultado top |
|---|---|
| `¿Qué dice Belorado sobre la tasa de suministro de agua?` | Belorado — modificación de diversas ordenanzas y reglamentos |
| `limpieza y vallado de solares en Cascajares` | Cascajares de la Sierra — limpieza y vallado de solares e inmuebles |
| `aprovechamiento de leñas de hogar` | Cascajares de la Sierra — aprovechamiento de leñas de hogar de montes municipales |
| `suministro de agua potable y saneamiento Ibeas` | Ibeas de Juarros — agua potable y saneamiento |
| `ocupación de dominio público con terrazas hostelería Burgos` | Burgos — ordenanza de terrazas de hostelería |
| `tenencia y protección de animales Arlanzón` | Arlanzón — tenencia y protección de animales |
| `vertidos de aguas residuales Quemada` | Quemada — vertidos de aguas residuales |
| `conservación de caminos rurales Estépar` | Estépar — conservación de caminos rurales |

Observación corregida: Miranda de Ebro queda cargado y aprobado. El smoke genérico `IBI ordenanzas fiscales Miranda de Ebro` no devolvía Miranda como top con el embedding local determinista; al aplicar filtros estructurados `municipality_name=Miranda de Ebro` y `topic=ordenanzas fiscales`, la búsqueda devuelve el documento de Miranda. La demo debe pasar municipio/materia como filtros cuando el usuario los mencione.

## Smoke real del conector BOPBUR

Consulta oficial ejecutada:

```text
ordenanza Ayuntamiento agua/residuos/terrazas/ruido/IBI/convivencia, año 2025
```

Candidatos oficiales útiles detectados por `search_bop_burgos_announcements`:

| Entidad | CVE | Decisión |
|---|---|---|
| Ayuntamiento de Ibeas de Juarros | BOPBUR-2025-06501 | Importado |
| Ayuntamiento de Medina de Pomar | BOPBUR-2025-06455 | Importado |
| Ayuntamiento de Estépar | BOPBUR-2025-06226 | Importado |
| Ayuntamiento de Burgos | BOPBUR-2025-05968 | Importado |
| Ayuntamiento de Miranda de Ebro | BOPBUR-2025-06257 | Importado |
| Ayuntamiento de Arlanzón | BOPBUR-2025-04798 | Importado |
| Ayuntamiento de Quemada | BOPBUR-2025-05899 | Importado |
| Mancomunidad de Oña-Bureba-Caderechas-Valdivielso | BOPBUR-2025-06138 | Fuera por ser entidad supramunicipal |
| Diputación Provincial de Burgos | BOPBUR-2025-06100 | Fuera por no ser ayuntamiento |

## Lectura go/no-go

Estado actual: **GO demo controlada ampliada** para doce municipios lógicos cargados.

Motivos:

- Hay fuente oficial BOPBUR sembrada.
- Hay 14 ordenanzas reales aprobadas para recuperación demo.
- Todos los chunks del corpus demo están `approved` y `ready`.
- No hay fallos de embeddings ni fallos de importación pendientes en la base local.
- El conector BOPBUR descubre nuevos candidatos reales desde fuente oficial y el importador ya cargó dos lotes Sprint 1.

Limitaciones:

- La cobertura real provincial sigue siendo baja frente al total de municipios de Burgos.
- Algunos anuncios futuros pueden requerir OCR o revisión manual si los PDFs no tienen texto extraíble.
- Algunos anuncios contienen varias ordenanzas/reglamentos en un único PDF; para una experiencia jurídica más fina conviene separar subordenanzas o mejorar el modelo de vigencia.
- Las entidades supramunicipales quedan fuera hasta decisión de producto.
- Esta instantánea histórica usó `local_hash`; desde 2026-07-17 se clasifica
  expresamente como ayuda léxica de desarrollo, no como ranking semántico ni
  como prueba de cobertura temática. Producción requiere un proveedor semántico
  aprobado y filtros estructurados cuando el usuario menciona municipio/materia.

## Siguiente lote recomendado

1. Añadir filtros estructurados de municipio/materia a la herramienta de consulta de ordenanzas para que el ranking no dependa solo del vector local.
2. Ejecutar otro lote BOPBUR por materias: `animales`, `caminos`, `cementerio`, `convivencia`, `ocupación vía pública`, `saneamiento`.
3. Importar solo ayuntamientos, dejando mancomunidades/diputación fuera salvo decisión explícita.
4. Tras cada lote, ejecutar:
   - `GET /ordinances/coverage/burgos`
   - `POST /ordinances/coverage/burgos/retry-embeddings` si aparecen chunks con `embedding_status='failed'`
   - smoke de `semantic_search_ordinances` con filtro de municipio/materia cuando esté disponible.
