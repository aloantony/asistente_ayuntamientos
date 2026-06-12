---
name: validar
description: Ejecuta la validación pre-handoff del proyecto (README §9 "Useful validation commands") — compileall, git diff --check, estado de migraciones, builds de Docker, build del frontend y la suite pytest en Docker — y presenta un resumen de resultados.
disable-model-invocation: true
argument-hint: "[rapido|backend|frontend]"
---

Ejecuta la validación pre-handoff de Asistente Ayuntamientos desde la raíz del repositorio.

Ámbito según `$ARGUMENTS`: vacío = validación completa; `rapido` (o `rápido`) = solo las comprobaciones rápidas (1–3); `backend` = rápidas + backend; `frontend` = rápidas + frontend. Ante cualquier otro valor, no asumas la validación completa: pregunta qué ámbito se quería.

## Comprobaciones rápidas (siempre)

1. `python3 -m compileall -q backend/app backend/alembic`
2. `git diff --check`
3. Estado de migraciones: compara `docker compose exec backend alembic current` con `docker compose exec backend alembic heads`; avisa si la base de datos no está en head.

## Backend (omitir si el ámbito es `frontend` o `rapido`)

4. `docker compose build backend`
5. Suite de tests (PostgreSQL de test aislado, no toca datos de desarrollo):

   ```bash
   docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
     sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q"
   ```

## Frontend (omitir si el ámbito es `backend` o `rapido`)

6. `npm --prefix frontend run build`
7. `docker compose build frontend`

## Reglas de ejecución

- El paso 3 requiere el servicio `backend` levantado (`docker compose exec` falla si no lo está); el paso 5 requiere el servicio `postgres` (si no está en marcha, `docker compose run` lo arranca vía `depends_on`); el paso 4 solo necesita el daemon de Docker. Si falta algo, indícalo y sugiere `docker compose up -d` en lugar de levantar el stack por tu cuenta.
- Ejecuta los pasos de forma independiente: si uno falla, continúa con el resto y recoge todos los fallos.
- No corrijas nada automáticamente: este skill solo valida e informa. El usuario decide qué arreglar.

## Informe final

Presenta una tabla `comprobación → resultado` (✅ / ❌ con el error resumido en una línea) y termina diciendo explícitamente si el proyecto está listo para handoff según README §9.
