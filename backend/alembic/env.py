from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.core.config import settings
from app.db.base import Base
from app.agent_office import models as agent_office_models  # noqa: F401
from app.assistant import models as assistant_models  # noqa: F401
from app.assets import models as asset_models  # noqa: F401
from app.documents import models as document_models  # noqa: F401
from app.geo import models as geo_models  # noqa: F401
from app.maintenance import models as maintenance_models  # noqa: F401
from app.municipalities import models as municipality_models  # noqa: F401
from app.ordinances import models as ordinance_models  # noqa: F401
from app.organizations import models as organization_models  # noqa: F401
from app.projects import models as project_models  # noqa: F401
from app.rbac import models as rbac_models  # noqa: F401
from app.requirements import models as requirement_models  # noqa: F401
from app.telegram import models as telegram_models  # noqa: F401
from app.users import models as user_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata

_AMBIGUOUS_LEGACY_REVISIONS = {
    "20260716_0026",
    "20260716_0027",
    "20260716_0028",
}
_RECONCILED_GEOGRAPHY_REVISION = "20260717_0029"


def _command_name() -> str | None:
    command_options = getattr(config, "cmd_opts", None)
    command = getattr(command_options, "cmd", None)
    if not command or not callable(command[0]):
        return None
    return str(command[0].__name__)


def _refuse_unsafe_legacy_history_mutation(connection) -> None:
    """Keep ambiguous historical schemas from moving in the wrong direction."""

    command_name = _command_name()
    if command_name not in {"upgrade", "downgrade", "stamp"}:
        return
    version_table_exists = connection.execute(
        text(
            "SELECT to_regclass("
            "format('%I.%I', current_schema(), 'alembic_version')"
            ") IS NOT NULL"
        )
    ).scalar_one()
    revisions = (
        set(
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        )
        if version_table_exists
        else set()
    )
    current_revision = next(iter(revisions)) if len(revisions) == 1 else None

    fingerprint = connection.execute(
        text(
            "SELECT "
            "to_regclass(format('%I.%I', current_schema(), "
            "'reference_dataset_versions')) IS NOT NULL "
            "OR to_regclass(format('%I.%I', current_schema(), "
            "'municipality_geography_snapshots')) IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'municipalities' "
            "AND column_name IN ('ine_check_digit', "
            "'directory_reference_date', 'directory_source_url', "
            "'directory_source_sha256')) AS has_geography, "
            "to_regclass(format('%I.%I', current_schema(), "
            "'assistant_message_attachments')) IS NOT NULL "
            "AS has_attachments, "
            "EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'assistant_message_attachments' "
            "AND column_name = 'authorization_scope') "
            "AS has_attachment_audit"
        )
    ).mappings().one()

    has_managed_legacy_objects = any(
        fingerprint[name]
        for name in ("has_geography", "has_attachments", "has_attachment_audit")
    )
    if current_revision is None and has_managed_legacy_objects:
        raise RuntimeError(
            "Ambiguous Alembic history detected: managed geography or "
            "attachment objects exist, but alembic_version is missing, empty, "
            "or contains multiple revisions. Audit the schema and revision "
            "history manually; automatic upgrade, downgrade, and stamp are "
            "refused."
        )

    if (
        fingerprint["has_geography"]
        and current_revision in _AMBIGUOUS_LEGACY_REVISIONS
        and command_name in {"downgrade", "stamp"}
    ):
        raise RuntimeError(
            "Unsafe legacy Alembic geography history detected at revision "
            f"{current_revision}. The first mutating operation must be "
            "`alembic upgrade 20260717_0029` (or `alembic upgrade head`). "
            "Downgrade and stamp are refused until reconciliation completes."
        )

    if fingerprint["has_geography"] and command_name == "stamp":
        raise RuntimeError(
            "Stamp is refused while municipality reference geography exists. "
            "Run the real Alembic upgrade/downgrade path, or audit and recover "
            "the schema manually; changing alembic_version alone is unsafe."
        )

    if (
        fingerprint["has_geography"]
        and command_name == "downgrade"
        and current_revision is not None
        and current_revision < _RECONCILED_GEOGRAPHY_REVISION
    ):
        raise RuntimeError(
            "Downgrade is refused because municipality reference geography "
            f"exists before reconciliation revision {_RECONCILED_GEOGRAPHY_REVISION}. "
            "Upgrade through the reconciliation first."
        )

    pre_rechain_attachments = (
        current_revision == "20260716_0026"
        and fingerprint["has_attachments"]
    ) or (
        current_revision == "20260716_0027"
        and fingerprint["has_attachment_audit"]
    )
    if pre_rechain_attachments:
        raise RuntimeError(
            "Unsupported pre-rechain assistant attachment migration history "
            f"detected at revision {current_revision}. Audit the complete "
            "schema fingerprint and recover manually; automatic upgrade, "
            "downgrade, and stamp are refused."
        )


def _include_object(
    object_,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to,
) -> bool:
    """Ignore only the table owned by the PostGIS extension."""

    return not (
        type_ == "table"
        and reflected
        and compare_to is None
        and name == "spatial_ref_sys"
        and getattr(object_, "schema", None) in {None, "public"}
    )


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=_include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
        )

        with context.begin_transaction():
            _refuse_unsafe_legacy_history_mutation(connection)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
