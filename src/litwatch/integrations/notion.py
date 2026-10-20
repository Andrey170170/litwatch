"""notion: a Notion database of the papers litwatch lists for the exported topics, for a reader
who does not use litwatch (docs/specs/v1/integrations.md, The Notion export).

One row per paper whose effective grade for an exported topic is at least the minimum grade.
The words are the export's own (GLOSSARY.md, The Notion export): Must read and Worth a look for
the grades, No longer listed for a paper that fell below them, TL;DR and Deep read for the Gist
and the Deep card; the Abstract is the paper's own. A page is never deleted: a paper that
falls below the minimum for a topic says No longer listed there, with a dated Why line saying
who took it off and why, from the `grade.changed` that did; a dismissed paper says it for every
topic, the owner having set it aside. A paper's page someone moves to Notion's trash or deletes
is left alone and never made again; "How to read this" is litwatch's and is made again.

`setup` reconciles on every start, idempotently: it makes what is missing under `parent_page`
(the database, its columns, "How to read this", the All papers view, a grade column and a view
per exported topic), then writes every paper that qualifies or already has a page. The same
pass makes the first fill and follows a changed configuration: a topic added, dropped or
renamed, a raised or lowered minimum grade. Anything made is first looked for (pages by arXiv
id, the database and "How to read this" by title, views by name), so a creation whose reply was
lost is adopted rather than made twice. Views are made once and are then the owner's; Status is
the reader's and never written. A page is written only when what litwatch would write changed.

Options: `parent_page` (the Notion page holding the database), `owner_name` (who Why lines name
for the owner's word), `topics` (a table of topic slug = name in Notion; every Focus topic under
its own name by default) and `minimum_grade` (`relevant`, the default, or `must_read`). The
token of the Notion integration the parent page is shared with comes from $NOTION_TOKEN, or the
file $NOTION_TOKEN_FILE names.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any
from urllib.parse import unquote

import httpx
import psycopg

from litwatch import events
from litwatch.config import Config
from litwatch.core import Cause, Depth, Event, Grade, Paper, Publication, Status
from litwatch.integrations import IntegrationError, required, required_secret
from litwatch.store import analyses as analyses_store
from litwatch.store import integrations as integrations_store
from litwatch.store import notion as state
from litwatch.store import outbox
from litwatch.store import papers as papers_store
from litwatch.store import publications as publications_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2026-03-11"
PACE = 1 / 3  # seconds between requests: Notion allows about three a second
WAITS = 5  # 429s waited out for one request before it fails
LONGEST_WAIT = 60.0  # seconds: a longer Retry-After is cut short, so the waits stay bounded
TEXT = 2000  # characters in one rich text object
BLOCKS = 100  # blocks in one append

DATABASE_TITLE = "litwatch papers"
GUIDE_TITLE = "How to read this"
ALL_PAPERS = "All papers"
DEFAULT_VIEW = "Default view"  # the view Notion gives a new database
EVENTS = {
    events.GRADE_CHANGED,
    events.GIST_WRITTEN,
    events.CARD_READY,
    events.PUBLICATION_FOUND,
    events.PAPER_REVISED,
}

SHOWN = {Grade.MUST_READ: "Must read", Grade.RELEVANT: "Worth a look"}
DROPPED = "No longer listed"
TRASHED = "the database is in Notion's trash, or the page holding it is; restore it to go on"
RANK = [SHOWN[Grade.MUST_READ], SHOWN[Grade.RELEVANT], DROPPED]  # best first
GRADE_OPTIONS = [
    {"name": SHOWN[Grade.MUST_READ], "color": "orange"},
    {"name": SHOWN[Grade.RELEVANT], "color": "blue"},
    {"name": DROPPED, "color": "gray"},
]
TOPIC_COLOURS = ["purple", "green", "pink", "brown", "yellow", "red"]
# The columns, in the layout's order. The title is written by its fixed id, `title`.
TITLE = "Title"
READERS = "Status"  # the reader's: made with the database, never written or put back
HIDDEN = "arXiv id"  # what identifies a paper's page
# What a row shows in a view as made; the other columns are there, hidden, for the owner to show.
VISIBLE = (TITLE, "Grade", "Topic", "TL;DR", "Published", READERS)
COLUMNS: dict[str, dict[str, Any]] = {
    TITLE: {"title": {}},
    "Grade": {"select": {"options": GRADE_OPTIONS}},
    "Topic": {"multi_select": {"options": []}},
    "TL;DR": {"rich_text": {}},
    "Published": {"date": {}},
    READERS: {
        "select": {"options": [{"name": n} for n in ("To read", "Read", "Useful", "Not useful")]}
    },
    "Authors": {"rich_text": {}},
    "Venue": {"rich_text": {}},
    "arXiv": {"url": {}},
    "PDF": {"url": {}},
    "Added": {"date": {}},
    "Why": {"rich_text": {}},
    HIDDEN: {"rich_text": {}},
}

# The Deep read's callouts: (card section, icon, colour, heading).
SECTIONS = [
    ("key_claim", "💬", "gray", "Key claim"),
    ("summary", "📖", "blue", "Summary"),
    ("faithfulness", "🎯", "purple", "Faithfulness"),
    ("monitoring", "🔎", "red", "Monitoring"),
    ("evaluation_setup", "🧪", "green", "Evaluation"),
    ("prior_work", "📚", "yellow", "Closest prior work"),
    ("limitations", "⚠️", "orange", "Limitations"),
]


class NotionError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # Notion's HTTP status, when it answered


class NotionClient:
    """Notion's API at version 2026-03-11, at about three requests a second. A 429 is waited
    out as its Retry-After says; any other failure raises `NotionError`."""

    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient | None = None,
        *,
        pace: float = PACE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.http = http or httpx.AsyncClient(timeout=60.0)
        self.http.headers.update({"Authorization": f"Bearer {token}", "Notion-Version": VERSION})
        self.pace = pace
        self.sleep = sleep
        self._next = 0.0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, **params: Any
    ) -> dict[str, Any]:
        clock = asyncio.get_running_loop().time
        for waited in range(WAITS + 1):
            if (wait := self._next - clock()) > 0:
                await self.sleep(wait)
            self._next = clock() + self.pace
            resp = await self.http.request(method, f"{API}{path}", json=body, params=params)
            if resp.status_code != 429 or waited == WAITS:
                break
            await self.sleep(min(float(resp.headers.get("Retry-After") or 1), LONGEST_WAIT))
        if not resp.is_success:
            raise NotionError(
                f"{method} {path}: {resp.status_code} {resp.text[:300]}", resp.status_code
            )
        return resp.json()

    async def every(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Every result of a paginated GET, page by page."""
        found: list[dict[str, Any]] = []
        while True:
            page = await self.request("GET", path, page_size=100, **params)
            found += page["results"]
            if not page.get("has_more"):
                return found
            params["start_cursor"] = page["next_cursor"]


