#!/usr/bin/env bash
# Rebuild the sandbox database from the v1 deployment's, for end-to-end runs
# (docs/agents/verification.md). Run from the repository root on the owner's server
# (docs/agents/dev-host.md).
#
# The deployment is only read: pg_dump takes a consistent snapshot. The copy is built as
# `sandbox_next` and replaces `sandbox` only once it is complete, so a failed refresh leaves
# the previous sandbox as it was. The copy keeps the deployment's pipeline backlog (it is
# realistic state), but none of its external work: copied events count as already
# delivered, so an integration enabled in dev starts after them, and the deep jobs and the
# Zotero and WebDAV items that point at the deployment's targets are dropped.
#
#   --no-migrate   keep the deployment's schema, to take a baseline before a migration
set -Eeuo pipefail

readonly SOURCE=litwatch-v1-db TARGET=litwatch-v1-dev DB=sandbox NEXT=sandbox_next OLD=sandbox_old

migrate=1
case "${1:-}" in
  "") ;;
  --no-migrate) migrate=0 ;;
  *) echo "usage: $0 [--no-migrate]" >&2; exit 2 ;;
esac

# Migrations connect through .env.v1's DSN; it must reach the same container the copy is
# restored into.
set -a; . ./.env.v1; set +a
dsn_server=$(sed -E 's#^[a-z]+://[^@]*@([^/]+)/.*#\1#' <<<"$LITWATCH_DATABASE_URL")
if [[ "$dsn_server" != "$(docker port "$TARGET" 5432/tcp | head -1)" ]]; then
  echo "LITWATCH_DATABASE_URL in .env.v1 does not point at $TARGET" >&2
  exit 1
fi

target_psql() {
  docker exec -i "$TARGET" psql -v ON_ERROR_STOP=1 -q -U postgres "$@"
}

drop_next() { target_psql -d postgres -c "DROP DATABASE IF EXISTS $NEXT WITH (FORCE)"; }
trap 'echo "refresh failed; $NEXT dropped" >&2; drop_next' ERR

echo "copying $SOURCE into $TARGET/$NEXT"
drop_next
target_psql -d postgres -c "CREATE DATABASE $NEXT"
docker exec "$SOURCE" pg_dump -U litwatch -d litwatch -Fc --no-owner --no-privileges \
  | docker exec -i "$TARGET" pg_restore -U postgres -d "$NEXT" --no-owner --no-privileges --exit-on-error

# Transaction ids belong to the cluster that wrote them. Every copied event gets xid 1,
# below any transaction here, so all of them count as settled and ahead of anything the
# sandbox writes, even while another transaction in this cluster is still open. What ties
# the copy to the deployment's targets goes: deep jobs and their queue, and the Zotero and
# WebDAV items (with the 0.x keys Zotero's setup would take over again), so integrations
# enabled in dev work against their test targets from scratch.
target_psql -d "$NEXT" -1 <<'SQL'
UPDATE outbox SET xid = '1'::xid8;
DELETE FROM integration_cursors;
DELETE FROM deep_waiting;
UPDATE deep_cards SET job_id = NULL, job_since = NULL, job_revision = NULL, job_version = NULL,
                      job_event = NULL, job_automatic = NULL
 WHERE job_id IS NOT NULL;
DELETE FROM zotero_items;
DELETE FROM webdav_files;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_name = 'papers' AND column_name = 'zotero_key') THEN
    UPDATE papers SET zotero_key = NULL, zotero_attachment_key = NULL
     WHERE zotero_key IS NOT NULL OR zotero_attachment_key IS NOT NULL;
  END IF;
END $$;
SQL

if (( migrate )); then
  LITWATCH_DATABASE_URL="${LITWATCH_DATABASE_URL%/*}/$NEXT" uv run litwatch db migrate
fi

# Swap the two in one transaction, so `sandbox` is always either the old copy or the new one.
target_psql -d postgres -c "DROP DATABASE IF EXISTS $OLD WITH (FORCE)"
target_psql -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB'" >/dev/null
if [[ -n "$(target_psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$DB'")" ]]; then
  target_psql -d postgres -1 -c "ALTER DATABASE $DB RENAME TO $OLD" -c "ALTER DATABASE $NEXT RENAME TO $DB"
else
  target_psql -d postgres -c "ALTER DATABASE $NEXT RENAME TO $DB"
fi
trap - ERR
target_psql -d postgres -c "DROP DATABASE IF EXISTS $OLD WITH (FORCE)"

target_psql -d "$DB" -Atc "SELECT 'papers: ' || count(*) FROM papers"
echo "done: point LITWATCH_DATABASE_URL at the database '$DB'"
