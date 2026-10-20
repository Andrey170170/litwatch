"""The MCP server at /mcp (docs/plans/mcp-server.md, V1–V4 and V9): its token and mount, search
against the web app's, that nothing is written, what a paper and a digest carry, and what the
monitor sees. Every call goes through `create_app` over HTTP, as an agent's does."""

import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import httpx
import httpx2
import psycopg
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from litwatch.api import app as app_module
from litwatch.api import create_app
from litwatch.api import mcp as mcp_api
from litwatch.api.monitor import MCP_WINDOW, McpCalls, create_monitor
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import Publication, Taxonomy
from litwatch.core.digests import DigestPeriod, DigestRole, DigestState
from litwatch.integrations.archive import Archive
from litwatch.jobs import publications as lookups
from litwatch.store import digests as digests_store
from litwatch.store import integrations as integrations_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import AGENTS, FAITH, INTERP, TREE, paper
from tests.fakes_http import FakeArchive, mock
from tests.test_archive_text import a_pdf
from tests.test_reading import P, Q, analyse, classify, grade

TOKEN, READ_TOKEN, MCP_TOKEN = "s3cret", "r3ad", "m-token"
HOST = "litwatch.andrei-homelab.com"  # production's name, which the SDK's host check must let in
AUTH = {"authorization": f"Bearer {TOKEN}"}
ONLY = paper("2610.07777", title="Hidden steps in the archive", authors=["Ada Lovelace"])
SEVEN = [
    "get_paper", "list_digests", "paper_outline", "read_digest", "read_paper", "search_papers",
    "view_pdf_page",
]  # fmt: skip
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
    assert sorted(t["name"] for t in tools) == SEVEN
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


