"""In-memory stand-ins for the services integrations talk to. Nothing here leaves the process:
every client in the integration tests is built on one of these through `httpx.MockTransport`."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, quote, unquote

import httpx

PDF = b"%PDF-1.7 fake body"


def mock(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeZotero:
    """Enough of api.zotero.org: collections, items, children, PATCH with versions, pages
    (`start`, `limit`) and write tokens (a token used before answers 412). `lose_answers`
    makes the next creates happen and then answer 502, as a proxy losing the answer would."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.version = 100
        self.calls: list[str] = []
        self.tokens: set[str] = set()
        self.lose_answers = 0
        self.made = 0

    def _key(self) -> str:
        self.made += 1  # never reused, as in Zotero
        return f"K{self.made:07d}"

    def add_item(self, key: str, data: dict[str, Any]) -> None:
        self.version += 1
        self.items[key] = {"key": key, "version": self.version, "data": {"key": key, **data}}

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/users/1")
        self.calls.append(f"{req.method} {path}")
        query = parse_qs(req.url.query.decode())
        if req.method == "POST":
            token = req.headers["Zotero-Write-Token"]
            assert len(token) == 32 and token.isalnum()
            if token in self.tokens:
                return httpx.Response(412)
            self.tokens.add(token)
            target = self.collections if path == "/collections" else self.items
            created = self._create(target, json.loads(req.content))
            if self.lose_answers:
                self.lose_answers -= 1
                return httpx.Response(502)
            return created
        if path == "/collections":
            return self._page(list(self.collections.values()), query)
        if path == "/items/top":
            q = query["q"][0].lower()
            # "everything" searches every field (and attachments' full text); otherwise titles.
            everything = query.get("qmode") == ["everything"]

            def searched(data: dict[str, Any]) -> str:
                fields = [v for v in data.values() if isinstance(v, str)] if everything else []
                return " ".join([data.get("title", ""), *fields]).lower()

            rows = [
                i
                for i in self.items.values()
                if not i["data"].get("parentItem") and q in searched(i["data"])
            ]
            return self._page(rows, query)
        if path.endswith("/children"):
            parent = path.split("/")[2]
            rows = [i for i in self.items.values() if i["data"].get("parentItem") == parent]
            return self._page(rows, query)
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

    @staticmethod
    def _page(rows: list[Any], query: dict[str, list[str]]) -> httpx.Response:
        start, limit = int(query.get("start", ["0"])[0]), int(query.get("limit", ["25"])[0])
        return httpx.Response(200, json=rows[start : start + min(limit, 100)])

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

    def notes_keys(self, key: str) -> list[str]:
        return [
            k
            for k, i in self.items.items()
            if i["data"].get("parentItem") == key and i["data"].get("itemType") == "note"
        ]

    def attachments(self, key: str) -> list[str]:
        return [
            k
            for k, i in self.items.items()
            if i["data"].get("parentItem") == key and i["data"].get("itemType") == "attachment"
        ]


