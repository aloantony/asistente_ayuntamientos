# Orquestación MVP: ordenanzas municipales de la provincia de Burgos

> **Para Hermes:** ejecutar esta orquestación con agentes/subagentes independientes y revisión final antes de tocar la demo. No asumir cobertura completa: medirla en base de datos y bloquear la demo si el agente no puede consultar lo importado.

**Goal:** Tener las ordenanzas municipales de los pueblos de la provincia de Burgos localizadas, importadas en PostgreSQL, troceadas, vectorizadas y disponibles para el asistente del MVP antes de la primera prueba con alcalde.

**Architecture:** Fan-out/fan-in: investigación de fuentes, preparación de dataset, hardening del pipeline, integración con asistente y validación corren en paralelo cuando no dependen entre sí. La base técnica existente ya incluye `Ordinance`, `OrdinanceImportJob`, `OrdinanceImportItem`, `OrdinanceLegalChunk`, embeddings y endpoints de comparación/búsqueda semántica; falta convertirlo en operación masiva, medible y accesible desde el agente conversacional.

**Tech Stack:** FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 17 con imagen `pgvector/pgvector:pg17`, Redis/RQ worker, Next.js, embeddings `local_hash` u OpenAI-compatible según `.env`.

---

## Contexto comprobado en el repo

- Rama activa al preparar el plan: `redesign/dashboard-shell`.
- Árbol ya estaba sucio antes de este plan: cambios en backend, frontend, docs, README y `.hermes/` sin trackear. No se deben mezclar con la ejecución de esta orquestación.
- Perfiles Hermes disponibles: `default`, `ayuntamientosdev`, `web`.
- Infraestructura existente:
  - Modelos: `backend/app/ordinances/models.py`.
  - Importación: `backend/app/ordinances/import_service.py`.
  - Embeddings: `backend/app/ordinances/embeddings.py`.
  - API: `backend/app/ordinances/routes.py`.
  - Tests: `backend/tests/test_ordinances.py`.
  - Worker RQ: `backend/app/worker.py`, `docker-compose.yml`.
- Gaps críticos detectados por inspección inicial:
  - Las herramientas del asistente (`backend/app/assistant/tools.py`) no exponen todavía búsqueda/consulta de ordenanzas, aunque exista `/ordinances/semantic-search`.
  - La búsqueda semántica carga hasta 500 chunks y calcula similitud en Python; puede ser insuficiente para todo Castilla y León.
  - `official_legal_sources` solo se siembra con BOE; para Castilla y León faltan BOCyL/BOP provinciales y posiblemente sedes municipales.
  - Los embeddings se guardan como texto; la imagen de Postgres trae pgvector, pero no hay índice/vector column operativo en el modelo actual.
  - No hay script/runbook visible para importación masiva ni métricas de cobertura.
  - Los PDF escaneados fallan con “requiere OCR o revisión manual”; hay que decidir contingencia para fuentes sin texto extraíble.

## Alcance corregido del MVP

El objetivo del primer MVP se reduce de “toda Castilla y León” a **los pueblos de la provincia de Burgos**.

Implicaciones:

- La fuente primaria pasa a ser el BOP de Burgos: `bopbur.diputaciondeburgos.es`.
- BOCyL y BOE quedan como apoyo jurídico, no como objetivo de cobertura masiva.
- Las sedes municipales de pueblos de Burgos se usan solo como segunda fuente cuando haga falta texto consolidado o verificación de vigencia.
- La métrica principal de cobertura será por municipio de Burgos, no por las 9 provincias de Castilla y León.
- Castilla y León completa queda como fase posterior; antes hay que cerrar un circuito sólido: fuente oficial → importación → revisión → chunks → embeddings → herramienta conversacional.

## Principio de entrega MVP

Para la prueba del alcalde, el objetivo operativo no es “tener un scraper perfecto”, sino que el producto pueda demostrar con seguridad:

1. Qué cobertura normativa tiene cargada.
2. De qué fuente oficial viene cada texto.
3. Qué está aprobado para uso del agente y qué queda pendiente de revisión.
4. Que el agente puede recuperar fragmentos citables sin inventar normativa.
5. Que cualquier ausencia se explica como “pendiente de cobertura/revisión”, no como una respuesta fabricada.

