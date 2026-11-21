"""The MCP server as an agent reaches it: the app with /mcp behind production's host name,
served with its lifespan, and JSON-RPC calls of its tools."""

from contextlib import asynccontextmanager

import httpx

from litwatch.api import create_app
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.integrations.archive import Archive
from tests.fakes_http import FakeArchive, mock
from tests.sessions import Session

TOKEN, READ_TOKEN, MCP_TOKEN = "s3cret", "r3ad", "m-token"
HOST = "litwatch.andrei-homelab.com"  # production's name, which the SDK's host check must let in


class Flaky(FakeArchive):
    """The fake archive, answering 503 to everything while `down`."""

    down = False

    def handle(self, req: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if self.down else super().handle(req)


def app_dsn(conn: Session) -> str:
    """The DSN an app reaches the test's schema by."""
    return f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"


def mcp_app(conn: Session, fake: FakeArchive | None = None, seen: list | None = None):
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
        app_dsn(conn),
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
