#!/usr/bin/env bash
# The restart's spending cap (evals/2026-10-16-restart.md, the run): stop a litwatch step once the
# run database's call journal has spent CAP dollars, all roles and providers together.
#
#   evals/2026-10-16-restart/cap.sh DB CAP PID
#
# Every 10 s it sums `inference_calls.usage->>'cost'` in DB (on the dev Postgres, container
# litwatch-v1-dev) and sends PID a TERM once the sum reaches CAP; it exits when PID does. PID must
# be a process run from this checkout, and is signalled only while it is still that process (same
# start time). Three failed sums in a row stop PID too: unwatched, the step could spend past CAP.
# A watchdog, not an admission check: a call is journalled with its cost when its answer comes, so
# the overshoot is the calls answered since the last sum and those in flight at the stop, about
# twice the step's concurrency times its dearest call (8 full-text passes at up to $0.46 each).
set -uo pipefail

db=${1:?usage: $0 DB CAP PID} cap=${2:?usage: $0 DB CAP PID} pid=${3:?usage: $0 DB CAP PID}
here=$(cd "$(dirname "$0")/../.." && pwd)

started() { awk '{ sub(/^.*\) /, ""); print $20 }' "/proc/$pid/stat" 2>/dev/null; }  # field 22
start=$(started)
ours() { [[ "$(readlink "/proc/$pid/cwd")" == "$here" && "$(started)" == "$start" ]]; }
ours || { echo "process $pid is not run from $here" >&2; exit 1; }

spent() {
    timeout 30 docker exec litwatch-v1-dev psql -U postgres -d "$db" -Atc \
        "SELECT coalesce(sum((usage->>'cost')::numeric), 0) FROM inference_calls"
}

stop() {
    echo "$1: stopping $pid"
    if ours; then kill -TERM "$pid"; fi
    exit "$2"
}

failed=0
while ours; do
    if ! now=$(spent) || [[ -z "$now" ]]; then
        failed=$((failed + 1))
        echo "$(date -u +%FT%TZ) could not sum the journal ($failed in a row)" >&2
        ((failed < 3)) || stop "no sum of the journal" 1
    else
        failed=0
        echo "$(date -u +%FT%TZ) spent \$$now of \$$cap"
        if awk -v a="$now" -v b="$cap" 'BEGIN { exit !(a >= b) }'; then stop "cap reached" 0; fi
    fi
    sleep 10
done
echo "$(date -u +%FT%TZ) $pid ended; spent \$$(spent || echo '?')"
