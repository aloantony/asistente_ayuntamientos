# Git-first Work Structure Plan

> **Para Hermes:** este plan define la estructura óptima de trabajo con git para este proyecto. Antes de cualquier implementación futura, usar esta estructura como checklist operativo.

**Goal:** trabajar siempre con git de forma explícita, aislada y auditable, evitando pisar cambios existentes y manteniendo cada tarea en ramas pequeñas, revisables y verificables.

**Architecture:** adoptar una variante pragmática de GitHub Flow/trunk-based development: ramas cortas por tarea, PRs pequeños, CI como puerta de entrada, y worktrees cuando haya trabajo paralelo o cambios ajenos en el árbol actual. Para cambios grandes, usar stacked PRs en lugar de una rama enorme.

**Tech Stack:** git, GitHub, GitHub PRs, opcional `gh`, opcional `git worktree`, backend Python/FastAPI/pytest, frontend Next.js/npm.

---

## Contexto actual del repositorio

Repo: `/home/dev/proyectos/asistente_ayuntamientos`

Rama actual al crear este plan:
- `codex/python-suite-runner-20260618`
- tracking: `origin/codex/python-suite-runner-20260618`

Estado actual observado:
- Hay cambios sin commitear previos en backend y frontend.
- Hay `.hermes/` sin trackear, incluyendo planes.
- `main` local aparece `ahead 14` respecto a `origin/main`.
- Remoto: `git@github.com:aloantony/asistente_ayuntamientos.git`.

Regla de `AGENTS.md` del repo:
- Comprobar rama antes de editar, commitear o ejecutar comandos mutantes.
- Si la rama actual es compartida/base, crear una rama única con prefijo `codex/`.
- No reutilizar ramas de otros agentes.
- Al cambiar/crear rama, preservar cambios existentes y nunca revertir cambios no propios salvo petición explícita.

---

## Opciones revisadas en GitHub / ecosistema

### 1. GitHub Flow

Fuente encontrada: GitHub Docs sobre GitHub Flow.

Idea útil:
- Una rama principal estable.
- Ramas cortas y descriptivas para cambios concretos.
- Pull request para revisión y CI.
- Merge cuando está revisado y verde.

Encaja bien para este proyecto porque hay producto en evolución rápida y se quieren cambios pequeños y auditables.

### 2. Trunk-based development con ramas cortas

Fuentes encontradas: repositorios y guías sobre trunk-based development.

Idea útil:
- Integración frecuente.
- Evitar ramas largas que divergen.
- Feature flags o cambios incrementales si una funcionalidad no está lista.

Encaja parcialmente: no conviene empujar directo a trunk; mejor PR corto hacia la rama base acordada.

### 3. Stacked PRs

Fuente encontrada: documentación de GitHub Stacked PRs / `gh-stack` y guías de stacked PRs.

Idea útil:
- Dividir un cambio grande en PRs encadenados.
- Cada PR es revisable por separado.
- Reduce PRs gigantes y facilita revertir partes.

Encaja para features grandes como “gestión desde mapa” o cambios de arquitectura del asistente.

### 4. Git worktrees para agentes/parallel work

Fuentes encontradas: guías de git worktrees para agentes de IA.

Idea útil:
- Cada agente/tarea trabaja en un directorio aislado con su propia rama.
- Evita mezclar cambios no relacionados en el mismo working tree.
- Especialmente útil cuando ya hay cambios sin commitear.

Encaja muy bien aquí porque el árbol actual ya tiene cambios previos y el usuario quiere disciplina git fuerte.

---

## Decisión recomendada

Usar esta estructura por defecto:

1. `origin/main` como referencia remota estable.
2. Una rama base de integración por época si el proyecto ya la usa, por ejemplo `revision-YYYY-MM-DD` o una rama `dev`, pero debe estar explícitamente acordada.
3. Ramas de tarea cortas:
   - `codex/fix-invalid-tool-json-YYYYMMDD`
   - `codex/feat-map-info-model-YYYYMMDD`
   - `codex/refactor-assistant-tool-contract-YYYYMMDD`
