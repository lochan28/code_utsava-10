# Official Postgres 16 + the HypoPG extension (hypothetical indexes).
# The official image already has the PostgreSQL apt repo, so this is one package.
FROM postgres:16
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-hypopg \
 && rm -rf /var/lib/apt/lists/*
COPY init.sql /docker-entrypoint-initdb.d/
