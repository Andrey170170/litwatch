"""In-memory stand-ins for the services integrations talk to. Nothing here leaves the process:
every client in the integration tests is built on one of these through `httpx.MockTransport`."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx

PDF = b"%PDF-1.7 fake body"


def mock(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeZotero:
    """Enough of api.zotero.org: collections, items, children, PATCH with versions."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.version = 100
        self.calls: list[str] = []

    def _key(self) -> str:
        return f"K{len(self.collections) + len(self.items) + 1:07d}"

    def add_item(self, key: str, data: dict[str, Any]) -> None:
        self.version += 1
        self.items[key] = {"key": key, "version": self.version, "data": {"key": key, **data}}

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/users/1")
        self.calls.append(f"{req.method} {path}")
        query = parse_qs(req.url.query.decode())
        if path == "/collections":
            if req.method == "GET":
                return httpx.Response(200, json=list(self.collections.values()))
            return self._create(self.collections, json.loads(req.content))
        if path == "/items" and req.method == "POST":
            return self._create(self.items, json.loads(req.content))
        if path == "/items/top":
            q = query["q"][0].lower()
            rows = [
                i
                for i in self.items.values()
                if not i["data"].get("parentItem") and q in i["data"].get("title", "").lower()
            ]
            return httpx.Response(200, json=rows)
        if path.endswith("/children"):
            parent = path.split("/")[2]
            rows = [i for i in self.items.values() if i["data"].get("parentItem") == parent]
            return httpx.Response(200, json=rows)
        key = path.split("/")[2]
        if key not in self.items:
            return httpx.Response(404)
        item = self.items[key]
        if req.method == "GET":
            return httpx.Response(200, json=item)
        if int(req.headers["If-Unmodified-Since-Version"]) != item["version"]:
            return httpx.Response(412)
        self.version += 1
        item["data"] = {**item["data"], **json.loads(req.content)}
        item["version"] = self.version
        return httpx.Response(204)

    def _create(self, store: dict[str, Any], objects: list[dict[str, Any]]) -> httpx.Response:
        successful = {}
        for i, data in enumerate(objects):
            key = self._key()
            self.version += 1
            store[key] = {"key": key, "version": self.version, "data": {"key": key, **data}}
            successful[str(i)] = {"key": key}
        return httpx.Response(200, json={"successful": successful, "failed": {}})

    # -- reading it back ----------------------------------------------------------------------

    def tops(self) -> list[dict[str, Any]]:
        return [
            i
            for i in self.items.values()
            if i["data"].get("itemType") not in ("note", "attachment")
        ]

    def tags(self, key: str) -> set[str]:
        return {t["tag"] for t in self.items[key]["data"]["tags"]}

    def collection_names(self, key: str) -> set[str]:
        names = {k: c["data"]["name"] for k, c in self.collections.items()}
        return {names.get(c, c) for c in self.items[key]["data"]["collections"]}

    def notes(self, key: str) -> list[str]:
        return [
            i["data"]["note"]
            for i in self.items.values()
            if i["data"].get("parentItem") == key and i["data"].get("itemType") == "note"
        ]

    def attachments(self, key: str) -> list[str]:
        return [
            k
            for k, i in self.items.items()
            if i["data"].get("parentItem") == key and i["data"].get("itemType") == "attachment"
        ]


class FakeDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def handle(self, req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        if req.method == "PUT":
            self.files[name] = req.content
            return httpx.Response(201)
        return httpx.Response(200 if name in self.files else 404)


class FakeArxiv:
    """arxiv.org for PDFs and HTML renderings; `html` maps an id to its page."""

    def __init__(self, html: dict[str, str] | None = None) -> None:
        self.html = html or {}
        self.calls: list[str] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url.path)
        kind, _, identity = req.url.path.strip("/").partition("/")
        if kind == "pdf":
            return httpx.Response(200, content=PDF)
        if kind == "html" and identity.split("v")[0] in self.html:
            return httpx.Response(200, text=self.html[identity.split("v")[0]])
        return httpx.Response(404)


class FakeArchive:
    """arxiv-archive's API: catalog records, jobs, PDFs and search."""

    def __init__(self, status: str = "complete", records: dict[str, int] | None = None) -> None:
        self.status = status
        self.records: dict[str, dict[str, Any]] = {
            pid: {"id": pid, "latest_version": v, "metadata": {}}
            for pid, v in (records or {}).items()
        }
        self.jobs: list[dict[str, Any]] = []
        self.search_items: list[dict[str, Any]] = []
        self.searches: list[dict[str, str]] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer archive-token"
        path = req.url.path
        if path == "/v1/search":
            self.searches.append(dict(req.url.params))
            return httpx.Response(200, json={"items": self.search_items, "has_more": False})
        if path == "/v1/jobs":
            body = json.loads(req.content)
            job = {"id": len(self.jobs) + 1, "status": self.status, **body}
            self.jobs.append(job)
            return httpx.Response(202, json=job)
        if path.startswith("/v1/jobs/"):
            return httpx.Response(200, json={"id": 1, "status": self.status})
        pid = path.removeprefix("/v1/papers/").removesuffix("/pdf")
        if path.endswith("/pdf"):
            return httpx.Response(200, content=PDF)
        if req.method == "PUT":
            body = json.loads(req.content)
            self.records[pid] = {"id": pid, **body}
            return httpx.Response(200, json=self.records[pid])
        if pid in self.records:
            return httpx.Response(200, json=self.records[pid])
        return httpx.Response(404)


class FakeDeep:
    """The deep service's job protocol: a job that needs two polls to finish."""

    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = outcome
        self.polls = 0
        self.requests: list[dict[str, Any]] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/process":
            self.requests.append(json.loads(req.content))
            self.polls = 0
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        if req.url.path == "/jobs/job-1":
            self.polls += 1
            if self.polls < 2:
                return httpx.Response(200, json={"status": "running", "stats": {"cells": 3}})
            return httpx.Response(200, json={"status": "done", **self.outcome})
        return httpx.Response(404)