@dataclass(frozen=True)
class Exported:
    name: str  # in Notion
    subject: str  # the topic's subject, for "How to read this"


class Notion:
    name = "notion"
    description = (
        "Keeps a Notion database of the papers listed for the exported topics, for a reader who "
        "does not use litwatch. Never deletes a page."
    )

    def __init__(
        self,
        client: NotionClient,
        *,
        name: str = "notion",
        parent_page: str,
        owner_name: str,
        topics: dict[str, str] | None = None,
        minimum: Grade = Grade.RELEVANT,
        zone: tzinfo = UTC,
        now: Callable[[], datetime] | None = None,
        adopt_tries: int = 3,
        adopt_wait: float = 2.0,
    ) -> None:
        self.name = name
        self.notion = client
        self.parent_page = parent_page
        self.owner_name = owner_name
        self.configured = topics
        self.minimum = minimum
        self.zone = zone
        self.now = now or (lambda: datetime.now(UTC))
        # A page whose creation may have happened is looked for this often, `adopt_wait`
        # seconds apart, before it is made: Notion's query may lag behind a fresh page.
        self.adopt_tries = adopt_tries
        self.adopt_wait = adopt_wait
        self.exported: dict[str, Exported] = {}  # known once set up
        self._db: state.Database | None = None

    def wants(self, event: Event) -> bool:
        if event.type not in EVENTS or event.paper_id is None:
            return False
        return event.type != events.GRADE_CHANGED or event.payload.get("topic") in self.exported

    async def aclose(self) -> None:
        await self.notion.aclose()

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        # Read again, under the dispatch lock: another service's setup may have made something
        # again since this one's.
        db = await state.database(conn, self.name)
        if self._db is None or db is None or db.parent_page != self.parent_page:
            raise NotionError("not set up")
        self._db = db
        await self._write(conn, event.paper_id)

    # -- reconcile ---------------------------------------------------------------------------------

    async def setup(self, conn: psycopg.AsyncConnection) -> None:
        """Reconcile: make what is missing, then write every paper that qualifies or has a
        page. Safe to stop and run again at any point."""
        self.exported = await self._exported(conn)
        db = await state.database(conn, self.name)
        if db is not None and db.parent_page != self.parent_page:
            log.info("%s: the parent page changed; building afresh", self.name)
            await state.forget(conn, self.name)
            db = None
        db = db or state.Database(self.name, self.parent_page)
        await self._database(conn, db)
        columns = await self._columns(conn, db)
        await self._guide(conn, db)
        await self._views(conn, db, columns)
        self._db = db
        grades = [g.value for g in Grade if g.at_least(self.minimum)]
        for paper_id in await state.papers(conn, self.name, list(self.exported), grades):
            await self._write(conn, paper_id)

    async def _exported(self, conn: psycopg.AsyncConnection) -> dict[str, Exported]:
        """The exported topics, in the configuration's order, or the Focus topics in the
        owner's."""
        tree = await taxonomy_store.load(conn)
        if tree is None:
            raise NotionError("there is no taxonomy yet")
        names = {c.slug: c.name for c in tree.classes}
        followed = {t.slug: t for t in tree.topics if t.status != Status.DRAFT}
        if self.configured is None:
            chosen = {s: names[s] for s, t in followed.items() if t.depth == Depth.FOCUS}
        else:
            chosen = dict(self.configured)
            if unknown := sorted(set(chosen) - set(followed)):
                raise NotionError(f"not followed topics: {', '.join(unknown)}")
        if not chosen:
            raise NotionError("no topic to export")
        taken = [*chosen.values(), *COLUMNS]
        if len(set(taken)) != len(taken):
            raise NotionError(f"topic names must differ from each other and from {list(COLUMNS)}")
        return {s: Exported(name, followed[s].subject) for s, name in chosen.items()}

    async def _child(self, kind: str, title: str) -> str | None:
        """A child page or database of the parent page, by its title."""
        for block in await self.notion.every(f"/blocks/{self.parent_page}/children"):
            if block["type"] == kind and block[kind]["title"] == title:
                return block["id"]
        return None

    async def _database(self, conn: psycopg.AsyncConnection, db: state.Database) -> None:
        if db.database_id:
            return
        if found := await self._child("child_database", DATABASE_TITLE):
            made = await self.notion.request("GET", f"/databases/{found}")
        else:
            made = await self.notion.request(
                "POST",
                "/databases",
                {
                    "parent": {"type": "page_id", "page_id": self.parent_page},
                    "title": _rich(DATABASE_TITLE),
                    "initial_data_source": {"properties": COLUMNS},
                },
            )
            log.info("%s: made the database %s", self.name, made["id"])
        db.database_id, db.data_source_id = made["id"], made["data_sources"][0]["id"]
        await state.save_database(conn, db)

    async def _columns(self, conn: psycopg.AsyncConnection, db: state.Database) -> dict[str, str]:
        """Put back the columns litwatch writes, add or rename each exported topic's grade
        column, and give Topic an option per topic exported so far. Returns the columns'
        property ids by name. Once a column was put back, every row is written again."""
        path = f"/data_sources/{db.data_source_id}"
        source = await self.notion.request("GET", path)
        if source.get("in_trash"):
            raise NotionError(TRASHED)
        props = source["properties"]
        # A name another topic's column is recorded under, renamed now or not, is that topic's.
        held = {e["name"]: s for s, e in db.topics.items()}
        if clash := sorted(t.name for s, t in self.exported.items() if held.get(t.name, s) != s):
            raise NotionError(
                f"already the column of another topic: {', '.join(clash)}; give the topic "
                "another name, or rename the other first"
            )
        by_id = {unquote(p["id"]): p for p in props.values()}
        missing = [c for c in COLUMNS if c not in props and c not in (TITLE, READERS)]
        change: dict[str, Any] = {c: COLUMNS[c] for c in missing}
        restored, added = bool(missing), []
        recorded = set(db.topics)  # the topics exported before this pass
        for slug, topic in self.exported.items():
            entry = db.topics.get(slug)
            column = by_id.get(entry["column"]) if entry else None
            if column is None:
                column = props.get(topic.name)  # made, though its reply was lost
            if column is None:
                change[topic.name] = {"select": {"options": GRADE_OPTIONS}}
                added.append(slug)
                restored |= entry is not None
            elif entry and entry["name"] != topic.name and column["name"] != topic.name:
                change[unquote(column["id"])] = {"name": topic.name}  # renamed in the config
            db.topics[slug] = {
                "name": topic.name,
                "column": unquote(column["id"]) if column else None,
                "view": entry["view"] if entry else None,
            }
        have = [
            o["name"] for o in props.get("Topic", {}).get("multi_select", {}).get("options", [])
        ]
        wanted = [db.topics[s]["name"] for s in self._order(db)]
        if "Topic" in missing or set(have) != set(wanted):
            # The API renames no option: a renamed topic's old one goes, which takes it off the
            # pages, and they are written again with the new name.
            options = [
                {"name": n} if n in have else {"name": n, "color": TOPIC_COLOURS[i % 6]}
                for i, n in enumerate(wanted)
            ]
            change["Topic"] = {"multi_select": {"options": options}}
            # Rows lose an option that goes, and lack one the owner deleted: write them again.
            gone = set(have) - set(wanted)
            lost = {db.topics[s]["name"] for s in recorded} - set(have)
            restored |= bool(gone or lost)
        if restored:  # first: the repair's reply may be lost, and the column then looks whole
            await state.rewrite_properties(conn, self.name)
        if change:
            props = (await self.notion.request("PATCH", path, {"properties": change}))["properties"]
        for slug in added:
            db.topics[slug]["column"] = unquote(props[db.topics[slug]["name"]]["id"])
        await state.save_database(conn, db)
        return {name: unquote(p["id"]) for name, p in props.items()}

    async def _guide(self, conn: psycopg.AsyncConnection, db: state.Database) -> None:
        """ "How to read this", beside the database, rewritten when what it says changed, and
        made again when it was taken away: it is litwatch's."""
        if db.guide_page and await self._trashed(db.guide_page):
            log.info("%s: %r was taken away in Notion; making it again", self.name, GUIDE_TITLE)
            db.guide_page = None
        if not db.guide_page:
            if not (found := await self._child("child_page", GUIDE_TITLE)):
                made = await self.notion.request(
                    "POST",
                    "/pages",
                    {
                        "parent": {"type": "page_id", "page_id": self.parent_page},
                        "properties": {"title": {"title": _rich(GUIDE_TITLE)}},
                    },
                )
                found = made["id"]
            db.guide_page, db.guide_hash = found, None
            await state.save_database(conn, db)
        blocks = self._guide_blocks()
        if (digest := _hash(blocks)) != db.guide_hash:
            if db.guide_hash:  # forgotten first, as a page's hashes are (`_write`)
                db.guide_hash = None
                await state.save_database(conn, db)
            await self._replace_body(db.guide_page, blocks)
            db.guide_hash = digest
            await state.save_database(conn, db)

    async def _views(
        self, conn: psycopg.AsyncConnection, db: state.Database, columns: dict[str, str]
    ) -> None:
        """Make the views not made yet: All papers, and one per exported topic. A view once made
        is the owner's and never touched again, even when the owner deleted it."""
        wanted = [(None, ALL_PAPERS, columns["Grade"])] if not db.all_view else []
        wanted += [
            (s, t.name, db.topics[s]["column"])
            for s, t in self.exported.items()
            if not db.topics[s]["view"]
        ]
        if not wanted:
            return
        known: dict[str, list[str]] = {}  # the database's views by name, oldest first
        for v in await self.notion.every("/views", database_id=db.database_id):
            view = await self.notion.request("GET", f"/views/{v['id']}")
            known.setdefault(view["name"], []).append(view["id"])
        fresh = list(known) == [DEFAULT_VIEW]  # a new database, with Notion's one view
        # Views litwatch made, whatever the owner named them since: none is taken twice.
        owned = {db.all_view, *(t["view"] for t in db.topics.values())} - {None}
        for slug, name, group in wanted:
            view = next((v for v in known.get(name, []) if v not in owned), None)
            if view is None:
                body = self._view(db, name, group, columns, filtered=slug is not None)
                if slug is None and fresh:
                    made = await self.notion.request(
                        "PATCH", f"/views/{known[DEFAULT_VIEW][0]}", body
                    )
                else:
                    target = {"database_id": db.database_id, "data_source_id": db.data_source_id}
                    made = await self.notion.request(
                        "POST", "/views", {**target, "type": "table", **body}
                    )
                view = made["id"]
            owned.add(view)
            if slug is None:
                db.all_view = view
            else:
                db.topics[slug]["view"] = view
            await state.save_database(conn, db)

    def _view(
        self,
        db: state.Database,
        name: str,
        group: str,
        columns: dict[str, str],
        *,
        filtered: bool,
    ) -> dict[str, Any]:
        """A table view grouped by the grade column `group`, newest published first; `filtered`
        shows only the rows with a grade there."""
        order = [*COLUMNS, *(db.topics[s]["name"] for s in self._order(db))]
        shown = sorted(columns, key=lambda c: order.index(c) if c in order else len(order))
        body: dict[str, Any] = {
            "name": name,
            "sorts": [{"property": columns["Published"], "direction": "descending"}],
            "configuration": {
                "type": "table",
                "group_by": {
                    "type": "select",
                    "property_id": group,
                    "sort": {"type": "manual"},
                    "hide_empty_groups": True,
                },
                # Every column, in the layout's order, only the VISIBLE ones shown.
                "properties": [{"property_id": columns[c], "visible": c in VISIBLE} for c in shown],
            },
        }
        if filtered:
            body["filter"] = {"property": group, "select": {"is_not_empty": True}}
        return body

    # -- a paper's page --------------------------------------------------------------------------

    async def _write(self, conn: psycopg.AsyncConnection, paper_id: str) -> None:
        """Bring the paper's page in line with what litwatch holds now, writing nothing that did
        not change. A page someone took away in Notion is left alone from then on."""
        page = await state.page(conn, self.name, paper_id)
        if page is not None and page.gone_at is not None:
            return
        paper = await papers_store.get(conn, paper_id)
        if paper is None:
            return
        dismissed = await papers_store.dismissed_at(conn, paper_id)
        effective = await word_store.effective_for(conn, paper_id)
        begun = page is not None and page.page_id is None
        topics = dict(page.topics) if page else {}
        history: list[Event] | None = None  # its grade changes, read when one may say why it left
        for slug in self.exported:
            grade = effective[slug].grade.value if slug in effective else None
            if slug in topics and not self._qualifies(grade, dismissed) and history is None:
                history = await outbox.of_paper(conn, paper_id, events.GRADE_CHANGED)
            changes = [e for e in history or [] if e.payload.get("topic") == slug]
            if (now := self._topic(topics.get(slug), grade, dismissed, changes)) is None:
                topics.pop(slug, None)
            else:
                topics[slug] = now
        if not any(slug in topics for slug in self.exported):
            return  # never listed, or only for topics no longer exported
        if page is None:
            page = state.Page(paper_id, added=self.now().astimezone(self.zone).date())
        record = (await publications_store.found(conn, [paper_id])).get(paper_id)
        gist = await analyses_store.current_gist(conn, paper_id)
        card = await integrations_store.card(conn, paper_id)
        properties = self._properties(paper, gist, record, page.added, topics)
        body = self._body(paper, gist, card, topics)
        written = (page.page_id, page.properties_hash, page.body_hash, page.topics)
        if page.page_id is None:
            page.topics = topics  # kept with the creation: a lost reply is then known listed
            listed = any(topics[s]["shown"] != DROPPED for s in self.exported if s in topics)
            if not await self._create(conn, page, properties, body, begun, make=listed):
                await state.forget_page(conn, self.name, paper_id)  # it never got a page
                return
        wanted = (_hash(properties), _hash(body))
        # A write that fails part way leaves the page unknown: its old hash must not match what
        # a later undo would write, so it is forgotten first.
        old = (page.properties_hash, page.body_hash)
        page.properties_hash, page.body_hash = (
            h if h in (None, w) else None for h, w in zip(old, wanted, strict=True)
        )
        if (page.properties_hash, page.body_hash) != old:
            await state.save_page(conn, self.name, page)
        try:
            if page.properties_hash != wanted[0]:
                path = f"/pages/{page.page_id}"
                await self.notion.request("PATCH", path, {"properties": properties})
                page.properties_hash = wanted[0]
            if page.body_hash != wanted[1]:
                await self._replace_body(page.page_id, body)
                page.body_hash = wanted[1]
        except NotionError:
            if not await self._gone(page.page_id):
                raise
            log.info("%s: the page of %s was taken away in Notion; left alone", self.name, paper_id)
            page.gone_at = self.now()
            await state.save_page(conn, self.name, page)
            return
        page.topics = topics
        if (page.page_id, page.properties_hash, page.body_hash, page.topics) != written:
            await state.save_page(conn, self.name, page)

    async def _create(
        self,
        conn: psycopg.AsyncConnection,
        page: state.Page,
        properties: dict[str, Any],
        body: list[dict[str, Any]],
        begun: bool,
        *,
        make: bool,
    ) -> bool:
        """Find the paper's page by its arXiv id, or make it when `make` (the paper is listed);
        returns whether it has one. A creation `begun` before may have happened with its reply
        lost: then the page is looked for again, a little later, before another is made."""
        for tried in range(self.adopt_tries if begun else 1):
            if tried:
                await asyncio.sleep(self.adopt_wait)
            found = await self.notion.request(
                "POST",
                f"/data_sources/{self._db.data_source_id}/query",
                {"filter": {"property": HIDDEN, "rich_text": {"equals": page.paper_id}}},
            )
            if found["results"]:
                page.page_id = found["results"][0]["id"]
                page.properties_hash = page.body_hash = None  # what it holds is not known
                log.info("%s: found the page of %s", self.name, page.paper_id)
                return True
        if not make:
            return False
        await state.save_page(conn, self.name, page)  # begun: its reply may be lost
        made = await self.notion.request(
            "POST",
            "/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": self._db.data_source_id},
                "properties": properties,
                "children": body[:BLOCKS],
            },
        )
        page.page_id, page.properties_hash = made["id"], _hash(properties)
        page.body_hash = _hash(body) if len(body) <= BLOCKS else None
        await state.save_page(conn, self.name, page)
        return True

    async def _replace_body(self, block: str, body: list[dict[str, Any]]) -> None:
        """Delete the page's blocks, then append the new ones."""
        for child in await self.notion.every(f"/blocks/{block}/children"):
            await self.notion.request("DELETE", f"/blocks/{child['id']}")
        for start in range(0, len(body), BLOCKS):
            await self.notion.request(
                "PATCH", f"/blocks/{block}/children", {"children": body[start : start + BLOCKS]}
            )

    async def _trashed(self, page_id: str) -> bool:
        """Whether the page is in Notion's trash, itself or with what holds it, or deleted."""
        try:
            found = await self.notion.request("GET", f"/pages/{page_id}")
        except NotionError as exc:
            if exc.status == 404:
                return True
            raise
        return bool(found.get("in_trash") or found.get("is_archived"))

    async def _gone(self, page_id: str) -> bool:
        """Whether someone took the paper's page away in Notion. Raises when the whole database
        went (trashed, deleted, or no longer shared), so no page is taken for gone with it."""
        if not await self._trashed(page_id):
            return False
        source = await self.notion.request("GET", f"/data_sources/{self._db.data_source_id}")
        if source.get("in_trash"):
            raise NotionError(TRASHED)
        return True

    def _qualifies(self, grade: str | None, dismissed: datetime | None) -> bool:
        return dismissed is None and grade is not None and Grade(grade).at_least(self.minimum)

    def _topic(
        self,
        before: dict[str, Any] | None,
        grade: str | None,
        dismissed: datetime | None,
        changes: list[Event],
    ) -> dict[str, Any] | None:
        """What the page says for one topic, from its effective grade now, whether the owner
        dismissed the paper, what was written before and the topic's grade changes (oldest
        first); None while it was never listed. A Why records the newest change it had seen
        (`seen`): a change after that which takes the paper off again replaces it."""
        if self._qualifies(grade, dismissed):
            return {"grade": grade, "shown": SHOWN[Grade(grade)], "why": None, "since": None}
        if before is None:
            return None
        dropped = before["shown"] == DROPPED
        off = [e for e in changes if self._takes_off(e)]
        newer = off and off[-1].id > before.get("seen", 0)

        def gone(why: str, since: datetime, by: str) -> dict[str, Any]:
            day = since.astimezone(self.zone).date().isoformat()
            seen = changes[-1].id if changes else 0
            return {
                "grade": grade,
                "shown": DROPPED,
                "why": why,
                "since": day,
                "by": by,
                "seen": seen,
            }

        if dropped and before.get("by") == "dismissed":
            if dismissed is not None:
                return {**before, "grade": grade}  # still set aside
        elif dropped and not newer:
            return {**before, "grade": grade}  # still why it is off
        if dismissed is not None and not dropped:
            return gone(f"{self.owner_name} set it aside.", dismissed, "dismissed")
        if not dropped and grade == before["grade"]:  # listed at this grade, no longer: it rose
            return gone(
                f"the list now shows only {SHOWN[self.minimum]} papers.", self.now(), "minimum"
            )
        if off:
            return gone(self._why(off[-1].payload), off[-1].created_at, "grade")
        return gone(self._why({}), self.now(), "grade")  # no change on record says: a verdict's

    def _takes_off(self, change: Event) -> bool:
        """Whether the grade change took the paper down from a listed grade to below the
        minimum."""
        before, after = change.payload.get("before"), change.payload.get("after")
        # Listed before under the lowest minimum: the minimum may have been raised since.
        if before is None or not Grade(before).at_least(Grade.RELEVANT):
            return False
        return after is None or (
            not Grade(after).at_least(self.minimum) and not Grade(after).at_least(Grade(before))
        )

    def _why(self, payload: dict[str, Any]) -> str:
        """Who took the paper off the list, and why, in plain words. An event written before
        `cause` was recorded has none: it was a verdict's."""
        cause = Cause(payload.get("cause") or Cause.VERDICT)
        moved = payload.get("fit_before") != payload.get("fit_after")
        what = "off-topic" if moved else "less important"
        if cause == Cause.OWNER:
            return f"{self.owner_name} marked it {what}."
        if cause == Cause.TAXONOMY:
            return "the topic's definition changed."
        return f"litwatch re-checked it and judged it {what}."

    def _order(self, db: state.Database) -> list[str]:
        """Every topic exported so far: those exported now in their order, then the rest."""
        return [*self.exported, *sorted(s for s in db.topics if s not in self.exported)]

    def _why_lines(self, topics: dict[str, Any]) -> list[str]:
        """One line per topic the paper left, the topic named."""
        return [
            f"{self._db.topics[s]['name']}, {_day(topics[s]['since'])}: {topics[s]['why']}"
            for s in self._order(self._db)
            if s in topics and topics[s]["why"]
        ]

    def _shown(self, topics: dict[str, Any]) -> str:
        """The page's Grade: the best over the exported topics."""
        return min((topics[s]["shown"] for s in self.exported if s in topics), key=RANK.index)

    def _properties(
        self,
        paper: Paper,
        gist: str | None,
        record: Publication | None,
        added: date,
        topics: dict[str, Any],
    ) -> dict[str, Any]:
        named = [self._db.topics[s]["name"] for s in self._order(self._db) if s in topics]
        where = (record.venue or record.container, record.year) if record else ()
        doi = f"https://doi.org/{record.doi}" if record and record.doi else None
        published = record.url or doi if record else None
        venue = " ".join(str(x) for x in where if x) or ("Published version" if published else "")
        return {
            "title": {"title": _rich(paper.title)},
            "Grade": {"select": {"name": self._shown(topics)}},
            "Topic": {"multi_select": [{"name": n} for n in named]},
            "TL;DR": {"rich_text": _rich(gist or "")},
            "Published": {"date": {"start": paper.published[:10]} if paper.published else None},
            "Authors": {"rich_text": _rich(", ".join(paper.authors))},
            "Venue": {"rich_text": _rich(venue, link=published)},
            "arXiv": {"url": f"https://arxiv.org/abs/{paper.id}"},
            "PDF": {"url": f"https://arxiv.org/pdf/{paper.id}"},
            "Added": {"date": {"start": added.isoformat()}},
            "Why": {"rich_text": _rich("\n".join(self._why_lines(topics)))},
            HIDDEN: {"rich_text": _rich(paper.id)},
            # The exported topics' columns only: a dropped topic's keeps its last value.
            **{
                self._db.topics[s]["column"]: {"select": {"name": topics[s]["shown"]}}
                for s in self.exported
                if s in topics
            },
        }

    def _body(
        self,
        paper: Paper,
        gist: str | None,
        card: dict[str, Any] | None,
        topics: dict[str, Any],
    ) -> list[dict[str, Any]]:
        blocks = []
        if self._shown(topics) == DROPPED:
            lines = "\n".join(self._why_lines(topics))
            blocks.append(
                _callout("🗄️", "gray", [*_rich(f"{DROPPED}.\n", bold=True), *_rich(lines)])
            )
        guide = {
            "type": "mention",
            "mention": {"type": "page", "page": {"id": self._db.guide_page}},
        }
        about = _rich(" explains the topics, the grades and the text on each paper.")
        blocks.append(_callout("💡", "gray", [guide, *about]))
        if gist:
            blocks += [_heading("TL;DR"), *map(_paragraph, _paragraphs(gist))]
        blocks += [_heading("Abstract"), *map(_paragraph, _paragraphs(paper.abstract))]
        if card and card["card"]:
            blocks.append(_heading("Deep read", children=_deep_read(card["card"])))
        return blocks

    def _guide_blocks(self) -> list[dict[str, Any]]:
        """ "How to read this": the topics, the grades, the text on each paper, the reader's
        column and where comments go."""
        grades = {
            SHOWN[Grade.MUST_READ]: "the paper's main question is the topic's, or its result is "
            "a landmark for it.",
            SHOWN[Grade.RELEVANT]: "the topic is a substantial part of the paper, or the paper "
            "takes a smaller step on its main question.",
            DROPPED: "the paper was on the list for the topic and was taken off it. The Why "
            "column says when, by whom and why; its page stays.",
        }
        if self.minimum == Grade.MUST_READ:
            del grades[SHOWN[Grade.RELEVANT]]
        text = {
            "TL;DR": "what the paper does, in a sentence or two.",
            "Abstract": "the paper's abstract, as its authors wrote it.",
            "Deep read": "for some papers, a longer reading of the full text: its key claim, "
            "its evaluation, the closest prior work and its limitations. It is folded under "
            "its heading.",
        }
        return [
            _paragraph(
                "This table lists new arXiv papers that litwatch, a paper-triage tool, judged "
                f"relevant to the topics below. {self.owner_name} runs litwatch, and the table is "
                "kept up to date from it as papers arrive and are checked again."
            ),
            _heading("Topics"),
            _paragraph(
                "Each paper is graded per topic. Topic lists the topics a paper is or was listed "
                "for, and each topic has a column with its grade there and a view of its own. "
                "Grade is the paper's best grade over its topics."
            ),
            *(_term(t.name, t.subject) for t in self.exported.values()),
            _heading("Grades"),
            *(_term(word, says) for word, says in grades.items()),
            _heading("On each paper"),
            *(_term(word, says) for word, says in text.items()),
            _heading("Status and comments"),
            _paragraph(
                "Status (To read, Read, Useful, Not useful) is the reader's column: litwatch never "
                "writes it. Every other column, and the text on each paper's page, is litwatch's "
                "and may be rewritten."
            ),
            _paragraph(
                "Comments belong at the top of a paper's page (page comments), not on a block of "
                "its text: the text is replaced when it is rewritten, and comments on it go too."
            ),
        ]


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b %Y}"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:32]


