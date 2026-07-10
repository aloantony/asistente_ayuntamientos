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

External AI calls must go through the Privacy/AI Gateway before any LLM API call; the voice pipeline (STT/TTS) egresses only through `app/assistant/speech.py` under the same discipline. Original documents and sensitive municipal data must not be sent directly to external AI services. External AI APIs may be used only after filtering, minimization and pseudonymization where needed.

The assistant can run either against Anthropic directly or against a private/local Hermes Agent API Server. Hermes Agent is treated as an external runtime/app, not as the institutional memory store and not as the source of authorization decisions.

Future priorities will be refined through Requirements Intake and through work with municipal stakeholders and developers. The exact first commercial module and user persona are intentionally still open.

## 3. Current technical stack

- Backend: FastAPI
- Frontend: Next.js
- Database: PostgreSQL
- Redis: shared rate limits and RQ background jobs
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

- Authentication: JWT login issuing an httpOnly session cookie for the browser (Bearer headers remain supported for API clients), `/auth/logout`, self-service password change, admin-driven password reset, distributed login/password rate limiting, `/auth/me` session restoration and first-admin bootstrap.
- Users and groups: administrative management. Deletion is physical (hard delete) but guarded: the last active superuser and your own account cannot be deleted, and association rows are cleaned up explicitly.
- Roles and permissions: RBAC model for administrative and functional capabilities. The permission catalog is seeded automatically and idempotently on backend startup.
- Organizations: tenant foundation for client entities using the application.
- Projects: organization-scoped expedientes, work areas and initiatives.
- Documents: upload, metadata, download and archive support for project documents.
- Requirements: structured intake for needs, product ideas and stakeholder requests.
- Municipalities: global reference data for real-world municipalities.
- Ordinances: structured ordinance records linked to municipalities and optionally documents.
- Ordinance import and comparison: official-source import jobs run through a Redis/RQ worker, create pending-review ordinances, split legal text into reviewable/vectorized chunks and expose a thematic comparison matrix between municipalities.
- AI Requirements Intake Assistant: Anacleto is a model-first Spanish assistant that captures stakeholder needs as draft requirements. It streams web turns over SSE, calls the configured LLM runtime only through the Privacy/AI Gateway (`app/assistant/gateway.py`) and executes tools with the calling user's RBAC permissions. Requirements are always created as drafts with `source_type=conversation`, `create_requirement` requires a later human confirmation turn, and every tool call leaves an auditable JSON trail. Conversations are private to their author. Gated by the `assistant.use` permission; disabled (503) unless the selected runtime is configured.
- Web voice dialogue with Anacleto: when both STT and TTS are enabled, the assistant panel offers `Modo voz`. The browser records with `MediaRecorder`, the backend transcribes through `/assistant/audio-transcriptions`, voice turns add `input_mode="voice"` for a concise oral prompt, and responses are synthesized through `/assistant/speech`. Hands-free mode can stop on silence, speak streamed responses by sentence and re-arm listening; turning auto-listen off falls back to one voice turn at a time.
- Controlled institutional memory: the assistant can propose organization memory, but only entries reviewed by authorized users become reusable context. Proposing, viewing and reviewing are separated by `assistant.memory.propose`, `assistant.memory.view` and `assistant.memory.review`.
- Telegram assistant channel: existing users can generate a short-lived one-use link code from the account page, link a Telegram chat and use the assistant through a separate audited conversation channel with the same RBAC permissions.

## 6. Architecture principles

- Access control is tenant-aware through `Organization`.
- Privileged platform operations are superuser-only: creating, updating or deleting global user identities; creating organizations (tenants); and mutating the global roles/permissions catalog. Tenant admins cannot choose another person's password or reserve a global email; a future tenant onboarding flow must use an organization-bound, one-use invitation.
- Municipalities are global reference data, separate from tenant organizations. Linking a document to an ordinance requires access to that document.
- List endpoints for municipalities, ordinances, requirements and admin users are paginated (`limit` 1-200 default 100, `offset`) and expose the total via the `X-Total-Count` header. Ordinance listings omit `text_content`; the full legal text only travels on the detail endpoint.
- Imported ordinances are never approved automatically: importer output enters `pending_review`, the review agent stores a checklist and score, and a user with `ordinances.review` must approve, reject or request changes.
- Official-source downloads require HTTPS, validate every redirect and connect only to the public IPs resolved during validation. BOP Burgos currently exposes only HTTP/obsolete TLS, so live ingestion fails closed until a secure official endpoint or integrity-preserving controlled archive proxy is available.
- Legal chunks are stored in PostgreSQL and use pgvector when available. Development uses deterministic local hash embeddings by default; production can switch to a configured OpenAI-compatible embeddings provider.
- Assistant voice capture and playback stay in the browser, but STT/TTS run only through backend endpoints in `app/assistant/speech.py`; there is no browser cloud recognition or `speechSynthesis` fallback (see ADR-021).
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

