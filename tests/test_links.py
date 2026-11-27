"""Release links (docs/plans/a2-work-gate.md, P6): when a find runs (an analysis that says the
paper releases code or weights, once per arXiv version; again for a new version; the owner's
"find links" each time), that a find whose answer committed is never asked again, and the
re-checks of a placeholder: weekly, four times, then left. Through `serve` with its Workers
running, on a fake deep service, a fake archive and fake GitHub."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from litwatch import links
from litwatch.archive import ArchiveClient
from litwatch.core.links import RECHECK_EVERY, RECHECKS, ReleaseLink, find_key
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Done, WorkGate
from litwatch.intake import batch
from litwatch.integrations.deep import Deep, DeepClient
from litwatch.jobs import nightly
from litwatch.jobs.context import model_run
from litwatch.links import adapter
from litwatch.links.worker import FindWorker, RecheckWorker
from litwatch.llm.context import spending
from litwatch.store import links as store
from litwatch.store import papers as papers_store
from litwatch.store import work as queue
from tests import seed, settling
from tests.conftest import TREE, paper
from tests.fakes_http import FakeArchive, mock
from tests.scripted import CONFIG, Scripted, decision
from tests.sessions import Session, in_read
from tests.working import QUIET, TOKEN, items_of, served, serving, until

A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes.", version=1)
BODY = "\\section{Code} Our code is at github.com/o/probes. " * 20
FINDING = word.Rules(automatic_deep=True, find_links=True)  # the deep integration configured
SERVED = CONFIG.model_copy(update={"schedule": QUIET})
NOW = datetime.now(UTC).isoformat()  # a find's check: its re-check is a week on, not due
USAGE = dict.fromkeys(
    ("cells", "turns", "input", "output", "reasoning", "cache_read", "seconds"), 1
)


def found(url: str, state: str, kind: str = "code", checked: str = NOW) -> dict[str, Any]:
    return {"url": url, "kind": kind, "state": state, "license": None, "checked_at": checked}


class Finder:
    """deep's finds: a key it has not seen starts a find, which ends on its first poll as the
    next of `ends` (its `result`, or its `error` and `failure`), else none found; a key it has
    is that find. `sent` is what each submission sent."""

    def __init__(self, *ends: dict[str, Any]) -> None:
        self.ends = list(ends)
        self.keys: dict[str, str] = {}
        self.finds: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []

    @property
    def started(self) -> int:
        return len(self.keys)

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/links":
            self.sent.append(json.loads(req.content))
            key = req.headers["idempotency-key"]
            if key not in self.keys:
                self.keys[key] = find_id = f"find-{len(self.keys) + 1}"
                self.finds[find_id] = self.ends.pop(0) if self.ends else {"result": {"links": []}}
            return httpx.Response(202, json={"id": self.keys[key], "status": "queued"})
        find_id = req.url.path.removeprefix("/links/")
        if req.method == "GET" and find_id in self.finds:
            end = self.finds[find_id]
            job = {
                "id": find_id,
                "paper_id": self.sent[-1]["paper"]["id"],
                "status": "error" if "error" in end else "done",
                "created_at": NOW,
                "last_activity": NOW,
                "model": "finder-model",
                "stats": USAGE | {"list_cost": 0.01},
            }
            return httpx.Response(200, json=job | end)
        return httpx.Response(404)


def deep(finder: Finder) -> Deep:
    return Deep(DeepClient("http://deep.test", http=mock(finder.handle), poll_seconds=0))


def archive(*versions: int) -> tuple[ArchiveClient, FakeArchive]:
    fake = FakeArchive()
    fake.texts[A.id] = (versions[0] if versions else A.version, BODY)
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle)), fake


@pytest.fixture(autouse=True)
def finding(monkeypatch):
    """Every configuration's rules find links, as with the deep integration configured."""
    monkeypatch.setattr(word, "rules", lambda config: FINDING)


@pytest.fixture
async def held(conn: Session):
    await settling.save_tree(conn, TREE)
    await settling.store(conn, [A], source="oai")
    return conn


async def finds(conn: Session) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM work_items WHERE kind = %s ORDER BY id", (store.FIND,))
    ).fetchall()


async def all_ended(conn: Session, count: int) -> bool:
    found = await finds(conn)
    return len(found) == count and all(f["state"] == "ended" for f in found)


async def urls(conn: Session) -> list[str]:
    return [link.url for link in await in_read(conn, store.of_paper, A.id)]


