"""A Brief over a paper's full text: `P` and the text the archive gives for it (`BODY`,
`ready`), the brief role scripted (`Writer`, `ANSWER`), `P` analysed from its abstract and
graded before it (`analysed`, `graded`), and the full-text work on a test's session, its
passes and the app reading them (`work`, `passes`, `client`)."""

import json

from litwatch import fulltext
from litwatch.api import create_app
from litwatch.archive import ArchiveClient
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import (
    Analysis,
    Cause,
    Contribution,
    Facets,
    Fit,
    Verdict,
    Weight,
)
from litwatch.llm import Gateway
from litwatch.store import analyses as analyses_store
from litwatch.store import papers as papers_store
from litwatch.store import verdicts
from tests import settling
from tests.conftest import FAITH, AppClient, gate_on, paper
from tests.fakes import ScriptedProvider
from tests.fakes_http import FakeArchive, mock
from tests.sessions import Session, in_read, in_write

P = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes.", version=1)
BODY = "\\section{Method} We train a linear probe on every layer and read hidden steps. " * 40
GIST = (
    "Linear probes on every layer recover hidden reasoning steps, and traces omit about a third "
    "of them on four tasks."
)
BRIEF = " ".join(["Probes trained on every layer recover the hidden steps of a computation."] * 11)
ANSWER = {
    "overstatement": None,
    "brief": BRIEF,
    "gist": GIST,
    "weight_reason": "A careful analysis with a fair comparison.",
    "weight": "landmark",
}


async def analysed(conn: Session, weight: str, paper_id: str = P.id) -> None:
    found = Analysis(
        gist="An abstract Gist of the paper.",
        weight=Weight(weight),
        weight_reason="From the abstract.",
        facets=Facets(
            contribution=Contribution.ANALYSIS, releases_code=False, releases_weights=False
        ),
    )
    current = await in_read(conn, papers_store.get, paper_id)
    await in_write(conn, analyses_store.add, current, "interpretability", found, "m")
    await settling.settle(conn, paper_id, cause=Cause.VERDICT)


BRIEF_ROLE = RoleConfig(provider="anthropic", model="test-brief")
CONFIG = Config(models=Models(brief=BRIEF_ROLE))
TOKEN = "t"


class Writer:
    """The `brief` role, scripted: `answers` in turn (an exception is raised), then ANSWER."""

    def __init__(self) -> None:
        self.answers: list[dict | Exception | str] = []
        self.asked: list[str] = []  # each request's user message

    def respond(self, role: RoleConfig, request) -> str:
        assert role.model == BRIEF_ROLE.model
        self.asked.append(request.user)
        answer = self.answers.pop(0) if self.answers else ANSWER
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)

    def gateway(self, config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))


def archive(fake: FakeArchive) -> ArchiveClient:
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))


def client(conn: Session) -> tuple:
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, CONFIG)
    http = AppClient(
        app,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    return app, http


async def graded(conn: Session, fit: str, topic: str = FAITH.slug) -> None:
    await in_write(
        conn,
        verdicts.add,
        Verdict(
            paper_id=P.id, topic=topic, topic_version="v", fit=Fit(fit), reason="r", opinions={}
        ),
    )
    await settling.settle(conn, P.id, cause=Cause.VERDICT)


async def passes(conn: Session) -> list[dict]:
    """P's text passes; its Deep card passes are tests/test_deep.py's."""
    return await (
        await conn.execute(
            "SELECT * FROM fulltext_passes WHERE paper_id = %s AND kind <> 'deep' ORDER BY id",
            (P.id,),
        )
    ).fetchall()


async def work(conn: Session, fake: FakeArchive, writer: Writer, **kwargs):
    async with gate_on(conn) as db:
        return await fulltext.work(db, CONFIG, archive(fake), gateway=writer.gateway, **kwargs)


def ready(fake: FakeArchive, version: int = 1) -> FakeArchive:
    fake.texts[P.id] = (version, BODY)
    return fake