## Agentes ya disparados en paralelo

Se han lanzado 3 subagentes read-only para acelerar el diagnóstico:

1. **Mapa de fuentes CyL** (`web`): dominios/base URLs de BOP provinciales, BOCyL, BOE y sedes municipales; estrategia de cobertura.
2. **Auditor de pipeline backend** (`file`, `terminal`): gaps críticos en importación, embeddings, búsqueda e integración con el asistente.
3. **Validador go/no-go** (`file`, `terminal`): métricas SQL, smoke tests API/UI y criterios de demo.

Cuando vuelvan sus resultados, incorporar hallazgos a este plan antes de ejecutar la fase de cambios.

## Orquestación propuesta

### A0 — Coordinación git/worktree

**Objetivo:** aislar el trabajo para no pisar cambios actuales.

**Responsable:** agente coordinador.

**Pasos:**
1. Ejecutar:
   ```bash
   pwd
   git status --short --branch
   git remote -v
   git branch --show-current
   ```
2. Crear worktree dedicado desde la rama de integración acordada, salvo que el usuario confirme trabajar sobre el checkout sucio:
   ```bash
   mkdir -p /home/dev/proyectos/asistente_ayuntamientos-worktrees
   git worktree add \
     /home/dev/proyectos/asistente_ayuntamientos-worktrees/mvp-ordenanzas-cyl \
     -b codex/mvp-ordenanzas-cyl-20260626 \
     origin/redesign/dashboard-shell
   ```
3. Ejecutar el resto desde:
   `/home/dev/proyectos/asistente_ayuntamientos-worktrees/mvp-ordenanzas-cyl`.

**Criterio de aceptación:** worktree limpio, rama `codex/mvp-ordenanzas-cyl-20260626`, sin tocar cambios no relacionados del checkout principal.

### A1 — Cartografía de fuentes oficiales

**Objetivo:** producir el catálogo mínimo de fuentes oficiales para Castilla y León.

**Responsable:** agente `web` o subagente con web.

**Entregables:**
- Tabla `provincia -> boletín oficial -> dominio -> buscador/API/RSS -> patrón de consulta`.
- BOCyL y BOE como fuentes transversales.
- Lista de municipios sin fuente directa si dependen solo de BOP.
- Riesgo por PDF escaneado/no indexado.

**Criterios de aceptación:** cada fuente debe ser un dominio oficial compatible con `OfficialLegalSource.domain`; no mezclar blogs, prensa ni agregadores no oficiales.

### A2 — Catálogo de municipios Castilla y León

**Objetivo:** confirmar que la tabla `municipalities` contiene todos los municipios activos de Castilla y León y sus provincias.

**Responsable:** agente backend/datos.

**Archivos probables:**
- `backend/app/municipalities/models.py`
- `backend/app/municipalities/routes.py`
- posible nuevo script bajo `backend/scripts/` si no existe semilla.

**Consultas de validación:**
```sql
select province, count(*)
from municipalities
where autonomous_community ilike 'Castilla y León'
  and status = 'active'
group by province
order by province;
```

**Criterio de aceptación:** conteo por las 9 provincias; desviaciones documentadas antes de importar.

### A3 — Preparar fuentes oficiales en DB

**Objetivo:** insertar/actualizar `official_legal_sources` con fuentes activas para BOE, BOCyL y BOP provinciales.

**Responsable:** agente backend.

**Archivos probables:**
- `backend/alembic/versions/...` si se decide sembrar nuevas fuentes por migración.
- Alternativa operativa: script idempotente bajo `backend/scripts/seed_cyl_official_sources.py`.

**Criterio de aceptación:**
```sql
select source_type, name, domain, status
from official_legal_sources
where status = 'active'
order by source_type, name;
```
Debe incluir BOCyL/BOP relevantes.

### A4 — Importación masiva por lotes

**Objetivo:** crear jobs de importación por provincia/tema/municipio con semillas o búsqueda oficial, no un job monolítico opaco.

**Responsable:** agente backend/datos.

