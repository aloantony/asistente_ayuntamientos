# Cobertura MVP Ordenanzas Burgos — ejecución 2026-06-28/29

## Objetivo

Cubrir ordenanzas municipales de toda la provincia de Burgos, priorizando base de datos oficial y recuperable por el asistente.

Esta ejecución amplía el corpus oficial BOPBUR, añade fallback con el repositorio oficial de ordenanzas fiscales de la Diputación Provincial de Burgos, ejecuta OCR sobre PDFs fiscales antiguos y añade fallback por webs municipales oficiales para municipios que seguían sin cobertura.

## Fuentes usadas

1. INE 2025: catálogo oficial de municipios.
2. BOPBUR: boletín oficial provincial.
3. Diputación Burgos: página oficial de ordenanzas fiscales municipales.
4. OCR PyMuPDF + Tesseract español para PDFs antiguos sin texto legible.
5. Webs municipales oficiales con sección `Normativa` cuando se localizaron fuentes verificables.

## Resultado vivo actual

| Métrica | Valor |
|---|---:|
| Municipios oficiales Burgos en catálogo local | 371 |
| Municipios con ordenanzas aprobadas/vectorizadas | 340 |
| Municipios todavía sin cobertura | 31 |
| Ordenanzas Burgos aprobadas | 1.184 |
| Chunks `ready` + `approved` | 20.239 |
| Chunks con embedding fallido | 0 |

## Incremento conseguido

| Momento | Municipios con ordenanzas | Ordenanzas aprobadas | Chunks listos/aprobados |
|---|---:|---:|---:|
| Corpus inicial | 12 | 14 | 535 |
| Tras ampliación BOPBUR | 86 | 164 | 2.548 |
| Tras fallback Diputación fiscal | 246 | 602 | 7.116 |
| Tras OCR fiscal | 322 | 1.139 | 20.123 |
| Tras webs municipales/BOP manual | 340 | 1.184 | 20.239 |

## Trabajos realizados en esta fase adicional

### BOPBUR ampliado para gaps

Se ejecutaron búsquedas BOPBUR más amplias para los 49 municipios que seguían sin cobertura tras OCR:

- `ordenanza <municipio>`
- `ordenanza fiscal <municipio>`
- `reglamento <municipio>`
- `aprobación definitiva ordenanza <municipio>`

Resultado: 0 candidatos nuevos que pasaran filtro estricto de entidad municipal exacta y título normativo.

### Webs municipales oficiales

Se localizaron e importaron secciones oficiales `Normativa` en varias webs municipales. Resultado del job de webs municipales:

- URLs importadas: 14
- Aprobadas: 14
- Fallidas: 0

Además se importó Fuentespina desde su web municipal:

- URLs importadas: 27
- Aprobadas: 27
- Fallidas: 0

### Fallback manual oficial BOP/web municipal

Se añadieron URLs oficiales verificadas manualmente para algunos gaps:

- Quintanilla del Agua y Tordueles: web municipal.
- Carcedo de Bureba: BOPBUR histórico por HTTP + OCR.
- Fresneda de la Sierra Tirón: BOPBUR histórico por HTTP + OCR.
- Villasandino: BOPBUR histórico.

Resultado: 4/4 aprobadas tras OCR en los BOP antiguos que lo requerían.

## Municipios todavía sin cobertura

Quedan 31 municipios sin ordenanza aprobada/vectorizada:

- Barbadillo del Pez
- Castrillo Mota de Judíos
- Cubo de Bureba
- Gumiel de Izán
- Horra, La
- Mambrilla de Castrejón
- Manciles
- Moncalvillo
- Revilla Vallejera
- Revilla y Ahedo, La
- Riocavado de la Sierra
- Rublacedo de Abajo
- San Millán de Lara
- Santa Cruz del Valle Urbión
- Santa María del Mercadillo
- Sequera de Haza, La
- Tobar
- Vallarta de Bureba
- Valle de Valdelaguna
- Vileña
- Villaespasa
- Villalbilla de Burgos
- Villambistia
- Villamiel de la Sierra
- Villanueva de Carazo
- Villanueva de Gumiel
- Villanueva de Teba
- Villaquirán de los Infantes
- Villatuelda
- Villoruebo
- Viloria de Rioja

## Lectura GO/NO-GO

Estado actual: **GO fuerte para demo interna y validación de valor de base de datos**.

Estado actual: **NO-GO todavía para prometer cobertura legal completa y vigente de toda la provincia**.

Sí está hecho:

- Catálogo completo de 371 municipios.
- 340 municipios con contenido legal recuperable por el asistente.
- 1.184 ordenanzas aprobadas/vectorizadas.
- 20.239 chunks legales listos/aprobados.
- BOPBUR + Diputación fiscal + OCR + webs municipales funcionando.
- 0 embeddings fallidos.

No está hecho todavía:

- 31 municipios sin fuente oficial importable localizada automáticamente.
- Falta auditoría jurídica de vigencia/derogación/sustitución.
- Algunas webs oficiales localizadas responden con timeouts persistentes desde entorno Docker/CLI.
- En Diputación, varios municipios figuran con `X` en la tabla fiscal pero sin enlace PDF; no se debe sintetizar una ordenanza sin documento fuente.

## Próximo paso para cierre real 371/371

Para los 31 restantes hace falta búsqueda asistida/manual por fuente primaria:

1. Revisar sedes electrónicas municipales una a una.
2. Consultar portales de transparencia si existen.
3. Revisar BOPBUR histórico por sumarios/PDF completo cuando el buscador no indexa el anuncio individual.
4. Contactar o usar inventario municipal si solo existe marca fiscal `X` sin PDF enlazado en Diputación.
5. Registrar explícitamente `sin fuente oficial localizada` cuando no haya documento, en vez de contaminar el corpus con fuentes no oficiales.