4. PR pequeño por tarea.
5. Commits frecuentes y atómicos, con Conventional Commits.
6. Para trabajo paralelo o si hay cambios sin commitear: crear un `git worktree` en vez de reutilizar el árbol actual.
7. Para cambios grandes: stacked PRs, no mega-PR.

---

## Estructura local recomendada

Mantener el repo principal como árbol de coordinación:

```bash
/home/dev/proyectos/asistente_ayuntamientos
```

Crear worktrees hermanos para tareas concretas:

```bash
/home/dev/proyectos/asistente_ayuntamientos-worktrees/
  fix-invalid-tool-json/
  feat-map-information/
  refactor-assistant-state/
```

Ejemplo:

```bash
mkdir -p /home/dev/proyectos/asistente_ayuntamientos-worktrees
cd /home/dev/proyectos/asistente_ayuntamientos

git fetch origin

git worktree add \
  /home/dev/proyectos/asistente_ayuntamientos-worktrees/fix-invalid-tool-json \
  -b codex/fix-invalid-tool-json-20260623 \
  origin/codex/python-suite-runner-20260618
```

Nota: la base anterior usa la rama actual porque tiene trabajo relevante reciente. Si se decide volver a `origin/main`, cambiar el último argumento por `origin/main` después de resolver que `main` local está divergente/ahead.

---

## Checklist obligatorio antes de tocar código

### Paso 1: estado git

```bash
git status --short --branch
git remote -v
git branch -vv --all | head -80
```

Registrar mentalmente:
- rama actual;
- si hay cambios sin commitear;
- si la rama tiene upstream;
- si hay divergencia local/remota;
- qué archivos están modificados por otros.

### Paso 2: decidir si usar worktree

Usar worktree si se cumple cualquiera:
- hay cambios sin commitear no propios;
- la tarea no está relacionada con los cambios actuales;
- se va a delegar a otro agente;
- la tarea puede durar más de una sesión;
- se necesita comparar dos enfoques.

No usar worktree solo si:
- el usuario confirma que los cambios actuales son nuestros y pertenecen a la misma tarea;
- el árbol está limpio;
- la rama actual ya es una rama de tarea específica.

### Paso 3: crear rama de tarea

Formato:

```text
codex/<tipo>-<slug>-<YYYYMMDD>
```

Tipos:
- `fix/` para bugs.
- `feat/` para funcionalidad.
- `refactor/` para cambios internos sin feature visible.
- `test/` para cobertura pura.
- `docs/` para documentación.

Como el repo pide prefijo `codex/`, usar ejemplos así:

```text
codex/fix-invalid-tool-json-20260623
codex/feat-map-information-management-20260623
codex/refactor-assistant-pending-work-20260623
```

---

## Flujo de trabajo por tarea

### Task 1: Abrir rama/worktree limpio

**Objective:** aislar la tarea.

**Commands:**

```bash
cd /home/dev/proyectos/asistente_ayuntamientos
git fetch origin
git status --short --branch

mkdir -p /home/dev/proyectos/asistente_ayuntamientos-worktrees

git worktree add \
  /home/dev/proyectos/asistente_ayuntamientos-worktrees/<task-slug> \
  -b codex/<type>-<task-slug>-<YYYYMMDD> \
  <base-branch>
```

**Base branch policy:**
- Si la tarea continúa trabajo actual del asistente: base `origin/codex/python-suite-runner-20260618`.
- Si la tarea es nueva e independiente: base `origin/main`, pero antes resolver que `main` local está `ahead 14` y no usarlo como fuente sin revisar.
- Si existe una rama de integración acordada: usar esa.

**Verification:**

```bash
cd /home/dev/proyectos/asistente_ayuntamientos-worktrees/<task-slug>
git status --short --branch
```

Expected:
- Rama `codex/...` activa.
- Working tree limpio.

### Task 2: Reproducir o definir test antes de implementar

**Objective:** toda tarea de bug empieza con test rojo; toda feature con prueba o criterio verificable.

**Bug del JSON actual:**

```bash
cd /home/dev/proyectos/asistente_ayuntamientos-worktrees/fix-invalid-tool-json/backend
pytest tests/test_assistant.py::<nuevo_test_reproduccion> -v --tb=long
```