async def test_an_analysis_that_says_code_finds_links_once_per_version_and_again_for_a_new_one(
    held: Session, db: DatabaseGate
):
    models = Scripted()  # its analysis says the paper releases code
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    with spending(deadline=datetime.now(UTC) + timedelta(hours=1)):
        async with model_run(db, "nightly", models=models.gate(CONFIG)) as night:
            await nightly.classify(db, CONFIG, night, ids=[A.id])
            await nightly.analyse(db, CONFIG, night, ids=[A.id])
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    client, fake = archive()
    async with served(
        db, SERVED, models=models.gate(SERVED), integrations=[deep(finder)], archive=client
    ):
        await until(lambda: all_ended(held, 1), "the find of v1 ends")
        assert await urls(held) == ["https://github.com/o/probes"]
        # Asked again for the same version (an analysis recorded again), the key holds.
        async with db.write("analysed again", taxonomy="shared", papers=[A.id]) as unit:
            await links.due(unit, [A.id])
        assert len(await finds(held)) == 1
        # arXiv's second version: found again, none this time.
        fake.texts[A.id] = (2, BODY)
        await batch.store(db, [A.model_copy(update={"version": 2})], source="oai", rules=FINDING)
        await until(lambda: all_ended(held, 2), "the find of v2 ends")
    assert finder.started == 2
    assert [f["key"] for f in await finds(held)] == [find_key(A.id, 1), find_key(A.id, 2)]
    assert [s["paper"]["body"] for s in finder.sent] == [BODY, BODY]  # from the paper's text
    assert finder.sent[0].get("comments") is None  # A has none: sent as absent
    assert await urls(held) == []  # the newest find's answer stands, none found included
    newest = await in_read(held, store.latest_find, A.id)
    assert newest is not None
    assert (newest["state"], newest["outcome"], newest["by"]) == ("ended", "done", "finder")
    assert newest["result"]["links"] == 0


async def test_no_find_for_a_paper_whose_analysis_says_no_release(held: Session, db):
    await seed.analyse(held, A, code=False)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    assert await finds(held) == []


async def test_the_owners_find_links_runs_a_fresh_find_each_time(held: Session, db):
    await seed.analyse(held, A, code=False)  # no automatic find
    async with db.write("asked twice", taxonomy="shared", papers=[A.id]) as unit:
        first = await links.request(unit, A.id)
        again = await links.request(unit, A.id)
    assert first is not None and again is not None and again.id == first.id  # one while open
    finder = Finder(
        {"result": {"links": [found("https://huggingface.co/o/m", "gated", "weights")]}},
        {"result": {"links": [found("https://github.com/o/probes", "placeholder")]}},
    )
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ) as app:
        await until(lambda: all_ended(held, 1), "the first find ends")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http:
            asked = await http.post(f"/api/papers/{A.id}/links")
            await until(lambda: all_ended(held, 2), "the second find ends")
            page = (await http.get(f"/api/papers/{A.id}")).json()
            missing = await http.post("/api/papers/2601.99999/links")
    assert asked.status_code == 202 and asked.json()["find_id"] != first.id
    assert missing.status_code == 404
    assert finder.started == 2
    assert [(link["url"], link["state"], link["found_by"]) for link in page["links"]] == [
        ("https://github.com/o/probes", "placeholder", "owner")
    ]
    assert (page["link_find"]["state"], page["link_find"]["by"]) == ("done", "owner")
    assert page["links_findable"]


async def test_a_find_tried_again_after_its_answer_committed_does_not_ask_again(
    held: Session, db: DatabaseGate
):
    """Its try ended after the answer was recorded and before the item's end committed (the
    process died): the next try of the same item ends with what was recorded."""
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    async with db.read("the find") as unit:
        item = await queue.find(unit, store.FIND, find_key(A.id, A.version))
    assert item is not None
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    worker = FindWorker(db, archive()[0], deep(finder))
    first, second = await worker(item), await worker(item)
    assert isinstance(first, Done) and first == second
    assert finder.started == 1 and len(finder.sent) == 1
    assert await urls(held) == ["https://github.com/o/probes"]


async def test_a_find_the_paper_failed_fails_its_item(held: Session, db: DatabaseGate):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    finder = Finder({"error": "the finder stopped without answering", "failure": "paper"})
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ):
        await until(lambda: all_ended(held, 1), "the find ends")
    (item,) = await finds(held)
    assert (item["outcome"], item["error"]) == (
        "failed",
        "FindError: the finder stopped without answering",
    )
    assert await urls(held) == []


class GitHub:
    """GitHub's API for one repository: its contents `files` (none: a repository with no
    commit)."""

    def __init__(self, *files: str) -> None:
        self.files = list(files)
        self.asked = 0

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.asked += 1
        if req.url.path == "/repos/o/probes":
            return httpx.Response(200, json={"license": {"spdx_id": "MIT", "name": "MIT License"}})
        if req.url.path == "/repos/o/probes/contents" and self.files:
            return httpx.Response(200, json=[{"path": f, "type": "file"} for f in self.files])
        return httpx.Response(404)


async def placeholder(held: Session, db: DatabaseGate) -> int:
    """A find that answered a placeholder on GitHub, and a public page elsewhere: the
    placeholder's link id."""
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
        item = await queue.find(unit, store.FIND, find_key(A.id, A.version))
    assert item is not None
    answer = [found("https://github.com/o/probes", "placeholder"), found("https://o.io", "public")]
    await FindWorker(db, archive()[0], deep(Finder({"result": {"links": answer}})))(item)
    (link,) = [
        link for link in await in_read(held, store.of_paper, A.id) if link.state == "placeholder"
    ]
    return link.id


