"""A Brief over a paper's full text: the text the archive gives (`BODY`), the brief role's
answer (`ANSWER`), and the paper analysed from its abstract before it (`analysed`)."""

from litwatch.core import (
    Analysis,
    Cause,
    Contribution,
    Facets,
    Weight,
)
from litwatch.store import analyses as analyses_store
from litwatch.store import papers as papers_store
from tests import settling
from tests.conftest import paper
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