**Archivos probables:**
- `backend/app/ordinances/import_service.py`
- posible `backend/scripts/create_cyl_ordinance_import_jobs.py`
- posible `backend/scripts/run_cyl_ordinance_import_batch.py`

**Estrategia:**
- Lote mínimo viable: dividir por provincia y fuente (`BOP Ávila`, `BOP Burgos`, etc.).
- `review_criteria` estándar: fuente oficial, municipio identificado, texto legal suficiente, título de ordenanza/reglamento, fecha si está disponible.
- Evitar depender solo de búsqueda web general. Priorizar índices oficiales y URLs semilla exportables.
- Ejecutar con RQ worker para lotes reales; `run-inline` solo para pruebas pequeñas.

**Criterio de aceptación:** jobs creados con `status in ('queued','running','completed')`, items con trazabilidad a fuente, duplicados identificados por `source_hash`.

### A5 — OCR/contingencia de PDFs no textuales

**Objetivo:** decidir qué hacer con PDFs escaneados antes de que bloqueen cobertura.

**Responsable:** agente backend + producto.

**Opciones MVP:**
1. Marcar `failed` con error claro y generar reporte de pendientes.
2. Añadir OCR local si está disponible y es seguro para datos públicos oficiales.
3. Priorizar fuentes HTML/text/PDF con texto y dejar OCR como backlog, pero mostrar brecha de cobertura.

**Criterio de aceptación:** ningún fallo de OCR queda invisible; hay métrica y listado exportable.

### A6 — Vectorización lista para producción MVP

**Objetivo:** asegurar que todo chunk aprobado o pendiente usable tenga embedding `ready`.

**Responsable:** agente backend.

**Archivos probables:**
- `backend/app/ordinances/embeddings.py`
- `backend/app/ordinances/import_service.py`
- `backend/app/ordinances/routes.py`
- migración si se cambia a columna `vector` real.

**Mínimo MVP aceptable:**
- `embedding_status='ready'` para los chunks que el agente vaya a usar.
- Métricas por municipio/provincia/estado.
- Si sigue con vector en texto + similitud Python, limitar demo a subconjunto cargado y documentar deuda.

**Recomendación si da tiempo:** mover `OrdinanceLegalChunk.embedding` a pgvector real o añadir columna paralela `embedding_vector` con índice, porque el límite actual de 500 chunks en `/semantic-search` no escala a toda Castilla y León.

**Consultas de validación:**
```sql
select embedding_status, count(*)
from ordinance_legal_chunks
group by embedding_status;

select m.province, count(distinct o.id) ordinances, count(c.id) chunks
from ordinances o
join municipalities m on m.id = o.municipality_id
left join ordinance_legal_chunks c on c.ordinance_id = o.id
where m.autonomous_community ilike 'Castilla y León'
group by m.province
order by m.province;
```

### A7 — Integración con el agente conversacional

**Objetivo:** que el agente pueda consultar ordenanzas vectorizadas desde chat, con citas y sin exponer detalles internos.

**Responsable:** agente backend assistant.

**Archivos probables:**
- `backend/app/assistant/tools.py`
- `backend/app/assistant/agents.py`
- `backend/app/assistant/service.py`
- `backend/tests/test_assistant.py`

**Cambios esperados:**
- Añadir herramienta read-only tipo `search_ordinances` o `search_legal_chunks` con dominio `ordinances`/`documents` según convención final.
- Permiso requerido: probablemente `ordinances.compare` o uno nuevo `ordinances.view/search` si se define.
- Respuesta del tool: título, municipio, cita, fragmento, fuente URL, score, estado de curación.
- Prompt del agente: si no hay resultados, decir que no hay cobertura suficiente; no inventar.

**Tests mínimos:**
- El agente de consulta solo puede usar herramientas read-only.
- Pregunta legal recupera fragmento de `OrdinanceLegalChunk` aprobado.
- Sin permiso, no consulta ordenanzas.
- Sin resultados, respuesta honesta de ausencia/cobertura.

### A8 — Panel/admin y smoke funcional

**Objetivo:** que un operador pueda ver jobs, revisar imports y comprobar búsquedas antes de la demo.

