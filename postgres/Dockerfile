ARG PGVECTOR_IMAGE=pgvector/pgvector:0.8.0-pg17@sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87
FROM ${PGVECTOR_IMAGE}

# Keep the existing PostgreSQL 17 + pgvector base and add only the PostGIS
# runtime/control files required by CREATE EXTENSION. Alembic remains the
# authority that enables the extension in each application database.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-17-postgis-3 \
        postgresql-17-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*
