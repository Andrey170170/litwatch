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
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from litwatch import links
from litwatch.api import integrations as integrations_api
from litwatch.api import mcp
from litwatch.api import schemas as s
from litwatch.archive import ArchiveClient
from litwatch.config import Schedule
from litwatch.core.contracts import deep as contract
from litwatch.core.links import RECHECK_EVERY, RECHECKS, Identity, ReleaseLink, recheck_key
from litwatch.core.work import WorkItem
from litwatch.feedback import misses, word
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Done, Later
from litwatch.intake import batch
from litwatch.integrations.deep import Deep, DeepClient
from litwatch.jobs import commands, nightly
from litwatch.jobs.context import model_run
from litwatch.links import adapter
from litwatch.links import worker as worker_module
from litwatch.links.worker import FindError, FindWorker, RecheckWorker
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
    the answer would; with `refuse`, every submission is turned down with that status. `sent` is
    what each submission sent."""

    def __init__(self, *ends: dict[str, Any], lose: int = 0, refuse: int | None = None) -> None:
        self.ends = list(ends)
        self.lose = lose
        self.refuse = refuse
        self.keys: dict[str, str] = {}
        self.finds: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []

    @property
    def started(self) -> int:
        return len(self.keys)

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/links":
            self.sent.append(json.loads(req.content))
            if self.refuse is not None:
                return httpx.Response(self.refuse, json={"error": "the paper is too large"})
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


def key(version: int | None, updated: str | None = None, paper_id: str = A.id) -> str:
    return Identity(version, updated).key(paper_id)


async def night(db: DatabaseGate, conn: Session) -> None:
    """The nightly as `litwatch nightly` runs it, without fetching or digests: an item
    (`jobs.commands`) the test's `serve` works, through to its end."""
    name = str(uuid4())
    scope = {"fetch": False, "days": None, "limit": None, "digests": False}
    async with db.write("litwatch nightly") as unit:
        item = await queue.submit(unit, commands.kind("nightly"), name, scope=scope, admission=name)

    async def ended() -> bool:
        return (await state_of(conn, item.id))["state"] == "ended"

    await until(ended, "the nightly ends")
    assert (await state_of(conn, item.id))["outcome"] == "done"


async def state_of(conn: Session, item_id: int) -> dict[str, Any]:
    found = await (
        await conn.execute(
            "SELECT state, outcome, error, cost, attempts FROM work_items WHERE id = %s",
            (item_id,),
        )
    ).fetchone()
    assert found is not None
    return found


async def died_running(conn: Session, item_id: int) -> None:
    """The item as a process that died working it leaves it: running, its lease lapsed, so a
    `serve`'s Work gate takes it back."""
    await conn.execute(
        "UPDATE work_items SET state = 'running', attempts = attempts + 1, started_at = now(), "
        "lease_token = gen_random_uuid(), lease_until = now() - interval '1 second' "
        "WHERE id = %s",
        (item_id,),
    )


def scripted() -> Scripted:
    models = Scripted()  # its analysis says the paper releases code
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    return models


async def item_of(conn: Session, k: str) -> WorkItem:
    found = await in_read(conn, queue.find, store.FIND, k)
    assert found is not None
    return found


async def owners(db: DatabaseGate, paper_id: str = A.id) -> WorkItem:
    async with db.write("asked", taxonomy="shared", papers=[paper_id]) as unit:
        item = await links.request(unit, paper_id)
    assert item is not None
    return item


async def page_of(db: DatabaseGate, *, finds: bool = True) -> dict[str, Any]:
    async with db.read("the page") as unit:
        return await integrations_api.release_links(unit, A.id, finds=finds, processed=True)


