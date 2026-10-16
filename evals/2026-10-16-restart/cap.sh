#!/usr/bin/env bash
# The restart's spending cap (evals/2026-10-16-restart.md, the run): stop a litwatch step once the
# run database's call journal has spent CAP dollars, all roles and providers together.
#
#   evals/2026-10-16-restart/cap.sh DB CAP PID
#
# Every 10 s it sums `inference_calls.usage->>'cost'` in DB (on the dev Postgres, container
# litwatch-v1-dev) and sends PID a TERM once the sum reaches CAP; it exits when PID does. PID must
# be a process run from this checkout. A call is journalled with its cost when its answer comes, so
# the calls in flight at the stop are paid but not counted: the overshoot is at most the step's
# concurrency times its dearest call (4 full-text passes at about $0.35 each).
set -euo pipefail

db=${1:?usage: $0 DB CAP PID} cap=${2:?usage: $0 DB CAP PID} pid=${3:?usage: $0 DB CAP PID}
here=$(cd "$(dirname "$0")/../.." && pwd)
[[ "$(readlink "/proc/$pid/cwd")" == "$here" ]] || { echo "process $pid is not run from $here" >&2; exit 1; }

spent() {
    docker exec litwatch-v1-dev psql -U postgres -d "$db" -Atc \
        "SELECT coalesce(sum((usage->>'cost')::numeric), 0) FROM inference_calls"
}

while kill -0 "$pid" 2>/dev/null; do
    now=$(spent)
    echo "$(date -u +%FT%TZ) spent \$$now of \$$cap"
    if awk -v a="$now" -v b="$cap" 'BEGIN { exit !(a >= b) }'; then
        echo "cap reached: stopping $pid"
        kill -TERM "$pid"
        exit 0
    fi
    sleep 10
done
echo "$(date -u +%FT%TZ) $pid ended; spent \$$(spent)"
