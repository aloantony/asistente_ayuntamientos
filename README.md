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
- `AssistantCanvasDocument`: private, non-official Markdown working draft linked to an assistant conversation, with immutable revisions. It is separate from uploaded `Document` files and official `Ordinance` records.
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
- Ordinance import and comparison: official-source import jobs run through a Redis/RQ worker, create pending-review ordinances, split legal text into reviewable/vectorized chunks and expose a thematic comparison matrix between municipalities.
- AI Requirements Intake Assistant: Anacleto is a model-first Spanish assistant that captures stakeholder needs as draft requirements. It streams web turns over SSE, calls the configured LLM runtime only through the Privacy/AI Gateway (`app/assistant/gateway.py`) and executes tools with the calling user's RBAC permissions. Requirements are always created as drafts with `source_type=conversation`, `create_requirement` requires a later human confirmation turn, and every tool call leaves an auditable JSON trail. Conversations are private to their author. Gated by the `assistant.use` permission; disabled (503) unless the selected runtime is configured.
- Assistant document canvas: Anacleto and the conversation owner can develop private, non-official Markdown drafts in a contextual editor with autosave, optimistic concurrency, immutable revision history and restore. Canvas tools use the narrowly scoped `draft_write/direct` policy; they never publish or promote content into `Document`, `Ordinance` or the legal corpus. See [docs/lienzo-documentos.md](docs/lienzo-documentos.md).
- The capability-parity scope, security gates and phased delivery plan for web reading, attachments, code/data analysis, images, connectors, browser automation and durable agents are maintained in [docs/herramientas-asistente.md](docs/herramientas-asistente.md).
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
- Uploaded document bytes are stored outside PostgreSQL.
- PostgreSQL stores uploaded-document metadata plus the Markdown and immutable revisions of assistant canvas drafts; it does not store uploaded raw file bytes.
- Uploaded files are stored in a persistent Docker volume.
- Important business objects should avoid hard delete where archive or status fields are available.
- A superuser bypass exists for administration, but normal users are permission-controlled.
- Backend and frontend should remain bound to localhost in local development unless deployment is intentionally changed.

## 7. Uploaded document storage

Documents are stored on our own server in the current architecture. They are not stored in external S3/AWS storage.

`DOCUMENT_STORAGE_ROOT` controls the filesystem path used by the backend to store uploaded files. In Docker Compose, the `document_storage` volume is mounted at `/var/lib/asistente_ayuntamientos/documents`, which is the default path configured in `.env.example`.

The `document_storage` Docker volume persists uploaded files across container rebuilds and restarts. PostgreSQL stores their metadata, not raw uploaded bytes. Canvas Markdown is intentionally different: it is structured application data and remains in PostgreSQL with its revision history.

## 8. Local development setup

Requirements:

- Docker
- Docker Compose

Create a local environment file:

```bash
cp .env.example .env
```

Configure secrets and local settings in `.env`. At minimum, review `SECRET_KEY`, `BOOTSTRAP_ADMIN_TOKEN`, `CORS_ALLOWED_ORIGINS`, `NEXT_PUBLIC_API_BASE_URL`, database settings and document storage settings.

To enable the AI assistant with Anthropic, keep `ASSISTANT_RUNTIME=anthropic` and set `ANTHROPIC_API_KEY` (optionally `ASSISTANT_MODEL`, default `claude-opus-4-8`). To use the OpenAI Responses API, set `ASSISTANT_RUNTIME=openai_responses`, `OPENAI_API_KEY` and, optionally, `OPENAI_RESPONSES_MODEL`, `OPENAI_RESPONSES_REASONING_EFFORT`, `OPENAI_RESPONSES_MAX_OUTPUT_TOKENS` and `OPENAI_RESPONSES_BASE_URL`. The default Responses model is `gpt-5.6`; its separate output cap defaults to 25,000 tokens so reasoning and the visible answer share sufficient headroom. Production deployments may use the approved regional API endpoint. An OpenAI API project and its separately billed API key are required: a ChatGPT subscription is not an API credential. To use Hermes Agent, run its API Server privately, set `ASSISTANT_RUNTIME=hermes_agent`, `HERMES_AGENT_BASE_URL`, `HERMES_AGENT_API_KEY` and `HERMES_AGENT_MODEL`. Hermes remains unavailable until an operator has technically verified that its main API Server exposes no native toolsets and sets `HERMES_AGENT_NATIVE_TOOLS_DISABLED_CONFIRMED=true`; an assumption or prompt instruction is not an attestation. Even then, this application omits and rejects `web_search` and `read_web_page` for the Hermes conversational runtime because their post-taint boundary cannot govern tools native to that server. Anthropic, OpenAI Responses and the development-only Codex bridge keep the controlled backend web tools. In production, Hermes Agent additionally stays disabled for real data unless `HERMES_AGENT_REAL_DATA_ALLOWED=true`. Without a complete runtime configuration, assistant endpoints return 503 and the UI shows the assistant as not configured.

