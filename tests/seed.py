"""A paper's records as the pipeline steps would write them, through the store: its
classification, its analysis and a verdict, each settled; and the two papers the reading tests
seed."""

from litwatch.core import Analysis, Cause, Facets, Fit, Opinion, Verdict, Weight
from litwatch.core.classification import Membership, Source
from litwatch.store import analyses as analyses_store
from litwatch.store import classifications as classifications_store
from litwatch.store import papers as papers_store
from litwatch.store import verdicts
from tests import settling
from tests.conftest import FAITH, TREE, paper
from tests.sessions import Session, in_read, in_write

P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation in CoT.")
Q = paper("2610.00002", title="A near neighbour", abstract="Sparse features.")
VERSIONS = {c.slug: c.version for c in TREE.classes}


async def classify(conn: Session, p, *classes, keyword=()) -> None:
    """A classification: the first class primary, each a classifier membership; `keyword`
    Focus classes joined by the keyword check alone."""
    primary = classes[0] if classes else "other"
    await in_write(conn, classifications_store.add, p, TREE.classes_version, primary, None, "test")
    found = [
        Membership(slug=c, source=Source.CLASSIFIER) for c in dict.fromkeys([primary, *classes])
    ]
    found += [Membership(slug=k, source=Source.KEYWORD) for k in keyword]
    await in_write(conn, classifications_store.replace, p, found, VERSIONS)
    await settling.settle(conn, p.id, cause=Cause.VERDICT)


async def analyse(
    conn: Session, p, weight="solid", *, code=False, gist="Probes find hidden steps."
) -> None:
    facets = Facets(contribution="method", releases_code=code, releases_weights=False)
    found = Analysis(gist=gist, weight=Weight(weight), weight_reason="Sound work.", facets=facets)
    await in_write(conn, analyses_store.add, p, "other", found, "test")
    await settling.settle(conn, p.id, cause=Cause.VERDICT)


async def grade(conn: Session, p, fit: str, topic=FAITH, *, check=None, escalated=False) -> None:
    """A verdict of `fit` on `topic`, on the paper's current text; `check` a second grader's
    differing Fit (a split)."""
    opinions = {"judge": Opinion(fit=Fit(fit), reason="judge's reason", model="anthropic:x")}
    if check or topic is FAITH:
        opinions["check"] = Opinion(fit=Fit(check or fit), reason="check's", model="openrouter:y")
    await in_write(
        conn,
        verdicts.add,
        Verdict(
            paper_id=p.id,
            topic=topic.slug,
            topic_version=topic.version,
            paper_revision=await in_read(conn, papers_store.revision, p.id),
            fit=Fit(fit),
            reason="judge's reason",
            opinions=opinions,
            split=check is not None,
            escalated=escalated,
        ),
    )
    await settling.settle(conn, p.id, cause=Cause.VERDICT)
