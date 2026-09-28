#!/bin/sh
# Applies each migration once, in order, inside its own transaction.
# Never drops data. Safe to run on every `docker compose up`.
set -eu
: "${DATABASE_URL:?DATABASE_URL is required}"

until pg_isready -d "$DATABASE_URL" >/dev/null 2>&1; do
  echo "waiting for postgres..."; sleep 1
done

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
  "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"

for f in /migrations/*.sql; do
  name=$(basename "$f")
  done_already=$(psql "$DATABASE_URL" -tAq -c "SELECT 1 FROM schema_migrations WHERE name='${name}'")
  if [ "$done_already" = "1" ]; then
    echo "skip   $name"
    continue
  fi
  echo "apply  $name"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -1 \
    -f "$f" \
    -c "INSERT INTO schema_migrations(name) VALUES ('${name}')"
done
echo "migrations complete"
