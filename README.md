# Asistente Ayuntamientos

## 1. Project overview

Asistente Ayuntamientos is the FastAPI + Next.js codebase for Anacleto, an operational AI agent for municipal work.

The target product and its limits are defined in [docs/vision-producto.md](docs/vision-producto.md). The current codebase is transitional: its municipal modules and supervised assistant are useful foundations, but requirements intake is a capability of Anacleto rather than the product itself.

## 2. Product objective and direction

The first user is the mayor, followed by the rest of the municipal staff. Each municipality will have its own instance and one organizational Anacleto, with private conversations and permission-scoped access to shared work and knowledge. Text and voice are the primary interface; projects, documents, requirements, ordinances and maps are contextual work surfaces.

Anacleto is intended to observe authorized events, plan, coordinate people and systems, and act within explicit, auditable policies and delegations. Risk determines whether it may act and notify, must ask permission, or must stop and escalate. It supports institutional municipal work, not personal, partisan or electoral activity.

The target deployment separates each municipality's operational instance from a central platform control plane and a reviewed knowledge network. The current multi-tenant `Organization` model remains an implemented security boundary during that transition; it must not be mistaken for the final shared deployment model.

All AI egress continues through the internal gateway. Contractually approved external providers may eventually process the data necessary for a task under municipal policy, minimization and traceability. The current stricter controls remain in force until those policies and contracts exist. Owned models can later be introduced behind the same gateway contract.

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
- `MunicipalProfile` and `MunicipalBlock`: an organization's town hall page — display name, shield and weather switch, plus a generic parent/position block tree that currently holds the configurable municipal menu.
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
- Town hall shell (`/ayuntamiento`): each organization configures its municipal bar — display name, a shield replaced by dropping an image on it, and a menu of sections and items created, renamed, reordered by drag and drop and deleted from its own editor. An optional block shows today's temperature for the municipality, fetched server-side from Open-Meteo (no API key) and cached for 30 minutes; it degrades to an em dash when the provider is unreachable. Section content is not implemented yet. Gated by `town_hall.view`, `town_hall.edit` and `town_hall.manage`. See ADR-022.
- Ordinance import and comparison: official-source import jobs run through a Redis/RQ worker, create pending-review ordinances, split legal text into reviewable/vectorized chunks and expose a thematic comparison matrix between municipalities.
- AI Requirements Intake Assistant: Anacleto is a model-first Spanish assistant that captures stakeholder needs as draft requirements. It streams web turns over SSE, calls the configured LLM runtime only through the Privacy/AI Gateway (`app/assistant/gateway.py`) and executes tools with the calling user's RBAC permissions. Requirements are always created as drafts with `source_type=conversation`, `create_requirement` requires a later human confirmation turn, and every tool call leaves an auditable JSON trail. Conversations are private to their author. Gated by the `assistant.use` permission; disabled (503) unless the selected runtime is configured.
- Web voice dialogue with Anacleto: when OpenAI Realtime is approved and enabled, the browser uses WebRTC with an ephemeral credential while every user transcript, tool call, confirmation and final turn remains server-owned and auditable. Exact safety confirmations are accepted only after their complete audio playback. The existing backend STT/TTS flow remains as a fallback: `MediaRecorder` audio is transcribed through `/assistant/audio-transcriptions` and responses are synthesized through `/assistant/speech`.
- Controlled institutional memory: the assistant can propose organization memory, but only entries reviewed by authorized users become reusable context. Proposing, viewing and reviewing are separated by `assistant.memory.propose`, `assistant.memory.view` and `assistant.memory.review`; municipal reviewers work from `/admin/memoria` with tenant isolation and optimistic concurrency protection.
- Local product feedback review: confirmed assistant feedback enters `/admin/producto`, a superuser-only transitional inbox. It is intentionally separate from municipal memory and does not yet anonymize or send records to a central platform.
- Telegram assistant channel: existing users can generate a short-lived one-use link code from the account page, link a Telegram chat and use the assistant through a separate audited conversation channel with the same RBAC permissions.

## 6. Architecture principles

- Access control is tenant-aware through `Organization`.
- Privileged platform operations are superuser-only: granting or revoking superuser status, creating organizations (tenants), and mutating the global roles/permissions catalog. `users.manage` only reaches users who share an organization where the admin holds the permission.
- Municipalities are global reference data, separate from tenant organizations. Linking a document to an ordinance requires access to that document.
- List endpoints for municipalities, ordinances, requirements and admin users are paginated (`limit` 1-200 default 100, `offset`) and expose the total via the `X-Total-Count` header. Ordinance listings omit `text_content`; the full legal text only travels on the detail endpoint.
- Imported ordinances are never approved automatically: importer output enters `pending_review`, the review agent stores a checklist and score, and a user with `ordinances.review` must approve, reject or request changes.
- Legal chunks are stored in PostgreSQL and use pgvector when available. Development uses deterministic local hash embeddings by default; production can switch to a configured OpenAI-compatible embeddings provider.
- Assistant voice capture and playback stay in the browser. In the fallback flow, STT/TTS run only through backend endpoints in `app/assistant/speech.py`, with no browser cloud recognition or `speechSynthesis` fallback (see ADR-021). When Realtime is explicitly enabled, browser audio is sent directly to the approved provider over WebRTC using a short-lived credential; tool execution and durable conversation state never leave the backend.
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

