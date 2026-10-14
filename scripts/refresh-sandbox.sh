#!/usr/bin/env bash
# Rebuild the sandbox database from the v1 deployment's, for end-to-end runs
# (docs/agents/verification.md). The deployment is only read: pg_dump takes a consistent
# snapshot. Data flows one way, into the sandbox, which never acts on the deployment's
# pending work: every integration starts from the copy's last event, and the deployment's
# queue of papers waiting for deep cards is dropped.
#
# Run from the repository root on the owner's server (docs/agents/dev-host.md). The
# containers and database names below are that server's and can be overridden.
set -euo pipefail

SOURCE_CONTAINER=${SOURCE_CONTAINER:-litwatch-v1-db}
SOURCE_USER=${SOURCE_USER:-litwatch}
SOURCE_DB=${SOURCE_DB:-litwatch}
TARGET_CONTAINER=${TARGET_CONTAINER:-litwatch-v1-dev}
TARGET_USER=${TARGET_USER:-postgres}
TARGET_DB=${TARGET_DB:-sandbox}

# The target is dropped and rebuilt, so only a database named for that is accepted.
case "$TARGET_DB" in
  sandbox*) ;;
  *) echo "refusing to replace '$TARGET_DB': the target's name must start with 'sandbox'" >&2; exit 1 ;;
esac
if [[ "$TARGET_CONTAINER" == "$SOURCE_CONTAINER" ]]; then
  echo "refusing to write into the source container '$SOURCE_CONTAINER'" >&2
  exit 1
fi

target_psql() {
  docker exec -i "$TARGET_CONTAINER" psql -v ON_ERROR_STOP=1 -q -U "$TARGET_USER" "$@"
}

echo "rebuilding $TARGET_CONTAINER/$TARGET_DB from $SOURCE_CONTAINER/$SOURCE_DB"
target_psql -d postgres -c "DROP DATABASE IF EXISTS \"$TARGET_DB\" WITH (FORCE)"
target_psql -d postgres -c "CREATE DATABASE \"$TARGET_DB\""
docker exec "$SOURCE_CONTAINER" pg_dump -U "$SOURCE_USER" -d "$SOURCE_DB" -Fc --no-owner --no-privileges \
  | docker exec -i "$TARGET_CONTAINER" pg_restore -U "$TARGET_USER" -d "$TARGET_DB" \
      --no-owner --no-privileges --exit-on-error

target_psql -d "$TARGET_DB" <<'SQL'
DELETE FROM integration_cursors;
DELETE FROM deep_waiting;
SQL

# Bring the copy up to this checkout's schema, which may be ahead of the deployment's.
set -a; . ./.env.v1; set +a
LITWATCH_DATABASE_URL="${LITWATCH_DATABASE_URL%/*}/$TARGET_DB" uv run litwatch db migrate

target_psql -d "$TARGET_DB" -Atc "SELECT 'papers: ' || count(*) FROM papers"
echo "done: point LITWATCH_DATABASE_URL at the database '$TARGET_DB'"
