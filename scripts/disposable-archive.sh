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
    (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$dir/token")
    local args=(--work "$dir/work" --archive "$dir/archive" --no-mount-guard)
    "$bin/paper-archive" "${args[@]}" index-dates > /dev/null  # an empty catalog lists at once
    nohup "$bin/paper-archive" "${args[@]}" serve --host 127.0.0.1 --port "$port" \
        --token-file "$dir/token" > "$dir/serve.log" 2>&1 &
    echo $! > "$dir/serve.pid"
    nohup "$bin/paper-archive-worker" "${args[@]}" > "$dir/worker.log" 2>&1 &
    echo $! > "$dir/worker.pid"
    for _ in $(seq 50); do
        if curl -sf -H "Authorization: Bearer $(cat "$dir/token")" \
            "http://127.0.0.1:$port/v1/status" > /dev/null; then
            # Ready only with both processes up: a worker that exited fetches nothing.
            if ! ours "$(cat "$dir/worker.pid")" "$dir"; then
                echo "the worker exited:" >&2
                tail -20 "$dir/worker.log" >&2
                stop "$dir"
                exit 1
            fi
            echo "export LITWATCH_ARCHIVE_URL=http://127.0.0.1:$port"
            echo "export LITWATCH_ARCHIVE_TOKEN=$(cat "$dir/token")"
            echo "# stop: scripts/disposable-archive.sh stop $dir"
            return
        fi
        sleep 0.2
    done
    echo "the archive did not answer; logs in $dir" >&2
    stop "$dir"
    exit 1
}

stop() {
    local dir=$1
    # Exactly a directory `start` made (mktemp's six characters), so `rm -rf` reaches nothing else.
    [[ "$dir" =~ ^/tmp/litwatch-archive\.[A-Za-z0-9]{6}$ && -d "$dir" && ! -L "$dir" ]] \
        || { echo "not a disposable archive: $dir" >&2; exit 1; }
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
