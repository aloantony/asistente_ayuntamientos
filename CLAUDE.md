# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

Asistente Ayuntamientos: FastAPI + Next.js platform for municipal management (documentation, requirements intake, ordinances, internal processes) with progressive, human-supervised AI support. Full context lives in `README.md` (English); architecture, decisions (ADRs) and requirements live in `docs/` (Spanish).

## Stack

- `backend/`: FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 17 (psycopg 3), JWT auth + RBAC. Deps pinned exactly in `backend/requirements.txt`.
- `frontend/`: Next.js 15 App Router, React 19, TypeScript strict. Multi-route app (ADR-014): `(auth)/login` plus an `(app)` route group whose sidebar shell (`app/(app)/layout.tsx`) gates nav items with the same permission predicates as the routes — `/asistente`, `/requisitos`, `/proyectos`, `/cuenta`, `/admin/*`. Session state and the 401 funnel live in `SessionProvider` (`app/lib/session.tsx`, mounted in the root layout); the route guard is client-side. Each route mounts only its own controller — admin is split into per-domain hooks under `app/lib/admin/`, cross-domain lists go through `app/lib/fetchers.ts` — and selection/filters/page state live in the URL (deep links and back button must keep working). API access goes through helpers in `app/lib/api.ts` (native fetch, `credentials: "include"`), except login (`app/(auth)/login/page.tsx`), logout (`app/lib/session.tsx`) and the document download (`app/lib/useProjectsController.ts`), which call fetch directly. Browser auth is an httpOnly SameSite=Lax cookie: frontend and backend must share the same host — localhost in dev (ADR-010).
- Orchestration: Docker Compose — backend on 127.0.0.1:8000, frontend on 127.0.0.1:3000, postgres and redis internal. Redis backs job scheduling (`app/core/jobs.py`), read-through caches (`app/weather/service.py`, `app/reference_layers/wms_cache.py`) — each degrading gracefully when it is unavailable — and the login/change-password rate limiter (`app/core/rate_limit.py`), which instead fails closed with 503 because an unverifiable attempt budget is an unlimited one (ADR-048). `RATE_LIMIT_BACKEND=memory` keeps the old per-process window for development and the test suite only.

## Commands

```bash
docker compose up -d --build                          # start the stack
docker compose exec backend alembic upgrade head      # apply migrations
```

Backend tests (require the postgres compose service; run inside Docker against an isolated `app_test_<uuid>` database by default):

```bash
docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q"
```

The runtime backend image intentionally excludes `pytest`; install
`requirements-dev.txt` in the disposable test container as shown above. If
`TEST_DATABASE_URL` is set manually, it must point to an `app_test`-prefixed
database.

Fresh database: create the first superuser via `POST /auth/bootstrap-admin` (header `X-Bootstrap-Admin-Token`; only works while no users exist). Without a complete assistant runtime configuration (`ANTHROPIC_API_KEY` for `ASSISTANT_RUNTIME=anthropic`, `OPENAI_API_KEY` for `ASSISTANT_RUNTIME=openai_responses`, `HERMES_AGENT_*` for `ASSISTANT_RUNTIME=hermes_agent`, or a dedicated authenticated Codex home plus both `CODEX_SUBSCRIPTION_*` opt-ins for development-only `ASSISTANT_RUNTIME=codex_subscription`) the assistant endpoints return 503 by design — not a bug. A ChatGPT subscription is not an OpenAI API credential; the Codex bridge is a separate local evaluation path, validated against CLI 0.144.4, and is forbidden in production (ADR-024).

Pre-handoff validation (README §9 "Useful validation commands"), packaged as the `/validar` skill. The backend test suite above is part of it; the remaining commands are:

```bash
python3 -m compileall -q backend/app backend/alembic
npm --prefix frontend run build
docker compose build backend
docker compose build frontend
docker compose exec backend alembic current
git diff --check
```

CI runs on every pull request (`.github/workflows/ci.yml`): a Backend job, a Frontend job and a `CI gate` that the `main` ruleset requires before merging, with branches kept up to date. The frontend has a linter (`npm --prefix frontend run lint`) and a type checker (`npm --prefix frontend run typecheck`); the backend has neither. Pre-handoff validation remains useful locally, but CI is the gate. Match the existing code style and do not introduce new tooling without recording an ADR.