For local evaluation only, `ASSISTANT_RUNTIME=codex_subscription` connects through the official `codex app-server` to a separately authenticated ChatGPT/Codex subscription. It does not turn a ChatGPT subscription into an API key and it is rejected unless `ENVIRONMENT=development`; `CODEX_SUBSCRIPTION_ENABLED` and `CODEX_SUBSCRIPTION_REAL_DATA_ALLOWED` are independent mandatory opt-ins and default to `false`. The bridge keeps municipal tools in the backend: Codex pauses on a dynamic tool request, then the existing loop applies RBAC, confirmation, audit, call budgets and Brave Search before resuming the same ephemeral turn. Use an exclusive `CODEX_SUBSCRIPTION_HOME` with mode `0700`; its `auth.json` must be a regular, non-symlink file with mode `0600`, and the developer's normal `~/.codex` must never be reused or copied. The bridge is validated against `codex-cli 0.144.4`. The standard backend image does not bundle the Codex CLI, and the possible sidecar/socket isolation is not implemented, so the current evaluation runs the backend on the isolated host with only PostgreSQL and Redis in Docker. Setup, executable host-run commands, isolation requirements and the known drawbacks are documented in [docs/runtime-codex-suscripcion.md](docs/runtime-codex-suscripcion.md). The underlying dynamic-tool API is experimental; this route has no production SLA and must be reevaluated when switching to Responses.

Anacleto v2 is model-first: the backend no longer runs a semantic planner/router or deterministic answer templates. Each turn calls the configured runtime through `gateway.py`, injects only the user-visible context and filtered tool list, and executes tools with backend RBAC/tenancy checks. The Responses integration is stateless at the provider (`store=false`) and intentionally supports the GPT-5.6 family: the application remains authoritative for conversations, memory, permissions, tool execution and audit. Reasoning items needed across a tool loop are encrypted by OpenAI and retained only in memory for that turn. Web clients should use `POST /assistant/conversations/{id}/messages/stream` for SSE frames (`message_start`, `text_delta`, optional `text_reset`, `tool_activity`, `done`); `text_reset` replaces speculative deltas when a continuation, refusal or upstream failure changes the canonical message. Voice playback starts only from the canonical `done` message, split into sentence-aware chunks no longer than the synthesis limit reported by `/assistant/status`. The classic `POST /assistant/conversations/{id}/messages` remains available for synchronous clients and Telegram. `ASSISTANT_HISTORY_MAX_MESSAGES` controls the recent message window sent to the model. `ASSISTANT_MAX_TOOL_ITERATIONS` limits tool rounds and `ASSISTANT_MAX_TOOL_CALLS` independently caps total calls in a turn, including parallel calls from one model response. On either limit, the backend disables tools for one final synthesis so the turn ends with an honest answer instead of another tool request. `ASSISTANT_TURN_TIMEOUT_SECONDS` adds a monotonic wall-clock budget checked between model and tool operations, while `ASSISTANT_GATEWAY_TIMEOUT_SECONDS` caps every individual model request and is reduced to the remaining turn budget. Provider retries are disabled so one request cannot silently multiply that timeout.

Chat messages may explicitly reference up to `ASSISTANT_MAX_ATTACHMENTS_PER_MESSAGE` existing project documents, or upload a document through the same controlled `Document` storage first. Only bounded, valid UTF-8 from plain-text files is placed in provider context for that single turn. PDF, DOCX, XLSX and every other structured format are retained as attachments but reported as `unsupported`; images expose metadata/preview only and are never analyzed. Structured parsing requires a future dedicated non-root service with no network access, a read-only filesystem and cgroup limits—it is intentionally not performed in the API request process. The durable message relation stores document identity, authorization timestamp/actor/scope, order and extraction status/count, never extracted text. Preflight retains only an immutable identity snapshot and explicitly rolls its transaction back, which also discards any unrelated pending DML; verified TXT bytes are then read without an active database transaction or authorization/row locks. The message-and-attachment commit is the linear authorization boundary: a canonical RBAC transaction lock plus deterministic conversation/user/document/project row locks reload and revalidate access and identity. Locks are released before provider I/O, and later revocations hide metadata without rewriting history. Attachment-tainted turns have an empty tool catalog and a fail-closed execution guard. `ASSISTANT_RUNTIME=hermes_agent` rejects every attachment turn before file or gateway I/O because the backend cannot prove that Hermes' internal agent tools are disabled; an empty API tool list alone is not an isolation boundary. Extracted content is untrusted data, never instructions. Per-file, aggregate and extraction-byte limits are configured with `ASSISTANT_ATTACHMENT_MAX_CONTEXT_CHARS`, `ASSISTANT_ATTACHMENT_TOTAL_CONTEXT_CHARS` and `ASSISTANT_ATTACHMENT_MAX_EXTRACT_BYTES`; concurrent TXT preparation is bounded by `ASSISTANT_ATTACHMENT_TEXT_MAX_CONCURRENCY` and consumes the turn deadline.