async def test_finds_run_once_per_version_after_the_nightly_s_analysis_and_after_a_new_version(
    held: Session, db: DatabaseGate
):
    models = scripted()
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    client, fake = archive()
    rules = settling.configured(SERVED)
    async with served(
        db, SERVED, models=models.gate(SERVED), integrations=[deep(finder)], archive=client
    ):
        await night(db, held)
        await until(all_ended(held, 1), "the find of v1 ends")
        assert await urls(held) == ["https://github.com/o/probes"]
        # arXiv's second version, its abstract rewritten: no analysis of it yet, so no find
        # when it is stored; the nightly analyses it, and that finds it, none this time.
        fake.texts[A.id] = (2, BODY)
        v2 = A.model_copy(update={"version": 2, "abstract": "Linear probes, read anew."})
        await batch.store(db, [v2], source="oai", rules=rules)
        assert len(await finds(held)) == 1
        await night(db, held)
        await until(all_ended(held, 2), "the find of v2 ends")
        # Another trigger of v2 (its analysis recorded again, the nightly's): its answer holds,
        # its item's results pruned too (P7's cleanup), the ended item's own record standing.
        await held.execute("DELETE FROM work_results")
        async with db.write("analysed again", taxonomy="shared", papers=[A.id]) as unit:
            await links.due(unit, [A.id])
        await night(db, held)
        assert len(await finds(held)) == 2
    assert finder.started == 2
    ended = await finds(held)
    assert [f["key"] for f in ended] == [key(1), key(2)]
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
        await night(db, held)
        await until(all_ended(held, 1), "the find ends")
    assert finder.started == 1
    assert settling.configured(SERVED.model_copy(update={"integrations": {}})).find_links is False


async def test_no_find_for_a_paper_whose_analysis_says_no_release(held: Session, db):
    await seed.analyse(held, A, code=False)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    assert await finds(held) == []


async def test_a_failed_find_fails_its_item_with_its_cost_and_the_next_nightly_finds_again(
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
        await night(db, held)
        await until(all_ended(held, 1), "the find fails")
        (failed,) = await finds(held)
        assert (failed["outcome"], failed["error"], failed["cost"]) == (
            "failed",
            "FindError: the deep service broke",
            Decimal("0.01"),  # its item's cost, as a find that answered has it
        )
        (kept,) = await in_read(held, queue.results, failed["id"])
        assert kept[1]["failed"] == "the deep service broke"
        assert kept[1]["usage"]["list_cost"] == 0.01  # paid for, though it failed
        assert finder.started == 1  # ended for good: not asked again by its key
        # A Process chunk's analysis of other papers does not retry it; the nightly does.
        with spending(deadline=datetime.now(UTC) + timedelta(hours=1)):
            async with model_run(db, "job", models=models.gate(SERVED)) as run:
                await nightly.analyse(db, SERVED, run, ids=["2601.99999"])
        assert len(await finds(held)) == 1
        await night(db, held)
        await until(all_ended(held, 2), "the second find ends")
    first, second = await finds(held)
    assert first["key"] == second["key"] == key(1)
    assert second["outcome"] == "done" and finder.started == 2
    assert len(set(finder.keys)) == 2  # a fresh find on deep, by the new item's key
    assert await urls(held) == ["https://github.com/o/probes"]


async def test_a_find_the_service_turns_down_fails_with_its_failure_recorded(held: Session, db):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    item = await item_of(held, key(1))
    finder = Finder(refuse=413)
    with pytest.raises(FindError, match="refused the find: 413"):
        await FindWorker(db, archive()[0], deep(finder))(item)
    (kept,) = await in_read(held, queue.results, item.id)
    assert "413" in kept[1]["failed"]


async def test_a_find_whose_answer_was_lost_follows_the_same_find(held: Session, db):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    item = await item_of(held, key(1))
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}}, lose=1)
    worker = FindWorker(db, archive()[0], deep(finder))
    assert isinstance(await worker(item), Later)  # the submission's answer lost
    assert isinstance(await worker(item), Done)
    assert finder.started == 1 and len(finder.sent) == 2  # sent twice, one find