## Hard rules

- Never commit or edit `.env`. `.env.example` is the documented template.
- Never delete the `postgres_data` or `document_storage` volumes by any means (`docker compose down -v`/`--volumes`, `docker volume rm`, `prune`, ...); they are persistent user data.
- Keep services bound to localhost; never expose PostgreSQL or Redis.
- External AI calls and private agent runtime calls go through the Privacy/AI Gateway (`backend/app/assistant/gateway.py`); voice STT/TTS egresses only through `backend/app/assistant/speech.py` (ADR-021), and controlled public web queries only through `backend/app/assistant/web_search.py` (ADR-022). No other module may call those services. Only conversation text, approved institutional memory and user-typed structured fields may be sent to the LLM; web search may send only the explicit normalized public query and must never fall back to another provider. Never send original documents or stored municipal files. Log metadata only (runtime, model, stop reason, token counts), never conversation, query or result content. Keep LLM runtime-specific code inside `gateway.py`; runtime selection comes from `ASSISTANT_RUNTIME` (ADR-013, ADR-016, ADR-023).
- Voice capture and playback stay in the browser, but STT/TTS cloud egress happens only through `backend/app/assistant/speech.py`; when the configured runtime is disabled or unavailable the voice UI stays unavailable — never fall back to browser cloud speech recognition or `speechSynthesis` (ADR-021).

## Architecture constraints

- `Organization` is the tenant and scopes users, groups, projects, documents and requirements. `Municipality` is global reference data shared across tenants — a different concept, not the tenant.
- Access control chain: user → group → role → permission, with an `is_superuser` bypass. Superuser-only operations: granting/revoking superuser, creating organizations, mutating the global roles/permissions catalog. New endpoints must enforce both tenancy and permissions; the permission catalog is seeded idempotently at backend startup.
- Paginated list endpoints (municipalities, ordinances, requirements, admin users) use `limit` (1–200, default 100) + `offset` via `app/core/pagination.py` and expose the total in the `X-Total-Count` header; new list endpoints should follow this pattern (projects, documents and organizations listings are currently unpaginated). Heavy fields (e.g. ordinance `text_content`) travel only on detail endpoints.
- Prefer archive/status fields over hard delete for business objects.
- Documents: bytes on the filesystem (`DOCUMENT_STORAGE_ROOT`, a Docker volume), metadata in PostgreSQL. Never store file bytes in the database. Uploads stream with a content-type whitelist, a size cap (`DOCUMENT_MAX_UPLOAD_BYTES`) and server-generated storage keys (path-traversal defense, `app/documents/storage.py`) — preserve all three.

## Testing

- `backend/tests/conftest.py` is the harness: per-test savepoint-rollback sessions (app code may commit freely), `client` TestClient with a `get_db` override, factory fixtures (`make_user`, `make_organization`, `superuser`, `add_member`, `grant_permissions`) and `headers_for()` to mint JWTs.
- Assistant tests must never call a real AI API or Hermes Agent server: override the `get_gateway` dependency with a fake via `app.dependency_overrides` (see `tests/test_assistant.py`).
- New endpoints need tests covering tenancy isolation and permission gating.
- The frontend runs vitest with `@testing-library/react` on jsdom (`npm --prefix frontend test`). Components under `app/components/` colocate their `*.test.tsx`; pure helpers extracted from a component are tested directly rather than through the component that used to hold them.

## Conventions

- Migrations: `backend/alembic/versions/`, named `YYYYMMDD_NNNN_description.py`, always with both `upgrade()` and `downgrade()`.
- Commits: English, imperative subject, no conventional-commit prefixes; detailed body that references ADRs ("See ADR-012") when relevant. Module milestones follow the pattern "Add <module> v1".
- Docs: `docs/` is written in Spanish; the living docs (`arquitectura.md`, `requisitos.md`) carry `Actualizado: <fecha>` headers. Decisions are recorded as numbered ADRs in `docs/decisiones.md`. Documentation is synced in dedicated docs commits after feature commits; `README.md` is the source of operational detail.
- `docs/requisitos.md` describes the current state only; new product requirements enter via the Requirements Intake module as drafts, not by editing the doc.
- UI text is Spanish; backend API error details are English and translated for the UI in `frontend/app/lib/api.ts` (`translateApiDetail`).
