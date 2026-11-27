"""Release links (docs/plans/a2-work-gate.md, P6): when a find runs (an analysis that says the
paper releases code or weights, once per arXiv version or, of no known version, per update;
again for a new one; again after a failure; the owner's "find links" each time), that a find
whose answer or failure committed is never asked again, that a find of what the paper no longer
is replaces nothing, and the re-checks of a placeholder: weekly, four times, each once, then
left. Through `serve` with its Workers running and the configuration's own rules, on a fake
deep service, a fake archive, a fake GitHub and Hugging Face."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from litwatch import links
from litwatch.archive import ArchiveClient
from litwatch.config import Schedule
from litwatch.core.contracts import deep as contract
from litwatch.core.links import RECHECK_EVERY, RECHECKS, ReleaseLink, find_key, recheck_key
from litwatch.feedback import misses, word
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Done, Later
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
from tests.conftest import TREE, answering, paper
from tests.fakes_http import FakeArchive, mock
from tests.scripted import CONFIG, Scripted, decision
from tests.sessions import Session, in_read
from tests.working import QUIET, TOKEN, served, until

A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes.", version=1)
BODY = "\\section{Code} Our code is at github.com/o/probes. " * 20
DEEP = {"deep": {"url": "http://deep.test"}}  # the deep integration configured: links found
SERVED = CONFIG.model_copy(update={"schedule": QUIET, "integrations": DEEP})
# A find's check, as deep gives it: its re-check is a week on, not due.
NOW = datetime.now(UTC).isoformat().replace("+00:00", "Z")
USAGE = dict.fromkeys(
    ("cells", "turns", "input", "output", "reasoning", "cache_read", "seconds"), 1
) | {"list_cost": 0.01}


def found(url: str, state: str, kind: str = "code", checked: str = NOW) -> dict[str, Any]:
    return {"url": url, "kind": kind, "state": state, "license": None, "checked_at": checked}


class Finder:
    """deep's finds: a key it has not seen starts a find, which ends on its first poll as the
    next of `ends` (its `result`, or its `error` and `failure`), else none found; a key it has
    is that find. `lose` submissions are answered 502 after the find started, as a proxy losing
    the answer would. `sent` is what each submission sent."""

    def __init__(self, *ends: dict[str, Any], lose: int = 0) -> None:
        self.ends = list(ends)
        self.lose = lose
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
            if self.lose:
                self.lose -= 1
                return httpx.Response(502)
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
                "stats": USAGE,
            }
            return httpx.Response(200, json=job | end)
        return httpx.Response(404)


def deep(finder: Finder) -> Deep:
    return Deep(DeepClient("http://deep.test", http=mock(finder.handle), poll_seconds=0))


def archive(fake: FakeArchive | None = None) -> tuple[ArchiveClient, FakeArchive]:
    fake = fake or FakeArchive()
    fake.texts.setdefault(A.id, (A.version, BODY))
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle)), fake


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """The configuration's own rules (conftest replaces them): links are found where the deep
    integration is configured, and only there."""
    monkeypatch.setattr(word, "rules", settling.configured)


@pytest.fixture
async def held(conn: Session):
    await settling.save_tree(conn, TREE)
    await settling.store(conn, [A], source="oai")
    return conn


def app_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def finds(conn: Session) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM work_items WHERE kind = %s ORDER BY id", (store.FIND,))
    ).fetchall()


def all_ended(conn: Session, count: int) -> Callable[[], Any]:
    async def check() -> bool:
        found = await finds(conn)
        return len(found) == count and all(f["state"] == "ended" for f in found)

    return check


async def urls(conn: Session, paper_id: str = A.id) -> list[str]:
    return [link.url for link in await in_read(conn, store.of_paper, paper_id)]


async def night(db: DatabaseGate, models: Scripted) -> None:
    """The nightly's classify and analyse of A, on the served configuration's rules."""
    with spending(deadline=datetime.now(UTC) + timedelta(hours=1)):
        async with model_run(db, "nightly", models=models.gate(SERVED)) as run:
            await nightly.classify(db, SERVED, run, ids=[A.id])
            await nightly.analyse(db, SERVED, run, ids=[A.id])


def scripted() -> Scripted:
    models = Scripted()  # its analysis says the paper releases code
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    return models


async def test_finds_run_once_per_version_after_the_nightly_s_analysis_and_after_a_new_version(
    held: Session, db: DatabaseGate
):
    models = scripted()
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    client, fake = archive()
    async with served(
        db, SERVED, models=models.gate(SERVED), integrations=[deep(finder)], archive=client
    ):
        await night(db, models)
        await until(all_ended(held, 1), "the find of v1 ends")
        assert await urls(held) == ["https://github.com/o/probes"]
        # Rewritten at the same version, the paper is analysed again: its find is answered.
        rewritten = A.model_copy(update={"abstract": "Linear probes, read anew. Code released."})
        await batch.store(db, [rewritten], source="oai", rules=settling.configured(SERVED))
        await night(db, models)
        assert len(await finds(held)) == 1
        # arXiv's second version, of the same text: found again, none this time.
        fake.texts[A.id] = (2, BODY)
        v2 = rewritten.model_copy(update={"version": 2})
        await batch.store(db, [v2], source="oai", rules=settling.configured(SERVED))
        await until(all_ended(held, 2), "the find of v2 ends")
    assert finder.started == 2
    ended = await finds(held)
    assert [f["key"] for f in ended] == [find_key(A.id, 1, None), find_key(A.id, 2, None)]
    assert [f["cost"] for f in ended] == [Decimal("0.01")] * 2  # what deep reported
    assert [s["paper"]["body"] for s in finder.sent] == [BODY, BODY]  # from the paper's text
    assert finder.sent[0].get("comments") is None  # A has none: sent as absent
    assert await urls(held) == []  # the newest find's answer stands, none found included


async def test_with_automatic_reads_off_links_are_still_found(held: Session, db: DatabaseGate):
    """`automatic = false` turns off Deep reads only; without the deep integration, no find."""
    off = SERVED.model_copy(update={"integrations": {"deep": DEEP["deep"] | {"automatic": False}}})
    assert settling.configured(off) == word.Rules(automatic_deep=False, find_links=True)
    models = scripted()
    finder = Finder()
    client, _ = archive()
    async with served(
        db, off, models=models.gate(off), integrations=[deep(finder)], archive=client
    ):
        with spending(deadline=datetime.now(UTC) + timedelta(hours=1)):
            async with model_run(db, "nightly", models=models.gate(off)) as run:
                await nightly.classify(db, off, run, ids=[A.id])
                await nightly.analyse(db, off, run, ids=[A.id])
        await until(all_ended(held, 1), "the find ends")
    assert finder.started == 1
    assert settling.configured(SERVED.model_copy(update={"integrations": {}})).find_links is False


async def test_no_find_for_a_paper_whose_analysis_says_no_release(held: Session, db):
    await seed.analyse(held, A, code=False)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    assert await finds(held) == []


async def test_a_failed_find_fails_its_item_with_its_cost_and_a_later_nightly_finds_again(
    held: Session, db: DatabaseGate
):
    models = scripted()
    finder = Finder(
        {"error": "the deep service broke", "failure": "service"},
        {"result": {"links": [found("https://github.com/o/probes", "public")]}},
    )
    client, _ = archive()
    async with served(
        db, SERVED, models=models.gate(SERVED), integrations=[deep(finder)], archive=client
    ):
        await night(db, models)
        await until(all_ended(held, 1), "the find fails")
        (failed,) = await finds(held)
        assert (failed["outcome"], failed["error"]) == (
            "failed",
            "FindError: the deep service broke",
        )
        (kept,) = await in_read(held, queue.results, failed["id"])
        assert kept[1]["failed"] == "the deep service broke"
        assert kept[1]["usage"]["list_cost"] == 0.01  # paid for, though it failed
        assert finder.started == 1  # ended for good: not asked again by its key
        await night(db, models)  # nothing to analyse; the failed find is tried again
        await until(all_ended(held, 2), "the second find ends")
    first, second = await finds(held)
    assert first["key"] == second["key"] == find_key(A.id, 1, None)
    assert second["outcome"] == "done" and finder.started == 2
    assert len(set(finder.keys)) == 2  # a fresh find on deep, by the new item's key
    assert await urls(held) == ["https://github.com/o/probes"]


async def test_a_find_whose_answer_was_lost_follows_the_same_find(held: Session, db):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
        item = await queue.find(unit, store.FIND, find_key(A.id, 1, None))
    assert item is not None
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}}, lose=1)
    worker = FindWorker(db, archive()[0], deep(finder))
    assert isinstance(await worker(item), Later)  # the submission's answer lost
    assert isinstance(await worker(item), Done)
    assert finder.started == 1 and len(finder.sent) == 2  # sent twice, one find


async def test_a_find_tried_again_after_its_end_committed_does_not_ask_again(
    held: Session, db: DatabaseGate
):
    """Its try ended after the answer, or the failure, was recorded and before the item's end
    committed (the process died): the next try of the same item ends as it did."""
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
        item = await queue.find(unit, store.FIND, find_key(A.id, 1, None))
    assert item is not None
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    worker = FindWorker(db, archive()[0], deep(finder))
    first, second = await worker(item), await worker(item)
    assert isinstance(first, Done) and first == second and first.cost == Decimal("0.01")
    assert finder.started == 1 and len(finder.sent) == 1
    assert await urls(held) == ["https://github.com/o/probes"]
    async with db.write("asked", taxonomy="shared", papers=[A.id]) as unit:
        owners = await links.request(unit, A.id)
    assert owners is not None
    failing = Finder({"error": "the finder stopped without answering", "failure": "paper"})
    worker = FindWorker(db, archive()[0], deep(failing))
    for _ in range(2):
        with pytest.raises(Exception, match="the finder stopped without answering"):
            await worker(owners)
    assert failing.started == 1 and len(failing.sent) == 1


async def test_a_find_of_what_the_paper_no_longer_is_replaces_nothing(held: Session, db):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
        v1 = await queue.find(unit, store.FIND, find_key(A.id, 1, None))
    assert v1 is not None
    await batch.store(
        db, [A.model_copy(update={"version": 2})], source="oai", rules=settling.configured(SERVED)
    )
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    worker = FindWorker(db, archive()[0], deep(finder))
    assert await worker(v1) == Done(result=links.SUPERSEDED)  # before it is sent
    assert finder.started == 0
    # Its answer, had it come as the version was stored: under the paper's row, refused.
    answer = contract.LinksAnswer.model_validate({"links": [found("https://o.io/x", "public")]})
    async with db.write("a late answer") as unit:
        assert await links.record(unit, v1, answer, {"links": 1}) == links.SUPERSEDED
    assert await urls(held) == []
    v2 = await in_read(held, queue.find, store.FIND, find_key(A.id, 2, None))
    assert v2 is not None and isinstance(await worker(v2), Done)  # the current one stays due
    assert await urls(held) == ["https://github.com/o/probes"]


async def test_a_new_version_the_owners_action_stores_is_found_again(held: Session, db):
    """A Process, Deep read or miss report that fetched arXiv's record before intake stored the
    paper stores its newer version as intake would (`misses.keep`): found again."""
    await seed.analyse(held, A, code=True)
    rules = settling.configured(SERVED)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    v2 = A.model_copy(update={"version": 2})
    async with db.write("owner's action", taxonomy="shared", papers=[A.id]) as unit:
        await misses.keep(unit, A.id, v2, rules)
    assert [f["key"] for f in await finds(held)] == [
        find_key(A.id, 1, None),
        find_key(A.id, 2, None),
    ]


async def test_a_paper_of_no_known_version_is_found_once_per_update(held: Session, db):
    first, second = "2026-11-01T00:00:00Z", "2026-11-08T00:00:00Z"
    b = paper("2610.00002", title="Versionless", abstract="No version known.", updated=first)
    await settling.store(held, [b], source="oai")
    await seed.analyse(held, b, code=True)
    rules = settling.configured(SERVED)
    async with db.write("analysed", taxonomy="shared", papers=[b.id]) as unit:
        await links.due(unit, [b.id])
    await batch.store(db, [b.model_copy(update={"updated": second})], source="oai", rules=rules)
    # Its version learnt for the update already found: no new find.
    known = b.model_copy(update={"updated": second, "version": 3})
    await batch.store(db, [known], source="oai", rules=rules)
    assert [f["key"] for f in await finds(held)] == [
        f"{b.id}@{datetime.fromisoformat(first).isoformat()}",
        f"{b.id}@{datetime.fromisoformat(second).isoformat()}",
    ]


async def test_the_owners_find_links_runs_a_fresh_find_each_time(held: Session, db, monkeypatch):
    await seed.analyse(held, A, code=False)  # no automatic find
    b = paper("2610.00003", title="Only in the archive", abstract="Not held.", version=1)
    monkeypatch.setattr(misses, "fetch_by_ids", answering(lambda ids: [b] if b.id in ids else []))
    finder = Finder(
        {"result": {"links": [found("https://huggingface.co/o/m", "gated", "weights")]}},
        {"result": {"links": [found("https://github.com/o/probes", "placeholder")]}},
        {"result": {"links": [found("https://github.com/o/b", "public")]}},
    )
    client, fake = archive()
    fake.texts[b.id] = (1, BODY)
    async with (
        served(
            db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
        ) as app,
        app_client(app) as http,
    ):
        first = await http.post(f"/api/papers/{A.id}/links")
        again = await http.post(f"/api/papers/{A.id}/links")
        await until(all_ended(held, 1), "the first find ends")
        asked = await http.post(f"/api/papers/{A.id}/links")
        await until(all_ended(held, 2), "the second find ends")
        page = (await http.get(f"/api/papers/{A.id}")).json()
        archived = await http.post(f"/api/papers/{b.id}/links")  # stored, then found
        await until(all_ended(held, 3), "the archive paper's find ends")
        missing = await http.post("/api/papers/2601.99999/links")
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json()  # one while it is open
    assert asked.status_code == 202 and asked.json()["find_id"] != first.json()["find_id"]
    assert archived.status_code == 202 and missing.status_code == 404
    assert finder.started == 3
    assert [(link["url"], link["state"], link["found_by"]) for link in page["links"]] == [
        ("https://github.com/o/probes", "placeholder", "owner")
    ]
    assert (page["link_find"]["state"], page["link_find"]["by"]) == ("done", "owner")
    assert page["owner_find"] is None and not page["finding_links"] and page["links_findable"]
    assert await in_read(held, papers_store.held, [b.id]) == {b.id}
    assert await urls(held, b.id) == ["https://github.com/o/b"]


async def test_the_page_tells_the_owners_open_find_from_an_automatic_one(held: Session, db):
    """Read with no Worker running: the finds stay as they are submitted."""
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])  # an automatic find, open
    # No card provider here: none would run the owner's find, so none is offered.
    gate = Scripted().gate(SERVED)
    async with served(db, SERVED, models=gate, integrations=[]) as app, app_client(app) as http:
        page = (await http.get(f"/api/papers/{A.id}")).json()
        refused = await http.post(f"/api/papers/{A.id}/links")
    assert (page["owner_find"], page["finding_links"], page["links_findable"]) == (
        None,
        True,
        False,
    )
    assert refused.status_code == 409
    finder = Finder()
    client, _ = archive()
    off = SERVED.model_copy(update={"schedule": Schedule(enabled=False)})
    async with (
        served(
            db, off, models=Scripted().gate(off), integrations=[deep(finder)], archive=client
        ) as app,
        app_client(app) as http,
    ):  # the schedule off: no find Worker runs
        page = (await http.get(f"/api/papers/{A.id}")).json()
        refused = await http.post(f"/api/papers/{A.id}/links")
    assert (page["links_findable"], refused.status_code) == (False, 409)
    async with db.write("asked", taxonomy="shared", papers=[A.id]) as unit:
        owners = await links.request(unit, A.id)
    assert owners is not None
    async with (
        served(
            db, off, models=Scripted().gate(off), integrations=[deep(finder)], archive=client
        ) as app,
        app_client(app) as http,
    ):
        page = (await http.get(f"/api/papers/{A.id}")).json()
    assert page["owner_find"]["state"] == "queued" and page["finding_links"]
    # The last finished is the one that ended last, whichever was submitted first.
    automatic, _ = [f["id"] for f in await finds(held)]
    await held.execute(
        "UPDATE work_items SET state = 'ended', outcome = 'done', ended_at = now() + "
        "CASE WHEN id = %s THEN interval '1 minute' ELSE interval '0' END WHERE kind = %s",
        (automatic, store.FIND),
    )
    async with (
        served(
            db, off, models=Scripted().gate(off), integrations=[deep(finder)], archive=client
        ) as app,
        app_client(app) as http,
    ):
        page = (await http.get(f"/api/papers/{A.id}")).json()
    assert page["link_find"]["by"] == "finder"


async def test_an_answer_that_breaks_the_contract_fails_the_find_and_replaces_nothing(
    held: Session, db: DatabaseGate
):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    finder = Finder(
        {"result": {"links": [found("https://github.com/o/probes", "public")]}},
        {"result": {"links": [found("https://o.io/x", "public"), found("not a URL", "public")]}},
        {"result": {"links": [found("https://o.io/y", "public", checked="2026-11-26")]}},
    )
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ) as app:
        await until(all_ended(held, 1), "the first find ends")
        async with app_client(app) as http:
            await http.post(f"/api/papers/{A.id}/links")
            await until(all_ended(held, 2), "the second find ends")
            await http.post(f"/api/papers/{A.id}/links")
            await until(all_ended(held, 3), "the third find ends")
    assert [f["outcome"] for f in await finds(held)] == ["done", "failed", "failed"]
    assert await urls(held) == ["https://github.com/o/probes"]


class GitHub:
    """GitHub's API for one repository, `o/probes`: its contents `files` (none: a repository
    with no commit); `o/old` redirects to it, as a renamed repository does. `asked` counts the
    requests."""

    def __init__(self, *files: str) -> None:
        self.files = list(files)
        self.asked = 0

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.asked += 1
        if req.url.path == "/repos/o/old":
            return httpx.Response(
                301, headers={"location": "https://api.github.com/repositories/7"}
            )
        if req.url.path in ("/repos/o/probes", "/repositories/7"):
            body = {"full_name": "o/probes", "license": {"spdx_id": "MIT", "name": "MIT License"}}
            return httpx.Response(200, json=body)
        if req.url.path == "/repos/o/probes/contents" and self.files:
            return httpx.Response(200, json=[{"path": f, "type": "file"} for f in self.files])
        return httpx.Response(404)


def hosting(monkeypatch, handle: Callable[[httpx.Request], httpx.Response]) -> None:
    monkeypatch.setattr(adapter, "client", lambda: mock(handle))


async def link_of(conn: Session, link_id: int) -> ReleaseLink:
    link = await in_read(conn, store.get, link_id)
    assert link is not None
    return link


async def rechecks(conn: Session) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM work_items WHERE kind = %s ORDER BY id", (store.RECHECK,))
    ).fetchall()


async def due_now(conn: Session) -> datetime:
    """Bring the pending re-check forward to now, as a week passing would, waking the gate; when
    it was due."""
    (pending,) = [r for r in await rechecks(conn) if r["state"] == "pending"]
    await conn.execute("UPDATE work_items SET not_before = now() WHERE id = %s", (pending["id"],))
    await conn.execute("SELECT pg_notify(%s, %s)", (queue.CHANNEL, store.RECHECK))
    return pending["not_before"]


async def placeholder_found(held: Session, app) -> int:
    """The owner's find answered a placeholder on GitHub, and a public page elsewhere: the
    placeholder's link id."""
    async with app_client(app) as http:
        await http.post(f"/api/papers/{A.id}/links")
    await until(all_ended(held, 1), "the find ends")
    (link,) = [
        link for link in await in_read(held, store.of_paper, A.id) if link.state == "placeholder"
    ]
    return link.id


