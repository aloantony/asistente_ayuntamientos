# Asistente Ayuntamientos

## 1. Project overview

Asistente Ayuntamientos is a FastAPI + Next.js application for municipal management workflows.

The project objective is: "A platform to help small and medium-sized municipalities manage documentation, requirements, ordinances and internal processes, with progressive support from AI."

The current product space is municipal documentation, internal work tracking, requirements intake, ordinance management and comparative reference data. It is intended for technical development and operational use, not as a public marketing site.

## 2. Product objective and direction

The project is evolving toward a municipal management and automation platform where:

- Municipalities or client entities that use the application are managed as `Organization` records.
- Real-world municipalities are stored as `Municipality` records for comparative and reference data.
- Projects represent expedientes, work areas or internal initiatives.
- Documents are stored securely on our own server.
- Requirements capture functional needs and product ideas from municipal stakeholders.
- Ordinances provide the basis for a future comparative knowledge base of municipal regulations.

AI is intended to be central to the product direction. The system should assist, compare, structure, propose and automate where appropriate, but AI must remain supervised by humans. It must not silently make final legal or administrative decisions.

Future AI functionality must go through a Privacy/AI Gateway before any external API call. Original documents and sensitive municipal data must not be sent directly to external AI services. External AI APIs may be used in the future only after filtering, minimization and pseudonymization where needed.

The assistant can run either against Anthropic directly or against a private/local Hermes Agent API Server. Hermes Agent is treated as an external runtime/app, not as the institutional memory store and not as the source of authorization decisions.

Future priorities will be refined through Requirements Intake and through work with municipal stakeholders and developers. The exact first commercial module and user persona are intentionally still open.

## 3. Current technical stack

- Backend: FastAPI
- Frontend: Next.js
- Database: PostgreSQL
- Redis: declared in Docker Compose and reserved for future workers/cache; no backend code consumes it yet
- Local orchestration: Docker Compose
- ORM: SQLAlchemy
- Migrations: Alembic
- Authentication: JWT
- Authorization: RBAC permissions

## 4. Core domain concepts

- `Organization`: customer or client tenant using the app. Organizations scope operational data such as users, projects, documents and requirements.
- `Municipality`: real-world municipality used for comparative and reference data. A municipality is not necessarily the same thing as the tenant using the application.
- `Project`: expediente, work area or internal initiative inside an organization.
- `Document`: uploaded file linked to an organization and a project.
- `Requirement`: structured functional or product need linked to an organization and optionally to a project.
- `Ordinance`: structured municipal ordinance linked to a municipality and optionally to a document.
- `User`, `Group`, `Role`, `Permission`: access control model used to assign capabilities to people and groups.

## 5. Implemented modules

- Authentication: JWT login issuing an httpOnly session cookie for the browser (Bearer headers remain supported for API clients), `/auth/logout`, self-service password change, admin-driven password reset, per-IP login rate limiting, `/auth/me` session restoration and first-admin bootstrap.
- Users and groups: administrative management. Deletion is physical (hard delete) but guarded: the last active superuser and your own account cannot be deleted, and association rows are cleaned up explicitly.
- Roles and permissions: RBAC model for administrative and functional capabilities. The permission catalog is seeded automatically and idempotently on backend startup.
- Organizations: tenant foundation for client entities using the application.
- Projects: organization-scoped expedientes, work areas and initiatives.
- Documents: upload, metadata, download and archive support for project documents.
- Requirements: structured intake for needs, product ideas and stakeholder requests.
- Municipalities: global reference data for real-world municipalities.
- Ordinances: structured ordinance records linked to municipalities and optionally documents.
- AI Requirements Intake Assistant: conversational agent (Spanish) that captures stakeholder needs as draft requirements. It runs a synchronous tool-use loop through the internal Privacy/AI Gateway (`app/assistant/gateway.py`) using either Anthropic or a private Hermes Agent API Server (`ASSISTANT_RUNTIME=hermes_agent`). Its tools execute with the calling user's RBAC permissions, requirements are always created as drafts with `source_type=conversation`, and every tool call leaves an auditable JSON trail. Conversations are private to their author. Gated by the `assistant.use` permission; disabled (503) unless the selected runtime is configured.
- Controlled institutional memory: the assistant can propose organization memory, but only entries reviewed by authorized users become reusable context. Proposing, viewing and reviewing are separated by `assistant.memory.propose`, `assistant.memory.view` and `assistant.memory.review`.

## 6. Architecture principles

