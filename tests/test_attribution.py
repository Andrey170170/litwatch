"""Every gate call is attributed (docs/plans/a2-work-gate.md, P5; V5): a Database or Model gate
call with no attribution raises in strict mode and is logged as an error otherwise; each Entry
sets its own (the API, the monitor and MCP per request, the CLI per command, `serve` for its
startup), so none of theirs is unattributed. Each runs in an empty context, as a process
starts, not under the test's own attribution."""

import asyncio
import contextvars
import logging
from typing import cast

import httpx
import pytest
from pydantic import BaseModel

from litwatch import cli
from litwatch.api.monitor import create_monitor
from litwatch.config import Config
from litwatch.core.work import ItemState
from litwatch.gates.context import Attribution, attributed, attribution
from litwatch.gates.database import STRICT_ENV, AttributionError
from litwatch.jobs import commands
from litwatch.llm import Request
from litwatch.store import work as queue
from tests.fakes import scripted_gate
from tests.mcp_client import call, mcp_app, serving
from tests.scripted import CONFIG
from tests.sessions import Session
from tests.working import TOKEN, command, served, until


class Answer(BaseModel):
    answer: str


def unattributed(coro):
    """`coro` in a task of an empty context: no attribution."""
    return asyncio.create_task(coro, context=contextvars.Context())


async def test_a_gate_call_with_no_attribution_raises_strict_and_is_logged_otherwise(
    db, monkeypatch, caplog
):
    async def read() -> int:
        async with db.read("unattributed") as unit:
            return (await (await unit.execute("SELECT 1 AS one")).fetchone())["one"]

    async def ask() -> object:
        gate = scripted_gate(lambda role, request: '{"answer": "yes"}', CONFIG)
        return await gate.on().ask("analyse", Request(system="s", user="u", output=Answer))

    with pytest.raises(AttributionError, match="unattributed"):
        await unattributed(read())
    with pytest.raises(AttributionError, match="role 'analyse'"):
        await unattributed(ask())
    monkeypatch.delenv(STRICT_ENV)
    with caplog.at_level(logging.ERROR):
        assert await unattributed(read()) == 1
    assert any("gate attribution:" in r.getMessage() for r in caplog.records)


async def test_serve_the_api_and_the_monitor_attribute_their_gate_calls(db):
    """`serve`'s startup and its Work gate, a request to the API and one to the monitor."""

    async def entries() -> tuple[int, int]:
        async with served(db, Config(), models=scripted_gate(lambda *_: "{}", CONFIG)) as app:
            auth = {"authorization": f"Bearer {TOKEN}"}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                api = (await http.get("/api/summary", headers=auth)).status_code
            transport = httpx.ASGITransport(app=create_monitor(app))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                status = (await http.get("/status")).status_code
            return api, status

    assert await unattributed(entries()) == (200, 200)


async def test_an_mcp_tool_attributes_its_gate_calls(conn: Session):
    async def listed() -> tuple[str, bool]:
        async with serving(mcp_app(conn)) as http:
            return await call(http, "list_digests")

    text, failed = await unattributed(listed())
    assert not failed, text


@pytest.fixture
def seen(monkeypatch) -> list[tuple[str, Attribution | None]]:
    """Each gate call, as the gate checks its attribution: what it is, and whom it serves."""
    from litwatch.gates import database  # noqa: PLC0415
    from litwatch.llm import gateway  # noqa: PLC0415

    calls: list[tuple[str, Attribution | None]] = []
    real = database.attributed_call

    def recorded(where: str) -> None:
        calls.append((where, attribution()))
        real(where)

    monkeypatch.setattr(database, "attributed_call", recorded)
    monkeypatch.setattr(gateway, "attributed_call", recorded)
    return calls


async def test_a_cli_command_attributes_its_gate_calls_to_itself_by_name(
    conn: Session, tmp_path, monkeypatch, seen
):
    ran = await unattributed(command(conn, tmp_path, monkeypatch, "eval", "report", config=None))
    assert "no such replay" in ran.output, ran.output  # read, attributed
    assert {a for _, a in seen} == {Attribution.request("cli eval report")}


async def test_serves_own_entry_attributes_its_startup_requests_and_items_not_itself(
    conn: Session, tmp_path, monkeypatch, seen
):
    """`litwatch serve` as the CLI starts it: no attribution around the process, so a gate call
    nothing attributed fails; its startup is the system's (`serve`), a request to its API that
    request's, and the work of an item it runs that item's."""
    around, submitted = [], []

    async def handler(s, item, out):
        async with s.db.read("test: the item's work") as unit:
            await unit.execute("SELECT 1")
        return commands.finished()

    monkeypatch.setitem(cast(dict, commands.HANDLERS), "ping", (handler, lambda s: True))

    async def entry(app_, host, port, metrics_port):
        around.append(attribution())
        async with app_.router.lifespan_context(app_):
            db = app_.state.db
            with attributed(Attribution.request("test: submit")):
                async with db.write("test: submit") as unit:
                    submitted.append(await queue.submit(unit, "command:ping", "k"))

                async def ended() -> bool:
                    async with db.read("test: ended") as unit:
                        found = await queue.get(unit, submitted[0].id)
                    return found is not None and found.state == ItemState.ENDED

                await until(ended, "serve to run the item")
            transport = httpx.ASGITransport(app=app_)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                auth = {"authorization": f"Bearer {TOKEN}"}
                assert (await http.get("/api/summary", headers=auth)).status_code == 200

    monkeypatch.setattr(cli, "_serve", entry)
    monkeypatch.setenv("LITWATCH_API_TOKEN", TOKEN)
    ran = await unattributed(command(conn, tmp_path, monkeypatch, "serve"))
    assert ran.exit_code == 0, ran.output
    assert around == [None]
    (work,) = [a for where, a in seen if "test: the item's work" in where]
    assert work == Attribution.work(submitted[0].id)
    found = {a for _, a in seen}
    assert Attribution.system("model gate") in found  # its startup: the batches taken over
    assert Attribution.request("api GET /api/summary") in found
