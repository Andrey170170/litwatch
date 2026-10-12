"""Published versions: DBLP matching, BibTeX from the record, lookup when due, the owner's
correction, and Copy BibTeX looking up only a single paper."""

import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Hand, Publication
from litwatch.feedback import word
from litwatch.jobs import publications as lookups
from litwatch.publications import SourceError, dblp
from litwatch.publications.bibtex import Cited, entries
from litwatch.store import papers as papers_store
from litwatch.store import publications as store
from litwatch.store import sibling as store_sibling
from tests.conftest import paper

TOKEN = "s3cret"
NIPS = Publication(
    dblp_key="conf/nips/VaswaniSPUJGKP17",
    title="Attention is All you Need",
    authors=["Ashish Vaswani", "Noam Shazeer"],
    kind="inproceedings",
    venue="NIPS",
    container="Advances in Neural Information Processing Systems 30.",
    pages="5998-6008",
    year=2017,
)
P = paper("1706.03762", title="Attention Is All You Need", authors=["Ashish Vaswani"])
Q = paper("2610.00002", title="Not yet published")


def _uri(v):
    return {"type": "uri", "value": v}


def _lit(v):
    return {"type": "literal", "value": v}


async def test_dblp_chooses_the_latest_record_outside_corr_and_reads_it():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)["query"][0]
        asked.append(request.headers["user-agent"])
        if "?rec ?year" in query:
            assert "10.48550/ARXIV.1706.03762" in query and '"abs/1706.03762"' in query
            # The paper's current arXiv title is matched too, normalized.
            assert '"attentionisallyouneed"' in query
            rows = [
                {"rec": _uri(dblp.REC + "conf/uist/Poster16"), "year": _lit("2016")},
                {"rec": _uri(dblp.REC + "conf/nips/VaswaniSPUJGKP17"), "year": _lit("2017")},
            ]
        elif "hasSignature" in query:
            rows = [
                {"n": _lit("1"), "name": _lit("Ashish Vaswani")},
                {"n": _lit("2"), "name": _lit("Wei Wang 0001")},
            ]
        else:
            assert "conf/nips/VaswaniSPUJGKP17" in query
            rows = [
                {
                    "title": _lit("Attention is All you Need."),
                    "type": _uri("http://purl.org/net/nknouf/ns/bibtex#Inproceedings"),
                    "venue": _lit("NIPS"),
                    "book": _lit("Advances in Neural Information Processing Systems 30"),
                    "pages": _lit("5998-6008"),
                    "year": _lit("2017"),
                    "doi": _uri("https://doi.org/10.5555/3295222"),
                }
            ]
        return httpx.Response(200, json={"results": {"bindings": rows}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    found = await dblp.Dblp(client, pause=0).find("1706.03762", "Attention Is All You Need")
    assert found.dblp_key == "conf/nips/VaswaniSPUJGKP17" and found.kind == "inproceedings"
    assert found.title == "Attention is All you Need" and found.doi == "10.5555/3295222"
    assert found.authors == ["Ashish Vaswani", "Wei Wang"]  # without DBLP's homonym number
    assert found.container == "Advances in Neural Information Processing Systems 30"
    assert all(ua.startswith("litwatch") for ua in asked)


async def test_a_doi_the_authors_gave_arxiv_names_the_record_directly():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)["query"][0]
        asked.append(query)
        if "LIMIT 1" in query and "dblp:doi" in query:
            rows = [{"rec": _uri(dblp.REC + "journals/jbi/Zong24")}]
        elif "hasSignature" in query:
            rows = []
        else:
            rows = [
                {"title": _lit("Advancing text mining."), "venue": _lit("J. Biomed. Informatics")}
            ]
        return httpx.Response(200, json={"results": {"bindings": rows}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    found = await dblp.Dblp(client, pause=0).find("2403.04261", None, "10.1016/j.jbi.2024.1")
    assert found.dblp_key == "journals/jbi/Zong24"
    assert not any("?rec ?year" in q for q in asked)  # no matching needed


def test_identifiers_are_checked_before_they_reach_a_query():
    with pytest.raises(ValueError):
        dblp._safe_id('1706.03762" } DROP')
    with pytest.raises(ValueError):
        dblp._safe_key("conf/nips/x> <y")
    assert dblp._safe_key("https://dblp.org/rec/conf/nips/X17.html") == "conf/nips/X17"
    assert dblp.clean_doi("https://doi.org/10.1145/3292500.3330701") == "10.1145/3292500.3330701"
    with pytest.raises(ValueError):
        dblp.clean_doi("not a doi")


def test_bibtex_cites_the_published_record_under_the_arxiv_key():
    cited = Cited(id=P.id, title=P.title, authors=P.authors, year=2017)
    arxiv = entries([cited])
    published = entries([cited], {P.id: NIPS})
    assert arxiv.split(",")[0] == "@misc{vaswani2017attention"
    assert published.split(",")[0] == "@inproceedings{vaswani2017attention"
    assert "booktitle = {Advances in Neural Information Processing Systems 30}" in published
    assert "pages = {5998--6008}" in published and "eprint = {1706.03762}" in published
    # A DOI the owner gave that DBLP does not know rides on the arXiv entry.
    doi_only = entries([cited], {P.id: Publication(doi="10.1/x")})
    assert doi_only.startswith("@misc{") and "doi = {10.1/x}" in doi_only


class Source:
    """A scripted PublicationSource."""

    def __init__(self, records=None):
        self.records = records or {}
        self.asked: list[str] = []
        self.down = False

    async def find(self, paper_id, title=None, doi=None):
        self.asked.append(paper_id)
        if self.down:
            raise SourceError("down")
        return self.records.get(paper_id)

    async def record(self, key):
        return next((r for r in self.records.values() if r.dblp_key == key), None)

    async def by_doi(self, doi):
        return None


@pytest.fixture
async def db(conn):
    await papers_store.upsert(conn, [P, Q])
    for p in (P, Q):
        await word.toggle(conn, p.id, Hand.SAVE)
    return conn


async def events(conn):
    rows = await (
        await conn.execute("SELECT paper_id FROM outbox WHERE type = 'publication.found'")
    ).fetchall()
    return [r["paper_id"] for r in rows]


async def test_saved_papers_are_looked_up_once_then_monthly_while_unpublished(db):
    source = Source({P.id: NIPS})
    counts = await lookups.run(db, source)
    assert counts == {"looked_up": 2, "found": 1, "failed": 0}
    assert await events(db) == [P.id]
    # Nothing is due until a month has passed, and a found record is never looked up again.
    assert await store.due(db, 10) == []
    await db.execute("UPDATE publications SET checked_at = now() - interval '31 days'")
    assert await store.due(db, 10) == [Q.id]
    # Two years after the first look and the save, an unpublished paper is left alone.
    await db.execute("UPDATE publications SET first_checked_at = now() - interval '731 days'")
    await db.execute("UPDATE library SET saved_at = now() - interval '731 days'")
    assert await store.due(db, 10) == []
    # Saved again years later, it is looked up at once, and monthly from that save.
    await db.execute("UPDATE library SET saved_at = now() WHERE paper_id = %s", (Q.id,))
    assert await store.due(db, 10) == [Q.id]
    # The same record found again is no new event.
    await lookups.look_up(db, source, P.id)
    assert await events(db) == [P.id]


async def test_a_run_stops_when_dblp_keeps_failing(db, monkeypatch):
    monkeypatch.setattr(lookups, "GIVE_UP_AFTER", 2)
    source = Source()
    source.down = True
    counts = await lookups.run(db, source)
    assert counts["failed"] == 2
    run = await (await db.execute("SELECT error FROM runs WHERE kind = 'publications'")).fetchone()
    assert "stopped after 2 failures" in run["error"]
    assert set(await store.due(db, 10)) == {P.id, Q.id}  # still due: nothing was stored


async def test_the_owners_choice_is_kept_and_copy_looks_up_only_one_paper(db):
    other = NIPS.model_copy(update={"dblp_key": "conf/iclr/Other18", "venue": "ICLR"})
    source = Source({P.id: NIPS, Q.id: other})
    dsn = f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    app = create_app(dsn, TOKEN, Config(), publications=source)
    headers = {"authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        # An export of several papers never waits on DBLP.
        text = (await c.post("/api/bibtex", json={"ids": [P.id, Q.id]})).text
        assert source.asked == [] and text.count("@misc{") == 2
        # Copying one paper looks it up first, and the entry cites the published version.
        text = (await c.post("/api/bibtex", json={"ids": [P.id]})).text
        assert source.asked == [P.id] and text.startswith("@inproceedings{")
        page = (await c.get(f"/api/papers/{P.id}")).json()
        assert page["publication"]["venue"] == "NIPS" and not page["publication_by_owner"]

        # The owner says Q's published version is P's record; a lookup does not replace it.
        r = await c.put(f"/api/papers/{Q.id}/publication", json={"dblp_key": NIPS.dblp_key})
        assert r.status_code == 200 and r.json()["venue"] == "NIPS"
        await lookups.look_up(db, source, Q.id)
        page = (await c.get(f"/api/papers/{Q.id}")).json()
        assert page["publication"]["venue"] == "NIPS" and page["publication_by_owner"]
        assert await store.due(db, 10) == []
        # "None" is a choice too; forgetting it makes the paper due again.
        r = await c.put(f"/api/papers/{Q.id}/publication", json={"none": True})
        assert r.status_code == 200 and r.json() is None
        assert (await c.post("/api/bibtex", json={"ids": [Q.id]})).text.startswith("@misc{")
        assert (await c.delete(f"/api/papers/{Q.id}/publication")).status_code == 204
        assert await store.due(db, 10) == [Q.id]
        # Exactly one of key, DOI or none; an unknown key is a 404.
        bad = await c.put(f"/api/papers/{Q.id}/publication", json={"doi": "10.1/x", "none": True})
        assert bad.status_code == 422
        missing = await c.put(f"/api/papers/{Q.id}/publication", json={"dblp_key": "conf/x/Y1"})
        assert missing.status_code == 404
    assert (await events(db)).count(Q.id) == 1


async def test_two_first_lookups_at_once_announce_the_record_once(db):
    async with await store_sibling(db) as other:
        await asyncio.gather(
            lookups.store_record(db, P.id, NIPS), lookups.store_record(other, P.id, NIPS)
        )
    assert await events(db) == [P.id]


async def test_copy_gives_what_is_stored_when_dblp_is_slow(db, monkeypatch):
    monkeypatch.setattr(lookups, "COPY_DEADLINE", 0.05)

    class Stalled(Source):
        async def find(self, paper_id, title=None, doi=None):
            await asyncio.sleep(5)

    async with asyncio.timeout(1):  # far short of the stalled lookup
        assert await lookups.for_copy(db, Stalled(), P.id) is None  # nothing stored: arXiv
    assert await store.get(db, P.id) is None  # an unanswered lookup stores nothing
