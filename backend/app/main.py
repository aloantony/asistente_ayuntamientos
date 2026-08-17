import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from app.admin.routes import router as admin_router
from app.agent_office.routes import router as agent_office_router
from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.assistant.routes import router as assistant_router
from app.administration.routes import router as administration_router
from app.assets.routes import router as assets_router
from app.budgets.routes import router as budgets_router
from app.communications.routes import router as communications_router
from app.core.config import settings
from app.db.session import SessionLocal
from app.documents.routes import router as documents_router
from app.geo.routes import router as geo_router
from app.government.routes import router as government_router
from app.heritage.routes import router as heritage_router
from app.maintenance.routes import router as maintenance_router
from app.municipal_data.routes import router as municipal_data_router
from app.municipalities.routes import router as municipalities_router
from app.ordinances.routes import router as ordinances_router
from app.ordinances.seed import ensure_initial_official_legal_sources
from app.organizations.branding_routes import router as branding_router
from app.organizations.routes import router as organizations_router
from app.plenos.routes import router as plenos_router
from app.projects.routes import router as projects_router
from app.rbac.permissions import ensure_initial_permissions
from app.requirements.routes import router as requirements_router
from app.reference_layers.routes import router as reference_layers_router
from app.reference_layers.wms_middleware import ReferenceWMSVaryMiddleware
from app.sede.routes import router as sede_router
from app.staff.routes import router as staff_router
from app.tasks.routes import router as tasks_router
from app.telegram.routes import router as telegram_router
from app.weather.routes import router as weather_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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


# La documentación interactiva describe cada endpoint, cada esquema y cada
# permiso. Es útil mientras se desarrolla y es un mapa regalado en cuanto la
# aplicación es alcanzable desde internet, así que se sirve solo en desarrollo.
_INTERACTIVE_DOCS = settings.is_development_like

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if _INTERACTIVE_DOCS else None,
    redoc_url="/redoc" if _INTERACTIVE_DOCS else None,
    openapi_url="/openapi.json" if _INTERACTIVE_DOCS else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"],
    allow_headers=["Authorization", "Content-Type", "X-Bootstrap-Admin-Token"],
    expose_headers=["X-Total-Count"],
)
app.add_middleware(ReferenceWMSVaryMiddleware)

for app_router in (
    auth_router,
    health_router,
    municipalities_router,
    ordinances_router,
    organizations_router,
    admin_router,
    projects_router,
    documents_router,
    requirements_router,
    geo_router,
    reference_layers_router,
    assets_router,
    maintenance_router,
    government_router,
    staff_router,
    tasks_router,
    municipal_data_router,
    administration_router,
    communications_router,
    budgets_router,
    plenos_router,
    sede_router,
    heritage_router,
    weather_router,
    branding_router,
    assistant_router,
    agent_office_router,
    telegram_router,
):
    app.include_router(app_router)