**Responsable:** agente frontend/backend según necesidad.

**Archivos probables:**
- `frontend/app/components/OrdinancesAdmin.tsx`
- `frontend/app/lib/admin/useOrdinancesAdmin.ts`
- `frontend/app/lib/api.ts`

**MVP mínimo:** si no se implementa UI completa de import jobs, preparar runbook de API/SQL y asegurar que el listado de ordenanzas muestra los datos finales. Para demo con alcalde, no enseñar detalles internos de importación salvo que se pida.

### A9 — Validación go/no-go

**Objetivo:** decidir si el alcalde puede probar con datos reales o si hay que acotar el mensaje de demo.

**Responsable:** agente QA/ops.

**Checks:**
```bash
# Backend tests
cd backend
pytest tests/test_ordinances.py -q
pytest tests/test_assistant.py -q

# Frontend build si se toca UI
cd frontend
npm run build

# Live app si se toca UI o backend para demo
cd /home/dev/proyectos/asistente_ayuntamientos
docker compose build backend worker frontend
docker compose up -d backend worker frontend
curl -I --max-time 10 http://127.0.0.1:3000
curl --max-time 10 http://127.0.0.1:8000/health
```

**Métricas go/no-go:**
- Municipios CyL activos por provincia presentes.
- Fuentes oficiales activas cargadas.
- Jobs completados vs fallidos.
- Ordenanzas por provincia y municipio.
- Chunks por `embedding_status` y `review_status`.
- Búsqueda semántica devuelve resultados citables para 5 consultas de prueba: residuos, agua, terrazas/ocupación vía pública, IBI/tasas, ruido/convivencia.
- Chat del asistente responde usando las ordenanzas o reconoce falta de cobertura.

## Orden de ejecución recomendado

1. A0 crear worktree.
2. A1 y A2 en paralelo.
3. A3 al terminar A1.
4. A4 y A5 tras A2/A3.
5. A6 en paralelo con A4, pero verificación final tras los imports.
6. A7 en cuanto exista al menos un dataset piloto con chunks.
7. A8 solo si la demo necesita UI operativa de importación; si no, posponer.
8. A9 siempre al final.

## Criterios de aceptación finales

- DB contiene ordenanzas de Castilla y León con fuente oficial, municipio, texto y chunks.
- `ordinance_legal_chunks.embedding_status='ready'` para los chunks aprobados/consultables.
- El asistente tiene una herramienta read-only para buscar normativa y citar fuente/fragmento.
- Hay reporte de cobertura y fallos por provincia/municipio/fuente.
- Tests backend relevantes pasan.
- Si se toca frontend o backend de demo, Docker Compose queda reconstruido y verificado.

## Riesgos y mitigaciones

- **Cobertura total antes de demo puede ser irrealista:** priorizar provincias/municipios del alcalde y temas frecuentes, pero mostrar métricas honestas.
- **Fuentes oficiales heterogéneas:** tratar cada BOP como conector/lote independiente.
- **PDF escaneado:** no bloquear todo el pipeline; registrar pendientes OCR.
- **Escalado semántico:** el límite de 500 chunks puede ocultar resultados. Para MVP pequeño sirve; para “toda Castilla y León” requiere índice vectorial real o prefiltrado por municipio/provincia/tema.
- **Privacidad/egress:** las ordenanzas son públicas, pero mantener la regla del proyecto: documentos originales internos no salen; IA externa solo por gateway.
- **Demo UX:** no exponer “agentes internos” ni detalles de importación al alcalde. Mostrar capacidad conversacional y citas de normativa.

## Handoff esperado de cada agente

Cada agente debe devolver:
- Qué ha comprobado realmente.
- Archivos tocados o propuestos.
- Comandos ejecutados y resultado.
- Datos/métricas verificables.
- Bloqueadores y decisión recomendada: continuar, acotar demo o bloquear.

## Resultados de los subagentes incorporados

Los 3 subagentes read-only terminaron y confirman que la orquestación debe tratarse como una entrega de datos + producto, no solo como scraping.

### Fuentes oficiales CyL priorizadas