async def test_a_find_taken_back_after_its_end_committed_does_not_ask_again(
    held: Session, db: DatabaseGate
):
    """Its process died after the answer, or the failure, was recorded and before the item's
    end committed: `serve`'s gate takes the item back, and its next try ends as it did, by
    the same find on deep, with its cost."""
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    item = await item_of(held, key(1))
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    assert isinstance(await FindWorker(db, archive()[0], deep(finder))(item), Done)
    failing = Finder({"error": "the finder stopped without answering", "failure": "paper"})
    client, _ = archive()

    async def taken_back(dead: WorkItem, n: int) -> None:
        await died_running(held, dead.id)  # one of a kind runs at a time: one, then the other
        async with served(
            db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
        ):
            await until(all_ended(held, n), "it is taken back and ends")

    await taken_back(item, 1)
    mine = await owners(db)
    with pytest.raises(FindError, match="the finder stopped without answering"):
        await FindWorker(db, archive()[0], deep(failing))(mine)
    await taken_back(mine, 2)
    answered, failed = await state_of(held, item.id), await state_of(held, mine.id)
    assert (answered["outcome"], answered["cost"], answered["attempts"]) == (
        "done",
        Decimal("0.01"),
        2,
    )
    assert (failed["outcome"], failed["error"], failed["cost"]) == (
        "failed",
        "FindError: the finder stopped without answering",
        Decimal("0.01"),
    )
    assert (finder.started, len(finder.sent), failing.started) == (1, 1, 1)  # neither asked again
    assert await urls(held) == ["https://github.com/o/probes"]


async def test_the_owners_find_that_fails_for_an_older_version_hands_over_with_its_cost(
    held: Session, db, monkeypatch
):
    """Through the app: the owner asks of v1, v2 is stored while deep works it, and deep's find
    fails for good. It ends superseded with what it spent, and the owner's is asked of v2."""
    await seed.analyse(held, A, code=False)  # no automatic find
    rules = settling.configured(SERVED)
    finder = Finder(
        {"error": "the finder stopped without answering", "failure": "paper"},
        {"result": {"links": [found("https://github.com/o/v2", "public")]}},
    )
    client, fake = archive()
    fake.texts[A.id] = (2, BODY)
    service = deep(finder)
    follow = service.client.follow_find

    async def v2_meanwhile(find_id: str) -> Any:
        if finder.started == 1:
            await batch.store(db, [A.model_copy(update={"version": 2})], source="oai", rules=rules)
        return await follow(find_id)

    monkeypatch.setattr(service.client, "follow_find", v2_meanwhile)
    async with (
        served(
            db, SERVED, models=Scripted().gate(SERVED), integrations=[service], archive=client
        ) as app,
        app_client(app) as http,
    ):
        assert (await http.post(f"/api/papers/{A.id}/links")).status_code == 202
        await until(all_ended(held, 2), "v1's find and v2's end")
        page = (await http.get(f"/api/papers/{A.id}")).json()
    v1, v2 = await finds(held)
    assert (v1["scope"]["version"], v1["outcome"], v1["cost"]) == (1, "done", Decimal("0.01"))
    assert v1["result"]["superseded"] and v1["result"]["usage"]["list_cost"] == 0.01
    assert (v2["scope"]["by"], v2["scope"]["version"], v2["outcome"]) == ("owner", 2, "done")
    assert page["owner_find"]["state"] == "done" and page["owner_find"]["links"] == 1
    assert await urls(held) == ["https://github.com/o/v2"] and finder.started == 2


