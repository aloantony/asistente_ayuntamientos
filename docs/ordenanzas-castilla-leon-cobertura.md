# Cobertura de ordenanzas Castilla y León

Fecha de revisión local: 2026-07-01.

## Estado observado

- Fuentes oficiales activas en la DB antes de este cambio: BOE, BOP Burgos, BOP Soria, una fuente provincial de Burgos (`burgos.es`) y varias webs municipales de Burgos.
- Municipios Castilla y León presentes en la DB local: Burgos 374, Soria 183, Valladolid 1.
- Ordenanzas aprobadas presentes: Burgos 1184, Soria 615.
- Importaciones fallidas presentes antes de filtrar fallos ya resueltos: Burgos 5, Soria 1397.

Conclusión: Burgos y Soria sí tienen corpus cargado, pero la base no contiene todavía todos los municipios ni todas las ordenanzas existentes de Castilla y León.

## Inicio de importación Soria 2026-07-01

El BOP de Soria redirige documentos de `http://bop.dipsoria.es` a HTTPS con una cadena TLS que no valida en el runtime Python estándar. Se añadió una excepción acotada al dominio oficial `bop.dipsoria.es` después de validar la URL contra fuentes oficiales.

Jobs ejecutados:

| Job | Resultado |
| --- | --- |
| 32 | Piloto de 5 URLs: 5 duplicadas, sin fallos nuevos. |
| 33 | Piloto de 10 URLs sin ordenanza existente: 10 importadas como `pending_review`. |
| 34 | Lote de 50 URLs sin ordenanza existente: 50 importadas como `pending_review`. |

Estado tras los lotes:

- Soria tiene 615 ordenanzas aprobadas y 60 nuevas ordenanzas pendientes de revisión humana.
- Chunks de Soria: 4381 totales/listos, 3696 aprobados.
- Fallos accionables de Soria en el reporte de cobertura: 510. Los fallos históricos cuya URL ya tiene ordenanza importada quedan fuera del reporte operativo.

## Fuentes provinciales sembradas

`backend/app/ordinances/seed.py` siembra de forma idempotente estas fuentes:

| Provincia | Dominio permitido | URL base |
| --- | --- | --- |
| Ávila | `diputacionavila.es` | `https://www.diputacionavila.es/boletin-oficial/` |
| Burgos | `bopbur.diputaciondeburgos.es` | `https://bopbur.diputaciondeburgos.es/` |
| León | `bop.dipuleon.es` | `https://bop.dipuleon.es/` |
| Palencia | `diputaciondepalencia.es` | `https://www.diputaciondepalencia.es/servicios/boletin-oficial-provincia` |
| Salamanca | `diputaciondesalamanca.gob.es` | `https://sede.diputaciondesalamanca.gob.es/BOP/` |
| Segovia | `dipsegovia.es` | `https://www.dipsegovia.es/bop` |
| Soria | `bop.dipsoria.es` | `https://bop.dipsoria.es/` |
| Valladolid | `diputaciondevalladolid.es` | `https://bop.sede.diputaciondevalladolid.es/` |
| Zamora | `diputaciondezamora.es` | `https://www.diputaciondezamora.es/opencms/servicios/BOP/bop/index.html` |

## Operativa

- Cobertura global: `GET /ordinances/coverage/castilla-y-leon`.
- Cobertura por provincia: `GET /ordinances/coverage/provinces/{province}`.
- Alias histórico Burgos: `GET /ordinances/coverage/burgos`.
- Reintento de embeddings por provincia: `POST /ordinances/coverage/provinces/{province}/retry-embeddings`.
- Alias histórico Burgos: `POST /ordinances/coverage/burgos/retry-embeddings`.

Para completar el objetivo real faltan dos líneas de trabajo separadas: cargar el censo completo de municipios de Castilla y León y crear/importar lotes por provincia desde las fuentes oficiales sembradas.