def _rich(text: str, *, bold: bool = False, link: str | None = None) -> list[dict[str, Any]]:
    """Rich text in the pieces Notion takes, 2000 characters each; none for an empty text."""
    pieces = []
    for start in range(0, len(text), TEXT):
        piece: dict[str, Any] = {"type": "text", "text": {"content": text[start : start + TEXT]}}
        if link:
            piece["text"]["link"] = {"url": link}
        if bold:
            piece["annotations"] = {"bold": True}
        pieces.append(piece)
    return pieces


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]


def _paragraph(text: str | list[dict[str, Any]]) -> dict[str, Any]:
    rich = _rich(text) if isinstance(text, str) else text
    return {"type": "paragraph", "paragraph": {"rich_text": rich}}


def _term(word: str, says: str) -> dict[str, Any]:
    """A bullet naming a term in bold, then what it means."""
    rich = [*_rich(f"{word}: ", bold=True), *_rich(says)]
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich}}


def _heading(text: str, children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A heading; with children, a folded toggle heading holding them."""
    heading: dict[str, Any] = {"rich_text": _rich(text)}
    if children is not None:
        heading |= {"is_toggleable": True, "children": children}
    return {"type": "heading_2", "heading_2": heading}


def _callout(icon: str, colour: str, rich: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "callout",
        "callout": {
            "rich_text": rich,
            "icon": {"type": "emoji", "emoji": icon},
            "color": f"{colour}_background",
        },
    }


def _deep_read(card: dict[str, Any]) -> list[dict[str, Any]]:
    """One callout per section of the card it has, then its code link."""
    blocks = [
        _callout(icon, colour, [*_rich(f"{heading}\n", bold=True), *_section(key, card[key])])
        for key, icon, colour, heading in SECTIONS
        if card.get(key)
    ]
    if url := card.get("code_url"):
        blocks.append(_paragraph([*_rich("Code: "), *_rich(url, link=url)]))
    return blocks


def _listed(values: list[str]) -> str:
    return ", ".join(values) or "none"


def _section(key: str, value: Any) -> list[dict[str, Any]]:
    """The text of one section of a Deep card."""
    if key == "prior_work":
        rich: list[dict[str, Any]] = []
        for i, work in enumerate(value):
            rich += _rich(("\n" if i else "") + work["title"])
            if arxiv := work.get("arxiv_id"):
                link = f"https://arxiv.org/abs/{arxiv}"
                rich += [*_rich(" ("), *_rich(f"arXiv:{arxiv}", link=link), *_rich(")")]
            rich += _rich(f": {work['relation']}")
        return rich
    if key == "limitations":
        return _rich("\n".join(f"• {x}" for x in value))
    if key == "evaluation_setup":
        lines = [
            f"Models: {_listed(value.get('models', []))}",
            f"Tasks: {_listed(value.get('tasks', []))}",
            f"Generalization: {value.get('generalization', '')}",
        ]
    elif key == "faithfulness":
        lines = [
            f"Detection method: {value['detection_method']}",
            f"Definition: {value['definition']}",
            f"Generality: {value['generality']}",
            f"Mismatch: {_listed(value.get('mismatch', []))} · Ground truth: "
            f"{value['ground_truth']} · Monitor: {value['monitor_type']} · Internals: "
            f"{_listed(value.get('internals_signal', []))}",
        ]
    elif key == "monitoring":
        lines = [
            f"Monitor method: {value['monitor_method']}",
            f"Robustness: {value['robustness']}",
            f"Generality: {value['generality']}",
            f"Target: {_listed(value.get('target', []))} · Monitor: {value['monitor_type']} · "
            f"Internals: {_listed(value.get('internals_signal', []))} · Pressure: "
            f"{value['pressure']}",
        ]
    else:  # key claim and summary: prose
        lines = _paragraphs(value)
        return _rich("\n\n".join(lines))
    return _rich("\n".join(lines))


def load(name: str, options: dict[str, Any], config: Config) -> Notion:
    parent = str(uuid.UUID(str(required(options, "parent_page"))))
    topics = options.get("topics")
    if topics is not None and (
        not isinstance(topics, dict)
        or not topics
        or not all(isinstance(v, str) and v.strip() for v in topics.values())
    ):
        raise IntegrationError("`topics` must be a table of topic slug = name in Notion")
    minimum = Grade(options.get("minimum_grade", Grade.RELEVANT))
    if minimum not in SHOWN:
        raise IntegrationError("`minimum_grade` must be relevant or must_read")
    return Notion(
        NotionClient(required_secret("NOTION_TOKEN")),
        name=name,
        parent_page=parent,
        owner_name=str(required(options, "owner_name")),
        topics=topics,
        minimum=minimum,
        zone=config.schedule.zone,
    )
