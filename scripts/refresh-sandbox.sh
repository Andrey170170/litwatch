#!/usr/bin/env bash
# Rebuild the sandbox database for end-to-end runs (docs/agents/verification.md): a fresh
# schema (the migrations) plus the papers of a window read from the archive, as the restart
# builds one; or, with --production, a copy of production's database brought to this
# checkout's schema. Run from the repository root on the owner's server (docs/agents/dev-host.md).
#
# The new database is built as `<db>_next` and replaces `<db>` only once it is complete, so a
# failed refresh leaves the previous sandbox as it was. It runs only against the dev Postgres
# (container litwatch-v1-dev, 127.0.0.1:55432) and never touches `litwatch` or `test`.
#
#   --days N        the N days ending today (UTC); the default, with N = 14
#   --since DATE    the window's first day instead (YYYY-MM-DD)
#   --until DATE    its last day; default today (UTC)
#   --db NAME       build NAME instead of `sandbox`, e.g. a scratch database
#   --env FILE      where the dev DSN is; default ./.env.v1 (a worktree names the main checkout's)
#   --production    copy production's database instead of reading a window from the archive
#
# Production is only read: one pg_dump in container litwatch-db, a single consistent snapshot.
# The copy carries the owner's word, verdicts, Deep cards and digests, so it is the one to use
# for web checks and data migrations; integrations stay off unless a dev litwatch.toml names them.
#
# The archive is only read: $LITWATCH_ARCHIVE_URL and $LITWATCH_ARCHIVE_TOKEN (from the
# environment, else ./.env), or the archive integration in litwatch.toml. The categories are
# litwatch.toml's.
set -Eeuo pipefail

readonly CONTAINER=litwatch-v1-dev SERVER=127.0.0.1:55432

readonly PROD_CONTAINER=litwatch-db PROD_DB=litwatch PROD_USER=litwatch

db=sandbox days=14 since="" until="" env_file=.env.v1 production="" window_given=""
while (( $# )); do
  case "$1" in
    --days) days=$2; window_given=1; shift 2 ;;
    --since) since=$2; window_given=1; shift 2 ;;
    --until) until=$2; window_given=1; shift 2 ;;
    --db) db=$2; shift 2 ;;
    --env) env_file=$2; shift 2 ;;
    --production) production=1; shift ;;
    *) echo "usage: $0 [--days N | --since DATE] [--until DATE] [--production] [--db NAME] [--env FILE]" >&2
       exit 2 ;;
  esac
done
if [[ -n "$production" && -n "$window_given" ]]; then
  echo "--production copies the whole database; it takes no window" >&2
  exit 2
fi
# PostgreSQL cuts names to 63 bytes: a longer base would make `<db>_next` the target itself.
if (( ${#db} > 58 )); then
  echo "the database name '$db' is longer than 58 characters" >&2
  exit 1
fi
readonly NEXT=${db}_next OLD=${db}_old
if [[ "$db" == "$NEXT" || "$db" == "$OLD" || "$NEXT" == "$OLD" ]]; then
  echo "the database names '$db', '$NEXT' and '$OLD' are not distinct" >&2
  exit 1
fi
for name in "$db" "$NEXT" "$OLD"; do
  if [[ ! "$name" =~ ^[a-z][a-z0-9_]*$ ]] || [[ "$name" =~ ^(litwatch|test|postgres|template[0-9]*)$ ]]
  then
    echo "refusing to build or replace the database '$name'" >&2
    exit 1
  fi
done

# Every database this script touches is on the server of the dev DSN, which must be the dev
# Postgres: the one known address, and the port the container publishes.
set -a; . "$env_file"; set +a
dsn=$LITWATCH_DATABASE_URL
dsn_server=$(sed -E 's#^[a-z]+://[^@/]*@([^/?]+)/[^/?]+$#\1#' <<<"$dsn")
dsn_user=$(sed -E 's#^[a-z]+://([^:@/]+)[:@].*#\1#' <<<"$dsn")
if [[ "$dsn_server" != "$SERVER" || "$(docker port "$CONTAINER" 5432/tcp | head -1)" != "$SERVER" ]]
then
  echo "LITWATCH_DATABASE_URL in $env_file is not a database on the dev Postgres ($SERVER)" >&2
  exit 1
fi
next_dsn="${dsn%/*}/$NEXT"

admin() {
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d postgres "$@"
}

drop_next() { admin -c "DROP DATABASE IF EXISTS $NEXT WITH (FORCE)"; }
# Undo the swap's lock-out if the swap failed; a no-op when `$db` does not exist.
reopen() { admin -c "ALTER DATABASE $db ALLOW_CONNECTIONS true" >/dev/null 2>&1 || true; }
trap 'echo "refresh failed; $NEXT dropped, $db untouched" >&2; reopen; drop_next' ERR

window=(--days "$days")
[[ -n "$since" ]] && window=(--since "$since")
[[ -n "$until" ]] && window+=(--until "$until")

echo "building $NEXT on $CONTAINER"
drop_next
admin -c "CREATE DATABASE $NEXT OWNER \"$dsn_user\""
if [[ -n "$production" ]]; then
  echo "copying $PROD_DB from $PROD_CONTAINER"
  docker exec "$PROD_CONTAINER" pg_dump -U "$PROD_USER" -d "$PROD_DB" -Fc --no-owner --no-privileges \
    | docker exec -i "$CONTAINER" pg_restore -U postgres --role "$dsn_user" --no-owner --no-privileges \
        --exit-on-error -d "$NEXT"
  # Production may be behind this checkout: a branch's new migrations apply to the copy.
  LITWATCH_DATABASE_URL=$next_dsn uv run litwatch db migrate
else
  LITWATCH_DATABASE_URL=$next_dsn uv run litwatch db migrate
  LITWATCH_DATABASE_URL=$next_dsn uv run litwatch intake archive "${window[@]}"
fi

# Swap the two in one transaction, so `$db` is always either the old sandbox or the new one.
admin -c "DROP DATABASE IF EXISTS $OLD WITH (FORCE)"
if [[ -n "$(admin -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'")" ]]; then
  # No new sessions between disconnecting the old ones and the rename.
  admin -c "ALTER DATABASE $db ALLOW_CONNECTIONS false"
  admin -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db'" >/dev/null
  # Termination is asynchronous: wait (up to 10 s) for the sessions to go.
  for _ in {1..20}; do
    [[ "$(admin -Atc "SELECT count(*) FROM pg_stat_activity WHERE datname = '$db'")" == 0 ]] && break
    sleep 0.5
  done
  admin -1 -c "ALTER DATABASE $db RENAME TO $OLD" -c "ALTER DATABASE $NEXT RENAME TO $db"
else
  admin -c "ALTER DATABASE $NEXT RENAME TO $db"
fi
trap - ERR
admin -c "DROP DATABASE IF EXISTS $OLD WITH (FORCE)"

docker exec "$CONTAINER" psql -U postgres -d "$db" -Atc "SELECT 'papers: ' || count(*) FROM papers"
echo "done: point LITWATCH_DATABASE_URL at the database '$db'"