Fuente primaria para el MVP: BOP provinciales. BOCyL y BOE son complementarias.

| Provincia | Dominio/base oficial | Prioridad técnica | Notas de ingestión |
|---|---|---:|---|
| Valladolid | `bop.sede.diputaciondevalladolid.es` | 1 | Portal moderno, histórico desde 15/12/2005, PDFs con patrón estable; buen primer conector. |
| Soria | `bop.dipsoria.es` | 2 | Anuncios desglosados y enlaces de descarga; favorable para scraping por índice. |
| Zamora | `diputaciondezamora.es` | 3 | Índices BOP y PDFs de anuncios con patrón claro. |
| Segovia | `dipsegovia.es` | 4 | Búsqueda contextual y documentos individuales en `documents/39512`. |
| Burgos | `bopbur.diputaciondeburgos.es` | 5 | Buscador oficial y PDFs por anuncio; requiere adaptar estructura Drupal/private. |
| Palencia | `diputaciondepalencia.es` | 6 | PDFs desde 2003; probablemente extracción desde boletín completo. |
| Ávila | `diputacionavila.es` | 7 | PDFs por fecha/anuncio; requiere rastreo por calendario. |
| León | `bop.dipuleon.es` | 8 | Cambio de plataforma en 2025; tratar como dos conectores. |
| Salamanca | `sede.diputaciondesalamanca.gob.es` | 9 | Portal OpenCMS; requiere validación técnica adicional de formularios/HTML. |

Fuentes transversales:

- BOCyL: `bocyl.jcyl.es`, útil para urbanismo, entidades locales y disposiciones autonómicas relacionadas; no sustituye a BOP.
- BOE: `boe.es`, útil como fuente jurídica auxiliar; no es fuente primaria de ordenanzas municipales.

Consultas base para conectores/buscadores:

```text
"ordenanza" AND "Ayuntamiento de"
"aprobación definitiva" AND "ordenanza"
"queda elevado a definitivo" AND "ordenanza"
"modificación de la ordenanza"
"ordenanza fiscal"
"tasa" AND "ordenanza"
"precio público" AND "ordenanza"
"reglamento" AND "Ayuntamiento"
"texto íntegro" AND "ordenanza"
```

### Gaps críticos confirmados

1. `official_legal_sources` solo contiene BOE por migración; cualquier URL BOP/BOCyL será rechazada hasta sembrar fuentes CyL.
2. La importación depende de URLs semilla o Hermes Web; no hay conectores deterministas por BOP.
3. No hay OCR; los PDFs escaneados fallan y deben pasar a cola/manual o quedar reportados.
4. El asistente no tiene herramientas para consultar ordenanzas; el RAG existe como endpoint REST pero no como tool conversacional.
5. La búsqueda semántica no usa pgvector real: limita a 500 chunks y rankea en Python.
6. `local_hash` no es embedding semántico suficiente para preguntas jurídicas reales.
7. El chunking por párrafo no conserva bien artículos/citas; “Fragmento N” es débil como trazabilidad jurídica.
8. La revisión automática no valida suficientemente vigencia, derogación, fechas, boletín ni municipio.

### Validación go/no-go aceptada

GO completo solo si:

- Stack sano (`backend`, `frontend`, `postgres`, `redis`, `worker`).
- Migraciones en head.
- Municipios CyL activos.
- Ordenanzas demo aprobadas y con `source_url` oficial.
- Chunks demo con `embedding_status='ready'` y `review_status='approved'`.
- `/ordinances/semantic-search` devuelve citas útiles para consultas pactadas.
- `/ordinances/comparison` devuelve filas para al menos dos municipios/tema.
- Assistant operativo si la promesa de demo incluye conversación.

NO-GO si:

- Cero ordenanzas aprobadas de CyL.
- Cero chunks `ready`.
- Búsqueda semántica vacía en queries demo.
- Comparación vacía para municipios demo.
- UI muestra errores visibles o assistant devuelve 500/503 sin degradación controlada.

GO demo controlada si hay subconjunto curado aunque no exista cobertura total: presentarlo explícitamente como piloto funcional sobre municipios/temas validados.

## Replan de ejecución por prioridad