Municipal shields share this volume under `organizations/<id>/brand/`, capped by `MUNICIPAL_SHIELD_MAX_UPLOAD_BYTES` (2 MiB by default) and restricted to images. They deliberately bypass the `Document` model, which requires a project.

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

Anacleto v2 is model-first: the backend no longer runs a semantic planner/router or deterministic answer templates. Each turn calls the configured runtime through `gateway.py`, injects only the user-visible context and filtered tool list, and executes tools with backend RBAC/tenancy checks. Web clients should use `POST /assistant/conversations/{id}/messages/stream` for SSE frames (`message_start`, `text_delta`, `tool_activity`, `done`); the classic `POST /assistant/conversations/{id}/messages` remains available for synchronous clients and Telegram. `ASSISTANT_HISTORY_MAX_MESSAGES` controls the recent message window sent to the model.

Controlled web search uses a second local Hermes API Server instance/profile, separate from the main assistant runtime. Configure `HERMES_WEB_BASE_URL`, `HERMES_WEB_API_KEY`, `HERMES_WEB_MODEL` and grant `assistant.web.search` only to users who may search the public web from the assistant. The main Hermes API server should keep native toolsets disabled for `api_server`; the web Hermes instance should expose only the `web` toolset. The backend sends only the explicit search query to this instance and records the call in the assistant action audit trail.

Ordinance import jobs use Redis/RQ. `docker compose up -d --build` starts the `worker` service; jobs can also be run inline from the admin UI in development. Search/crawl is restricted to configured official legal source domains. Configure embeddings with `EMBEDDINGS_RUNTIME`, `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY` and `EMBEDDINGS_MODEL` when moving beyond local hash embeddings.

### Voz realtime y fallback STT/TTS

Voice is disabled by default in `.env.example`. Realtime voice requires a contractually approved provider because the browser sends the conversation audio directly to it. To enable it, set `ASSISTANT_REALTIME_ENABLED=true` and `OPENAI_API_KEY`. Optional settings include `ASSISTANT_REALTIME_MODEL`, `ASSISTANT_REALTIME_VOICE`, the server-side transcription model/language/delay and the VAD thresholds. The backend issues only short-lived client credentials, persists the authoritative transcript and actions, executes tools with the current user's RBAC permissions, and seals interrupted turns before another workflow can continue.

The non-realtime fallback remains available. To enable transcription, set `SPEECH_TRANSCRIPTION_RUNTIME=nvidia_nim`, `NVIDIA_API_KEY` and `NVIDIA_WHISPER_FUNCTION_ID`; optional STT settings are `NVIDIA_RIVA_SERVER`, `SPEECH_TRANSCRIPTION_LANGUAGE_CODE` and `SPEECH_TRANSCRIPTION_MAX_BYTES`. With `SPEECH_TRANSCRIPTION_RUNTIME=nvidia_nim`, `NVIDIA_WHISPER_FUNCTION_ID` is required or transcription returns 503. The web microphone button is hidden unless either realtime or fallback transcription is enabled.

To enable fallback synthesis, set `SPEECH_SYNTHESIS_RUNTIME=azure`, `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`. Optional TTS settings are `SPEECH_SYNTHESIS_VOICE` (default `es-ES-DarioNeural`), `SPEECH_SYNTHESIS_LANGUAGE_CODE`, `SPEECH_SYNTHESIS_OUTPUT_FORMAT`, `SPEECH_SYNTHESIS_RATE`, `SPEECH_SYNTHESIS_MAX_CHARS` and `SPEECH_SYNTHESIS_TIMEOUT_SECONDS`. The Azure resource used for development/evaluation may live in Azure for Students, but production or real-data use requires recreating the Speech resource in a pay-as-you-go subscription and reviewing the provider DPA/ENS position; this is an environment-only change.

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

The roadmap follows the transition from the current supervised assistant to the product defined in `docs/vision-producto.md`:

- Consolidate product feedback and institutional-memory review without mixing platform and municipal authority.
- Add municipal roles, competencies, delegations and a risk-based autonomy policy.
- Generalize memory into permission-scoped information with consent and retention policies.
- Evolve the agent office into the event, action, approval and audit substrate for proactive work.
- Make Anacleto the persistent primary experience and expose modules as contextual work surfaces.
- Add controlled connectors to official municipal systems.
- Build the anonymized improvement network and central fleet control plane.
- Add owned model runtimes and a separate citizen assistant only in later phases.

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
