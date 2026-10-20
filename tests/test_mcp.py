"""The MCP server at /mcp (docs/plans/mcp-server.md, V1–V4 and V9): its token and mount, search
against the web app's, that nothing is written, what a paper and a digest carry, and what the
monitor sees. Every call goes through `create_app` over HTTP, as an agent's does."""

import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import httpx
import psycopg
import pytest

from litwatch.api import app as app_module
from litwatch.api import create_app
from litwatch.api import mcp as mcp_api
from litwatch.api.monitor import MCP_WINDOW, McpCalls, create_monitor
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import Publication
from litwatch.core.digests import DigestPeriod, DigestRole, DigestState
from litwatch.integrations.archive import Archive
from litwatch.jobs import publications as lookups
from litwatch.store import digests as digests_store
from litwatch.store import integrations as integrations_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import AGENTS, FAITH, INTERP, TREE, paper
from tests.fakes_http import FakeArchive, mock
from tests.test_reading import P, Q, analyse, classify, grade

TOKEN, READ_TOKEN, MCP_TOKEN = "s3cret", "r3ad", "m-token"
HOST = "litwatch.andrei-homelab.com"  # production's name, which the SDK's host check must let in
AUTH = {"authorization": f"Bearer {TOKEN}"}
ONLY = paper("2610.07777", title="Hidden steps in the archive", authors=["Ada Lovelace"])
INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "1"},
}


