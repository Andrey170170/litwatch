"""The worked example in `docs/integrations.md`, taken from the page as written, loaded by
dotted path like any integration kept outside litwatch, and run through the dispatcher."""

import re
import sys
from pathlib import Path

import httpx
import pytest

from litwatch import integrations
from litwatch.config import Config
from litwatch.core import Grade, Hand, Opinion, Verdict
from litwatch.events import Integration, dispatch
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TOPICS, paper

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
    opinion = Opinion(grade=Grade(g), reason="r", model="m")
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade(g),
            judge=opinion,
            check=opinion,
        ),
    )
    await word.settle(conn, P.id)


async def test_the_guides_example_loads_by_dotted_path_and_works(conn, example):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
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
    await dispatch.start(conn, phone)

    await grade(conn, "relevant")  # saved, but not must read: nothing
    await dispatch.deliver(conn, phone)
    assert sent == []

    await grade(conn, "must_read")
    await dispatch.deliver(conn, phone)
    assert sent == [
        (
            "https://ntfy.sh/litwatch-must-read",
            P.title,
            f"https://arxiv.org/abs/{P.id}",
        )
    ]

    # Unsaved by the owner, then saved again: told again, as the guide says.
    await word.toggle(conn, P.id, Hand.UNSAVE)
    await dispatch.deliver(conn, phone)
    assert len(sent) == 1
    await word.toggle(conn, P.id, Hand.SAVE)
    await dispatch.deliver(conn, phone)
    assert len(sent) == 2
    await phone.aclose()
