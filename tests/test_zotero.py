"""Zotero client and router against an in-memory fake of the Web API and the WebDAV share."""

import hashlib
import io
import json
import zipfile
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from litwatch import route as route_mod
from litwatch.models import Paper
from litwatch.route import Router
from litwatch.webdav import WebDAVWriter
from litwatch.zotero import NOTE_MARKER, ZoteroClient, attachment_filename, webdav_payload
from tests.conftest import screen_result

PAPER = Paper(
    id="2605.25052",
    title="BonaFide: Grounding Chain-of-Thought Faithfulness",
    abstract="We define faithfulness.",
    authors=["Ana Marasović", "Jane Q. Doe"],
    categories=["cs.CL"],
    primary_category="cs.CL",
    published="2026-05-27T12:00:00+00:00",
    url="https://arxiv.org/abs/2605.25052",
)
PDF = b"%PDF-1.7 fake body"


def result(tier: int):
    return screen_result(PAPER.id, cot_faithfulness=tier, mech_interp=3)


class FakeZotero:
    """Enough of api.zotero.org for the client: collections, items, children, PATCH versions."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.version = 100
        self.calls: list[str] = []

    def _key(self) -> str:
        return f"K{len(self.collections) + len(self.items) + 1:07d}"

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
                if not i["data"].get("parentItem") and q in i["data"]["title"].lower()
            ]
            return httpx.Response(200, json=rows)
        if path.endswith("/children"):
            parent = path.split("/")[2]
            rows = [i for i in self.items.values() if i["data"].get("parentItem") == parent]
            return httpx.Response(200, json=rows)
        key = path.split("/")[2]
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


class FakeDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def handle(self, req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        if req.method == "PUT":
            self.files[name] = req.content
            return httpx.Response(201)
        return httpx.Response(200 if name in self.files else 404)


@pytest.fixture
def fake() -> FakeZotero:
    return FakeZotero()


@pytest.fixture
def client(fake: FakeZotero) -> ZoteroClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    return ZoteroClient("1", "key", http=http)


def tags(fake: FakeZotero, key: str) -> set[str]:
    return {t["tag"] for t in fake.items[key]["data"]["tags"]}


async def test_ensure_item_creates_tree_and_converges_on_tier_change(fake, client):
    key = await client.ensure_item(PAPER, 1, ["litwatch:sel:cot-faithfulness:1"])
    names = {c["data"]["name"] for c in fake.collections.values()}
    assert names == {"Litwatch", "Tier 1", "Tier 2"}
    cols = await client.collections()
    data = fake.items[key]["data"]
    assert data["itemType"] == "preprint" and data["archiveID"] == "arXiv:2605.25052"
    assert data["creators"][0] == {
        "creatorType": "author",
        "firstName": "Ana",
        "lastName": "Marasović",
    }
    assert set(data["collections"]) == {cols.root, cols.by_tier[1]}
    assert tags(fake, key) == {"litwatch:tier:1", "litwatch:sel:cot-faithfulness:1"}

    # The user adds their own collection and tag; a later demotion keeps both.
    data["collections"].append("USERCOLL")
    data["tags"].append({"tag": "mine"})
    again = await client.ensure_item(PAPER, 2, ["litwatch:sel:cot-faithfulness:2"])
    assert again == key, "no duplicate item"
    data = fake.items[key]["data"]
    assert set(data["collections"]) == {"USERCOLL", cols.root, cols.by_tier[2]}
    assert tags(fake, key) == {"mine", "litwatch:tier:2", "litwatch:sel:cot-faithfulness:2"}
    assert sum(1 for i in fake.items.values() if not i["data"].get("parentItem")) == 1

    # Dropping below tier 2 releases the item: managed collections and litwatch tags go.
    await client.release_item(key)
    data = fake.items[key]["data"]
    assert data["collections"] == ["USERCOLL"] and tags(fake, key) == {"mine"}


async def test_legacy_subcollections_are_managed(fake, client):
    """Items filed under the phase 3 names move to the tier collections on re-route."""
    fake._create(fake.collections, [{"name": "Litwatch"}])
    (root,) = fake.collections
    fake._create(fake.collections, [{"name": "Must Read", "parentCollection": root}])
    legacy = [k for k in fake.collections if k != root][0]
    key = await client.ensure_item(PAPER, 2)
    fake.items[key]["data"]["collections"].append(legacy)
    await client.ensure_item(PAPER, 2)
    cols = await client.collections()
    assert legacy in cols.managed and legacy not in fake.items[key]["data"]["collections"]


async def test_find_item_adopts_hand_added_item(fake, client):
    fake._create(
        fake.items,
        [
            {
                "itemType": "preprint",
                "title": "bonafide: grounding chain-of-thought faithfulness",
                "extra": "arXiv:2605.25052 [cs]",
                "collections": [],
                "tags": [],
            }
        ],
    )
    (existing,) = fake.items
    assert await client.ensure_item(PAPER, 2) == existing


async def test_upsert_note_is_single_and_replaced_in_place(fake, client):
    key = await client.ensure_item(PAPER, 2)
    note = await client.upsert_note(key, f"{NOTE_MARKER}<p>one</p>")
    assert await client.upsert_note(key, f"{NOTE_MARKER}<p>two</p>") == note
    assert fake.items[note]["data"]["note"].endswith("<p>two</p>")
    assert [i for i in fake.items.values() if i["data"].get("itemType") == "note"] == [
        fake.items[note]
    ]


async def test_patch_retries_after_version_conflict(fake, client, monkeypatch):
    key = await client.ensure_item(PAPER, 2)
    real = fake.handle
    bumped = False

    def racy(req):
        nonlocal bumped
        if req.method == "GET" and req.url.path.endswith(key) and not bumped:
            bumped = True
            resp = real(req)
            fake.items[key]["version"] += 1  # someone else wrote in between
            return resp
        return real(req)

    client.http._transport = httpx.MockTransport(racy)
    monkeypatch.setattr("litwatch.zotero.asyncio.sleep", _no_sleep)
    await client.patch_item(key, lambda d: d.__setitem__("title", "changed"))
    assert fake.items[key]["data"]["title"] == "changed"


async def _no_sleep(_):
    return None


async def test_webdav_payload_matches_zotero_layout():
    archive, prop = webdav_payload(PAPER, PDF)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.namelist() == [attachment_filename(PAPER)]
        assert zf.read(zf.namelist()[0]) == PDF
    assert attachment_filename(PAPER) == (
        "Marasović et al. - 2026 - BonaFide  Grounding Chain-of-Thought Faithfulness.pdf"
    )
    assert prop.decode() == (
        '<properties version="1"><mtime>1779883200000</mtime>'
        f"<hash>{hashlib.md5(PDF).hexdigest()}</hash></properties>"  # noqa: S324
    )


@pytest.fixture
def router(fake: FakeZotero, monkeypatch) -> tuple[Router, FakeDAV, list[str]]:
    dav = FakeDAV()
    downloads: list[str] = []

    def arxiv(req: httpx.Request) -> httpx.Response:
        downloads.append(req.url.path)
        return httpx.Response(200, content=PDF)

    monkeypatch.setattr(route_mod, "ARXIV_PACING_SECONDS", 0)
    zotero = ZoteroClient(
        "1", "key", http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    )
    webdav = WebDAVWriter(
        "https://dav/zotero",
        "u",
        "p",
        http=httpx.AsyncClient(transport=httpx.MockTransport(dav.handle)),
    )
    return (
        Router(zotero, webdav, http=httpx.AsyncClient(transport=httpx.MockTransport(arxiv))),
        dav,
        downloads,
    )


async def test_router_attaches_pdf_once_for_tier_1(fake, router):
    r, dav, downloads = router
    routed = await r.route(PAPER, result(1))
    assert routed.tier == 1 and routed.attachment_key
    att = fake.items[routed.attachment_key]["data"]
    assert att["parentItem"] == routed.item_key and att["linkMode"] == "imported_url"
    assert att["md5"] == hashlib.md5(PDF).hexdigest()  # noqa: S324
    assert set(dav.files) == {f"{routed.attachment_key}.zip", f"{routed.attachment_key}.prop"}
    note = next(i for i in fake.items.values() if i["data"].get("itemType") == "note")
    assert "Best tier</b></td><td>1" in note["data"]["note"]
    assert (
        "<b>cot-faithfulness</b></td><td>tier 1, high: Defines faithfulness &amp; a"
        in note["data"]["note"]
    )
    assert tags(fake, routed.item_key) == {
        "litwatch:tier:1",
        "litwatch:sel:cot-faithfulness:1",
        "litwatch:sel:mech-interp:3",
    }

    again = await r.route(PAPER, result(1))
    assert again == routed
    assert downloads == ["/pdf/2605.25052"], "downloaded exactly once"


async def test_router_skips_pdf_for_tier_2(fake, router):
    r, dav, downloads = router
    routed = await r.route(PAPER, result(2))
    assert routed.attachment_key is None and not dav.files and not downloads


async def test_router_refuses_non_members(router):
    r, _, _ = router
    with pytest.raises(ValueError, match="not routable"):
        await r.route(PAPER, result(3))


async def test_pending_tier1_pdf_raises_and_retry_reuses_zotero_item(fake, router, monkeypatch):
    r, dav, downloads = router
    real_download = r._download

    async def pending(_paper):
        return None

    monkeypatch.setattr(r, "_download", pending)
    with pytest.raises(route_mod.PDFUnavailableError, match="not available yet"):
        await r.route(PAPER, result(1))
    items = [i for i in fake.items.values() if i["data"].get("itemType") == "preprint"]
    assert len(items) == 1
    item_key = items[0]["key"]
    assert not dav.files and not downloads

    monkeypatch.setattr(r, "_download", real_download)
    routed = await r.route(PAPER, result(1))
    assert routed.item_key == item_key and routed.attachment_key
    assert sum(i["data"].get("itemType") == "preprint" for i in fake.items.values()) == 1
    assert len(downloads) == 1