PLACEHOLDER = [found("https://github.com/o/probes", "placeholder"), found("https://o.io", "public")]


async def test_a_placeholder_is_checked_again_weekly_four_times_then_left(
    held: Session, db, monkeypatch
):
    github = GitHub("README.md", "LICENSE")
    hosting(monkeypatch, github.handle)
    finder = Finder({"result": {"links": PLACEHOLDER}})
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ) as app:
        link_id = await placeholder_found(held, app)
        (first,) = await rechecks(held)  # only the placeholder's, none for a public one
        assert first["key"] == recheck_key(link_id, 1)
        assert first["not_before"] == datetime.fromisoformat(NOW) + RECHECK_EVERY
        for n in range(1, RECHECKS + 1):
            due = await due_now(held)
            if n > 1:  # each next one a week after the check before it
                checked = (await link_of(held, link_id)).checked_at
                assert due == checked + RECHECK_EVERY

            async def counted(n=n) -> bool:
                link = await link_of(held, link_id)
                done = [r for r in await rechecks(held) if r["state"] == "ended"]
                return link.rechecks == n and len(done) == n

            await until(counted, f"re-check {n}")
    link = await link_of(held, link_id)
    assert (link.state, link.license, link.found_by, link.rechecks) == (
        "placeholder",
        "MIT",
        "recheck",
        4,
    )
    ended = await rechecks(held)
    assert [r["key"] for r in ended] == [recheck_key(link_id, n) for n in range(1, RECHECKS + 1)]
    assert [r["state"] for r in ended] == ["ended"] * RECHECKS  # left after the fourth


