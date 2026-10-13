"""zotero: one Zotero item per library paper, with litwatch's tags and notes kept in step.

Every event it hears for a paper converges that paper's item on what litwatch holds now, so
handling an event twice, or in a different order, ends in the same place:

- a library paper gets a preprint item (found by arXiv id or title before one is made) in
  the `Litwatch` collection, and in a subcollection for its best Watch grade;
- tags `litwatch:watch:<topic>:<grade>` for each Watch topic at `relevant` or above;
- a `litwatch:screen` note with the takes and the published version, and a
  `litwatch:enrichment` note and tags from the deep card when one is stored;
- a paper the owner unsaved, or that left the library, is taken out of the collections and
  tagged `litwatch:dropped`. Its item is never deleted.

Only the litwatch namespace is ever managed: the collection tree, `litwatch:*` tags and the
two notes. Everything else on an item is preserved verbatim. The items 0.x made are taken
over from `papers.zotero_key` at start-up, so none is made twice.

Options: `user_id` (or $LITWATCH_ZOTERO_USER_ID), the API key in $ZOTERO_API_KEY,
`subcollections` ({grade: name}, default 0.x's "Tier 1" and "Tier 2"), and `inbox_collection`
with `inbox_topics`: a paper whose card is ready and that is `must_read` for one of those
topics also joins that collection (a reading app picks it up there).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import secrets
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg

from litwatch import events
from litwatch.config import Config
from litwatch.core import Event, Grade, Paper, Publication
from litwatch.integrations import required, required_secret
from litwatch.store import integrations as state
from litwatch.store import papers as papers_store
from litwatch.store import publications as publications_store
from litwatch.store import reading

log = logging.getLogger(__name__)

API = "https://api.zotero.org"
NOTE_MARKER = "<!-- litwatch:screen schema=2 -->"
NOTE_FIND = "<!-- litwatch:screen"  # 0.x's schema=1 note is replaced, not duplicated
NOTE_TAG = "litwatch:screen"
ENRICH_MARKER = "<!-- litwatch:enrichment schema=1 -->"
ENRICH_TAG = "litwatch:enrichment"
COLLECTION = "Litwatch"
SUBCOLLECTIONS = {Grade.MUST_READ: "Tier 1", Grade.RELEVANT: "Tier 2"}
WATCH_TAG_PREFIX = "litwatch:watch:"
DROPPED_TAG = "litwatch:dropped"
PDF_TAG = "litwatch:pdf"  # on the PDF attachments litwatch made
# 0.x's per-tier and per-selection tags, replaced the first time v1 updates an item.
LEGACY_PREFIXES = ("litwatch:tier:", "litwatch:sel:")
MANAGED_PREFIXES = (WATCH_TAG_PREFIX, DROPPED_TAG, *LEGACY_PREFIXES)
# What the deep card's tags may be; all are replaced when a card is written again.
CARD_PREFIXES = (
    "litwatch:kind:",
    "litwatch:monitor:",
    "litwatch:signal:",
    "litwatch:mismatch:",
    "litwatch:ground-truth:",
    "litwatch:target:",
    "litwatch:pressure:",
    "litwatch:threat:",  # pre-0.6 cards; cleared on rewrite
    "litwatch:code",
    "litwatch:processed",
)
RETRY_STATUSES = {409, 412, 429, 500, 502, 503, 504}
EVENTS = {
    events.PAPER_SAVED,
    events.PAPER_UNSAVED,
    events.GRADE_CHANGED,
    events.TAKE_WRITTEN,
    events.PUBLICATION_FOUND,
    events.CARD_READY,
}


class ZoteroError(Exception):
    pass


class GoneError(ZoteroError):
    """The item is no longer in the library."""


class AlreadyWrittenError(ZoteroError):
    """A create's write token was used before: an earlier try made the object, though its
    answer was lost. Find the object rather than make it again."""


PAGE = 100  # the API's largest page


def write_token(*parts: str) -> str:
    """A `Zotero-Write-Token` for one create: 32 characters, the same for every try of that
    create, so Zotero makes the object once however often the request is sent (it remembers
    a token for about 12 hours)."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Collections:
    """Keys of the managed collection tree, resolved once per client."""

    root: str
    by_grade: dict[Grade, str]

    @property
    def managed(self) -> set[str]:
        return {self.root, *self.by_grade.values()}


