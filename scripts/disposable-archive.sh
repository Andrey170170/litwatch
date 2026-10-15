#!/usr/bin/env bash
# A disposable archive for verification (docs/agents/verification.md): this checkout's archive/,
# its API and its acquisition worker, over a fresh scratch directory. Its worker downloads from
# arXiv under the archive's pacing like any instance, so ask it for a handful of papers only.
#
#   scripts/disposable-archive.sh start PORT   start it; prints the export lines for litwatch
#   scripts/disposable-archive.sh stop DIR     stop its two processes by their recorded PIDs and
#                                              remove DIR
#
# Needs archive/.venv (archive/README.md). Never point it at an instance's data: it runs
# without the archive's data-mount checks.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
MARK=.litwatch-disposable-archive  # what `start` leaves in its directory, and `stop` asks for
bin="$here/archive/.venv/bin"

start() {
    local port=$1
    [[ -x "$bin/paper-archive" ]] || { echo "no archive/.venv: see archive/README.md" >&2; exit 1; }
    if ss -H -ltn "sport = :$port" | grep -q .; then
        echo "port $port is taken" >&2
        exit 1
    fi
    local dir
    dir=$(mktemp -d /tmp/litwatch-archive.XXXXXX)
    # Its own arXiv pacing state: the instance's is shared by its processes, and not ours to write.
    mkdir -p "$dir/work/pacing" "$dir/archive"
    touch "$dir/$MARK"
    (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$dir/token")
    local args=(--work "$dir/work" --archive "$dir/archive" --no-mount-guard)
    # An empty catalog lists at once.
    "$bin/paper-archive" "${args[@]}" index-dates > "$dir/serve.log" 2>&1 \
        || failed "$dir" "the archive's catalog did not start"
    nohup "$bin/paper-archive" "${args[@]}" serve --host 127.0.0.1 --port "$port" \
        --token-file "$dir/token" > "$dir/serve.log" 2>&1 &
    echo $! > "$dir/serve.pid"
    nohup "$bin/paper-archive-worker" "${args[@]}" > "$dir/worker.log" 2>&1 &
    echo $! > "$dir/worker.pid"
    for _ in $(seq 50); do
        if curl -sf --max-time 2 -H "Authorization: Bearer $(cat "$dir/token")" \
            "http://127.0.0.1:$port/v1/status" > /dev/null; then
            # Ready only with both processes up: a worker that exited fetches nothing.
            ours "$(cat "$dir/worker.pid")" "$dir" || failed "$dir" "the worker exited"
            echo "export LITWATCH_ARCHIVE_URL=http://127.0.0.1:$port"
            echo "export LITWATCH_ARCHIVE_TOKEN=$(cat "$dir/token")"
            echo "# stop: scripts/disposable-archive.sh stop $dir"
            return
        fi
        sleep 0.2
    done
    failed "$dir" "the archive did not answer"
}

# Startup failed: show both logs, then stop the archive (which removes them) and fail.
failed() {
    echo "$2" >&2
    for log in "$1"/serve.log "$1"/worker.log; do
        [[ -f "$log" ]] || continue
        echo "== ${log##*/}" >&2
        tail -20 "$log" >&2
    done
    stop "$1"
    exit 1
}

stop() {
    local dir=$1
    # Only a directory `start` made (its name and its mark), so `rm -rf` reaches nothing else.
    [[ "$dir" =~ ^/tmp/litwatch-archive\.[A-Za-z0-9]{6}$ && -d "$dir" && ! -L "$dir" \
        && -f "$dir/$MARK" ]] || { echo "not a disposable archive: $dir" >&2; exit 1; }
    local alive=0
    for file in "$dir"/serve.pid "$dir"/worker.pid; do
        [[ -f "$file" ]] || continue
        local pid
        pid=$(cat "$file")
        ours "$pid" "$dir" || continue
        kill "$pid"
        for _ in $(seq 50); do ours "$pid" "$dir" || break; sleep 0.2; done
        # A worker blocked in a download can outlast its stop signal.
        if ours "$pid" "$dir"; then kill -KILL "$pid"; sleep 0.5; fi
        if ours "$pid" "$dir"; then echo "process $pid did not stop" >&2; alive=1; fi
    done
    [[ $alive == 0 ]] || { echo "kept $dir" >&2; exit 1; }
    rm -rf "$dir"
}

# Whether `pid` is a live process of the archive in `dir`: its command line names the directory.
ours() {
    { tr '\0' ' ' < "/proc/$1/cmdline"; } 2>/dev/null | grep -qF "$2"
}

case "${1:-}" in
    start) start "${2:?usage: $0 start PORT}" ;;
    stop) stop "${2:?usage: $0 stop DIR}" ;;
    *) echo "usage: $0 start PORT | stop DIR" >&2; exit 2 ;;
esac