async def test_a_find_of_what_the_paper_no_longer_is_hands_over_and_replaces_nothing(
    held: Session, db, monkeypatch
):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    v1 = await item_of(held, key(1))
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    worker = FindWorker(db, archive()[0], deep(finder))
    # v2 stored while the find waits for the archive: seen right before it pays.
    read_text = worker_module.read_text

    async def stored_meanwhile(*args, **kwargs):
        # By a path that submits no find (`papers.upsert` alone): the hand-over keeps it found.
        async with db.write("v2", taxonomy="shared", papers=[A.id]) as unit:
            await papers_store.upsert(unit, [A.model_copy(update={"version": 2})])
        return await read_text(*args, **kwargs)

    monkeypatch.setattr(worker_module, "read_text", stored_meanwhile)
    assert await worker(v1) == Done(result=links.SUPERSEDED)
    monkeypatch.setattr(worker_module, "read_text", read_text)
    assert finder.started == 0
    # Its answer, had it come as the version was stored: under the paper's lock, refused.
    answer = contract.LinksAnswer.model_validate({"links": [found("https://o.io/x", "public")]})
    async with db.write("a late answer", taxonomy="shared", papers=[A.id]) as unit:
        assert await links.record(unit, v1, answer, {"links": 1}) == links.SUPERSEDED
    assert await urls(held) == []
    # The current one stays due, once: handed over twice, it is the same item.
    assert [f["key"] for f in await finds(held)] == [key(1), key(2)]
    assert isinstance(await worker(await item_of(held, key(2))), Done)
    assert await urls(held) == ["https://github.com/o/probes"]


async def ended_by_the_gate(conn: Session, item: WorkItem, result: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE work_items SET state = 'ended', outcome = 'done', result = %s, ended_at = now() "
        "WHERE id = %s",
        (Jsonb(result), item.id),
    )


async def test_the_owners_find_of_an_older_version_hands_over_and_blocks_nothing(held: Session, db):
    await seed.analyse(held, A, code=False)  # no automatic find
    rules = settling.configured(SERVED)
    client, fake = archive()
    fake.texts[A.id] = (3, BODY)
    finder = Finder({"result": {"links": []}})
    worker = FindWorker(db, client, deep(finder))
    v1 = await owners(db)
    await batch.store(db, [A.model_copy(update={"version": 2})], source="oai", rules=rules)
    page = await page_of(db)
    assert (page["owner_find"], page["links_findable"]) == (None, True)  # v1's is not v2's
    # Worked, v1's is asked again of the paper as it is.
    assert await worker(v1) == Done(result=links.SUPERSEDED)
    await ended_by_the_gate(held, v1, links.SUPERSEDED)
    assert [(f["scope"]["by"], f["scope"]["version"]) for f in await finds(held)] == [
        ("owner", 1),
        ("owner", 2),
    ]
    v2 = await owners(db)  # the owner asking again is that one
    assert v2.id == (await finds(held))[-1]["id"]
    # A third version: the owner's ask is a new find, and v2's, worked, hands over to it.
    await batch.store(db, [A.model_copy(update={"version": 3})], source="oai", rules=rules)
    v3 = await owners(db)
    assert v3.id != v2.id and v3.scope["version"] == 3
    assert await worker(v2) == Done(result=links.SUPERSEDED)
    await ended_by_the_gate(held, v2, links.SUPERSEDED)
    assert len(await finds(held)) == 3
    page = await page_of(db)  # a superseded find is no answer
    assert page["link_answer"] is None and page["owner_find"].state == "queued"
    assert not page["links_findable"]
    assert isinstance(await worker(v3), Done) and finder.started == 1
    page = await page_of(db)
    assert page["link_answer"].links == 0  # none found, of v3


async def test_an_owners_find_open_keeps_the_automatic_one_which_ends_once_answered(
    held: Session, db
):
    mine = await owners(db)  # asked before the analysis lands
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    automatic = await item_of(held, key(1))  # submitted though the owner's is open
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    worker = FindWorker(db, archive()[0], deep(finder))
    assert isinstance(await worker(mine), Done)  # answered, its item not yet ended
    assert await worker(automatic) == Done(result=links.ANSWERED)  # without asking deep
    assert finder.started == 1