class ZoteroClient:
    """Thin async wrapper over the Web API v3. Writes are conflict-aware: read, mutate, PATCH
    with the version."""

    def __init__(
        self,
        user_id: str,
        api_key: str,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 4,
        subcollections: dict[Grade, str] | None = None,
        retry_seconds: float = 1.0,
    ) -> None:
        self.base = f"{API}/users/{user_id}"
        self.http = http or httpx.AsyncClient(timeout=60.0)
        self.http.headers.update({"Zotero-API-Key": api_key, "Zotero-API-Version": "3"})
        self.max_retries = max_retries
        self.retry_seconds = retry_seconds
        self.subcollections = subcollections or SUBCOLLECTIONS
        self._collections: Collections | None = None
        # Collections are made at most once per run; the tokens are new each run.
        self._run = secrets.token_hex(8)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _request(
        self, method: str, path: str, retry: set[int] = RETRY_STATUSES, **kw: Any
    ) -> httpx.Response:
        """One request with backoff on rate limits and transient failures."""
        for attempt in range(self.max_retries):
            resp = await self.http.request(method, f"{self.base}{path}", **kw)
            if resp.status_code not in retry or attempt == self.max_retries - 1:
                return resp
            wait = float(resp.headers.get("Retry-After") or resp.headers.get("Backoff") or 0)
            await asyncio.sleep(max(wait, self.retry_seconds * 2.0**attempt))
        raise AssertionError("unreachable")

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._request("GET", path, params=params)
        if resp.status_code == 404:
            raise GoneError(f"GET {path}: 404")
        if resp.status_code != 200:
            raise ZoteroError(f"GET {path}: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def _get_all(self, path: str, **params: Any) -> list[Any]:
        """Every result, page by page: absence is only known after the last page."""
        found: list[Any] = []
        while True:
            page = await self._get(path, start=len(found), limit=PAGE, **params)
            found += page
            if len(page) < PAGE:
                return found

    async def _create(self, path: str, objects: list[dict[str, Any]], token: str) -> list[str]:
        """POST new objects under a write token; returns their keys in input order. A 502
        or a timeout may hide a create that happened; sent again with the same token, it
        answers 412, raised as `AlreadyWrittenError`."""
        resp = await self._request(
            "POST",
            path,
            retry=RETRY_STATUSES - {412},
            json=objects,
            headers={"Zotero-Write-Token": token},
        )
        if resp.status_code == 412:
            raise AlreadyWrittenError(f"POST {path}: write token already used")
        if resp.status_code != 200:
            raise ZoteroError(f"POST {path}: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        if body.get("failed"):
            raise ZoteroError(f"POST {path} failed: {body['failed']}")
        return [body["successful"][str(i)]["key"] for i in range(len(objects))]

    async def item(self, key: str) -> dict[str, Any]:
        return await self._get(f"/items/{key}")

    async def children(self, key: str) -> list[dict[str, Any]]:
        return await self._get_all(f"/items/{key}/children")

    async def patch_item(self, key: str, mutate: Any) -> dict[str, Any]:
        """Apply `mutate(data)` to a fresh copy and PATCH it; retries on version conflicts."""
        for attempt in range(self.max_retries):
            current = await self.item(key)
            data = dict(current["data"])
            mutate(data)
            if data == current["data"]:
                return data
            resp = await self.http.patch(
                f"{self.base}/items/{key}",
                json=data,
                headers={"If-Unmodified-Since-Version": str(current["version"])},
            )
            if resp.status_code == 204:
                return data
            if resp.status_code not in RETRY_STATUSES:
                raise ZoteroError(f"PATCH {key}: {resp.status_code} {resp.text[:200]}")
            await asyncio.sleep(self.retry_seconds * 2.0**attempt)
        raise ZoteroError(f"PATCH {key}: gave up after {self.max_retries} conflicts")

    # -- collections ------------------------------------------------------------------------

    async def collections(self) -> Collections:
        """Resolve (creating if needed) `Litwatch` and its per-grade subcollections."""
        if self._collections:
            return self._collections
        root = await self._collection(COLLECTION, None)
        by_grade = {g: await self._collection(n, root) for g, n in self.subcollections.items()}
        self._collections = Collections(root=root, by_grade=by_grade)
        return self._collections

    async def _collection(self, name: str, parent: str | None) -> str:
        """The collection's key, made when there is none."""

        async def find() -> str | None:
            for c in await self._get_all("/collections"):
                if (c["data"]["name"], c["data"].get("parentCollection") or None) == (name, parent):
                    return c["key"]
            return None

        if key := await find():
            return key
        token = write_token(self._run, "collection", name, parent or "")
        data = {"name": name, **({"parentCollection": parent} if parent else {})}
        try:
            (key,) = await self._create("/collections", [data], token)
        except AlreadyWrittenError:
            if not (key := await find()):
                raise
        log.info("created Zotero collection %s (%s)", name, key)
        return key

    # -- items --------------------------------------------------------------------------------

    async def find_item(self, paper: Paper) -> str | None:
        """An existing top-level item for this paper, by arXiv id or exact title."""
        rows = await self._get_all(
            "/items/top", q=paper.title, qmode="titleCreatorYear", itemType="-note"
        )
        for row in rows:
            data = row["data"]
            haystack = " ".join(str(data.get(k, "")) for k in ("url", "extra", "archiveID", "DOI"))
            if paper.id in haystack or _norm(data.get("title", "")) == _norm(paper.title):
                return row["key"]
        return None

    async def ensure_item(
        self, paper: Paper, key: str | None, grade: Grade | None, tags: list[str], attempt: str
    ) -> str:
        """Find or create the item, then converge its collections and managed tags. `grade`
        is the paper's best Watch grade, which picks the subcollection. `attempt` names this
        try (the event being handled), so a create sent twice makes one item."""
        cols = await self.collections()
        wanted = {cols.root} | ({cols.by_grade[grade]} if grade in cols.by_grade else set())
        if key is None:
            key = await self.find_item(paper)
        if key is None:
            token = write_token(paper.id, "item", attempt)
            try:
                (key,) = await self._create("/items", [item_data(paper, sorted(wanted))], token)
                log.info("created Zotero item %s for %s", key, paper.id)
            except AlreadyWrittenError as exc:
                if not (key := await self.find_item(paper)):
                    raise ZoteroError(
                        f"{paper.id}: the item was made but is not found yet"
                    ) from exc

        def mutate(data: dict[str, Any]) -> None:
            kept = [c for c in data.get("collections", []) if c not in cols.managed]
            data["collections"] = [*kept, *sorted(wanted)]
            data["tags"] = _retag(data.get("tags", []), MANAGED_PREFIXES, tags)

        await self.patch_item(key, mutate)
        return key

    async def drop_item(self, key: str) -> None:
        """Out of the managed collections, its grade tags replaced by `litwatch:dropped`. The
        item itself stays: deleting is the owner's call, and it may carry a PDF."""
        cols = await self.collections()

        def mutate(data: dict[str, Any]) -> None:
            data["collections"] = [c for c in data.get("collections", []) if c not in cols.managed]
            data["tags"] = _retag(data.get("tags", []), MANAGED_PREFIXES, [DROPPED_TAG])

        await self.patch_item(key, mutate)

    async def upsert_note(
        self, parent_key: str, html: str, find: str, tag: str, attempt: str
    ) -> str:
        """One note per marker per item, replaced in place. `html` must contain `find`."""

        async def update() -> str | None:
            for child in await self.children(parent_key):
                data = child["data"]
                if data.get("itemType") == "note" and find in data.get("note", ""):
                    if data["note"] != html:
                        await self.patch_item(child["key"], lambda d: d.__setitem__("note", html))
                    return child["key"]
            return None

        if key := await update():
            return key
        note = {"itemType": "note", "parentItem": parent_key, "note": html, "tags": [{"tag": tag}]}
        try:
            (key,) = await self._create("/items", [note], write_token(parent_key, tag, attempt))
        except AlreadyWrittenError:
            if not (key := await update()):
                raise
        return key

    async def set_tags(self, key: str, tags: list[str], prefixes: tuple[str, ...]) -> None:
        """Replace the item's tags under the given litwatch prefixes; everything else stays."""

        def mutate(data: dict[str, Any]) -> None:
            data["tags"] = _retag(data.get("tags", []), prefixes, tags)

        await self.patch_item(key, mutate)

    async def add_to_collection(self, key: str, collection_key: str) -> None:
        def mutate(data: dict[str, Any]) -> None:
            if collection_key not in data.get("collections", []):
                data["collections"] = [*data.get("collections", []), collection_key]

        await self.patch_item(key, mutate)

    async def pdf_attachments(self, parent_key: str) -> list[dict[str, Any]]:
        """The item's PDF attachments, as the API returns them."""
        return [
            c
            for c in await self.children(parent_key)
            if c["data"].get("contentType") == "application/pdf"
        ]

    async def create_attachment(
        self, parent_key: str, paper: Paper, pdf: bytes, attempt: str
    ) -> str:
        """Register an imported PDF attachment whose bytes will live on WebDAV. It carries
        `litwatch:pdf`, which tells litwatch's attachments from the owner's."""
        token = write_token(parent_key, "attachment", attempt)
        (key,) = await self._create("/items", [attachment_data(parent_key, paper, pdf)], token)
        return key


def client(options: dict[str, Any], subcollections: dict[Grade, str] | None = None) -> ZoteroClient:
    """A client from a `[integrations.zotero]` section and the environment."""
    return ZoteroClient(
        str(required(options, "user_id", env="LITWATCH_ZOTERO_USER_ID")),
        required_secret("ZOTERO_API_KEY"),
        subcollections=subcollections,
    )


class Zotero:
    name = "zotero"
    description = (
        "Keeps a Zotero item for every library paper, with its grades as tags and its takes, "
        "published version and deep card as notes. Never deletes an item."
    )

    def __init__(
        self,
        zotero: ZoteroClient,
        *,
        name: str = "zotero",
        inbox_collection: str | None = None,
        inbox_topics: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.zotero = zotero
        self.inbox_collection = inbox_collection
        self.inbox_topics = set(inbox_topics)

    def wants(self, event: Event) -> bool:
        return event.type in EVENTS and event.paper_id is not None

    async def setup(self, conn: psycopg.AsyncConnection) -> None:
        if taken := await state.seed_zotero(conn):
            log.info("zotero: took over %d items made by 0.x", taken)

    async def aclose(self) -> None:
        await self.zotero.aclose()

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        paper_id = event.paper_id
        key = await state.zotero_item(conn, paper_id)
        seen = await reading.paper(conn, paper_id)
        if seen is None:
            return
        if not seen["saved"]:
            if key:
                try:
                    await self.zotero.drop_item(key)
                except GoneError:
                    await state.set_zotero_item(conn, paper_id, None)
            return
        paper = await papers_store.get(conn, paper_id)
        watch = [t for t in seen["topics"] if t["kind"] == "watch" and _shown(t["grade"])]
        best = min((Grade(t["grade"]) for t in watch), key=lambda g: g.rank, default=None)
        tags = [f"{WATCH_TAG_PREFIX}{t['slug']}:{t['grade']}" for t in watch]
        attempt = str(event.id)
        try:
            key = await self.zotero.ensure_item(paper, key, best, tags, attempt)
        except GoneError:
            # The owner removed the item; a library paper gets one again, as in 0.x.
            key = await self.zotero.ensure_item(paper, None, best, tags, attempt)
        await state.set_zotero_item(conn, paper_id, key)
        record = (await publications_store.found(conn, [paper_id])).get(paper_id)
        note = screen_note(paper, watch, record)
        await self.zotero.upsert_note(key, note, NOTE_FIND, NOTE_TAG, attempt)
        card = await state.card(conn, paper_id)
        if card and card["card"]:
            await self.zotero.upsert_note(
                key,
                enrichment_note(paper, card["card"], card["model"], card["stats"]),
                ENRICH_MARKER,
                ENRICH_TAG,
                attempt,
            )
            await self.zotero.set_tags(key, enrichment_tags(card["card"]), CARD_PREFIXES)
            if self.inbox_collection and any(
                t["grade"] == Grade.MUST_READ and t["slug"] in self.inbox_topics for t in watch
            ):
                await self.zotero.add_to_collection(key, self.inbox_collection)


def load(name: str, options: dict[str, Any], config: Config) -> Zotero:
    subcollections = {Grade(g): n for g, n in options.get("subcollections", {}).items()}
    return Zotero(
        client(options, subcollections or None),
        name=name,
        inbox_collection=options.get("inbox_collection"),
        inbox_topics=tuple(options.get("inbox_topics", ())),
    )


# -- pure helpers --------------------------------------------------------------------------------


def _shown(grade: str | None) -> bool:
    return grade is not None and Grade(grade).at_least(Grade.RELEVANT)


def _retag(
    tags: list[dict[str, Any]], prefixes: tuple[str, ...], replacement: list[str]
) -> list[dict[str, Any]]:
    kept = [t for t in tags if not str(t.get("tag")).startswith(prefixes)]
    return [*kept, *({"tag": t} for t in replacement)]


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _creator(name: str) -> dict[str, str]:
    first, _, last = name.strip().rpartition(" ")
    if not first:
        return {"creatorType": "author", "name": last}
    return {"creatorType": "author", "firstName": first, "lastName": last}


def item_data(paper: Paper, collections: list[str]) -> dict[str, Any]:
    """A preprint item shaped like Zotero's own arXiv translator output."""
    date = (paper.published or "")[:10]
    return {
        "itemType": "preprint",
        "title": paper.title,
        "abstractNote": paper.abstract,
        "creators": [_creator(a) for a in paper.authors],
        "repository": "arXiv",
        "archiveID": f"arXiv:{paper.id}",
        "DOI": f"10.48550/arXiv.{paper.id}",
        "url": paper.url or f"https://arxiv.org/abs/{paper.id}",
        "date": date,
        "extra": f"arXiv:{paper.id} [{paper.primary_category or 'cs'}]",
        "libraryCatalog": "arXiv.org",
        "accessDate": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collections": collections,
        "tags": [],
    }


def attachment_filename(paper: Paper) -> str:
    """Zotero's default pattern: `Last et al. - Year - Title.pdf`, kept filesystem-safe."""
    if paper.authors:
        creator = _creator(paper.authors[0])
        last = creator.get("lastName") or creator["name"]
        who = f"{last} et al." if len(paper.authors) > 1 else last
    else:
        who = "Unknown"
    year = (paper.published or "")[:4] or "n.d."
    title = re.sub(r'[\\/:*?"<>|]+', " ", paper.title).strip()
    return f"{who} - {year} - {title}"[:180] + ".pdf"


def attachment_data(parent_key: str, paper: Paper, pdf: bytes) -> dict[str, Any]:
    return {
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_url",
        "title": "Preprint PDF",
        "url": f"https://arxiv.org/pdf/{paper.id}",
        "accessDate": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contentType": "application/pdf",
        "filename": attachment_filename(paper),
        "md5": hashlib.md5(pdf).hexdigest(),  # noqa: S324 - Zotero's file hash, not security
        "mtime": pdf_mtime(paper),
        "tags": [{"tag": PDF_TAG}],
    }


def pdf_mtime(paper: Paper) -> int:
    """Millisecond mtime for the attachment: the paper's publication time, so retries agree."""
    stamp = datetime.fromisoformat(paper.published) if paper.published else datetime.now(UTC)
    return int(stamp.timestamp() * 1000)


def webdav_payload(paper: Paper, pdf: bytes) -> tuple[bytes, bytes]:
    """(KEY.zip, KEY.prop) bodies in the layout Zotero's WebDAV sync reads."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(attachment_filename(paper), pdf)
    prop = (
        f'<properties version="1"><mtime>{pdf_mtime(paper)}</mtime>'
        f"<hash>{hashlib.md5(pdf).hexdigest()}</hash></properties>"  # noqa: S324
    ).encode()
    return buf.getvalue(), prop


GRADE_WORDS = {"must_read": "must read", "relevant": "relevant"}


def screen_note(paper: Paper, watch: list[dict[str, Any]], record: Publication | None) -> str:
    """The `litwatch:screen` note: litwatch's take per Watch topic, then the published
    version when one is known."""
    parts = [NOTE_MARKER, "<p><b>litwatch</b></p>"]
    for topic in watch:
        parts.append(f"<p><b>{_esc(topic['name'])}</b> · {GRADE_WORDS[topic['grade']]}</p>")
        if topic.get("take"):
            parts.append(f"<p>{_esc(topic['take'])}</p>")
    if record is not None:
        where = " ".join(str(x) for x in (record.venue or record.container, record.year) if x)
        doi = f", doi:{_esc(record.doi)}" if record.doi else ""
        parts.append(f"<p><b>Published.</b> {_esc(where or 'yes')}{doi}</p>")
    parts.append(f"<p><i>arXiv {paper.id}</i></p>")
    return "".join(parts)


def _slug(value: str) -> str:
    return value.replace(" ", "-")


def enrichment_tags(result: dict[str, Any]) -> list[str]:
    tags = ["litwatch:processed", f"litwatch:kind:{_slug(result['paper_kind'])}"]
    if result.get("code_available"):
        tags.append("litwatch:code")
    if faith := result.get("faithfulness"):
        tags += [
            f"litwatch:monitor:{_slug(faith['monitor_type'])}",
            *(f"litwatch:signal:{_slug(x)}" for x in faith.get("internals_signal", [])),
            *(f"litwatch:mismatch:{_slug(x)}" for x in faith.get("mismatch", [])),
            f"litwatch:ground-truth:{_slug(faith['ground_truth'])}",
        ]
    if mon := result.get("monitoring"):
        tags += [
            f"litwatch:monitor:{_slug(mon['monitor_type'])}",
            *(f"litwatch:signal:{_slug(x)}" for x in mon.get("internals_signal", [])),
            *(f"litwatch:target:{_slug(x)}" for x in mon.get("target", [])),
            f"litwatch:pressure:{_slug(mon['pressure'])}",
        ]
    return sorted(set(tags), key=tags.index)


def enrichment_note(
    paper: Paper,
    result: dict[str, Any],
    model: str | None = None,
    stats: dict[str, Any] | None = None,
) -> str:
    """The `litwatch:enrichment` note: the reading card as HTML, core first, extensions after."""
    setup = result.get("evaluation_setup", {})
    parts = [
        ENRICH_MARKER,
        "<p><b>litwatch reading card</b></p>",
        f"<p><b>Key claim.</b> {_esc(result['key_claim'])}</p>",
        *(f"<p>{_esc(p)}</p>" for p in _paragraphs(result["summary"])),
        "<table>",
        _row("Kind", result["paper_kind"]),
        _row("Models", ", ".join(setup.get("models", []))),
        _row("Tasks", ", ".join(setup.get("tasks", []))),
        _row("Generalization", setup.get("generalization", "")),
        _row(
            "Code",
            (
                f"available: {result['code_url']}"
                if result.get("code_available")
                else "not available"
            )
            + ("; weights released" if result.get("weights_available") else ""),
        ),
        "</table>",
    ]
    if faith := result.get("faithfulness"):
        parts += [
            "<p><b>Faithfulness.</b></p><table>",
            _row("Mismatch detected", ", ".join(faith.get("mismatch", [])) or "none"),
            _row("Ground truth", faith["ground_truth"]),
            _row("Monitor type", faith["monitor_type"]),
            _row("Internals signal", ", ".join(faith.get("internals_signal", [])) or "none"),
            "</table>",
            f"<p><b>Detection method.</b> {_esc(faith['detection_method'])}</p>",
            f"<p><b>Definition.</b> {_esc(faith['definition'])}</p>",
            f"<p><b>Generality.</b> {_esc(faith['generality'])}</p>",
        ]
    if mon := result.get("monitoring"):
        parts += [
            "<p><b>Monitoring.</b></p><table>",
            _row("Target", ", ".join(mon.get("target", [])) or "none"),
            _row("Monitor type", mon["monitor_type"]),
            _row("Internals signal", ", ".join(mon.get("internals_signal", [])) or "none"),
            _row("Pressure", mon["pressure"]),
            "</table>",
            f"<p><b>Monitor method.</b> {_esc(mon['monitor_method'])}</p>",
            f"<p><b>Robustness.</b> {_esc(mon['robustness'])}</p>",
            f"<p><b>Generality.</b> {_esc(mon['generality'])}</p>",
        ]
    parts += [
        "<p><b>Closest prior work.</b></p><ul>",
        *(
            f"<li>{_esc(w['title'])}"
            + (f" (arXiv:{w['arxiv_id']})" if w.get("arxiv_id") else "")
            + f": {_esc(w['relation'])}</li>"
            for w in result.get("prior_work", [])
        ),
        "</ul><p><b>Limitations.</b></p><ul>",
        *(f"<li>{_esc(x)}</li>" for x in result.get("limitations", [])),
        "</ul>",
    ]
    if model or stats:
        st = stats or {}
        parts.append(
            f"<p><i>{_esc(model or '')}: {st.get('cells', '?')} cells, "
            f"{st.get('turns', '?')} turns, {st.get('seconds', '?')}s; arXiv {paper.id}</i></p>"
        )
    return "".join(parts)


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]


def _row(label: str, value: str) -> str:
    return f"<tr><td><b>{label}</b></td><td>{_esc(value)}</td></tr>"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
