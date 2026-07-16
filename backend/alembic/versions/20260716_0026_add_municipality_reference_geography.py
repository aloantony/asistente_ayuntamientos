"""add versioned municipality reference geography

Revision ID: 20260716_0026
Revises: 20260716_0025
Create Date: 2026-07-16 23:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260716_0026"
down_revision: Union[str, None] = "20260716_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "municipalities",
        sa.Column("ine_check_digit", sa.String(length=1), nullable=True),
    )
    op.add_column(
        "municipalities",
        sa.Column("directory_reference_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "municipalities",
        sa.Column("directory_source_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "municipalities",
        sa.Column("directory_source_sha256", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_municipalities_directory_provenance_complete",
        "municipalities",
        "num_nonnulls(ine_check_digit, directory_reference_date, "
        "directory_source_url, directory_source_sha256) in (0, 4)",
    )
    op.create_check_constraint(
        "ck_municipalities_ine_check_digit",
        "municipalities",
        "ine_check_digit is null or ine_check_digit ~ '^[0-9]$'",
    )
    op.create_check_constraint(
        "ck_municipalities_directory_has_ine_code",
        "municipalities",
        "directory_reference_date is null or ine_code ~ '^[0-9]{5}$'",
    )
    op.create_check_constraint(
        "ck_municipalities_directory_source_url",
        "municipalities",
        "directory_source_url is null or directory_source_url like 'https://%'",
    )
    op.create_check_constraint(
        "ck_municipalities_directory_source_sha256",
        "municipalities",
        "directory_source_sha256 is null "
        "or directory_source_sha256 ~ '^[0-9a-f]{64}$'",
    )

    op.create_table(
        "reference_dataset_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_key", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("version_label", sa.String(length=100), nullable=False),
        sa.Column("reference_date", sa.Date(), nullable=False),
        sa.Column("catalog_url", sa.Text(), nullable=False),
        sa.Column("download_url", sa.Text(), nullable=False),
        sa.Column("member_name", sa.String(length=255), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("license_name", sa.String(length=100), nullable=False),
        sa.Column("license_url", sa.Text(), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("national_row_count", sa.Integer(), nullable=False),
        sa.Column("target_row_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(dataset_key) <> ''",
            name="ck_ref_datasets_key_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(title) <> '' and btrim(version_label) <> ''",
            name="ck_ref_datasets_labels_nonempty",
        ),
        sa.CheckConstraint(
            "catalog_url like 'https://%' and download_url like 'https://%'",
            name="ck_ref_datasets_source_urls",
        ),
        sa.CheckConstraint(
            "license_url like 'https://%'",
            name="ck_ref_datasets_license_url",
        ),
        sa.CheckConstraint(
            "archive_sha256 ~ '^[0-9a-f]{64}$' "
            "and content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_ref_datasets_sha256",
        ),
        sa.CheckConstraint(
            "national_row_count > 0 and target_row_count > 0 "
            "and target_row_count <= national_row_count",
            name="ck_ref_datasets_row_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_key",
            "content_sha256",
            name="uq_ref_dataset_key_content_sha",
        ),
    )
    op.create_index(
        "ix_ref_datasets_key_reference_date",
        "reference_dataset_versions",
        ["dataset_key", "reference_date"],
        unique=False,
    )

    op.create_table(
        "municipality_geography_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("municipality_id", sa.Integer(), nullable=False),
        sa.Column("dataset_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "is_current",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("source_municipality_code", sa.String(length=11), nullable=False),
        sa.Column("relationship_id", sa.Integer(), nullable=False),
        sa.Column("geographic_code", sa.String(length=5), nullable=False),
        sa.Column("source_province_code", sa.String(length=2), nullable=False),
        sa.Column("source_province_name", sa.String(length=255), nullable=False),
        sa.Column("source_municipality_name", sa.String(length=255), nullable=False),
        sa.Column("source_population", sa.Integer(), nullable=False),
        sa.Column("surface_km2", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("perimeter_m", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("capital_ine_code", sa.String(length=11), nullable=False),
        sa.Column("capital_name", sa.String(length=255), nullable=False),
        sa.Column("capital_population", sa.Integer(), nullable=False),
        sa.Column("mtn25_sheet", sa.String(length=50), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=12, scale=9), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=12, scale=9), nullable=False),
        sa.Column("coordinate_origin", sa.String(length=100), nullable=False),
        sa.Column("altitude_m", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("altitude_origin", sa.String(length=100), nullable=False),
        sa.Column(
            "crs",
            sa.String(length=32),
            server_default="EPSG:4258",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_municipality_code ~ '^[0-9]{5}000000$'",
            name="ck_muni_geo_source_code",
        ),
        sa.CheckConstraint(
            "source_province_code ~ '^[0-9]{2}$' "
            "and source_province_code = left(source_municipality_code, 2)",
            name="ck_muni_geo_province_code",
        ),
        sa.CheckConstraint(
            "relationship_id > 0 and geographic_code ~ '^[0-9]{5}$'",
            name="ck_muni_geo_identifiers",
        ),
        sa.CheckConstraint(
            "source_population >= 0 and capital_population >= 0 "
            "and capital_population <= source_population",
            name="ck_muni_geo_populations",
        ),
        sa.CheckConstraint(
            "surface_km2 > 0 and perimeter_m > 0",
            name="ck_muni_geo_measurements",
        ),
        sa.CheckConstraint(
            "capital_ine_code ~ '^[0-9]{11}$' "
            "and left(capital_ine_code, 5) = left(source_municipality_code, 5)",
            name="ck_muni_geo_capital_code",
        ),
        sa.CheckConstraint(
            "longitude between -180 and 180 and latitude between -90 and 90",
            name="ck_muni_geo_coordinates",
        ),
        sa.CheckConstraint(
            "btrim(source_province_name) <> '' "
            "and btrim(source_municipality_name) <> '' "
            "and btrim(capital_name) <> '' and btrim(crs) <> ''",
            name="ck_muni_geo_labels_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["reference_dataset_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["municipality_id"],
            ["municipalities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "municipality_id",
            "dataset_version_id",
            name="uq_muni_geo_municipality_dataset",
        ),
        sa.UniqueConstraint(
            "dataset_version_id",
            "source_municipality_code",
            name="uq_muni_geo_dataset_source_code",
        ),
        sa.UniqueConstraint(
            "dataset_version_id",
            "relationship_id",
            name="uq_muni_geo_dataset_relationship",
        ),
        sa.UniqueConstraint(
            "dataset_version_id",
            "geographic_code",
            name="uq_muni_geo_dataset_geographic_code",
        ),
        sa.UniqueConstraint(
            "dataset_version_id",
            "capital_ine_code",
            name="uq_muni_geo_dataset_capital_code",
        ),
    )
    op.create_index(
        "ix_muni_geo_dataset_version_id",
        "municipality_geography_snapshots",
        ["dataset_version_id"],
        unique=False,
    )
    op.create_index(
        "uq_muni_geo_current",
        "municipality_geography_snapshots",
        ["municipality_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_muni_geo_current",
        table_name="municipality_geography_snapshots",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_index(
        "ix_muni_geo_dataset_version_id",
        table_name="municipality_geography_snapshots",
    )
    op.drop_table("municipality_geography_snapshots")
    op.drop_index(
        "ix_ref_datasets_key_reference_date",
        table_name="reference_dataset_versions",
    )
    op.drop_table("reference_dataset_versions")

    op.drop_constraint(
        "ck_municipalities_directory_source_sha256",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_directory_source_url",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_directory_has_ine_code",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_ine_check_digit",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_directory_provenance_complete",
        "municipalities",
        type_="check",
    )
    op.drop_column("municipalities", "directory_source_sha256")
    op.drop_column("municipalities", "directory_source_url")
    op.drop_column("municipalities", "directory_reference_date")
    op.drop_column("municipalities", "ine_check_digit")
