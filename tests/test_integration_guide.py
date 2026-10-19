"""The worked example in `docs/integrations.md`, taken from the page as written, loaded by
dotted path like any integration kept outside litwatch, and run through the dispatcher."""

import re
import sys
from pathlib import Path

import httpx
import pytest

from litwatch import integrations
from litwatch.config import Config
from litwatch.core import Cause, Fit, Hand, Opinion, Verdict
from litwatch.events import Integration
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TREE, deliver, paper, start

GUIDE = Path(__file__).parent.parent / "docs" / "integrations.md"
P = paper("2610.00001", title="Traces that hide steps")


@pytest.fixture
def example(tmp_path, monkeypatch):
    """The guide's first Python block after its worked-example heading, as a module."""
    worked = GUIDE.read_text().split("## Worked example", 1)[1]
    code = re.search(r"```python\n(.*?)```", worked, re.S).group(1)
    (tmp_path / "my_litwatch_ntfy.py").write_text(code)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield
    sys.modules.pop("my_litwatch_ntfy", None)


async def grade(conn, g: str) -> None:
    """A verdict whose grade, with no Weight yet (solid), is `g`."""
    fit = {"must_read": Fit.CENTRAL, "relevant": Fit.RELATED}.get(g) or Fit(g)
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=fit,
            reason="r",
            opinions={"judge": Opinion(fit=fit, reason="r", model="anthropic:x")},
        ),
    )
    await word.settle(conn, P.id, cause=Cause.VERDICT)


async def test_the_guides_example_loads_by_dotted_path_and_works(conn, example):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P])
    config = Config(
        integrations={"phone": {"use": "my_litwatch_ntfy", "topic": "litwatch-must-read"}}
    )
    (phone,) = integrations.load(config)
    assert isinstance(phone, Integration) and phone.name == "phone"
    sent = []

    def ntfy(req: httpx.Request) -> httpx.Response:
        sent.append((str(req.url), req.content.decode(), req.headers["Click"]))
        return httpx.Response(200)

    await phone.http.aclose()
    phone.http = httpx.AsyncClient(transport=httpx.MockTransport(ntfy))
    await start(conn, phone)

    await grade(conn, "relevant")  # saved, but not must read: nothing
    await deliver(conn, phone)
    assert sent == []

    await grade(conn, "must_read")
    await deliver(conn, phone)
    assert sent == [
        (
            "https://ntfy.sh/litwatch-must-read",
            P.title,
            f"https://arxiv.org/abs/{P.id}",
        )
    ]

    # Unsaved by the owner, then saved again: told again, as the guide says.
    await word.toggle(conn, P.id, Hand.UNSAVE)
    await deliver(conn, phone)
    assert len(sent) == 1
    await word.toggle(conn, P.id, Hand.SAVE)
    await deliver(conn, phone)
    assert len(sent) == 2
    await phone.aclose()
