"""Zotero Web API v3 client: collections, preprint items, notes, and WebDAV-backed attachments.

Only the litwatch namespace is ever managed: the `Litwatch` collection tree, `litwatch:*` tags,
and the `litwatch:screen` child note. Everything else on an item is preserved verbatim.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from litwatch.models import ROUTED_TIERS, Paper, ScreenResult

log = logging.getLogger(__name__)

API = "https://api.zotero.org"
TAG_PREFIX = "litwatch:"
NOTE_TAG = "litwatch:screen"
NOTE_MARKER = "<!-- litwatch:screen schema=1 -->"
ENRICH_TAG = "litwatch:enrichment"
ENRICH_MARKER = "<!-- litwatch:enrichment schema=1 -->"
COLLECTION = "Litwatch"
SUBCOLLECTIONS = {1: "Tier 1", 2: "Tier 2"}
LEGACY_SUBCOLLECTIONS = ("Must Read", "Aware")  # phase 3 names, emptied as items re-route
TIER_TAG_PREFIX = "litwatch:tier:"
SELECTION_TAG_PREFIX = "litwatch:sel:"
RETRY_STATUSES = {409, 412, 429, 500, 502, 503, 504}


class ZoteroError(Exception):
    pass


@dataclass(frozen=True)
class Collections:
    """Keys of the managed collection tree, resolved once per client."""

    root: str
    by_tier: dict[int, str]
    legacy: frozenset[str] = frozenset()

    @property
    def managed(self) -> set[str]:
        return {self.root, *self.by_tier.values(), *self.legacy}


class ZoteroClient:
    """Thin async wrapper. Writes are conflict-aware: read, mutate, PATCH with the version."""

    def __init__(
        self,
        user_id: str,
        api_key: str,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 4,
    ) -> None:
        self.base = f"{API}/users/{user_id}"
        self.http = http or httpx.AsyncClient(timeout=60.0)
        self.http.headers.update({"Zotero-API-Key": api_key, "Zotero-API-Version": "3"})
        self.max_retries = max_retries
        self._collections: Collections | None = None

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """One request with backoff on rate limits and transient failures."""
        for attempt in range(self.max_retries):
            resp = await self.http.request(method, f"{self.base}{path}", **kw)
            if resp.status_code not in RETRY_STATUSES or attempt == self.max_retries - 1:
                return resp
            wait = float(resp.headers.get("Retry-After") or resp.headers.get("Backoff") or 0)
            await asyncio.sleep(max(wait, 2.0**attempt))
        raise AssertionError("unreachable")

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._request("GET", path, params=params)
        if resp.status_code != 200:
            raise ZoteroError(f"GET {path}: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def _create(self, path: str, objects: list[dict[str, Any]]) -> list[str]:
        """POST new objects; returns their keys in input order."""
        resp = await self._request("POST", path, json=objects)
        if resp.status_code != 200:
            raise ZoteroError(f"POST {path}: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        if body.get("failed"):
            raise ZoteroError(f"POST {path} failed: {body['failed']}")
        return [body["successful"][str(i)]["key"] for i in range(len(objects))]

    async def item(self, key: str) -> dict[str, Any]:
        return await self._get(f"/items/{key}")

    async def children(self, key: str) -> list[dict[str, Any]]:
        return await self._get(f"/items/{key}/children", limit=100)

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
            await asyncio.sleep(2.0**attempt)
        raise ZoteroError(f"PATCH {key}: gave up after {self.max_retries} conflicts")

    # -- collections ------------------------------------------------------------------------

    async def collections(self) -> Collections:
        """Resolve (creating if needed) `Litwatch` and its per-tier subcollections."""
        if self._collections:
            return self._collections
        existing = await self._get("/collections", limit=100)
        by_name = {
            (c["data"]["name"], c["data"].get("parentCollection") or None): c["key"]
            for c in existing
        }
        root = by_name.get((COLLECTION, None))
        if not root:
            (root,) = await self._create("/collections", [{"name": COLLECTION}])
            log.info("created Zotero collection %s (%s)", COLLECTION, root)
        by_tier: dict[int, str] = {}
        for tier, name in SUBCOLLECTIONS.items():
            key = by_name.get((name, root))
            if not key:
                (key,) = await self._create(
                    "/collections", [{"name": name, "parentCollection": root}]
                )
                log.info("created Zotero subcollection %s (%s)", name, key)
            by_tier[tier] = key
        legacy = frozenset(k for n in LEGACY_SUBCOLLECTIONS if (k := by_name.get((n, root))))
        self._collections = Collections(root=root, by_tier=by_tier, legacy=legacy)
        return self._collections

    # -- items --------------------------------------------------------------------------------

    async def find_item(self, paper: Paper) -> str | None:
        """An existing top-level item for this paper, by arXiv id or exact title."""
        rows = await self._get(
            "/items/top", q=paper.title, qmode="titleCreatorYear", limit=25, itemType="-note"
        )
        for row in rows:
            data = row["data"]
            haystack = " ".join(str(data.get(k, "")) for k in ("url", "extra", "archiveID", "DOI"))
            if paper.id in haystack or _norm(data.get("title", "")) == _norm(paper.title):
                return row["key"]
        return None

    async def create_item(self, paper: Paper, collections: list[str]) -> str:
        (key,) = await self._create("/items", [item_data(paper, collections)])
        return key

    async def ensure_item(self, paper: Paper, tier: int, selection_tags: list[str] = ()) -> str:
        """Find or create the item, then converge collections, tier tag, and selection tags."""
        if tier not in ROUTED_TIERS:
            raise ValueError(f"tier {tier} gets no Zotero item")
        cols = await self.collections()
        wanted = {cols.root, cols.by_tier[tier]}
        key = await self.find_item(paper)
        if key is None:
            key = await self.create_item(paper, sorted(wanted))
            log.info("created Zotero item %s for %s", key, paper.id)
        tags = [f"{TIER_TAG_PREFIX}{tier}", *selection_tags]

        def mutate(data: dict[str, Any]) -> None:
            kept = [c for c in data.get("collections", []) if c not in cols.managed]
            data["collections"] = [*kept, *sorted(wanted)]
            # Tier and selection tags are replaced; other litwatch:* tags belong to later stages.
            others = [
                t
                for t in data.get("tags", [])
                if not str(t.get("tag")).startswith((TIER_TAG_PREFIX, SELECTION_TAG_PREFIX))
            ]
            data["tags"] = [*others, *({"tag": t} for t in tags)]

        await self.patch_item(key, mutate)
        return key

    async def release_item(self, key: str) -> None:
        """Take the item out of the managed collections and drop its tier and selection tags.

        The item itself stays in the library: deleting is the user's call, and it may carry a PDF.
        """
        cols = await self.collections()

        def mutate(data: dict[str, Any]) -> None:
            data["collections"] = [c for c in data.get("collections", []) if c not in cols.managed]
            data["tags"] = [
                t
                for t in data.get("tags", [])
                if not str(t.get("tag")).startswith((TIER_TAG_PREFIX, SELECTION_TAG_PREFIX))
            ]

        await self.patch_item(key, mutate)

    async def upsert_note(
        self, parent_key: str, html: str, marker: str = NOTE_MARKER, tag: str = NOTE_TAG
    ) -> str:
        """One note per marker per item, replaced in place. `html` must contain the marker."""
        for child in await self.children(parent_key):
            data = child["data"]
            if data.get("itemType") == "note" and marker in data.get("note", ""):
                if data["note"] != html:
                    await self.patch_item(child["key"], lambda d: d.__setitem__("note", html))
                return child["key"]
        (key,) = await self._create(
            "/items",
            [{"itemType": "note", "parentItem": parent_key, "note": html, "tags": [{"tag": tag}]}],
        )
        return key

    async def set_tags(self, key: str, tags: list[str], prefixes: tuple[str, ...]) -> None:
        """Replace the item's tags under the given litwatch prefixes; everything else stays."""

        def mutate(data: dict[str, Any]) -> None:
            kept = [t for t in data.get("tags", []) if not str(t.get("tag")).startswith(prefixes)]
            data["tags"] = [*kept, *({"tag": t} for t in tags)]

        await self.patch_item(key, mutate)

    async def add_to_collection(self, key: str, collection_key: str) -> None:
        def mutate(data: dict[str, Any]) -> None:
            if collection_key not in data.get("collections", []):
                data["collections"] = [*data.get("collections", []), collection_key]

        await self.patch_item(key, mutate)

    async def pdf_attachment(self, parent_key: str) -> str | None:
        for child in await self.children(parent_key):
            if child["data"].get("contentType") == "application/pdf":
                return child["key"]
        return None

    async def create_attachment(self, parent_key: str, paper: Paper, pdf: bytes) -> str:
        """Register an imported PDF attachment whose bytes will live on WebDAV."""
        (key,) = await self._create("/items", [attachment_data(parent_key, paper, pdf)])
        return key


# -- pure helpers --------------------------------------------------------------------------------


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
        "tags": [],
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


def selection_tags(result: ScreenResult) -> list[str]:
    """`litwatch:sel:<slug>:<tier>` for every selection the paper is a member of."""
    return [
        f"{SELECTION_TAG_PREFIX}{slug}:{v.tier}"
        for slug, v in result.verdicts.items()
        if v.tier < 4
    ]


def screen_note(paper: Paper, result: ScreenResult) -> str:
    """The `litwatch:screen` note: one row per selection, then which models said so."""
    rows = [("Best tier", str(result.best_tier))]
    for slug, v in sorted(result.verdicts.items(), key=lambda kv: (kv[1].tier, kv[0])):
        rows.append((_esc(slug), f"tier {v.tier}, {v.confidence.value}: {_esc(v.reason)}"))
    rows += [
        ("Stages", ", ".join(f"{s.role.value} ({_esc(s.model)})" for s in result.stages)),
        ("Prompt", result.prompt_version),
        ("arXiv", paper.id),
    ]
    body = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
    return f"{NOTE_MARKER}<p><b>litwatch screening</b></p><table>{body}</table>"


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
