"""A stand-in for the deep service in the stack smoke test: the same job protocol (POST /process,
GET /jobs/{id}, GET /keys/{key}), no model. Its answers are built from litwatch's models of the
service's contracts (`litwatch.core.contracts.deep`, generated from deep/src/contracts.ts), so
they are what the service sends and cannot drift from it. Like the real service's agent, it
searches litwatch's collection with the read token ($LITWATCH_URL, $LITWATCH_TOKEN) and places
the paper among the hits, so what it returns has live "Among the collection" rows and shows the
stack's wiring end to end.

It returns a Deep read (docs/integrations.md, deep's result): the web app's fixture read
($READ_FIXTURE), with a small figure under every name the read cites and one review round. A
job is queued when submitted, running once picked up, then done. It runs on the litwatch image's
Python, with litwatch installed. A Deep card in the format from before is seeded into the
database instead (`legacy_card.py`): the service writes none."""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from litwatch.core.contracts import deep as contract

JOBS: dict[str, contract.Job] = {}
KEYS: dict[str, str] = {}
LOCK = threading.Lock()
# A 320 x 180 WebP of one colour, sent as each of the read's figures.
FIGURE = "UklGRiQAAABXRUJQVlA4TBgAAAAvP8EsAAdQt6IUuf8BAEX6/58i+p/6/wM="
NO_USAGE = contract.Stats(
    cells=0, turns=0, input=0, output=0, reasoning=0, cache_read=0, list_cost=0.0, seconds=0
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def search(query: str) -> list[dict]:
    """`store.search` as deep's agent calls it: litwatch's Papers, with the read token."""
    url = os.environ["LITWATCH_URL"] + "/api/papers?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {os.environ['LITWATCH_TOKEN']}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["rows"]


def among(paper_id: str) -> list[dict]:
    """Up to three of the collection's papers, placed beside this one."""
    hits = [r for r in search("language model") if r["id"] != paper_id][:3]
    return [
        {
            "arxiv_id": hit["id"],
            "title": hit["title"],
            "relation": "agrees",
            "note": "Placed by the smoke test from litwatch's own search.",
        }
        for hit in hits
    ]


def figure_names(block: Any) -> list[str]:
    """The names of the figures a read cites, in its `fig` blocks at any depth."""
    if isinstance(block, list):
        return [name for b in block for name in figure_names(b)]
    if not isinstance(block, dict):
        return []
    own = [block["name"]] if block.get("type") == "fig" else []
    return own + [name for b in block.values() for name in figure_names(b)]


def read(paper_id: str) -> dict[str, Any]:
    """What a finished job carries with its read: the fixture read, about another paper, with
    this stack's collection papers beside it, its figures and its one review round."""
    result = json.loads(Path(os.environ["READ_FIXTURE"]).read_text())
    result["among_collection"] = among(paper_id)
    return {
        "result": contract.Result.model_validate(result),
        "figures": [
            contract.Figure(name=name, mime="image/webp", data=FIGURE)
            for name in dict.fromkeys(figure_names(result["sections"]))
        ],
        "reviews": [
            {
                "round": 1,
                "passed": True,
                "scores": {"faithfulness": 4, "clarity": 4, "depth": 4, "calibration": 4},
                "checks": {},
                "findings": [],
            }
        ],
    }


def update(job_id: str, **fields: Any) -> None:
    """The job with `fields` changed, checked against the contract again."""
    with LOCK:
        JOBS[job_id] = contract.Job.model_validate(
            JOBS[job_id].model_copy(update=fields | {"last_activity": now()}).model_dump()
        )


def run(job_id: str) -> None:
    update(job_id, status="running", started_at=now(), model="smoke-test")
    try:
        done = read(JOBS[job_id].paper_id)
    except Exception as exc:  # reported to litwatch as the job's error, as deep does
        error = f"{type(exc).__name__}: {exc}"
        update(job_id, status="error", error=error, failure="service", finished_at=now())
        return
    update(job_id, status="done", finished_at=now(), trace=[], **done)


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ref(self, status: int, job: contract.Job) -> None:
        self._json(status, contract.JobRef(id=job.id, status=job.status).model_dump(mode="json"))

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path == "/healthz":
            return self._json(200, {"ok": True, "auth": {"state": "ok"}})
        if self.path.startswith("/keys/"):
            job_id = KEYS.get(urllib.parse.unquote(self.path.removeprefix("/keys/")))
            if job_id is None:
                return self._json(404, {"error": "no job for that key"})
            return self._ref(200, JOBS[job_id])
        job = JOBS.get(self.path.removeprefix("/jobs/"))
        if job is None:
            return self._json(404, {"error": "no such job"})
        return self._json(200, job.model_dump(mode="json", exclude_none=True))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/process":
            return self._json(404, {"error": "no such route"})
        raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            submission = contract.JobInput.model_validate(raw)
        except ValueError as exc:
            return self._json(422, {"error": f"not a job's input: {exc}"})
        key = self.headers.get("Idempotency-Key")
        with LOCK:
            if key in KEYS:
                return self._ref(200, JOBS[KEYS[key]])
            at = now()
            job = contract.Job(
                id=str(uuid.uuid4()),
                paper_id=submission.paper.id,
                title=submission.paper.title,
                extensions=submission.extensions or [],
                status="queued",
                created_at=at,
                stats=NO_USAGE,
                last_activity=at,
                key=key,
            )
            JOBS[job.id] = job
            if key is not None:
                KEYS[key] = job.id
        threading.Thread(target=run, args=(job.id,), daemon=True).start()
        return self._ref(202, job)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