class Flaky(FakeArchive):
    """The fake archive, answering 503 to everything while `down`."""

    down = False

    def handle(self, req: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if self.down else super().handle(req)


def _dsn(conn) -> str:
    return f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"


def _app(conn, fake: FakeArchive | None = None, seen: list | None = None):
    """The app with /mcp served behind production's host name, and `fake` as the archive (its
    client and its search provider), every request it gets recorded in `seen`."""
    archive, integrations = None, []
    if fake is not None:

        def handle(req: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.append((req.method, req.url.path))
            return fake.handle(req)

        archive = ArchiveClient("http://archive.test", "archive-token", http=mock(handle))
        search = httpx.Client(transport=httpx.MockTransport(handle))
        integrations = [Archive(archive, search_http=search, register=False)]
    return create_app(
        _dsn(conn),
        TOKEN,
        Config(mcp={"allowed_hosts": [HOST]}),
        read_token=READ_TOKEN,
        archive=archive,
        integrations=integrations,
        mcp_token=MCP_TOKEN,
    )


@asynccontextmanager
async def serving(app):
    """The app running with its lifespan, and so the MCP's session manager, as `serve` runs it."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as http:
            yield http


async def rpc(http, method: str, params: dict | None = None, token: str | None = MCP_TOKEN, **h):
    headers = {"accept": "application/json, text/event-stream", **h}
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await http.post("/mcp", json=body, headers=headers)


async def call(http, tool: str, **arguments) -> tuple[str, bool]:
    """A tool's answer, and whether it is an error."""
    answer = await rpc(http, "tools/call", {"name": tool, "arguments": arguments})
    assert answer.status_code == 200, answer.text
    result = answer.json()["result"]
    return result["content"][0]["text"], result["isError"]


async def ok(http, tool: str, **arguments) -> str:
    text, failed = await call(http, tool, **arguments)
    assert not failed, text
    return text


def ids(text: str) -> list[str]:
    return re.findall(r"arXiv:(\S+)", text)


# -- V1: the token and the mount -------------------------------------------------------------------


async def test_only_the_mcp_token_opens_mcp_and_it_opens_nothing_else(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P])
    async with serving(_app(conn)) as http:
        opened = await rpc(http, "initialize", INIT)
        assert opened.status_code == 200
        assert "before the web" in opened.json()["result"]["instructions"]
        for token in (None, TOKEN, READ_TOKEN, "wrong"):
            assert (await rpc(http, "initialize", INIT, token=token)).status_code == 401, token
        # Neither is the web app's cookie a way in.
        http.cookies.set("litwatch_token", TOKEN)
        assert (await rpc(http, "initialize", INIT, token=None)).status_code == 401
        http.cookies.clear()
        mcp = {"authorization": f"Bearer {MCP_TOKEN}"}
        for path in ("/api/status", "/api/papers", f"/api/papers/{P.id}"):
            assert (await http.get(path, headers=mcp)).status_code == 401, path
        assert (await http.get(f"/api/papers/{P.id}", headers=AUTH)).status_code == 200
        # The host check (R3): production's name answers, another gets the SDK's 421.
        assert (await rpc(http, "initialize", INIT, host="elsewhere.example")).status_code == 421
        # Stateless: no stream to open, no session to end.
        assert (await http.get("/mcp", headers=mcp)).status_code == 405
        tools = (await rpc(http, "tools/list")).json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == [
        "get_paper", "list_digests", "read_digest", "search_papers",
    ]  # fmt: skip
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


async def test_mcp_is_not_shadowed_by_the_web_app_and_absent_without_its_token(
    conn, tmp_path, monkeypatch
):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<p>shell</p>")
    monkeypatch.setattr(app_module, "WEB", tmp_path)
    await taxonomy_store.save(conn, TREE)
    async with serving(_app(conn)) as http:
        opened = (await rpc(http, "initialize", INIT)).json()
        assert opened["result"]["serverInfo"]["name"] == "litwatch"
        # The catch-all takes only GET: mounted after it, /mcp would serve the shell here.
        mcp = {"authorization": f"Bearer {MCP_TOKEN}"}
        assert (await http.get("/mcp", headers=mcp)).status_code == 405
        assert (await http.get("/papers")).text == "<p>shell</p>"
    without = create_app(_dsn(conn), TOKEN, Config(mcp={"allowed_hosts": [HOST]}))
    async with serving(without) as http:
        assert (await rpc(http, "initialize", INIT)).status_code == 405  # the web app's GET route
        assert (await http.get("/mcp")).text == "<p>shell</p>"


def test_the_mcp_token_must_be_its_own():
    for same in (TOKEN, READ_TOKEN):
        with pytest.raises(ValueError, match="must differ"):
            create_app("postgresql://unused", TOKEN, read_token=READ_TOKEN, mcp_token=same)


# -- V2: search is the web app's -------------------------------------------------------------------


@pytest.fixture
async def shelves(conn):
    """A Focus must read (P), an Area must read on a landmark, a Field adjacent paper, one
    fetched but not processed, and one only the archive holds (`ONLY`), which the fake archive's
    search finds beside P."""
    await taxonomy_store.save(conn, TREE)
    landmark = paper(
        "2610.00003",
        title="Hidden features at scale",
        abstract="Hidden dictionaries.",
        published="2026-10-02T00:00:00Z",
    )
    agents = paper("2610.00005", title="Agents hide tools", abstract="Hidden tool use.")
    unread = paper("2610.00006", title="Hidden and not read yet", abstract="Pending.")
    early = P.model_copy(update={"published": "2026-09-20T00:00:00Z"})
    await papers_store.upsert(conn, [early, Q, landmark, agents, unread])
    await classify(conn, P, FAITH.slug)
    await analyse(conn, P)
    await grade(conn, P, "central")
    await classify(conn, landmark, INTERP.slug)
    await analyse(conn, landmark, "landmark")
    await grade(conn, landmark, "related", INTERP)
    await classify(conn, agents, AGENTS.slug)
    await analyse(conn, agents, "incremental", code=True)
    await grade(conn, agents, "adjacent", AGENTS)
    fake = FakeArchive()
    hit = {"title": ONLY.title, "authors": ["Ada Lovelace"], "published": "2026-10-05"}
    fake.search_items = [{"id": ONLY.id, **hit}, {"id": P.id, "title": P.title}]
    item = {**ONLY.model_dump(mode="json"), "version_history": [{"version": 1}]}
    fake.records[ONLY.id] = {"id": ONLY.id, "latest_version": 1, "metadata": {}, "item": item}
    return fake, landmark, agents, unread


async def test_search_papers_answers_as_the_web_apps_search(conn, shelves):
    fake, *_ = shelves
    cases = [
        ({"q": "hidden"}, {"query": "hidden"}),
        ({"q": "hidden", "sort": "newest"}, {"query": "hidden", "sort": "newest"}),
        ({"q": "hidden", "limit": 2, "offset": 2}, {"query": "hidden", "limit": 2, "offset": 2}),
        ({}, {}),
    ]
    async with serving(_app(conn, fake)) as http:
        for web_params, arguments in cases:
            asked = {"limit": 20, **web_params}
            web = (await http.get("/api/papers", params=asked, headers=AUTH)).json()
            answer = await ok(http, "search_papers", **arguments)
            expected = [r["id"] for r in web["rows"]] + [r["id"] for r in web["outside"]]
            assert ids(answer) == expected, web_params
            assert f"{web['total']} papers match" in answer
        # The archive's hit beside the collection's, P once, as the collection's.
        found = ids(await ok(http, "search_papers", query="hidden"))
    assert found[-1] == ONLY.id and found.count(P.id) == 1 and len(found) == 5


async def test_each_litwatch_filter_keeps_exactly_the_papers_it_names(conn, shelves):
    fake, landmark, agents, _ = shelves
    async with serving(_app(conn, fake)) as http:
        thumbs = [(landmark, {"topic": INTERP.slug, "value": "up"}), (agents, {"value": "down"})]
        for p, thumb in thumbs:
            answer = await http.put(f"/api/reactions/{p.id}", json=thumb, headers=AUTH)
            assert answer.status_code == 200

        async def found(**arguments) -> set[str]:
            return set(ids(await ok(http, "search_papers", query="hidden", **arguments)))

        searched = len(fake.searches)
        assert await found(reaction="up") == {landmark.id}
        assert await found(reaction="down") == {agents.id}
        assert await found(processed=True) == {P.id, landmark.id, agents.id}
        assert await found(topic=INTERP.slug) == {P.id, landmark.id}  # its subtree's too
        assert await found(weight="landmark") == {landmark.id}
        # A minimum grade keeps the grades at or above it.
        assert await found(min_grade="must_read") == {P.id, landmark.id}
        assert await found(min_grade="adjacent") == {P.id, landmark.id, agents.id}
        # Any litwatch filter searched the collection alone.
        assert len(fake.searches) == searched
        # A date range applies to the collection and to the archive.
        dated = ids(await ok(http, "search_papers", query="hidden", since="2026-10-01"))
        assert dated == [landmark.id, ONLY.id]
        assert fake.searches[-1]["date_from"] == "2026-10-01"
        unknown, failed = await call(http, "search_papers", topic="nope")
    assert not failed and "No followed topic 'nope'" in unknown and INTERP.slug in unknown


# -- V3: nothing is written ------------------------------------------------------------------------


async def _digest(conn, topic, period, day, papers=(), **fields) -> int:
    fields = {"gist": f"{topic.slug} gist.", "text": f"{topic.slug} text.", **fields}
    outcome = digests_store.Outcome(
        topic.slug, period, day, DigestState.WRITTEN, papers=list(papers), **fields
    )
    return await digests_store.record(conn, outcome, None)


def _shown(p, role: DigestRole, *, why=None, version=None, fit="central", grade="must_read"):
    due = digests_store.Due(
        paper_id=p.id,
        revision=1,
        title=p.title,
        announced=None,
        gist=f"Gist of {p.title}.",
        fit=fit,
        grade=grade,
        rarity="epic",
        graded_on=FAITH.slug,
        fit_reason=None,
        brief=None,
        version=version,
    )
    return digests_store.Shown(due=due, role=role, why=why)


async def test_no_tool_writes_and_its_only_effect_is_reading_the_archive(conn, shelves):
    fake, *_ = shelves
    digest = await _digest(
        conn, FAITH, DigestPeriod.DAY, date(2026, 10, 14), [_shown(P, DigestRole.PICK)]
    )
    async with await mcp_api.connect(_dsn(conn)) as c:
        assert (await papers_store.get(c, P.id)).id == P.id
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await papers_store.upsert(c, [paper("2610.09998")])
    outbox = "SELECT count(*) AS n, max(id) AS last FROM outbox"
    before = await (await conn.execute(outbox)).fetchone()
    seen: list[tuple[str, str]] = []
    async with serving(_app(conn, fake, seen)) as http:
        await ok(http, "search_papers", query="hidden")
        await ok(http, "get_paper", paper_id=P.id)
        await ok(http, "get_paper", paper_id=ONLY.id)  # only the archive's
        await ok(http, "list_digests", since="2026-10-01", until="2026-10-20")
        await ok(http, "read_digest", digest_id=digest)
    assert await (await conn.execute(outbox)).fetchone() == before
    assert await papers_store.get(conn, ONLY.id) is None
    assert ("GET", "/v1/search") in seen and ("GET", f"/v1/papers/{ONLY.id}") in seen
    assert {method for method, _ in seen} == {"GET"}  # no job asked for, no record written


# -- V4: a paper and a digest in full --------------------------------------------------------------

CARD = {
    "summary": "Probes read the hidden steps.",
    "key_claim": "Probes predict unfaithful reasoning.",
    "evaluation_setup": {"models": ["Qwen3-8B"]},
    "code_available": True,
}


async def test_a_paper_carries_what_litwatch_made_of_it_and_its_citation(conn, shelves):
    fake, *_ = shelves
    done = await (
        await conn.execute(
            "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, state, "
            "priority) VALUES (%s, 1, 'brief', 'focus', 'done', 0) RETURNING id",
            (P.id,),
        )
    ).fetchone()
    await conn.execute(
        "INSERT INTO briefs (paper_id, paper_revision, pass_id, text, text_source, text_hash, "
        "model) VALUES (%s, 1, %s, 'The Brief: probes on three tasks.', 'latex', 'h', 'm')",
        (P.id, done["id"]),
    )
    await integrations_store.put_card(
        conn, P.id, CARD, "deep-model", {}, [], revision=1, version=2, source="latex", pass_id=None
    )
    published = Publication(
        dblp_key="conf/nips/Smith26",
        title=P.title,
        authors=["Alice Smith"],
        kind="inproceedings",
        venue="NeurIPS",
        container="Advances in Neural Information Processing Systems",
        year=2026,
    )
    await lookups.store_record(conn, P.id, published)
    async with serving(_app(conn, fake)) as http:
        for path, body in [
            (f"/api/labels/{FAITH.slug}/{P.id}", {"value": "related"}),
            (f"/api/reactions/{P.id}", {"topic": FAITH.slug, "value": "up"}),
        ]:
            assert (await http.put(path, json=body, headers=AUTH)).status_code == 200
        held = await ok(http, "get_paper", paper_id=f"https://arxiv.org/abs/{P.id}v1")
        only = await ok(http, "get_paper", paper_id=ONLY.id)
        missing = await call(http, "get_paper", paper_id="2610.05555")
        no_id = await call(http, "get_paper", paper_id="the SAE paper")
    assert held.startswith(f"# {P.title}\narXiv:{P.id}")
    assert "## Gist (from the abstract)\nProbes find hidden steps." in held
    assert "The Brief: probes on three tasks." in held and "Hidden computation in CoT." in held
    assert "- Key claim: Probes predict unfaithful reasoning." in held
    assert "  - Models:\n    - Qwen3-8B" in held and "- Code available: yes" in held
    assert "Weight: solid (from the abstract): Sound work." in held
    faith = next(line for line in held.splitlines() if line.startswith("- **Chain-of-thought"))
    assert "verdict central" in faith and "the owner's word: related" in faith
    assert "labelled related" in faith and "thumb up" in faith and "grade " in faith
    assert "@inproceedings{" in held and "NeurIPS" in held and "Published: NeurIPS 2026" in held
    assert "Only in the archive" in only and "Ada Lovelace" in only and ONLY.id in only
    assert "```bibtex\n@" in only
    assert missing == ("Neither litwatch nor the archive has a paper 2610.05555.", False)
    assert no_id[1] is False and "No arXiv id" in no_id[0]


async def test_a_paper_the_archive_cannot_be_asked_for_is_an_error_not_a_missing_paper(conn):
    await taxonomy_store.save(conn, TREE)
    flaky = Flaky()
    flaky.down = True
    async with serving(_app(conn, flaky)) as http:
        text, failed = await call(http, "get_paper", paper_id="2610.05555")
    assert failed and "The archive did not answer" in text


async def test_digests_list_by_topic_and_read_in_full(conn, shelves):
    _, landmark, agents, _ = shelves
    day = date(2026, 10, 14)
    text = f"[{P.title}]({P.id}) shows the hidden steps."
    picks = [
        _shown(P, DigestRole.PICK, why="It detects the omission."),
        _shown(landmark, DigestRole.LISTED, fit="related", grade="relevant"),
        _shown(agents, DigestRole.UPDATED, version=3),
    ]
    counts = {"papers": 2, "picks": 1, "central": 1, "related": 1, "must_read": 1, "updated": 1}
    faith = await _digest(
        conn, FAITH, DigestPeriod.DAY, day, picks, gist="Probes, today.", text=text, counts=counts
    )
    interp = await _digest(conn, INTERP, DigestPeriod.DAY, day, gist="Interpretability, today.")
    monday = day - timedelta(days=day.weekday())
    weekly = await _digest(conn, AGENTS, DigestPeriod.WEEK, monday, gist="Agents, this week.")
    async with serving(_app(conn)) as http:
        listed = await ok(
            http, "list_digests", topic=FAITH.slug, since="2026-10-01", until="2026-10-20"
        )
        every = await ok(http, "list_digests", since="2026-10-13", until="2026-10-20")
        before = await ok(http, "list_digests", since="2026-10-01", until="2026-10-05")
        unknown = await ok(http, "list_digests", topic="nope")
        full = await ok(http, "read_digest", digest_id=faith)
        missing = await ok(http, "read_digest", digest_id=faith + 1000)
    assert f"digest {faith} · 2026-10-14" in listed and "Probes, today." in listed
    assert f"digest {interp}" not in listed and f"digest {weekly}" not in listed
    for found in (faith, interp, weekly):
        assert f"digest {found} " in every
    assert every.index(f"digest {weekly} ") > every.index("## Weekly")
    assert "No digests in this range." in before
    assert "No followed topic 'nope'" in unknown
    assert full.startswith(f"# Chain-of-thought faithfulness: daily digest of {day}")
    assert f"## Text\n{text}" in full and "## Gist\nProbes, today." in full
    sections = [line for line in full.splitlines() if line.startswith("## ")]
    assert sections[2:] == ["## Picks", "## Listed", "## Updated: back with a new version"]
    assert "Why: It detects the omission." in full
    assert f"**{agents.title}** · arXiv:{agents.id} · grade must read; Fit central; v3" in full
    assert missing == f"No digest {faith + 1000} of a followed topic."


# -- V9: what the monitor sees ---------------------------------------------------------------------


async def test_the_monitor_counts_calls_and_degrades_mcp_only_while_calls_fail(conn, caplog):
    await taxonomy_store.save(conn, TREE)
    caplog.set_level(logging.INFO, logger="litwatch.api.mcp")
    flaky = Flaky()
    app = _app(conn, flaky)
    monitor = httpx.AsyncClient(
        transport=httpx.ASGITransport(create_monitor(app)), base_url="http://m"
    )

    async def mcp_status() -> dict:
        found = (await monitor.get("/status")).json()["components"]
        return next(c for c in found if c["id"] == "mcp")

    async with serving(app) as http, monitor:
        assert (await mcp_status())["status"] == "healthy"
        assert (await call(http, "get_paper", paper_id="2610.05555"))[1] is False  # not found
        assert (await mcp_status())["status"] == "healthy"
        flaky.down = True
        assert (await call(http, "get_paper", paper_id="2610.05555"))[1] is True
        degraded = await mcp_status()
        assert degraded["status"] == "degraded"
        assert degraded["reason"].startswith("1 calls failed in the last hour (get_paper 1)")
        flaky.down = False
        await ok(http, "search_papers", query="secret words")
        assert (await mcp_status())["status"] == "healthy"
        metrics = (await monitor.get("/metrics")).text
    for line in [
        'litwatch_mcp_calls_total{tool="get_paper",outcome="not_found"} 1',
        'litwatch_mcp_calls_total{tool="get_paper",outcome="error"} 1',
        'litwatch_mcp_calls_total{tool="search_papers",outcome="ok"} 1',
        'litwatch_mcp_calls_total{tool="read_digest",outcome="ok"} 0',
        'litwatch_mcp_call_seconds_count{tool="get_paper"} 2',
    ]:
        assert line in metrics.splitlines(), line
    assert "# TYPE litwatch_mcp_call_seconds summary" in metrics
    # One line per call: tool, outcome, duration, never the arguments.
    lines = [r for r in caplog.records if r.name == "litwatch.api.mcp"]
    assert [(r.fields["tool"], r.fields["outcome"]) for r in lines] == [
        ("get_paper", "not_found"), ("get_paper", "error"), ("search_papers", "ok"),
    ]  # fmt: skip
    assert not any("secret" in r.getMessage() or "secret" in str(r.fields) for r in lines)


async def test_an_mcp_without_its_token_has_no_component_or_metrics(conn):
    app = create_app(_dsn(conn), TOKEN)
    transport = httpx.ASGITransport(create_monitor(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://m") as monitor:
        components = (await monitor.get("/status")).json()["components"]
        assert "mcp" not in {c["id"] for c in components}
        assert "litwatch_mcp" not in (await monitor.get("/metrics")).text


def test_a_failure_older_than_the_window_no_longer_degrades():
    calls = McpCalls(tools=("get_paper",))
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    calls.record("get_paper", "error", 0.1, now - MCP_WINDOW - timedelta(minutes=1))
    assert calls.component(now)["status"] == "healthy"
    calls.record("get_paper", "error", 0.1, now - timedelta(minutes=5))
    calls.record("get_paper", "error", 0.1, now - timedelta(minutes=4))
    assert "2 calls failed" in calls.component(now)["reason"]
    calls.record("get_paper", "not_found", 0.1, now - timedelta(minutes=3))
    assert calls.component(now)["status"] == "healthy"
