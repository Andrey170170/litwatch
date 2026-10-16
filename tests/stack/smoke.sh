#!/usr/bin/env bash
# The stack smoke test (docs/agents/verification.md): a fresh stack from deploy/compose.yaml with
# its own volumes, an empty archive seeded with real papers (papers.json) and their text, the
# day's papers read into litwatch from the archive and classified by hand into the collection,
# then pages.py: every page at both widths, and a Deep card from a fake deep service that
# searched litwatch with the read token; then touch.py: the phone's gestures under real touch.
#
#   tests/stack/smoke.sh [OUT]    OUT keeps the screenshots and the stack's logs (./smoke-out)
#
# Environment: SMOKE_PROJECT (compose project, litwatch-smoke), SMOKE_PORT (18080),
# SMOKE_BUILD (1: build the images first), SMOKE_KEEP (1: leave the stack up). Removes only its
# own project's containers and volumes. Needs docker compose, uv, and Playwright's Chromium.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
out=$(mkdir -p "${1:-smoke-out}" && cd "${1:-smoke-out}" && pwd)
project=${SMOKE_PROJECT:-litwatch-smoke}
port=${SMOKE_PORT:-18080}
env_file=$(mktemp)
token() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
cat > "$env_file" <<EOF
LITWATCH_DB_PASSWORD=$(token)
LITWATCH_API_TOKEN=$(token)
LITWATCH_READ_TOKEN=$(token)
LITWATCH_ARCHIVE_TOKEN=$(token)
ARXIV_CONTACT=smoke-test@example.invalid
LITWATCH_PORT=$port
LITWATCH_TOML=../tests/stack/litwatch.toml
LITWATCH_TAG=${LITWATCH_TAG:-local}
EOF
# Compose prefers the shell's variables to --env-file: export these over any already set.
set -a
. "$env_file"
set +a
api_token=$LITWATCH_API_TOKEN
dc() {
    docker compose -p "$project" -f "$root/deploy/compose.yaml" -f "$here/compose.smoke.yaml" \
        --env-file "$env_file" "$@"
}

finish() {
    status=$?
    dc logs --no-color > "$out/stack.log" 2>&1 || true
    if [[ "${SMOKE_KEEP:-0}" != 1 ]]; then
        dc down --volumes --remove-orphans > /dev/null 2>&1 || true
    fi
    rm -f "$env_file"
    exit $status
}
trap finish EXIT

if [[ "${SMOKE_BUILD:-1}" == 1 ]]; then
    dc build
fi
dc up -d --wait
dc exec -T litwatch litwatch taxonomy import > /dev/null
dc exec -T litwatch python /smoke/seed.py /smoke/papers.json
day=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[0]["published"])' \
    "$here/papers.json")
dc exec -T litwatch litwatch intake archive --since "$day" --until "$day"
dc exec -T litwatch python /smoke/classify.py /smoke/papers.json language-models
uv run --no-project --with playwright==1.64.0 python "$here/pages.py" \
    "http://127.0.0.1:$port" "$api_token" "$here/papers.json" "$out"
uv run --no-project --with playwright==1.64.0 python "$here/touch.py" \
    "http://127.0.0.1:$port" "$api_token" "$out"
