"""Every gate call is attributed (docs/plans/a2-work-gate.md, P5; V5): a Database or Model gate
call with no attribution raises in strict mode and is logged as an error otherwise; each Entry
sets its own (the API, the monitor and MCP per request, the CLI per command, `serve` for its
startup), so none of theirs is unattributed. Each runs in an empty context, as a process
starts, not under the test's own attribution."""

import asyncio
import contextvars
import logging

import httpx
import pytest
from pydantic import BaseModel

from litwatch.api.monitor import create_monitor
from litwatch.config import Config
from litwatch.gates.database import STRICT_ENV, AttributionError
from litwatch.llm import Request
from tests.fakes import scripted_gate
from tests.mcp_client import call, mcp_app, serving
from tests.scripted import CONFIG
from tests.sessions import Session
from tests.working import TOKEN, command, served


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


async def test_a_cli_command_attributes_its_gate_calls(conn: Session, tmp_path, monkeypatch):
    ran = await unattributed(command(conn, tmp_path, monkeypatch, "eval", "report", config=None))
    assert "no such replay" in ran.output, ran.output  # read, attributed
