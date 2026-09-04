"""Drop the reference layer mirror schema

El espejo cartográfico se retira: el mapa del municipio se dibuja ahora desde
un fichero estático que viaja con la aplicación, sin GeoServer, sin GeoWebCache
y sin sincronización con el proveedor. Estas treinta tablas sostenían aquel
espejo —catálogo, versiones observadas, estilos, entregas, atestaciones,
estrategias y ejecuciones de sincronización— y no queda código que las lea.

Todo lo que guardaban era cartografía derivada de fuentes públicas
(IDECyL, IGN, Catastro) y su rastro de sincronización: nada introducido por un
usuario del ayuntamiento. Los ficheros del espejo viven fuera de la base de
datos, en `reference_storage_root`, y se retiran aparte.

Revision ID: 20260904_0045
Revises: 20260903_0044
Create Date: 2026-09-04
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision: str = "20260904_0045"
down_revision: str | None = "20260903_0044"
branch_labels: str | None = None
depends_on: str | None = None


# En orden inverso al de creación. `CASCADE` cubre las claves ajenas entre
# ellas sin obligar a mantener aquí un grafo de dependencias que ya no
# describe ningún modelo.
TABLES = (
    "organization_reference_layer_settings",
    "reference_layer_mirror_strategy_dependencies",
    "reference_layer_mirror_strategies",
    "reference_layer_delivery_state",
    "reference_delivery_promotions",
    "reference_delivery_style_resources",
    "reference_delivery_style_parities",
    "reference_delivery_assets",
    "reference_delivery_version_artifacts",
    "reference_delivery_versions",
    "reference_style_parity_plan_resources",
    "reference_style_parity_plan_items",
    "reference_style_parity_plans",
    "reference_sync_run_artifacts",
    "reference_source_artifacts",
    "reference_sync_runs",
    "reference_style_update_reviews",
    "reference_style_update_checks",
    "reference_style_observed_versions",
    "reference_mirror_authorization_reviews",
    "reference_layer_sources",
    "reference_delivery_attestations",
    "reference_license_reviews",
    "reference_wms_capabilities_snapshots",
    "reference_layer_styles",
    "reference_layers",
    "reference_services",
    "reference_catalog_update_checks",
    "reference_catalog_observed_versions",
    "reference_catalog_snapshots",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def downgrade() -> None:
    # La cadena de migraciones de este repositorio es reversible de punta a
    # punta, y hay pruebas que lo comprueban bajando desde la cabeza hasta
    # revisiones muy anteriores. Soltar treinta tablas rompería esa propiedad:
    # al seguir bajando, las migraciones que las crearon intentarían soltarlas
    # otra vez y no encontrarían nada.
    #
    # Por eso el downgrade las devuelve tal y como estaban, desde el volcado que
    # acompaña a esta revisión. No es un esquema vivo —ningún modelo lo
    # describe ya— sino el peldaño que permite seguir bajando; las migraciones
    # de 20260717_0031 en adelante lo desmontan a continuación, cada una la
    # parte que creó.
    esquema = Path(__file__).with_name(
        "20260904_0045_reference_layer_mirror_schema.sql"
    )
    op.execute(esquema.read_text(encoding="utf-8"))