async def test_an_owners_find_that_fails_leaves_the_automatic_one_to_run(held: Session, db):
    finder = Finder(
        {"error": "the finder stopped without answering", "failure": "paper"},
        {"result": {"links": [found("https://github.com/o/probes", "public")]}},
    )
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ) as app:
        async with app_client(app) as http:
            asked = await http.post(f"/api/papers/{A.id}/links")
        assert asked.status_code == 202
        await until(all_ended(held, 1), "the owner's find fails")
        await seed.analyse(held, A, code=True)
        async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
            await links.due(unit, [A.id])
        await until(all_ended(held, 2), "the automatic find ends")
    assert [(f["scope"]["by"], f["outcome"]) for f in await finds(held)] == [
        ("owner", "failed"),
        ("finder", "done"),
    ]
    assert finder.started == 2 and await urls(held) == ["https://github.com/o/probes"]


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
    assert [f["key"] for f in await finds(held)] == [key(1), key(2)]


async def test_a_paper_of_no_known_version_is_found_once_per_update(held: Session, db):
    first, second = "2026-11-01T00:00:00Z", "2026-11-08T00:00:00Z"
    b = paper("2610.00002", title="Versionless", abstract="No version known.", updated=first)
    await settling.store(held, [b], source="oai")
    await seed.analyse(held, b, code=True)
    rules = settling.configured(SERVED)
    async with db.write("analysed", taxonomy="shared", papers=[b.id]) as unit:
        await links.due(unit, [b.id])
    await batch.store(db, [b.model_copy(update={"updated": second})], source="oai", rules=rules)
    # Its version learnt for the update already found: no new find, and that one is current.
    known = b.model_copy(update={"updated": second, "version": 3})
    await batch.store(db, [known], source="oai", rules=rules)
    at = [datetime.fromisoformat(t).isoformat() for t in (first, second)]
    assert [f["key"] for f in await finds(held)] == [key(None, u, b.id) for u in at]
    finder = Finder({"result": {"links": [found("https://github.com/o/v", "public")]}})
    client, fake = archive()
    fake.texts[b.id] = (3, BODY)
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ):
        await until(all_ended(held, 2), "both finds end")
    ended = await finds(held)
    assert (ended[0]["result"], ended[1]["result"]["links"]) == (links.SUPERSEDED, 1)
    assert finder.started == 1 and await urls(held, b.id) == ["https://github.com/o/v"]


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
    owner_find = page["owner_find"]
    assert (owner_find["state"], owner_find["answered"], owner_find["links"]) == ("done", True, 1)
    assert page["link_answer"]["links"] == 1
    assert page["open_find"] is None and not page["finding_links"] and page["links_findable"]
    assert await in_read(held, papers_store.held, [b.id]) == {b.id}
    assert await urls(held, b.id) == ["https://github.com/o/b"]


async def test_the_page_polls_only_a_find_that_runs_here(held: Session, db):
    """Read with no Worker running: the finds stay as they are submitted."""
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])  # an automatic find, open
    # No card provider here: none would run a find, so none is offered or polled.
    gate = Scripted().gate(SERVED)
    async with served(db, SERVED, models=gate, integrations=[]) as app, app_client(app) as http:
        page = (await http.get(f"/api/papers/{A.id}")).json()
        refused = await http.post(f"/api/papers/{A.id}/links")
    assert page["open_find"]["state"] == "queued" and page["owner_find"] is None
    assert (page["finding_links"], page["links_findable"], refused.status_code) == (
        False,
        False,
        409,
    )
    assert "(A find of its links is queued.)" in mcp._links_lines(s.Paper.model_validate(page))
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
    assert (page["finding_links"], page["links_findable"], refused.status_code) == (
        False,
        False,
        409,
    )
    page = await page_of(db)  # where finds run
    assert page["finding_links"] and page["links_findable"] and page["owner_find"] is None


