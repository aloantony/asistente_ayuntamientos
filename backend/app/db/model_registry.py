"""Load every ORM model before standalone commands configure SQLAlchemy."""

from importlib import import_module

MODEL_MODULES = (
    "app.agent_office.models",
    "app.assistant.models",
    "app.assets.models",
    "app.documents.models",
    "app.geo.models",
    "app.government.models",
    "app.maintenance.models",
    "app.municipalities.models",
    "app.ordinances.models",
    "app.organizations.models",
    "app.projects.models",
    "app.rbac.models",
    "app.staff.models",
    "app.requirements.models",
    "app.reference_layers.models",
    "app.telegram.models",
    "app.users.models",
)


def register_all_models() -> None:
    """Import model modules so string-based relationships can be resolved."""
    for module_name in MODEL_MODULES:
        import_module(module_name)
