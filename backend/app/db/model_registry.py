"""Load every ORM model before standalone commands configure SQLAlchemy."""

from importlib import import_module

MODEL_MODULES = (
    "app.agent_office.models",
    "app.assistant.models",
    "app.administration.models",
    "app.assets.models",
    "app.budgets.models",
    "app.communications.models",
    "app.documents.models",
    "app.geo.models",
    "app.government.models",
    "app.heritage.models",
    "app.maintenance.models",
    "app.municipal_data.models",
    "app.municipalities.models",
    "app.ordinances.models",
    "app.organizations.branding",
    "app.organizations.models",
    "app.plenos.models",
    "app.projects.models",
    "app.rbac.models",
    "app.requirements.models",
    "app.reference_layers.models",
    "app.sede.models",
    "app.staff.models",
    "app.tasks.models",
    "app.telegram.models",
    "app.users.models",
)


def register_all_models() -> None:
    """Import model modules so string-based relationships can be resolved."""
    for module_name in MODEL_MODULES:
        import_module(module_name)