Expected antes del fix:
- FAIL que demuestre el bug exacto.

### Task 3: Commit del test rojo si aporta valor

**Objective:** separar reproducción de implementación.

```bash
git add backend/tests/test_assistant.py
git commit -m "test: reproduce assistant tool-call JSON leak"
```

Si el equipo no quiere commits rojos, al menos mantener el diff separado y commitear test+fix juntos. Mi recomendación para trazabilidad: commits separados si no se sube a PR hasta que todo esté verde.

### Task 4: Implementación mínima

**Objective:** tocar el mínimo conjunto de archivos.

Para el bug actual, candidatos probables:
- `backend/app/assistant/gateway.py`
- `backend/app/assistant/service.py`
- `backend/tests/test_assistant.py`

Regla:
- no arreglar por regex de frases del usuario;
- arreglar contrato explícito de tool-call o estado estructurado;
- mantener acciones auditables en `messages.actions`;
- no mostrar JSON técnico en `messages.content`.

### Task 5: Verificación local

**Backend específico:**

```bash
cd backend
pytest tests/test_assistant.py::<test_nuevo> -v
pytest tests/test_assistant.py -q
```

**Backend más amplio si el cambio toca routing/tools:**

```bash
cd backend
pytest -q
```

**Frontend si toca UI:**

```bash
cd frontend
npm run lint
npm run build
```

### Task 6: Commit atómico

```bash
git status --short
git diff -- backend/app/assistant/gateway.py backend/app/assistant/service.py backend/tests/test_assistant.py

git add <solo-archivos-de-la-tarea>
git commit -m "fix: normalize assistant textual tool calls"
```

Regla crítica:
- no usar `git add .` si hay archivos ajenos o planes `.hermes/` que no forman parte del PR.
- revisar siempre `git diff --cached` antes del commit.

### Task 7: Push y PR

```bash
git push -u origin HEAD
```

Con `gh`:

```bash
gh pr create \
  --title "fix: normalize assistant textual tool calls" \
  --body "## Summary
- Normalize assistant tool-call payloads consistently
- Prevent technical JSON from becoming visible chat content
- Add regression coverage for the invalid JSON/tool-name case

## Test Plan
- [ ] cd backend && pytest tests/test_assistant.py -q"
```

Sin `gh`, usar GitHub web o API.

### Task 8: CI y revisión

```bash
gh pr checks --watch
```

Si falla:
1. leer logs;
2. reproducir localmente;
3. fix mínimo;
4. commit `fix: ...`;
5. push;
6. repetir máximo 3 ciclos antes de replantear arquitectura.

---

## Política para cambios grandes: stacked PRs

Usar stacks cuando una tarea tenga más de 3-5 archivos o mezcle conceptos.

Ejemplo para “gestionar información desde mapa”:

```text
Stack: map information management

PR 1: codex/feat-map-entity-model-20260623
- Modelos backend y migración para tipos de elementos geolocalizados.
- Tests de permisos y CRUD básico.

PR 2: codex/feat-map-api-20260623
- Endpoints de consulta/creación/edición.
- Tests API.
- Base: PR 1.

PR 3: codex/feat-map-ui-20260623
- Vista mapa y formulario.
- Base: PR 2.

PR 4: codex/feat-map-assistant-intake-20260623
- Integración conversacional para crear/consultar elementos.
- Base: PR 3 o PR 2 según dependencia real.
```

Reglas de stacked PRs:
- Cada PR debe pasar tests por sí mismo respecto a su base.
- Cada PR debe poder revisarse en 10-20 minutos.
- Cada PR debe tener una descripción clara de dependencia: “Base: #N”.
- No mezclar refactors grandes con feature visible salvo que sea PR previo explícito.

---

## Política de commits

Usar Conventional Commits:

```text
fix: normalize assistant textual tool calls
test: cover invalid assistant tool payloads
feat: add geolocated information model
refactor: persist assistant pending work explicitly
docs: document git-first agent workflow
```

Commit body cuando sea útil:

```text
fix: normalize assistant textual tool calls

Accepts explicit `tool` as an alias for `name` in textual tool-call
payloads emitted by the Hermes runtime, while keeping mutating tools
permissioned and auditable.

Test: cd backend && pytest tests/test_assistant.py -q
```