class FakeDAV:
    """A WebDAV share; `fail` names files whose next PUT fails."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.fail: set[str] = set()

    def handle(self, req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        if req.method == "PUT":
            if any(name.endswith(f) for f in self.fail):
                self.fail = {f for f in self.fail if not name.endswith(f)}
                return httpx.Response(507)
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
    """arxiv-archive's API: catalog records, jobs (and their retries, in `retried`), PDFs, LaTeX
    text, search, and window listings over `listing` (items as `/v1/papers` gives them), a page
    of `limit` at a time with the next index as the cursor. Jobs end in `status`, or in
    `statuses[kind]` for that kind; `texts` holds each paper's LaTeX text as `(version, text)`,
    served for that version or none asked."""

    def __init__(self, status: str = "complete", records: dict[str, int] | None = None) -> None:
        self.status = status
        self.statuses: dict[str, str] = {}
        self.texts: dict[str, tuple[int | None, str]] = {}
        self.records: dict[str, dict[str, Any]] = {
            pid: {"id": pid, "latest_version": v, "metadata": {}}
            for pid, v in (records or {}).items()
        }
        self.jobs: list[dict[str, Any]] = []
        self.retried: list[int] = []
        self.search_items: list[dict[str, Any]] = []
        self.searches: list[dict[str, str]] = []
        self.listing: list[dict[str, Any]] = []
        self.listings: list[dict[str, str]] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer archive-token"
        path = req.url.path
        if path == "/v1/search":
            self.searches.append(dict(req.url.params))
            return httpx.Response(200, json={"items": self.search_items, "has_more": False})
        if path == "/v1/papers":
            params = dict(req.url.params)
            self.listings.append(params)
            start, limit = int(params.get("cursor", 0)), int(params["limit"])
            end = start + limit
            more = end < len(self.listing)
            body = {"items": self.listing[start:end], "next": str(end) if more else None}
            return httpx.Response(200, json=body)
        if path == "/v1/jobs":
            body = json.loads(req.content)
            status = self.statuses.get(body["kind"], self.status)
            job = {"id": len(self.jobs) + 1, "status": status, **body}
            self.jobs.append(job)
            return httpx.Response(202, json=job)
        if path.startswith("/v1/jobs/") and path.endswith("/retry"):
            job_id = int(path.split("/")[3])
            self.retried.append(job_id)
            self.statuses[self.jobs[job_id - 1]["kind"]] = "pending"  # until a test says
            return httpx.Response(200, json={"id": job_id, "status": "pending"})
        if path.startswith("/v1/jobs/"):
            return httpx.Response(200, json={"id": 1, "status": self.status})
        if path.endswith("/text"):
            pid = path.removeprefix("/v1/papers/").removesuffix("/text")
            asked = req.url.params.get("version")
            if pid not in self.texts or asked not in (None, str(self.texts[pid][0] or 0)):
                return httpx.Response(404, json={"error": "paper, artifact or job not found"})
            version, text = self.texts[pid]
            return httpx.Response(
                200, json={"paper_id": pid, "version": version, "source": "latex", "text": text}
            )
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
    """The deep service's job protocol: a job that needs two polls to finish, ending as
    `outcome` (its result, stats and model, or its error and whose failure it was). A
    submission's key it has (`keys`) is answered with that key's job, not started again
    (`started` counts those started), and found by `GET /keys/<key>`. It can fail the next
    submissions (`fail_submits`) or polls and lookups (`fail_polls`), keep a job running
    (`hold`), forget its jobs (`vanish`, as a restart does), or accept a submission and then
    lose the answer (`lose_answers`), as a dropped connection would."""

    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = outcome
        self.polls = 0
        self.requests: list[dict[str, Any]] = []
        self.keys: dict[str, str] = {}
        self.keys_sent: list[str | None] = []
        self.started = 0
        self.fail_submits = 0
        self.fail_polls = 0
        self.hold = False
        self.vanish = False
        self.lose_answers = 0

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/process":
            self.requests.append(json.loads(req.content))
            self.keys_sent.append(req.headers.get("idempotency-key"))
            if self.fail_submits:
                self.fail_submits -= 1
                return httpx.Response(503)
            key = self.keys_sent[-1]
            if key in self.keys:
                return httpx.Response(200, json={"id": self.keys[key], "status": "running"})
            self.keys[key], self.started, self.polls = "job-1", self.started + 1, 0
            if self.lose_answers:
                self.lose_answers -= 1
                raise httpx.ReadTimeout("the answer never came", request=req)
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        if req.method == "GET" and req.url.path.startswith("/keys/"):
            job = self.keys.get(unquote(req.url.path.removeprefix("/keys/")))
            if self.fail_polls:
                self.fail_polls -= 1
                return httpx.Response(503)
            return httpx.Response(200, json={"id": job}) if job else httpx.Response(404)
        if req.url.path == "/jobs/job-1" and not self.vanish:
            if self.fail_polls:
                self.fail_polls -= 1
                return httpx.Response(503)
            self.polls += 1
            if self.polls < 2 or self.hold:
                return httpx.Response(200, json={"status": "running", "stats": {"cells": 3}})
            status = "error" if self.outcome.get("error") else "done"
            return httpx.Response(200, json={"status": status, **self.outcome})
        return httpx.Response(404)


class FakeNotion:
    """Enough of api.notion.com at version 2026-03-11 for the `notion` integration: pages under a
    parent page, a database with its data source (properties with percent-encoded ids, as Notion
    gives them), views, page properties and body blocks. Only the token `notion-token` is let in.

    `limited` answers that many next requests 429 with a Retry-After of 7 seconds. `lose_answers`
    makes the next row creations happen and then answer 502, `lose_patches` the next changes to the
    data source's properties; `refuse_appends` answers the next block appends 500. `hidden` keeps
    that many next queries from finding the newest page, as a lagging index would. `down` answers
    500 to all. `requests` holds every request as (method, path, body). `trash` and `delete` take an
    object away as a person in Notion would; what it holds reads as trashed too, and answers as
    Notion does (probed 2026-10-19): a trashed page reads `in_trash`, refuses edits with a 400 and
    its blocks with a 404; a trashed data source reads `in_trash` and refuses changes and
    queries."""

    def __init__(self, *pages: str) -> None:
        self.objects: dict[str, dict[str, Any]] = {
            p: {"object": "page", "children": []} for p in pages
        }
        self.views: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.limited = 0
        self.lose_answers = 0
        self.lose_patches = 0
        self.refuse_appends = 0
        self.hidden = 0
        self.down = False
        self.made = 0
        self.newest: str | None = None

    def _id(self, kind: str) -> str:
        self.made += 1
        return f"{kind}-{self.made:04d}"

    # -- reading it back ----------------------------------------------------------------------

    def kept(self, method: str | None = None, prefix: str = "") -> list[tuple[str, str, Any]]:
        """The requests made, of a method and under a path when given."""
        return [r for r in self.requests if (method in (None, r[0])) and r[1].startswith(prefix)]

    def writes(self) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] != "GET" and not r[1].endswith("/query")]

    def source(self) -> dict[str, Any]:
        (found,) = [o for o in self.objects.values() if o["object"] == "data_source"]
        return found

    def rows(self) -> list[dict[str, Any]]:
        return [o for o in self.objects.values() if o["object"] == "page" and "source" in o]

    def row(self, arxiv_id: str) -> dict[str, Any]:
        (found,) = [r for r in self.rows() if self.value(r, "arXiv id") == arxiv_id]
        return found

    def value(self, row: dict[str, Any], name: str) -> Any:
        """A row's property by its column's name: plain text, a select's name, a multi-select's
        names, a date's start or a URL."""
        column = self.source()["properties"][name]
        value = row["properties"].get(unquote(column["id"]))
        if value is None:
            return None
        kind = column["type"]
        if kind in ("title", "rich_text"):
            return "".join(t["text"]["content"] for t in value[kind])
        if kind == "select":
            return value["select"]["name"] if value["select"] else None
        if kind == "multi_select":
            return [o["name"] for o in value["multi_select"]]
        if kind == "date":
            return value["date"]["start"] if value["date"] else None
        return value[kind]

    def body(self, block_id: str) -> list[dict[str, Any]]:
        return [self.objects[b] for b in self.objects[block_id]["children"]]

    def trash(self, key: str) -> None:
        self.objects[key]["in_trash"] = True

    def delete(self, key: str) -> None:
        del self.objects[key]

    def in_trash(self, key: str) -> bool:
        """Whether the object or anything holding it (data source, database, page) is trashed."""
        obj: dict[str, Any] | None = self.objects[key]
        while obj is not None:
            if obj.get("in_trash"):
                return True
            up = obj.get("source") or obj.get("database") or obj.get("parent")
            obj = self.objects.get(up) if up else None
        return False

    def view_named(self, name: str) -> dict[str, Any]:
        (found,) = [v for v in self.views.values() if v["name"] == name]
        return found

    # -- the API ------------------------------------------------------------------------------

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer notion-token"
        assert req.headers["Notion-Version"] == "2026-03-11"
        path = req.url.path.removeprefix("/v1")
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, path, body))
        if self.limited:
            self.limited -= 1
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"code": "rate_limited"})
        if self.down:
            return httpx.Response(500, json={"code": "internal_server_error"})
        parts = path.strip("/").split("/")
        route = (req.method, parts[0], parts[2] if len(parts) > 2 else None)
        target = parts[1] if len(parts) > 1 and parts[0] != "views" else None
        if target is not None and target not in self.objects:
            return httpx.Response(404, json={"code": "object_not_found"})
        trashed = target is not None and self.in_trash(target)
        if trashed and (req.method != "GET" or parts[0] == "blocks"):
            if parts[0] == "blocks" and req.method == "GET" or parts[0] == "data_sources":
                return httpx.Response(404, json={"code": "object_not_found"})
            return httpx.Response(400, json={"code": "validation_error", "message": "archived"})
        if route == ("GET", "blocks", "children"):
            return self._list([b for b in self.body(parts[1]) if not b.get("in_trash")])
        if route == ("PATCH", "blocks", "children") and self.refuse_appends:
            self.refuse_appends -= 1
            return httpx.Response(500, json={"code": "internal_server_error"})
        if route == ("PATCH", "blocks", "children"):
            for child in body["children"]:
                self._block(parts[1], child)
            return httpx.Response(200, json={"results": []})
        if req.method == "DELETE" and parts[0] == "blocks":
            parent = self.objects.pop(parts[1])["parent"]
            self.objects[parent]["children"].remove(parts[1])
            return httpx.Response(200, json={"id": parts[1]})
        if route == ("POST", "databases", None):
            return self._database(body)
        if route == ("GET", "databases", None):
            return httpx.Response(200, json=self.objects[parts[1]])
        if parts[0] == "data_sources":
            if len(parts) == 3:
                return self._query(parts[1], body)
            if req.method == "PATCH":
                self._properties(parts[1], body["properties"])
                if self.lose_patches:
                    self.lose_patches -= 1
                    return httpx.Response(502, text="Bad Gateway")
            return httpx.Response(200, json=self.objects[parts[1]] | {"in_trash": trashed})
        if route == ("GET", "pages", None):
            return httpx.Response(200, json={"object": "page", "id": target, "in_trash": trashed})
        if route == ("POST", "pages", None):
            return self._page(body)
        if route == ("PATCH", "pages", None):
            self._write(self.objects[parts[1]], body["properties"])
            return httpx.Response(200, json={"id": parts[1]})
        if parts[0] == "views":
            return self._view(
                req.method, parts[1] if len(parts) > 1 else None, body, req.url.params
            )
        return httpx.Response(404, json={"code": "object_not_found"})

    def _list(self, items: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(200, json={"results": items, "has_more": False, "next_cursor": None})

    def _block(self, parent: str, block: dict[str, Any]) -> str:
        key = self._id("block")
        kind = block["type"]
        children = block[kind].get("children", [])
        content = {k: v for k, v in block[kind].items() if k != "children"}
        self.objects[key] = {
            "object": "block",
            "id": key,
            "type": kind,
            kind: content,
            "parent": parent,
            "children": [],
        }
        self.objects[parent]["children"].append(key)
        for child in children:
            self._block(key, child)
        return key

    def _database(self, body: dict[str, Any]) -> httpx.Response:
        parent = body["parent"]["page_id"]
        db, ds, view = self._id("db"), self._id("ds"), self._id("view")
        title = body["title"][0]["text"]["content"]
        self.objects[db] = {
            "object": "database", "id": db, "data_sources": [{"id": ds, "name": title}],
            "parent": parent, "children": [],
        }  # fmt: skip
        self.objects[ds] = {"object": "data_source", "id": ds, "properties": {}, "database": db}
        self._properties(ds, body["initial_data_source"]["properties"])
        self.views[view] = {"id": view, "name": "Default view", "database": db}
        self.objects[parent]["children"].append(db)
        self.objects[db] |= {"type": "child_database", "child_database": {"title": title}}
        return httpx.Response(200, json=self.objects[db])

    def _properties(self, ds: str, changes: dict[str, Any]) -> None:
        """Add, rename and reconfigure properties, by name or (decoded) id. Options left out of
        a select's list are removed, from the pages too."""
        props = self.objects[ds]["properties"]
        for key, change in changes.items():
            name = next((n for n, p in props.items() if unquote(p["id"]) == key), key)
            if name not in props:
                kind = next(k for k in change if k not in ("name", "description"))
                pid = "title" if kind == "title" else quote(f"{self._id('p')}\\", safe="")
                props[name] = {"id": pid, "name": name, "type": kind, kind: {}}
            prop = props[name]
            if new := change.get("name"):
                props[new] = props.pop(name) | {"name": new}
                prop = props[new]
            kind = next((k for k in change if k not in ("name", "description")), prop["type"])
            if kind != prop["type"]:  # converted: the values go
                prop.pop(prop["type"])
                prop.update(type=kind, **{kind: {}})
                for row in self.rows():
                    row["properties"].pop(unquote(prop["id"]), None)
            if "options" in change.get(kind, {}):
                kept = [
                    {"name": o["name"], "color": o.get("color", "default")}
                    for o in change[kind]["options"]
                ]
                old = {o["name"]: o for o in prop[kind].get("options", [])}
                prop[kind]["options"] = [old.get(o["name"], o) for o in kept]
                names = {o["name"] for o in kept}
                for row in self.rows():
                    value = row["properties"].get(unquote(prop["id"]))
                    if value and kind == "multi_select":
                        value["multi_select"] = [
                            o for o in value["multi_select"] if o["name"] in names
                        ]
                    elif value and kind == "select":
                        chosen = value["select"]
                        value["select"] = chosen if chosen and chosen["name"] in names else None

    def _write(self, row: dict[str, Any], properties: dict[str, Any]) -> None:
        props = self.objects[row["source"]]["properties"]
        for key, value in properties.items():
            prop = props.get(key) or next(p for p in props.values() if unquote(p["id"]) == key)
            kind = prop["type"]
            if kind in ("select", "multi_select"):  # a name not yet an option becomes one
                have = prop[kind].setdefault("options", [])
                chosen = value[kind] if kind == "multi_select" else [value[kind] or {}]
                have += [
                    {"name": o["name"]}
                    for o in chosen
                    if o and o["name"] not in [h["name"] for h in have]
                ]
            row["properties"][unquote(prop["id"])] = value

    def _page(self, body: dict[str, Any]) -> httpx.Response:
        key = self._id("page")
        parent = body["parent"]
        if parent["type"] == "page_id":
            title = body["properties"]["title"]["title"][0]["text"]["content"]
            self.objects[key] = {
                "object": "page", "id": key, "type": "child_page",
                "child_page": {"title": title}, "parent": parent["page_id"], "children": [],
            }  # fmt: skip
            self.objects[parent["page_id"]]["children"].append(key)
        else:
            self.objects[key] = {
                "object": "page", "id": key, "source": parent["data_source_id"],
                "properties": {}, "children": [],
            }  # fmt: skip
            self._write(self.objects[key], body["properties"])
            self.newest = key
        for child in body.get("children", []):
            self._block(key, child)
        if self.lose_answers and parent["type"] == "data_source_id":
            self.lose_answers -= 1
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json={"object": "page", "id": key})

    def _query(self, ds: str, body: dict[str, Any]) -> httpx.Response:
        wanted = body["filter"]["rich_text"]["equals"]
        name = body["filter"]["property"]
        rows = [r for r in self.rows() if r["source"] == ds and self.value(r, name) == wanted]
        if self.hidden:
            self.hidden -= 1
            rows = [r for r in rows if r["id"] != self.newest]
        return self._list([{"object": "page", "id": r["id"]} for r in rows])

    def _view(self, method: str, key: str | None, body: Any, params: Any) -> httpx.Response:
        if method == "GET" and key is None:
            db = params["database_id"]
            return self._list(
                [{"object": "view", "id": v} for v, x in self.views.items() if x["database"] == db]
            )
        if method == "POST":
            key = self._id("view")
            self.views[key] = {"id": key, "database": body["database_id"]}
        if method in ("POST", "PATCH"):
            self.views[key] |= {k: v for k, v in body.items() if k not in ("database_id", "type")}
        return httpx.Response(200, json=self.views[key])