async def test_a_re_check_tried_again_after_its_write_committed_asks_nothing(
    held: Session, db, monkeypatch
):
    """Its try ended after its count and the next check committed, before the item's end did
    (the process died): the next try of the same item ends at once."""
    github = GitHub("README.md")
    hosting(monkeypatch, github.handle)
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
        item = await queue.find(unit, store.FIND, find_key(A.id, 1, None))
    assert item is not None
    await FindWorker(db, archive()[0], deep(Finder({"result": {"links": PLACEHOLDER}})))(item)
    (first,) = await rechecks(held)
    check = await in_read(held, queue.find, store.RECHECK, first["key"])
    assert check is not None
    worker = RecheckWorker(db)
    await worker(check)
    asked = github.asked
    await worker(check)
    assert github.asked == asked  # the host is not asked again
    (link,) = [k for k in await in_read(held, store.of_paper, A.id) if k.state == "placeholder"]
    assert link.rechecks == 1
    assert [r["key"] for r in await rechecks(held)] == [
        recheck_key(link.id, 1),
        recheck_key(link.id, 2),
    ]


async def test_a_placeholder_that_has_its_code_now_is_public_and_left(
    held: Session, db, monkeypatch
):
    github = GitHub("README.md", "probes/train.py")
    hosting(monkeypatch, github.handle)
    finder = Finder({"result": {"links": PLACEHOLDER}})
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ) as app:
        link_id = await placeholder_found(held, app)
        await due_now(held)

        async def settled() -> bool:
            return all(r["state"] == "ended" for r in await rechecks(held))

        await until(settled, "the re-check ends")
    link = await link_of(held, link_id)
    assert (link.state, link.rechecks) == ("public", 1)
    assert len(await rechecks(held)) == 1  # never again


