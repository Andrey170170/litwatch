"""Local administration and HTTP server entry point."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from .api import PaperServer, read_token
from .store import Store

def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--work", type=Path, default=Path("/srv/arxiv-work"))
    cli.add_argument("--archive", type=Path, default=Path("/srv/arxiv-archive"))
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    retry = commands.add_parser("retry", help="retry a failed or unavailable acquisition")
    retry.add_argument("job_id", type=int)
    strip = commands.add_parser("strip-sources",
                                help="rewrite sources stored before filtering as text-only (destructive)")
    strip.add_argument("--dry-run", action="store_true", help="report savings without writing")
    strip.add_argument("--limit", type=positive_int, help="process at most this many packages")
    commands.add_parser("index-dates", help="date every paper so papers can be listed by window (idempotent)")
    imp = commands.add_parser("import-history")
    imp.add_argument("--ledger", type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--token-file", type=Path, required=True)
    serve.add_argument("--max-workers", type=int, default=4)
    for name in ("get", "put", "sources", "source-file", "enqueue", "metadata"):
        command = commands.add_parser(name)
        command.add_argument("paper_id")
        if name != "metadata":
            command.add_argument("--version", type=int, required=name in ("put", "enqueue"))
        if name in ("get", "put", "enqueue"):
            command.add_argument("--kind", choices=("pdf", "source"), default="pdf")
        if name == "put":
            command.add_argument("input", type=Path)
            command.add_argument("--provenance", type=Path, help="JSON object file")
        if name == "source-file":
            command.add_argument("path")
        if name == "enqueue":
            command.add_argument("--priority", type=int, default=100, help="0 urgent; 100 background")
    return cli


def run(argv=None) -> int:
    args = parser().parse_args(argv)
    # Validate token before initializing runtime state for serve.
    token = read_token(args.token_file) if args.command == "serve" else None
    store = Store(work=args.work, archive=args.archive)
    try:
        if args.command == "serve":
            server = PaperServer((args.host, args.port), store, token, args.max_workers)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        if args.command == "status":
            result = store.status()
        elif args.command == "retry":
            result = store.catalog.retry_job(args.job_id)
        elif args.command == "strip-sources":
            swept = 0 if args.dry_run else store.sweep_replaced_sources()
            counts, before, after = {}, 0, 0
            for row in store.catalog.unfiltered_sources(args.limit):
                outcome = store.strip_source(row["paper_id"], row["version"], dry_run=args.dry_run)
                counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
                before, after = before + outcome.get("before", 0), after + outcome.get("after", 0)
                print(json.dumps(outcome), flush=True)
            result = {"dry_run": args.dry_run, "swept_leftovers": swept, "counts": counts,
                      "stored_bytes_before": before, "stored_bytes_after": after}
        elif args.command == "import-history":
            with store.write_lock:
                result = store.catalog.import_history(
                    args.archive, args.ledger or args.work / "state/progress.sqlite",
                    progress=lambda checkpoint: print(json.dumps(checkpoint), flush=True))
        elif args.command == "index-dates":
            result = store.catalog.index_dates(
                progress=lambda done: print(json.dumps(done), flush=True))
        elif args.command == "get":
            print(store.get(args.paper_id, kind=args.kind, version=args.version))
            return 0
        elif args.command == "put":
            provenance = json.loads(args.provenance.read_text()) if args.provenance else {"origin": "local-cli"}
            if not isinstance(provenance, dict):
                raise ValueError("provenance must be a JSON object")
            result = store.put(args.paper_id, args.version, args.kind, args.input, provenance=provenance)
        elif args.command == "sources":
            result = {"files": store.sources(args.paper_id, version=args.version)}
        elif args.command == "source-file":
            print(store.source_file(args.paper_id, args.path, version=args.version))
            return 0
        elif args.command == "enqueue":
            result = store.catalog.enqueue(args.paper_id, args.version, args.kind, priority=args.priority,
                                           provenance={"origin": "local-cli"})
        elif args.command == "metadata":
            result = store.catalog.get_paper(args.paper_id)
            if result is None:
                raise FileNotFoundError(args.paper_id)
            result["artifacts"] = store.catalog.list_artifacts(args.paper_id)
            result["has_historical_source"] = bool(store.catalog.latex_locations(args.paper_id))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    finally:
        store.catalog.close()


def main(argv=None) -> int:
    try:
        return run(argv)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"paper-archive: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
