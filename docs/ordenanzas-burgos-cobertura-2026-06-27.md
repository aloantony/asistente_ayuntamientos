# Reporte de cobertura Sprint 1 — Ordenanzas Burgos

Fecha de ejecución: 2026-06-27

## Alcance

Este reporte mide el corpus local disponible para el MVP de ordenanzas Burgos y valida que el nuevo conector determinista BOPBUR puede descubrir anuncios oficiales desde `bopbur.diputaciondeburgos.es`.

No representa cobertura completa de la provincia de Burgos ni de Castilla y León. Es un checkpoint operativo para decidir el siguiente lote de carga.

## Cobertura local en base de datos

Comando ejecutado desde el worktree `mvp-ordenanzas-burgos`, contra la base local `app`:

```bash
docker run --rm --network host \
  -v "$PWD:/app" \
  -w /app \
  -e PYTHONPATH=/app \
  -e DATABASE_URL='postgresql+psycopg://app:app@127.0.0.1:5432/app' \
  python:3.12-slim \
  sh -c 'pip install --no-cache-dir -r requirements.txt >/tmp/pip.log && python <coverage-script>'
```

Resultado:

| Métrica | Valor |
|---|---:|
| Provincia | Burgos |
| Municipios Burgos en DB local | 5 |
| Municipios con ordenanzas aprobadas | 3 |
| Municipios listos para asistente | 3 |
| Ordenanzas totales | 3 |
| Ordenanzas aprobadas | 3 |
| Chunks totales | 3 |
| Chunks `ready` | 3 |
| Chunks `approved` | 3 |
| Chunks con embedding fallido | 0 |
| Fallos de importación detectados | 0 |

Municipios listos para el asistente:

| Municipio | Ordenanzas aprobadas | Chunks ready | Chunks approved |
|---|---:|---:|---:|
| Condado de Treviño | 1 | 1 | 1 |
| Hoyales de Roa | 1 | 1 | 1 |
| Quintanar de la Sierra | 1 | 1 | 1 |

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

Estado actual: **GO demo controlada** para los tres municipios ya cargados.

Motivos:

- Hay fuente oficial BOPBUR sembrada.
- Hay 3 ordenanzas reales aprobadas para recuperación demo.
- Todos los chunks del corpus demo están `approved` y `ready`.
- No hay fallos de embeddings ni fallos de importación pendientes en la base local.
- El conector BOPBUR descubre nuevos candidatos reales desde fuente oficial.

Limitaciones:

- Solo 3 de los 5 municipios Burgos presentes en DB local tienen corpus aprobado.
- La cobertura real provincial sigue siendo muy baja.
- Los resultados nuevos descubiertos todavía no están importados/aprobados en DB.
- Algunos anuncios pueden requerir OCR o revisión manual si los PDFs no tienen texto extraíble.

## Siguiente lote recomendado

1. Importar y revisar Belorado (`BOPBUR-2025-06332`) como candidato amplio de varias ordenanzas/reglamentos.
2. Importar y revisar Cascajares de la Sierra (`BOPBUR-2025-06143` y `BOPBUR-2025-06142`) para probar temas no estrictamente fiscales.
3. Decidir si la Mancomunidad de Oña-Bureba-Caderechas-Valdivielso entra en el MVP municipal o se deja fuera por no ser ayuntamiento.
4. Tras cada lote, ejecutar:
   - `GET /ordinances/coverage/burgos`
   - `POST /ordinances/coverage/burgos/retry-embeddings` si aparecen chunks con `embedding_status='failed'`
   - smoke de `semantic_search_ordinances` para residuos/agua/tasas.
