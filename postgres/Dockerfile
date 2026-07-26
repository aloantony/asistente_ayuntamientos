ARG PGVECTOR_IMAGE=pgvector/pgvector:0.8.2-pg17-bookworm@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966
FROM ${PGVECTOR_IMAGE}

# Keep PostgreSQL 17 and the exact pgvector 0.8.2 runtime/control files used by
# existing persistent databases, then add only the PostGIS files required by
# CREATE EXTENSION. Alembic remains the authority that enables and reconciles
# extensions in each application database.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-17-postgis-3 \
        postgresql-17-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*