The turn deadline is cooperative and does not kill Python work in another thread: an already-running tool is allowed to return safely, then no further tool or model call is started. Consequently, a turn may exceed its orchestration deadline by the timeout of the in-flight tool; network-backed tools must keep their own provider timeout configured (for example `BRAVE_SEARCH_TIMEOUT_SECONDS`). Gateway stalls are bounded by the smaller gateway/remaining-turn provider timeout, continuous streams are checked between chunks, and a timeout is persisted as an explicit terminal assistant reply rather than leaving the SSE stream open. These are provider/socket I/O timeouts, not unsafe process interruption; a peer that continuously trickles a non-stream response can only be stopped once the current read returns.

Controlled web search is selected explicitly with `WEB_SEARCH_PROVIDER=brave|hermes|disabled`; provider failures never trigger an automatic fallback. `.env.example` selects Brave for new configurations, while the code-level default remains Hermes so existing installations do not switch processors implicitly. Brave uses its fixed HTTPS Web Search endpoint. Configure `BRAVE_SEARCH_API_KEY` and grant `assistant.web.search` only to users who may search the public web from the assistant. Optional locale and timeout settings are documented in `.env.example`. Hermes remains available as a compatibility provider through `HERMES_WEB_*` settings and still requires a separate, web-only local instance.