async def test_an_answer_that_breaks_the_contract_fails_the_find_and_replaces_nothing(
    held: Session, db: DatabaseGate
):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    bad = [
        [found("https://o.io/x", "public"), found("not a URL", "public")],
        [found("https://o.io/y", "public", checked="2026-11-26")],
        [found("https://[", "public")],
        [found("https://o.io:99999/x", "public")],
        [found("https://o.io/z", "public", checked="2026-02-30T03:00:00Z")],
    ]
    finder = Finder(
        {"result": {"links": [found("https://github.com/o/probes", "public")]}},
        *({"result": {"links": b}} for b in bad),
    )
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(finder)], archive=client
    ) as app:
        await until(all_ended(held, 1), "the first find ends")
        async with app_client(app) as http:
            for n in range(len(bad)):
                await http.post(f"/api/papers/{A.id}/links")
                await until(all_ended(held, n + 2), f"find {n + 2} ends")
    outcomes = [(f["outcome"], f["error"] or "") for f in await finds(held)]
    assert [o for o, _ in outcomes] == ["done"] + ["failed"] * len(bad)
    assert all("cannot read" in e for _, e in outcomes[1:])
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


async def test_a_re_check_taken_back_after_its_write_committed_asks_nothing(
    held: Session, db, monkeypatch
):
    """Its process died after its count and the next check committed, before the item's end
    did: `serve`'s gate takes the item back, and its next try ends at once."""
    github = GitHub("README.md")
    hosting(monkeypatch, github.handle)
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    item = await item_of(held, key(1))
    await FindWorker(db, archive()[0], deep(Finder({"result": {"links": PLACEHOLDER}})))(item)
    await ended_by_the_gate(held, item, {"links": 2})
    (first,) = await rechecks(held)
    check = await in_read(held, queue.find, store.RECHECK, first["key"])
    assert check is not None
    await RecheckWorker(db)(check)  # its count and the next check committed, then it died
    await died_running(held, check.id)
    asked = github.asked
    client, _ = archive()
    async with served(
        db, SERVED, models=Scripted().gate(SERVED), integrations=[deep(Finder())], archive=client
    ):

        async def taken_back() -> bool:
            return (await state_of(held, check.id))["state"] == "ended"

        await until(taken_back, "the re-check is taken back and ends")
    assert github.asked == asked  # the host is not asked again
    assert (await state_of(held, check.id))["attempts"] == 2
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


# The host checks' table, shared with deep's finder (deep/src/hostcheck.ts).
HOST_CHECKS = json.loads(
    (Path(__file__).parent.parent / "deep/tests/fixtures/host-checks.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", HOST_CHECKS, ids=[c["name"] for c in HOST_CHECKS])
async def test_a_hosts_api_says_what_a_link_holds_as_the_shared_table_says(monkeypatch, case):
    def handle(req: httpx.Request) -> httpx.Response:
        answer = case["answers"].get(str(req.url))
        if answer is None:
            return httpx.Response(404)
        if "location" in answer:
            return httpx.Response(answer["status"], headers={"location": answer["location"]})
        if "json" in answer:
            return httpx.Response(answer["status"], json=answer["json"])
        return httpx.Response(answer["status"])

    hosting(monkeypatch, handle)
    if not adapter.checkable(case["url"]):
        said: Any = "unsupported"
    else:
        checked = await adapter.check(case["url"], case["kind"])
        said = (
            None
            if checked is None
            else {"state": checked[0]} | ({"license": checked[1]} if checked[1] else {})
        )
    assert said == case["expect"]


async def test_a_paper_dropped_during_its_find_keeps_its_links(held: Session, db):
    await seed.analyse(held, A, code=True)
    async with db.write("analysed", taxonomy="shared", papers=[A.id]) as unit:
        await links.due(unit, [A.id])
    item = await item_of(held, key(1))
    await held.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (A.id,))
    finder = Finder({"result": {"links": [found("https://github.com/o/probes", "public")]}})
    ended = await FindWorker(db, archive()[0], deep(finder))(item)
    assert ended == Done(result=links.SKIPPED)
    assert finder.started == 0 and await urls(held) == []
    async with db.write("asked", taxonomy="shared", papers=[A.id]) as unit:
        assert await links.request(unit, A.id) is None
    assert await in_read(held, papers_store.held, [A.id]) == {A.id}