- Access control is tenant-aware through `Organization`.
- Privileged platform operations are superuser-only: granting or revoking superuser status, creating organizations (tenants), and mutating the global roles/permissions catalog. `users.manage` only reaches users who share an organization where the admin holds the permission.
- Municipalities are global reference data, separate from tenant organizations. Linking a document to an ordinance requires access to that document.
- List endpoints for municipalities, ordinances, requirements and admin users are paginated (`limit` 1-200 default 100, `offset`) and expose the total via the `X-Total-Count` header. Ordinance listings omit `text_content`; the full legal text only travels on the detail endpoint.
- Assistant voice input uses the browser's on-device speech recognition only (`processLocally`); it is disabled rather than falling back to the browser's cloud service (see ADR-012).
- Uploaded documents are stored outside PostgreSQL.
- PostgreSQL stores document metadata, ownership, status and relationships, not raw file bytes.
- Uploaded files are stored in a persistent Docker volume.
- Important business objects should avoid hard delete where archive or status fields are available.
- A superuser bypass exists for administration, but normal users are permission-controlled.
- Backend and frontend should remain bound to localhost in local development unless deployment is intentionally changed.

## 7. Document storage

Documents are stored on our own server in the current architecture. They are not stored in external S3/AWS storage.

`DOCUMENT_STORAGE_ROOT` controls the filesystem path used by the backend to store uploaded files. In Docker Compose, the `document_storage` volume is mounted at `/var/lib/asistente_ayuntamientos/documents`, which is the default path configured in `.env.example`.

The `document_storage` Docker volume persists uploaded files across container rebuilds and restarts. PostgreSQL stores metadata only, not raw file bytes.

## 8. Local development setup

Requirements:

- Docker
- Docker Compose

Create a local environment file:

```bash
cp .env.example .env
```

Configure secrets and local settings in `.env`. At minimum, review `SECRET_KEY`, `BOOTSTRAP_ADMIN_TOKEN`, `CORS_ALLOWED_ORIGINS`, `NEXT_PUBLIC_API_BASE_URL`, database settings and document storage settings.

To enable the AI assistant with Anthropic, keep `ASSISTANT_RUNTIME=anthropic` and set `ANTHROPIC_API_KEY` (optionally `ASSISTANT_MODEL`, default `claude-opus-4-8`). To use Hermes Agent, run its API Server privately, set `ASSISTANT_RUNTIME=hermes_agent`, `HERMES_AGENT_BASE_URL`, `HERMES_AGENT_API_KEY` and `HERMES_AGENT_MODEL`. In production, Hermes Agent stays disabled for real data unless `HERMES_AGENT_REAL_DATA_ALLOWED=true`. Without a complete runtime configuration, assistant endpoints return 503 and the UI shows the assistant as not configured.

Build and start the stack:

```bash
docker compose up -d --build
```

Apply database migrations:

```bash
docker compose exec backend alembic upgrade head
```

Local services:

- Frontend: http://localhost:3000
- Backend: http://localhost:8000
- Health check: http://localhost:8000/health

The frontend and backend are published on localhost. PostgreSQL and Redis are internal Docker Compose services.

To create the first administrator, configure `BOOTSTRAP_ADMIN_TOKEN` in `.env`, start the backend and call:

```bash
curl -X POST http://localhost:8000/auth/bootstrap-admin \
  -H "Content-Type: application/json" \
  -H "X-Bootstrap-Admin-Token: dev-bootstrap-token" \
  -d '{"email":"admin@example.com","password":"change-me-strong","full_name":"Admin"}'
```

The bootstrap endpoint only creates a superuser when no users exist yet.

## 9. Useful validation commands

Run the relevant checks before handing off code changes:

```bash
python3 -m compileall -q backend/app backend/alembic
npm --prefix frontend run build
docker compose build backend
docker compose build frontend
docker compose exec backend alembic current
git diff --check
```

Run the backend test suite (PostgreSQL test database, fully isolated from dev data):

```bash
docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q"
```

## 10. Operational cautions

- Never commit `.env`.
- Do not use `docker compose down -v` unless intentionally deleting volumes.
- `document_storage` contains uploaded files and must be treated as persistent user data.
- Run migrations after pulling backend changes that include Alembic or model updates.
- Keep backend and frontend bound to localhost unless deployment is intentionally changed.
- Do not expose PostgreSQL or Redis publicly.
- Avoid destructive database or storage actions unless the data loss is intentional and understood.

## 11. Current roadmap

The roadmap is technical and directional. Items are subject to refinement through Requirements Intake and stakeholder feedback.

- AI Requirements Intake Agent v1 (conversational; the first external user is a mayor who feeds requirements through it) — shipped, see Implemented modules
- Privacy/AI Gateway — v1 shipped with the intake agent; filtering/pseudonymization hardening pending
- Voice input for the intake agent
- Ordinance Comparison v1
- Ordinance AI Assistant v1
- Draft Generator v1
- Future AI-assisted municipal workflows

## 12. Developer handoff checklist

```bash
git pull
```

1. Inspect `.env.example`.
2. Create or update `.env` with valid local secrets and settings.
3. Start the stack with `docker compose up -d --build`.
4. Apply migrations with `docker compose exec backend alembic upgrade head`.
5. Run the validation commands relevant to the change.
6. Inspect latest commits for schema, permission, API or frontend changes.
7. Avoid deleting volumes, uploaded files or secrets.