---

## Política de archivos `.hermes/`

Por defecto:
- Los planes `.hermes/plans/*.md` son útiles para coordinación local.
- No asumir que deben entrar al PR.

Antes de commitear `.hermes/`, decidir explícitamente:
- Si el plan documenta trabajo interno no necesario para el producto: no commitear.
- Si se quiere versionar como documentación del repo: mover/convertir a `docs/` y revisar contenido.

---

## Guardrails para agentes

Todo agente/codificador debe empezar con este bloque:

```bash
pwd
git status --short --branch
git remote -v
git branch --show-current
```

Y debe terminar con:

```bash
git status --short --branch
git diff --stat HEAD
```

Si modifica código:
- crear tests o explicar por qué no aplica;
- ejecutar tests relevantes;
- no declarar éxito sin output real;
- no revertir cambios que no sean propios;
- no mezclar tareas no relacionadas en un commit.

---

## Aplicación inmediata al bug del chat

Para depurar el error `invalid json in tool call - missing function name`:

1. No trabajar directamente sobre el árbol actual si esos cambios no son todos de esta tarea.
2. Crear worktree:

```bash
cd /home/dev/proyectos/asistente_ayuntamientos
git fetch origin
mkdir -p /home/dev/proyectos/asistente_ayuntamientos-worktrees

git worktree add \
  /home/dev/proyectos/asistente_ayuntamientos-worktrees/fix-invalid-tool-json \
  -b codex/fix-invalid-tool-json-20260623 \
  origin/codex/python-suite-runner-20260618
```

3. En el worktree, aplicar el plan existente:

```text
/home/dev/proyectos/asistente_ayuntamientos/.hermes/plans/2026-06-23_145854-debug-invalid-json-tool-call.md
```

4. Crear test rojo.
5. Fix mínimo.
6. Tests.
7. Commit.
8. PR.

---

## Aplicación inmediata a la feature de mapa

No empezar directamente implementando mapas. Primero dividir en descubrimiento + arquitectura:

### Rama 1: plan/modelado

```text
codex/plan-map-information-management-20260623
```

Deliverable:
- ADR o plan técnico sobre modelo de dominio:
  - `MapLayer` / capa
  - `MapFeatureType` / tipo configurable
  - `MapFeature` / elemento geolocalizado
  - propiedades flexibles JSON Schema
  - permisos por organización
  - auditoría/historial

### Rama 2: backend base

```text
codex/feat-map-information-backend-20260623
```

Deliverable:
- migración;
- modelos;
- schemas;
- CRUD;
- tests.

### Rama 3: frontend base

```text
codex/feat-map-information-ui-20260623
```

Deliverable:
- página/listado/mapa básico;
- creación/edición;
- filtros.

### Rama 4: asistente conversacional

```text
codex/feat-map-information-assistant-20260623
```

Deliverable:
- herramientas del asistente para crear/consultar elementos;
- estado pendiente estructurado;
- tests conversacionales.

---

## Decisiones abiertas

1. ¿Cuál es la rama base real del producto ahora mismo?
   - `origin/main` está detrás de `main` local según el estado observado.
   - La rama activa `codex/python-suite-runner-20260618` parece contener trabajo reciente no integrado en `origin/main`.

2. ¿Queremos versionar planes `.hermes/`?
   - Recomendación: no por defecto; mover a `docs/` solo si son documentación de equipo.

3. ¿Usaremos `gh` y GitHub PRs siempre?
   - Recomendación: sí si hay auth disponible; si no, push + PR manual.

4. ¿Adoptamos stacked PRs formalmente?
   - Recomendación: sí para features grandes, especialmente mapa/asistente.

---

## Definición de terminado para esta estructura

La estructura queda aceptada cuando:
- todo trabajo nuevo arranca con `git status --short --branch`;
- ninguna tarea nueva se hace sobre una rama base compartida;
- si hay cambios ajenos, se usa `git worktree`;
- cada PR tiene scope pequeño y test plan;
- los cambios grandes se dividen en stacked PRs;
- el asistente reporta siempre rama, archivos tocados, tests ejecutados y estado final.