The backend sends only the normalized explicit query, caps it at 400 characters and 50 words, applies strict safe search, bounds the response and records the query and returned sources in the assistant action audit trail. It blocks obvious email, DNI/NIE and Spanish telephone formats, including common spacing and hyphenation, but this is not a complete DLP policy: names and postal addresses require a separately reviewed policy. Search snippets are treated as untrusted content, never as instructions. Brave documents API-query retention and requires a plan with explicit storage rights to persist any part of search results. Because the application persists those results in every environment, Brave remains unavailable unless `BRAVE_SEARCH_STORAGE_RIGHTS_CONFIRMED=true`; set that flag only after the DPA/retention review and compatible plan are confirmed. See the [Brave Search API privacy policy](https://api-dashboard.search.brave.com/privacy-policy) and [API terms](https://api-dashboard.search.brave.com/documentation/resources/terms-of-service).

`read_web_page` is an experimental, independently gated second step after `web_search`; it is disabled by default and requires `ASSISTANT_WEB_READER_ENABLED=true`. It accepts only an exact normalized URL included in the search payload visible to the model during the same text turn. The authorization records query, provider and result rank, is ephemeral and is unavailable to later turns. Realtime voice intentionally exposes only `web_search` snippets: the status fields `web_page_reader_enabled` and `realtime_web_page_reader_enabled` make that distinction explicit, and no complete page body enters `conversation.state`.

The reader is anonymous and read-only, sends no cookies or credentials, executes no JavaScript, accepts only bounded HTML/XHTML or plain text, and does not support remote PDF extraction. It allows only ports 80 and 443; rejects private, loopback, link-local, reserved and metadata destinations; validates every DNS answer; and pins the connection to a validated public IP while retaining hostname-based Host, SNI and certificate verification. Redirects must remain on the exact same scheme, hostname and effective port; a cross-origin target requires a new search. URL validation, DNS, connect/TLS, headers, redirects, bounded body reads and extraction all run in one disposable process. Parent and child communicate through a Unix stream socket: the parent reads incrementally with a selector and the same absolute deadline, requires EOF to complete the JSON message, and rejects more than 512 KiB. A worker that stops after a partial message is therefore terminated and reaped without a blocking framed receive.

`WEB_PAGE_TIMEOUT_SECONDS` is one absolute deadline that starts before admission/process creation and covers the complete operation, while `WEB_PAGE_DNS_TIMEOUT_SECONDS` additionally caps each DNS resolution. `WEB_PAGE_MAX_CONCURRENT_READERS` (default 4) is a process-wide admission limit; a saturated server fails fast instead of creating more workers. `Process.start()` runs in a bounded launcher thread. If it returns after the request deadline, that launcher kills/reaps the late child and releases the slot only after verifying that the process is dead, reaped and closed; until then the slot remains reserved. Reaping, terminate and kill use bounded joins and may add at most 0.55 seconds after the configured deadline. If any kill, liveness, reap or close result is uncertain, the lease is permanently quarantined instead of being returned to the semaphore, the process handle is never closed while it may still be alive, and the backend emits the critical counter-style log `web_reader_quarantined_slots_total`; this deliberately degrades the bounded reader capacity rather than risking double ownership of a live worker. A kernel primitive that is itself permanently blocked cannot be forcibly cancelled from Python and remains a local host failure; the caller still returns at the request deadline for a blocked process launch and the retained slot keeps concurrent reader resource use bounded rather than allowing unbounded launches.

As soon as the first successful search exposes a title or snippet, the text runtime permits only `read_web_page` calls for any of the exact URLs already present in that initial search provenance. Its post-taint input must be exactly `{"url": "<canonical URL>"}`: one key, no extra fields, and a raw URL string identical to the canonical provenance key. Fragments, userinfo, case or normalization variants and additional fields are denied even if they would otherwise resolve to the same address. It blocks further web searches, local/semantic tools, mutations and unknown tool names; reading one authorized page does not authorize new URLs or searches. Realtime exposes no page reader, so it blocks every tool after that first search. The execution boundary enforces the same rule even outside the model loop. Names and complete inputs of all post-taint denied calls are replaced with constants before events, actions or durable state are written; an allowed page read emits and persists only a freshly constructed canonical one-key input. Realtime also replaces the model-controlled call ID with a domain-separated HMAC-SHA-256 correlation ID keyed by the application secret. Legacy open Realtime turns reconstruct the taint from finalized search actions and fail closed when their result is ambiguous.

The model receives the bounded text only inside the active text-tool loop and must cite the `final_url` actually downloaded while also showing the original `source_url` when it differs. The durable action audit contains no text or preview: it retains source/final URLs, search query/provider/rank, content type, byte and character lengths, SHA-256 of the extracted text, truncation state and the bounded redirect chain. The audit itself stays below 4,000 characters; for pathological long chains it stores a count and SHA-256 summary, and if necessary a bounded source prefix plus hash, while preserving the exact final URL used for citation.

Ordinance import jobs use Redis/RQ. `docker compose up -d --build` starts the `worker` service; jobs can also be run inline from the admin UI in development. Search/crawl is restricted to configured official legal source domains. Configure embeddings with `EMBEDDINGS_RUNTIME`, `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_MODEL`, `EMBEDDINGS_TIMEOUT_SECONDS` and `EMBEDDINGS_MAX_CONCURRENT_WORKERS` when moving beyond local hash embeddings. Agent Office persists the provider deadline before releasing its claim transaction; supervised embedding calls then run without a database session or row lock. Late or indeterminately cleaned workers quarantine their attempt and capacity slot instead of retrying automatically.

### Voz realtime y fallback STT/TTS

Voice is disabled by default in `.env.example`. Realtime voice requires a contractually approved provider because the browser sends the conversation audio directly to it. To enable it, set `ASSISTANT_REALTIME_ENABLED=true` and `OPENAI_API_KEY`. This separately billed API credential is required even when the conversational runtime is `codex_subscription`; a ChatGPT/Codex subscription does not include Realtime. Optional settings include `ASSISTANT_REALTIME_MODEL`, `ASSISTANT_REALTIME_VOICE`, the server-side transcription model/language/delay and the VAD thresholds. The backend issues only short-lived client credentials, persists the authoritative transcript and actions, executes tools with the current user's RBAC permissions, and seals interrupted turns before another workflow can continue. Realtime may use bounded `web_search` snippets but never exposes `read_web_page`; full page reading is text-chat-only until a design can guarantee that bodies never enter persisted Realtime state.

The non-realtime fallback remains available. To enable transcription, set `SPEECH_TRANSCRIPTION_RUNTIME=nvidia_nim`, `NVIDIA_API_KEY` and `NVIDIA_WHISPER_FUNCTION_ID`; optional STT settings are `NVIDIA_RIVA_SERVER`, `SPEECH_TRANSCRIPTION_LANGUAGE_CODE` and `SPEECH_TRANSCRIPTION_MAX_BYTES`. With `SPEECH_TRANSCRIPTION_RUNTIME=nvidia_nim`, `NVIDIA_WHISPER_FUNCTION_ID` is required or transcription returns 503. The web microphone button is hidden unless either realtime or fallback transcription is enabled.

To enable fallback synthesis, set `SPEECH_SYNTHESIS_RUNTIME=azure`, `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`. Optional TTS settings are `SPEECH_SYNTHESIS_VOICE` (default `es-ES-DarioNeural`), `SPEECH_SYNTHESIS_LANGUAGE_CODE`, `SPEECH_SYNTHESIS_OUTPUT_FORMAT`, `SPEECH_SYNTHESIS_RATE`, `SPEECH_SYNTHESIS_MAX_CHARS` and `SPEECH_SYNTHESIS_TIMEOUT_SECONDS`. The Azure resource used for development/evaluation may live in Azure for Students, but production or real-data use requires recreating the Speech resource in a pay-as-you-go subscription and reviewing the provider DPA/ENS position; this is an environment-only change.

Telegram is disabled by default. To enable it, set `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`, then configure the Telegram Bot API webhook to point to `/telegram/webhook` with the same secret token. Telegram text messages work without speech configuration. Telegram voice notes use the STT settings above.

Expected local split:

- Main assistant Hermes: `127.0.0.1:8642`; verify technically that no native `api_server` toolset remains, then record that attestation with `HERMES_AGENT_NATIVE_TOOLS_DISABLED_CONFIRMED=true`. Without it the runtime fails closed.
- Controlled web Hermes: `127.0.0.1:8643`, only `web` enabled.

Build and start the stack:

```bash
docker compose up -d --build
```

Apply database migrations:

```bash
docker compose exec backend alembic upgrade head
```

Complete or refresh the Castilla y León municipality catalogue from the
reviewed INE snapshots. The directory is the official relation at 1 January
2026 (2,248 municipalities) and population is the official revision at
1 January 2025. Always run and review the dry-run before applying:

```bash
docker compose exec -T backend \
  python -m app.municipalities.ine_directory

docker compose exec -T backend \
  python -m app.municipalities.ine_directory --apply
```

The command creates missing official municipalities by five-digit INE code,
normalizes their official identity, and records the directory date, control
digit, URL and SHA-256 alongside the population provenance. It reports but does
not delete or merge legacy rows that are absent from the official directory.
For an offline run, pass
`--directory-workbook /path/diccionario26.xlsx` and
`--population-archive /path/pobmun.zip`; both reviewed checksums are still
enforced. See [docs/catalogo-municipal-castilla-leon.md](docs/catalogo-municipal-castilla-leon.md).

Load the reviewed IGN NGMEP 2026 reference geography only after the directory
sync. The dry-run validates both ZIP and CSV hashes, all 8,132 national rows,
the 2,248 Castilla y León codes and every provincial count:

```bash
docker compose exec -T backend \
  python -m app.municipalities.ngmep_geography --year 2026

docker compose exec -T backend \
  python -m app.municipalities.ngmep_geography --year 2026 --apply
```

The import creates a versioned official snapshot with surface, perimeter,
capital code/name/population, MTN25 sheet, ETRS89 coordinates, altitude, field
origins, source hashes and CC BY 4.0 attribution. It mirrors the current
surface to `Municipality` and derives density from the canonical INE population.
NGMEP population is retained only for comparison because it is not identical
to the INE population snapshot. The published point identifies the population
nucleus of the municipal capital; it is not a centroid of the municipal term.
For an offline run, pass `--archive /path/BD_Municipios-Entidades.zip`; the
reviewed archive and inner CSV hashes remain mandatory.

Synchronize the reviewed INE 2025 municipal population snapshot only after the
migration is applied. The first command is a mandatory dry-run and never writes
to the database. Review its conflicts and unmatched codes before applying:

```bash
docker compose exec -T backend \
  python -m app.municipalities.ine_population --year 2025

docker compose exec -T backend \
  python -m app.municipalities.ine_population \
  --year 2025 --apply --overwrite-existing
```

`--overwrite-existing` only permits replacement of figures without recorded
provenance. It never overwrites a newer official year, a different source, or a
different revision of the same annual source. For a controlled/offline run,
pass the previously downloaded official ZIP with `--archive /path/pobmun.zip`;
the inner XLSX checksum is still verified. The synchronization is intentionally
not part of application startup.

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
