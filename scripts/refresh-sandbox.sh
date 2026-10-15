#!/usr/bin/env bash
# Rebuild the sandbox database for end-to-end runs (docs/agents/verification.md): a fresh
# schema (the migrations) plus the papers of a window read from the archive, as the restart
# builds one. Run from the repository root on the owner's server (docs/agents/dev-host.md).
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
#
# The archive is only read: $LITWATCH_ARCHIVE_URL and $LITWATCH_ARCHIVE_TOKEN (from the
# environment, else ./.env), or the archive integration in litwatch.toml. The categories are
# litwatch.toml's.
set -Eeuo pipefail

readonly CONTAINER=litwatch-v1-dev SERVER=127.0.0.1:55432

db=sandbox days=14 since="" until="" env_file=.env.v1
while (( $# )); do
  case "$1" in
    --days) days=$2; shift 2 ;;
    --since) since=$2; shift 2 ;;
    --until) until=$2; shift 2 ;;
    --db) db=$2; shift 2 ;;
    --env) env_file=$2; shift 2 ;;
    *) echo "usage: $0 [--days N | --since DATE] [--until DATE] [--db NAME] [--env FILE]" >&2
       exit 2 ;;
  esac
done
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
trap 'echo "refresh failed; $NEXT dropped, $db untouched" >&2; drop_next' ERR

window=(--days "$days")
[[ -n "$since" ]] && window=(--since "$since")
[[ -n "$until" ]] && window+=(--until "$until")

echo "building $NEXT on $CONTAINER"
drop_next
admin -c "CREATE DATABASE $NEXT OWNER \"$dsn_user\""
LITWATCH_DATABASE_URL=$next_dsn uv run litwatch db migrate
LITWATCH_DATABASE_URL=$next_dsn uv run litwatch intake archive "${window[@]}"

# Swap the two in one transaction, so `$db` is always either the old sandbox or the new one.
admin -c "DROP DATABASE IF EXISTS $OLD WITH (FORCE)"
admin -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db'" >/dev/null
if [[ -n "$(admin -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'")" ]]; then
  admin -1 -c "ALTER DATABASE $db RENAME TO $OLD" -c "ALTER DATABASE $NEXT RENAME TO $db"
else
  admin -c "ALTER DATABASE $NEXT RENAME TO $db"
fi
trap - ERR
admin -c "DROP DATABASE IF EXISTS $OLD WITH (FORCE)"

docker exec "$CONTAINER" psql -U postgres -d "$db" -Atc "SELECT 'papers: ' || count(*) FROM papers"
echo "done: point LITWATCH_DATABASE_URL at the database '$db'"
