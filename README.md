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

Local AI is not implemented in the current phase.

Future priorities will be refined through Requirements Intake and through work with municipal stakeholders and developers. The exact first commercial module and user persona are intentionally still open.

## 3. Current technical stack

- Backend: FastAPI
- Frontend: Next.js
- Database: PostgreSQL
- Cache or future worker support: Redis
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

- Authentication: JWT login, `/auth/me` session restoration and first-admin bootstrap.
- Users and groups: administrative management with safe deletion behavior.
- Roles and permissions: RBAC model for administrative and functional capabilities.
- Organizations: tenant foundation for client entities using the application.
- Projects: organization-scoped expedientes, work areas and initiatives.
- Documents: upload, metadata, download and archive support for project documents.
- Requirements: structured intake for needs, product ideas and stakeholder requests.
- Municipalities: global reference data for real-world municipalities.
- Ordinances: structured ordinance records linked to municipalities and optionally documents.

## 6. Architecture principles

- Access control is tenant-aware through `Organization`.
- Municipalities are global reference data, separate from tenant organizations.
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

- Ordinance Comparison v1
- Ordinance AI Assistant v1
- Draft Generator v1
- Privacy/AI Gateway
- Future voice-based requirement intake
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
