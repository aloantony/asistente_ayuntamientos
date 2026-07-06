# Cobertura MVP Ordenanzas Soria — ejecución 2026-06-29

## Objetivo

Primer sprint de expansión tras Burgos dentro de Castilla y León. Objetivo: reutilizar el patrón de cobertura provincial con fuentes oficiales, sin crear ordenanzas sintéticas ni asociar resultados por coincidencia débil.

## Fuentes usadas

1. INE 2025: catálogo oficial de municipios.
2. BOP Soria (`bop.dipsoria.es`): boletín oficial provincial.
3. Buscador oficial BOP Soria (`mod.boloficial/mem.buscadorbop`).

No se usaron fuentes no oficiales. La página de Diputación Soria localizada de ordenanzas fiscales corresponde a ordenanzas provinciales, no a repositorio municipal equivalente al de Burgos, por lo que no se incorporó como cobertura municipal.

## Resultado vivo actual

| Métrica | Valor |
|---|---:|
| Municipios oficiales Soria en catálogo local | 183 |
| Municipios con ordenanzas aprobadas/vectorizadas | 130 |
| Municipios todavía sin cobertura | 53 |
| Ordenanzas Soria aprobadas | 558 |
| Chunks `ready` + `approved` | 3.437 |
| Chunks con embedding fallido | 0 |

## Implementación

Se añadió el runner:

- `backend/app/ordinances/soria_coverage.py`

Capacidades:

- descarga y parsea el diccionario oficial INE 2025;
- crea/actualiza municipios de Soria por código INE;
- registra BOP Soria como fuente oficial;
- consulta el buscador oficial del BOP;
- parsea páginas de detalle/sumario;
- filtra por sección municipal (`AYUNTAMIENTOS`) y excluye entidades no municipales;
- exige título normativo (`ordenanza`, `reglamento`, `norma urbanística`, etc.);
- crea jobs de importación con `municipality_id` por URL;
- aprueba técnicamente para recuperación en demo con nota de revisión jurídica humana.

También se ajustó el importador común:

- `backend/app/ordinances/import_service.py`

Cambio:

- `bop.dipsoria.es` usa una excepción SSL acotada al host, porque el endpoint oficial de PDFs falla con la cadena de certificados por defecto dentro del contenedor Python. No se desactiva verificación globalmente.

## Jobs ejecutados

### Jobs útiles finales

- Job: `28`
- Título: `MVP Ordenanzas Soria - BOP Soria ordenanzas municipales`
- Candidatos descubiertos: 232
- Municipios con candidatos: 84
- Items aprobados: 232
- Duplicados: 0
- Fallidos: 0
- Chunks listos: 1.244

- Job: `29`
- Título: `MVP Ordenanzas Soria - BOP Soria ordenanzas municipales`
- Items aprobados nuevos: 326
- Duplicados detectados: 232
- Fallidos: 0

Resultado combinado: 558 ordenanzas aprobadas para 130 municipios.

### Jobs descartables/previos

- Job `26`/`27`: intentos anteriores fallidos por certificado SSL antes de acotar la excepción para BOP Soria.

## Límites actuales

El lote inicial usó el buscador oficial BOP Soria con `max-results-per-query=200`. Un segundo pase con `500` amplió la cobertura y cerró correctamente con duplicados detectados. Intentar `1000` resultados por consulta se volvió demasiado lento por el coste de recorrer páginas de detalle oficiales una a una.

Quedan 53 municipios sin cobertura. El siguiente paso de mayor valor es un segundo pase orientado a gaps:

1. consultas BOP por municipio restante;
2. webs/sedes municipales oficiales;
3. portales de transparencia;
4. OCR solo si aparecen PDFs escaneados fallidos.

## Veredicto

Estado actual: **GO para demo interna Soria v1**.

Estado actual: **NO-GO para prometer cobertura completa de todas las ordenanzas de todos los municipios de Soria**.

Motivo: hay 130/183 municipios cubiertos con 558 documentos oficiales; faltan 53 municipios y auditoría de vigencia jurídica.