async def test_the_sdks_own_client_initializes_lists_and_calls_through_create_app(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P])
    app = _app(conn)
    headers = {"authorization": f"Bearer {MCP_TOKEN}"}
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with (
            httpx2.AsyncClient(transport=transport, headers=headers) as http,
            streamable_http_client(f"http://{HOST}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            opened = await session.initialize()
            tools = await session.list_tools()
            found = await session.call_tool("search_papers", {"query": "hide"})
    assert opened.server_info.name == "litwatch" and "before the web" in opened.instructions
    assert sorted(t.name for t in tools.tools) == SEVEN
    assert "call paper_outline first" in opened.instructions
    assert not found.is_error and ids(found.content[0].text) == [P.id]


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
    """V3, for all seven tools: the only effect is the archive's, asked for a paper's files."""
    fake, *_ = shelves
    fake.texts[P.id] = (None, "\\section{Probes} Hidden steps.")  # its historical LaTeX
    fake.records[ONLY.id]["item"]["version"] = 1
    fake.pdfs[ONLY.id] = a_pdf("Figure 1: Hidden steps.")
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
        found = len(seen)
        assert "1. Probes" in await ok(http, "paper_outline", paper_id=P.id)
        assert "Hidden steps." in await ok(http, "read_paper", paper_id=P.id, section=1)
        assert "Hidden steps." in await ok(http, "read_paper", paper_id=ONLY.id)  # its PDF's
        arguments = {"paper_id": ONLY.id, "page": 1}
        shown = await rpc(http, "tools/call", {"name": "view_pdf_page", "arguments": arguments})
        assert not shown.json()["result"]["isError"]
    assert await (await conn.execute(outbox)).fetchone() == before
    assert await papers_store.get(conn, ONLY.id) is None
    assert ("GET", "/v1/search") in seen and ("GET", f"/v1/papers/{ONLY.id}") in seen
    assert {method for method, _ in seen[:found]} == {"GET"}  # the first four only read
    # Reading asks the archive for a paper's files, its one effect; no record is written.
    asked = [path for method, path in seen if method != "GET"]
    assert asked and set(asked) == {"/v1/jobs"}
    assert {(j["paper_id"], j["kind"], j["priority"]) for j in fake.jobs} == {
        (ONLY.id, "pdf", 0), (ONLY.id, "source", 0),
    }  # fmt: skip


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
        conn, P.id, CARD, "deep-model", {}, revision=1, version=2, source="latex", pass_id=None
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
    topics, owners = held.split("## Topics\n")[1].split("## The owner's word and reactions\n")
    faith = next(line for line in topics.splitlines() if line.startswith("- **Chain-of-thought"))
    assert "verdict central" in faith and "grade " in faith
    said = owners.split("\n\n")[0].splitlines()
    assert len(said) == 1 and said[0].startswith(
        f"- **Chain-of-thought faithfulness** ({FAITH.slug}): the owner's word: related; "
    )
    assert sorted(x.rsplit(", ", 1)[0] for x in said[0].split(": ", 1)[1].split("; ")[1:]) == [
        "labelled related",
        "thumb up",
    ]
    assert "@inproceedings{" in held and "NeurIPS" in held and "Published: NeurIPS 2026" in held
    assert "Only in the archive" in only and "Ada Lovelace" in only and ONLY.id in only
    assert "```bibtex\n@" in only
    assert missing == ("Neither litwatch nor the archive has a paper 2610.05555.", False)
    assert no_id[1] is False and "No arXiv id" in no_id[0]


async def test_a_deep_card_from_an_older_text_says_which_text_it_read(conn, shelves):
    fake, *_ = shelves
    await conn.execute("UPDATE papers SET version = 1, revision = 1 WHERE id = %s", (P.id,))
    await integrations_store.put_card(
        conn, P.id, CARD, "deep-model", {}, revision=1, version=1, source="latex", pass_id=None
    )
    async with serving(_app(conn, fake)) as http:
        current = await ok(http, "get_paper", paper_id=P.id)
        await conn.execute("UPDATE papers SET version = 3, revision = 2 WHERE id = %s", (P.id,))
        older = await ok(http, "get_paper", paper_id=P.id)
    for page in (current, older):
        assert "## Deep card (latex, read arXiv v1, revision 1" in page
    assert mcp_api.OLDER_CARD not in current and mcp_api.OLDER_CARD in older


def _owners(page: str) -> dict[str, list[str]]:
    """The owner's section, by line head: each record without its date, sorted."""
    section = page.split("## The owner's word and reactions\n")[1].split("\n\n")[0]
    return {
        head: sorted(x.rsplit(", ", 1)[0] for x in said.split("; "))
        for head, said in (line.split(": ", 1) for line in section.splitlines())
    }


async def test_the_owners_records_on_a_topic_no_longer_followed_all_still_show(conn, shelves):
    fake, landmark, _, _ = shelves
    async with serving(_app(conn, fake)) as http:
        for method, path, body in [
            ("PUT", f"/api/reactions/{landmark.id}", {"topic": FAITH.slug, "value": "up",
                                                      "reason": "great_find"}),
            ("PUT", f"/api/reactions/{landmark.id}", {"value": "down", "reason": "already_knew"}),
            ("POST", "/api/misses", {"paper_id": landmark.id, "topic": FAITH.slug}),
            ("PUT", f"/api/labels/{FAITH.slug}/{landmark.id}", {"value": "central"}),
        ]:  # fmt: skip
            answer = await http.request(method, path, json=body, headers=AUTH)
            assert answer.status_code == 200, answer.text
        followed = _owners(await ok(http, "get_paper", paper_id=landmark.id))
    await taxonomy_store.save(conn, Taxonomy(classes=TREE.classes, topics=[INTERP, AGENTS]))
    async with serving(_app(conn, fake)) as http:
        assert landmark.id in ids(await ok(http, "search_papers", reaction="up"))
        unfollowed = _owners(await ok(http, "get_paper", paper_id=landmark.id))
    records = ["labelled central", "reported as a miss", "thumb up (great find)"]
    on_no_topic = ["thumb down (already knew)"]
    assert followed == {
        f"- **Chain-of-thought faithfulness** ({FAITH.slug})": [
            *records[:2], "the owner's word: central", records[2]
        ],
        "- On no topic": on_no_topic,
    }  # fmt: skip
    assert unfollowed == {
        f"- **Chain-of-thought faithfulness** ({FAITH.slug}, no longer followed)": records,
        "- On no topic": on_no_topic,
    }


async def test_the_library_says_whether_the_owner_or_the_grades_chose(conn, shelves):
    fake, _, _, unread = shelves
    async with serving(_app(conn, fake)) as http:
        by_grades = await ok(http, "get_paper", paper_id=P.id)
        for paper_id, saved in [(unread.id, True), (P.id, False)]:
            answer = await http.put(f"/api/library/{paper_id}", json={"saved": saved}, headers=AUTH)
            assert answer.status_code == 200, answer.text
        saved = await ok(http, "get_paper", paper_id=unread.id)
        unsaved = await ok(http, "get_paper", paper_id=P.id)
        only = await ok(http, "get_paper", paper_id=ONLY.id)
    assert "Saved in the owner's library: saved automatically, by its grades." in by_grades
    assert (
        "Fetched by litwatch, not processed yet.\nSaved in the owner's library: saved by the owner."
        in saved
    )
    assert "Not in the owner's library: unsaved by the owner." in unsaved
    assert "library" not in only


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
        # The archive's search left out: an answer that names it, neither clearing nor failing.
        partial = await ok(http, "search_papers", query="secret words")
        assert "Not searched, as they did not answer: archive" in partial
        assert (await mcp_status())["status"] == "degraded"
        flaky.down = False
        await ok(http, "search_papers", query="secret words")
        assert (await mcp_status())["status"] == "healthy"
        metrics = (await monitor.get("/metrics")).text
    for line in [
        'litwatch_mcp_calls_total{tool="get_paper",outcome="not_found"} 1',
        'litwatch_mcp_calls_total{tool="get_paper",outcome="error"} 1',
        'litwatch_mcp_calls_total{tool="search_papers",outcome="ok"} 1',
        'litwatch_mcp_calls_total{tool="search_papers",outcome="partial"} 1',
        'litwatch_mcp_calls_total{tool="read_digest",outcome="ok"} 0',
        'litwatch_mcp_call_seconds_count{tool="get_paper"} 2',
    ]:
        assert line in metrics.splitlines(), line
    assert "# TYPE litwatch_mcp_call_seconds summary" in metrics
    # One line per call: tool, outcome, duration, never the arguments.
    lines = [r for r in caplog.records if r.name == "litwatch.api.mcp"]
    assert [(r.fields["tool"], r.fields["outcome"]) for r in lines] == [
        ("get_paper", "not_found"), ("get_paper", "error"), ("search_papers", "partial"),
        ("search_papers", "ok"),
    ]  # fmt: skip
    assert not any("secret" in r.getMessage() or "secret" in str(r.fields) for r in lines)


async def test_a_date_out_of_range_is_an_argument_error_not_a_server_error(conn):
    await taxonomy_store.save(conn, TREE)
    app = _app(conn)
    transport = httpx.ASGITransport(create_monitor(app))
    async with (
        serving(app) as http,
        httpx.AsyncClient(transport=transport, base_url="http://m") as m,
    ):
        for tool, arguments in [
            ("list_digests", {"until": "0001-01-01"}),
            ("list_digests", {"since": "1990-12-31", "until": "2026-10-20"}),
            ("search_papers", {"until": "9999-12-31"}),
            ("search_papers", {"since": "2101-01-01"}),
        ]:
            text, failed = await call(http, tool, **arguments)
            assert failed and "validation error" in text, (tool, arguments, text)
        component = [c for c in (await m.get("/status")).json()["components"] if c["id"] == "mcp"]
        assert component[0]["status"] == "healthy"
        assert 'outcome="error"} 1' not in (await m.get("/metrics")).text
        # The bounds themselves are days a tool takes.
        bounds = await ok(http, "list_digests", since="1991-01-01", until="2100-12-31")
        assert "(The range is cut to a 92-day span, ending on its last day.)" in bounds


async def test_arguments_no_query_can_take_are_refused_and_not_counted_as_failures(conn):
    """An offset past bigint, U+0000 (which PostgreSQL cannot hold) and a range ending before
    it starts are the caller's mistakes: errors before any query, never server failures."""
    await taxonomy_store.save(conn, TREE)
    weekly = await _digest(conn, AGENTS, DigestPeriod.WEEK, date(2026, 10, 12))
    app = _app(conn)
    transport = httpx.ASGITransport(create_monitor(app))
    async with (
        serving(app) as http,
        httpx.AsyncClient(transport=transport, base_url="http://m") as m,
    ):
        for tool, arguments in [
            ("search_papers", {"query": "x", "offset": 9223372036854775808}),
            ("search_papers", {"query": "x", "offset": 10001}),
            ("search_papers", {"query": "\u0000"}),
            ("search_papers", {"topic": "a\u0000b"}),
            ("get_paper", {"paper_id": "2610.00001\u0000"}),
            ("list_digests", {"topic": "\u0000"}),
            ("list_digests", {"since": "2026-10-15", "until": "2026-10-14"}),
            ("search_papers", {"since": "2026-10-15", "until": "2026-10-14"}),
        ]:
            text, failed = await call(http, tool, **arguments)
            assert failed and f"digest {weekly}" not in text, (tool, arguments, text)
        component = [c for c in (await m.get("/status")).json()["components"] if c["id"] == "mcp"]
        assert component[0]["status"] == "healthy"
        assert re.search(r'outcome="error"} [1-9]', (await m.get("/metrics")).text) is None
        await ok(http, "search_papers", query="x", offset=10000)
        assert f"digest {weekly} " in await ok(
            http, "list_digests", since="2026-10-12", until="2026-10-12"
        )


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
    calls.record("search_papers", "partial", 0.1, now - timedelta(minutes=3))
    assert calls.component(now)["status"] == "degraded"
    calls.record("get_paper", "not_found", 0.1, now - timedelta(minutes=2))
    assert calls.component(now)["status"] == "healthy"
    calls.record("search_papers", "partial", 0.1, now - timedelta(minutes=1))
    assert calls.component(now)["status"] == "healthy"