HF_GATED = {"gated": "manual", "siblings": [{"rfilename": "model-00001-of-00002.safetensors"}]}


@pytest.mark.parametrize(
    ("url", "kind", "answers", "checked"),
    [
        ("https://github.com/o/gone", "code", {}, ("missing", None)),
        ("https://github.com/o/probes", "code", {"/repos/o/probes": 403}, None),  # rate limited
        ("https://example.org/code", "code", {}, None),  # not a host it checks
        (
            "https://github.com/o/old",  # renamed: followed to its contents
            "code",
            {
                "/repos/o/old": (301, "https://api.github.com/repositories/7"),
                "/repositories/7": {"full_name": "o/probes", "license": None},
                "/repos/o/probes/contents": [{"path": "train.py"}],
            },
            ("public", None),
        ),
        (
            "https://github.com/o/away",  # a redirect off GitHub's API is not followed
            "code",
            {"/repos/o/away": (301, "https://elsewhere.test/repos/o/away")},
            None,
        ),
        (
            "https://huggingface.co/o/m",
            "weights",
            {"/api/models/o/m": HF_GATED | {"cardData": {"license": "llama3"}}},
            ("gated", "llama3"),
        ),
        (
            "https://huggingface.co/o/m",
            "weights",
            {"/api/models/o/m": {"siblings": [{"rfilename": "model.gguf"}]}},
            ("public", None),
        ),
        (
            "https://huggingface.co/o/m",  # a README and a config hold no weights
            "weights",
            {
                "/api/models/o/m": {
                    "gated": False,
                    "siblings": [{"rfilename": "README.md"}, {"rfilename": "config.json"}],
                }
            },
            ("placeholder", None),
        ),
        (
            "https://huggingface.co/datasets/o/d",
            "code",
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
async def test_a_hosts_api_says_what_a_link_holds(monkeypatch, url, kind, answers, checked):
    def handle(req: httpx.Request) -> httpx.Response:
        answer = answers.get(req.url.path, 404)
        if isinstance(answer, int):
            return httpx.Response(answer)
        if isinstance(answer, tuple):
            return httpx.Response(answer[0], headers={"location": answer[1]})
        return httpx.Response(200, json=answer)

    hosting(monkeypatch, handle)
    assert await adapter.check(url, kind) == checked


async def test_a_paper_dropped_during_its_find_keeps_its_links(held: Session, db):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
        item = await queue.find(unit, store.FIND, find_key(A.id, 1, None))
    assert item is not None
    await held.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (A.id,))
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    ended = await FindWorker(db, archive()[0], deep(finder))(item)
    assert ended == Done(result=links.SKIPPED)
    assert finder.started == 0 and await urls(held) == []
    async with db.write("asked", taxonomy="shared", papers=[A.id]) as unit:
        assert await links.request(unit, A.id) is None
    assert await in_read(held, papers_store.held, [A.id]) == {A.id}
