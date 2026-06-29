import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from app.admin.routes import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.assistant.routes import router as assistant_router
from app.core.config import settings
from app.db.session import SessionLocal
from app.documents.routes import router as documents_router
from app.geo.routes import router as geo_router
from app.municipalities.routes import router as municipalities_router
from app.ordinances.routes import router as ordinances_router
from app.ordinances.seed import ensure_initial_official_legal_sources
from app.organizations.routes import router as organizations_router
from app.projects.routes import router as projects_router
from app.rbac.permissions import ensure_initial_permissions
from app.requirements.routes import router as requirements_router
from app.telegram.routes import router as telegram_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed the permission catalog idempotently; skip when migrations have not
    # been applied yet so a fresh container can still boot and run alembic.
    try:
        with SessionLocal() as db:
            created_codes = ensure_initial_permissions(db)
            created_source_domains = ensure_initial_official_legal_sources(db)
        if created_codes:
            logger.info("Seeded permissions: %s", ", ".join(created_codes))
        if created_source_domains:
            logger.info(
                "Seeded official legal sources: %s",
                ", ".join(created_source_domains),
            )
    except SQLAlchemyError:
        logger.warning(
            "Could not seed permissions; run migrations and restart",
            exc_info=True,
        )
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)

app.include_router(auth_router)
app.include_router(health_router)
app.include_router(municipalities_router)
app.include_router(ordinances_router)
app.include_router(organizations_router)
app.include_router(admin_router)
app.include_router(projects_router)
app.include_router(documents_router)
app.include_router(requirements_router)
app.include_router(geo_router)
app.include_router(assistant_router)
app.include_router(telegram_router)
