import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
from app.core.logging import configure_logging
from app.core.middleware import OriginCsrfMiddleware, SecurityHeadersMiddleware
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
from app.security.routes import router as security_router
from app.sede.routes import router as sede_router
from app.staff.routes import router as staff_router
from app.tasks.routes import router as tasks_router
from app.telegram.routes import router as telegram_router
from app.town_hall.routes import router as town_hall_router
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


configure_logging()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    # Detrás del proxy la API vive bajo /api. El prefijo lo quita el proxy y
    # `root_path` mantiene coherentes las redirecciones y el esquema. Ver ADR-035.
    root_path=settings.api_root_path,
    # La documentación interactiva solo se publica en desarrollo (ADR-036).
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

# El orden importa: el primero que se añade es el más externo. Las cabeceras de
# seguridad se sellan fuera de todo para que también viajen en los rechazos.
app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)

if "*" not in settings.allowed_hosts_list:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts_list,
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

app.add_middleware(OriginCsrfMiddleware, allowed_origins=settings.cors_origins)

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
    town_hall_router,
    assistant_router,
    agent_office_router,
    telegram_router,
    security_router,
):
    app.include_router(app_router)