### Sprint 0 — desbloqueo inmediato de demo controlada — COMPLETADO

1. Crear worktree dedicado. ✅ `/home/dev/proyectos/asistente_ayuntamientos-worktrees/mvp-ordenanzas-burgos` en rama `codex/mvp-ordenanzas-burgos-20260626`.
2. Sembrar BOP de Burgos en `official_legal_sources`; BOCyL/BOE solo si se usan como fuentes auxiliares. ✅ `backend/app/ordinances/seed.py`, arranque de backend y migración actualizados.
3. Añadir herramienta read-only del asistente para `semantic_search_ordinances`. ✅ Disponible para el agente `consultation`, protegida por `ordinances.compare`, con resultados citables desde chunks aprobados.
4. Preparar dataset piloto con pueblos de Burgos y 1-2 temas (`residuos`, `agua`, `tasas` u ordenanza fiscal, según disponibilidad real en BOP Burgos). ✅ 3 anuncios reales BOPBUR: Hoyales de Roa (basuras/residuos), Condado de Treviño (agua potable en Zurbitu) y Quintanar de la Sierra (agua/alcantarillado/basuras).
5. Aprobar manualmente solo lo revisado para demo. ✅ Ordenanzas y chunks marcados como aprobados para recuperación demo, con nota explícita de validación jurídica humana pendiente antes de uso oficial.
6. Ejecutar checklist go/no-go. ✅ DB local con 3 ordenanzas, 3 chunks, embeddings `ready`, chunks `approved`; smoke positivo de `semantic_search_ordinances`; suite backend `pytest -q` con 227 tests pasando.

### Sprint 1 — cobertura Burgos reproducible

1. Conector determinista para BOP Burgos: buscador oficial + descarga de PDFs/anuncios por resultado.
2. Reporte de cobertura por municipio de Burgos/tema/estado.
3. Reintentos de embeddings y listado de fallos OCR/manual.
4. Chunking por artículo con cita legal mejorada.

### Sprint 2 — escalado real

1. pgvector real con índice y ranking SQL.
2. Embeddings productivos OpenAI-compatible/UE o modelo local serio.
3. OCR local para históricos escaneados.
4. Modelo de vigencia: texto base + modificaciones + derogaciones.

## Queries operativas obligatorias antes de enseñar la demo

```sql
select province, count(*) filter (where status='active') as active
from municipalities
where province ilike 'Burgos'
group by province
order by province;

select m.province,
       count(o.id) as ordinances_total,
       count(o.id) filter (where o.curation_status='approved') as approved
from municipalities m
left join ordinances o on o.municipality_id = m.id
where m.province ilike 'Burgos'
group by m.province
order by m.province;

select count(*) as chunks_total,
       count(*) filter (where embedding_status='ready') as chunks_ready,
       count(*) filter (where embedding_status='failed') as chunks_failed,
       count(*) filter (where review_status='approved') as chunks_approved
from ordinance_legal_chunks c
join ordinances o on o.id = c.ordinance_id
join municipalities m on m.id = o.municipality_id
where m.province ilike 'Burgos';

select count(*) as approved_without_source
from ordinances o
join municipalities m on m.id = o.municipality_id
where m.province ilike 'Burgos'
  and o.curation_status='approved'
  and (o.source_url is null or length(trim(o.source_url)) = 0);
```

## Estado actual

Sprint 0 queda cerrado como **demo controlada Burgos**: hay fuente oficial BOPBUR sembrada, herramienta conversacional read-only sobre ordenanzas aprobadas, bootstrap idempotente con 3 PDFs oficiales reales, chunks vectorizados y aprobados para recuperación demo, smoke positivo y tests backend completos pasando.

Siguiente paso recomendado: iniciar **Sprint 1 — cobertura Burgos reproducible** en la misma rama/worktree o en un branch derivado, empezando por un conector determinista BOPBUR y un reporte de cobertura por municipio/tema/estado. Mantener fuera de alcance de Sprint 1: Castilla y León completa, pgvector productivo, OCR generalizado y modelo completo de vigencia; esos temas pertenecen al Sprint 2 o fases posteriores.