Configure secrets and local settings in `.env`. At minimum, review `SECRET_KEY`, `BOOTSTRAP_ADMIN_TOKEN`, `CORS_ALLOWED_ORIGINS`, `NEXT_PUBLIC_API_BASE_URL`, `REDIS_URL`, rate-limit settings, database settings and document storage settings. `RATE_LIMIT_BACKEND=redis` is mandatory in production; `memory` is an explicit development/test fallback only.

To enable the AI assistant with Anthropic, keep `ASSISTANT_RUNTIME=anthropic` and set `ANTHROPIC_API_KEY` (optionally `ASSISTANT_MODEL`, default `claude-opus-4-8`). To use Hermes Agent, run its API Server privately, set `ASSISTANT_RUNTIME=hermes_agent`, `HERMES_AGENT_BASE_URL`, `HERMES_AGENT_API_KEY` and `HERMES_AGENT_MODEL`. In production, Hermes Agent stays disabled for real data unless `HERMES_AGENT_REAL_DATA_ALLOWED=true`. Without a complete runtime configuration, assistant endpoints return 503 and the UI shows the assistant as not configured.

Anacleto v2 is model-first: the backend no longer runs a semantic planner/router or deterministic answer templates. Each turn calls the configured runtime through `gateway.py`, injects only the user-visible context and filtered tool list, and executes tools with backend RBAC/tenancy checks. Web clients should use `POST /assistant/conversations/{id}/messages/stream` for SSE frames (`message_start`, `text_delta`, `tool_activity`, `done`); the classic `POST /assistant/conversations/{id}/messages` remains available for synchronous clients and Telegram. `ASSISTANT_HISTORY_MAX_MESSAGES` controls the recent message window sent to the model.

Controlled web search uses a second local Hermes API Server instance/profile, separate from the main assistant runtime. Configure `HERMES_WEB_BASE_URL`, `HERMES_WEB_API_KEY`, `HERMES_WEB_MODEL` and grant `assistant.web.search` only to users who may search the public web from the assistant. The main Hermes API server should keep native toolsets disabled for `api_server`; the web Hermes instance should expose only the `web` toolset. The backend sends only the explicit search query to this instance and records the call in the assistant action audit trail.

Ordinance import jobs use Redis/RQ. `docker compose up -d --build` starts the `worker` service; jobs can also be run inline from the admin UI in development. Search/crawl is restricted to configured official legal source domains. Configure embeddings with `EMBEDDINGS_RUNTIME`, `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY` and `EMBEDDINGS_MODEL` when moving beyond local hash embeddings.

### Voz (STT/TTS)

Voice is disabled by default. To enable transcription, set `SPEECH_TRANSCRIPTION_RUNTIME=nvidia_nim`, `NVIDIA_API_KEY` and `NVIDIA_WHISPER_FUNCTION_ID`; optional STT settings are `NVIDIA_RIVA_SERVER`, `SPEECH_TRANSCRIPTION_LANGUAGE_CODE` and `SPEECH_TRANSCRIPTION_MAX_BYTES`. With `SPEECH_TRANSCRIPTION_RUNTIME=nvidia_nim`, `NVIDIA_WHISPER_FUNCTION_ID` is required or transcription returns 503. The web microphone button is hidden unless transcription is enabled.

To enable synthesis, set `SPEECH_SYNTHESIS_RUNTIME=azure`, `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`. Optional TTS settings are `SPEECH_SYNTHESIS_VOICE` (default `es-ES-ElviraNeural`), `SPEECH_SYNTHESIS_LANGUAGE_CODE`, `SPEECH_SYNTHESIS_MAX_CHARS` and `SPEECH_SYNTHESIS_TIMEOUT_SECONDS`. The `Modo voz` toggle is shown only when transcription and synthesis are both enabled and the browser supports recording. The Azure resource used for development/evaluation may live in Azure for Students, but production or real-data use requires recreating the Speech resource in a pay-as-you-go subscription and reviewing the provider DPA/ENS position; this is an environment-only change.

Telegram is disabled by default. To enable it, set `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`, then configure the Telegram Bot API webhook to point to `/telegram/webhook` with the same secret token. Telegram text messages work without speech configuration. Telegram voice notes use the STT settings above.

Expected local split:

- Main assistant Hermes: `127.0.0.1:8642`, no native `api_server` toolsets exposed.
- Controlled web Hermes: `127.0.0.1:8643`, only `web` enabled.

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

The frontend, backend, PostgreSQL and Redis are published on localhost only.

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

The runtime backend image intentionally does not include `pytest`; the command
above installs dev-only dependencies in a disposable container. Unless
`TEST_DATABASE_URL` is explicitly set, the test harness creates a unique
`app_test_<uuid>` database and drops it after the run. Custom test databases
must keep an `app_test` prefix.

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
- Voice dialogue for the intake agent — shipped, see ADR-021
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
