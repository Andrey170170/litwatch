"""A stand-in for the deep service in the stack smoke test: the same job protocol (POST /process,
GET /jobs/{id}), no model. Like the real service's agent, it searches litwatch's collection with
the read token ($LITWATCH_URL, $LITWATCH_TOKEN) and places the paper among the hits, so a card
with live "Among the collection" rows shows the stack's wiring end to end. Standard library
only: it runs on the litwatch image's Python."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JOBS: dict[str, dict] = {}
STOP = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with", "via", "do", "how"}


def search(query: str) -> list[dict]:
    """`store.search` as deep's agent calls it: litwatch's Papers, with the read token."""
    url = os.environ["LITWATCH_URL"] + "/api/papers?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {os.environ['LITWATCH_TOKEN']}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["rows"]


def card(paper: dict) -> dict:
    """A card that passes deep's schema, with up to three collection papers it extends."""
    hits = [r for r in search("language model") if r["id"] != paper["id"]][:3]
    words = [w for w in re.findall(r"[A-Za-z]+", paper["title"]) if w.lower() not in STOP]
    return {
        "summary": "A stand-in card written by the stack smoke test. " * 8,
        "key_claim": f"The smoke test read {len(paper['body'])} characters of its text.",
        "paper_kind": "method",
        "evaluation_setup": {
            "models": [],
            "tasks": words[:2],
            "generalization": "Not judged: no model wrote this card.",
        },
        "prior_work": [
            {"title": "The paper's own references", "relation": "Not read by the smoke test."}
        ],
        "among_collection": [
            {
                "arxiv_id": hit["id"],
                "title": hit["title"],
                "relation": "extends",
                "note": "Placed by the smoke test from litwatch's own search.",
            }
            for hit in hits
        ],
        "code_available": False,
        "weights_available": False,
        "limitations": ["No model read the paper; this card only shows the wiring."],
    }


def run(job_id: str, submission: dict) -> None:
    job = JOBS[job_id]
    try:
        job["result"] = card(submission["paper"])
        job["status"] = "done"
    except Exception as exc:  # reported to litwatch as the job's error, as deep does
        job["status"], job["error"] = "error", f"{type(exc).__name__}: {exc}"
    job["stats"] = {"turns": 0, "input": 0, "output": 0, "list_cost": 0.0, "cells": 0}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        job = JOBS.get(self.path.removeprefix("/jobs/"))
        if job is None:
            return self._json(404, {"error": "no such job"})
        return self._json(200, job)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/process":
            return self._json(404, {"error": "no such route"})
        submission = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        job_id = str(uuid.uuid4())
        JOBS[job_id] = {"id": job_id, "status": "running", "model": "smoke-test"}
        threading.Thread(target=run, args=(job_id, submission), daemon=True).start()
        return self._json(202, {"id": job_id})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