async def link_of(conn: Session, link_id: int) -> ReleaseLink:
    link = await in_read(conn, store.get, link_id)
    assert link is not None
    return link


async def rechecks(conn: Session, link_id: int) -> list[dict[str, Any]]:
    return await items_of(conn, store.RECHECK, str(link_id))


async def due_now(conn: Session) -> datetime:
    """Bring the pending re-check forward to now, as a week passing would; when it was due."""
    (pending,) = [
        r
        for r in await (
            await conn.execute("SELECT * FROM work_items WHERE kind = %s", (store.RECHECK,))
        ).fetchall()
        if r["state"] == "pending"
    ]
    await conn.execute("UPDATE work_items SET not_before = now() WHERE id = %s", (pending["id"],))
    return pending["not_before"]


async def test_a_placeholder_is_checked_again_weekly_four_times_then_left(held: Session, db):
    link_id = await placeholder(held, db)
    (first,) = await rechecks(held, link_id)  # only the placeholder's, none for a public one
    assert first["not_before"] == datetime.fromisoformat(NOW) + RECHECK_EVERY
    github = GitHub("README.md", "LICENSE")
    workers = {store.RECHECK: RecheckWorker(db, http=mock(github.handle))}
    async with serving(WorkGate(db, poll=0.1), workers):
        for n in range(1, RECHECKS + 1):
            due = await due_now(held)
            if n > 1:  # each next one a week after the check before it
                checked = (await link_of(held, link_id)).checked_at
                assert due == checked + RECHECK_EVERY

            async def counted(n=n) -> bool:
                link = await link_of(held, link_id)
                done = [r for r in await rechecks(held, link_id) if r["state"] == "ended"]
                return link.rechecks == n and len(done) == n

            await until(counted, f"re-check {n}")
    link = await link_of(held, link_id)
    assert (link.state, link.license, link.found_by, link.rechecks) == (
        "placeholder",
        "MIT",
        "recheck",
        4,
    )
    assert [r["state"] for r in await rechecks(held, link_id)] == ["ended"] * RECHECKS  # left


async def test_a_placeholder_that_has_its_code_now_is_public_and_left(held: Session, db):
    link_id = await placeholder(held, db)
    github = GitHub("README.md", "probes/train.py")
    async with serving(
        WorkGate(db, poll=0.1), {store.RECHECK: RecheckWorker(db, http=mock(github.handle))}
    ):
        await due_now(held)

        async def checked() -> bool:
            return (await link_of(held, link_id)).rechecks == 1

        await until(checked, "the re-check")
        await until(lambda: _settled(held, link_id), "its item ends")
    assert (await link_of(held, link_id)).state == "public"
    assert [r["state"] for r in await rechecks(held, link_id)] == ["ended"]  # never again


async def _settled(conn: Session, link_id: int) -> bool:
    return all(r["state"] == "ended" for r in await rechecks(conn, link_id))


@pytest.mark.parametrize(
    ("url", "answers", "checked"),
    [
        ("https://github.com/o/gone", {}, ("missing", None)),
        ("https://github.com/o/probes", {"/repos/o/probes": 403}, None),  # rate limited
        ("https://example.org/code", {}, None),  # not a host it checks
        (
            "https://huggingface.co/o/m",
            {
                "/api/models/o/m": {
                    "gated": "manual",
                    "siblings": [{"rfilename": "model.safetensors"}],
                    "cardData": {"license": "llama3"},
                }
            },
            ("gated", "llama3"),
        ),
        (
            "https://huggingface.co/datasets/o/d",
            {
                "/api/datasets/o/d": {
                    "gated": False,
                    "siblings": [{"rfilename": "README.md"}],
                    "tags": ["license:cc-by-4.0"],
                }
            },
            ("placeholder", "cc-by-4.0"),
        ),
    ],
)
async def test_a_hosts_api_says_what_a_link_holds(url, answers, checked):
    def handle(req: httpx.Request) -> httpx.Response:
        answer = answers.get(req.url.path, 404)
        return (
            httpx.Response(answer) if isinstance(answer, int) else httpx.Response(200, json=answer)
        )

    assert await adapter.check(url, mock(handle)) == checked


async def test_a_paper_dropped_during_its_find_keeps_its_links(held: Session, db):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
        item = await queue.find(unit, store.FIND, find_key(A.id, A.version))
    assert item is not None
    await held.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (A.id,))
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    ended = await FindWorker(db, archive()[0], deep(finder))(item)
    assert isinstance(ended, Done) and "skipped" in ended.result
    assert finder.started == 0 and await urls(held) == []
    async with db.write("asked", taxonomy="shared", papers=[A.id]) as unit:
        assert await links.request(unit, A.id) is None
    assert await in_read(held, papers_store.held, [A.id]) == {A.id}
