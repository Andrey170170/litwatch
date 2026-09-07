"""API contract without a database: the run-in-progress guard and request validation."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from litwatch import api
from litwatch.config import Settings


@pytest.fixture
def app(monkeypatch):
    settings = Settings(openrouter_api_key="x", database_url="postgresql://unused")
    application = api.create_app(settings)

    async def fake_run(*args, **kwargs):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(api.ingest, "run", fake_run)
    monkeypatch.setattr(api.db, "connect", _fake_connect)
    return application


async def _fake_connect(dsn):
    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return Conn()


async def test_second_run_is_rejected_while_first_is_active(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        first = await c.post("/runs", json={"days": 2})
        assert first.status_code == 202 and first.json()["days"] == 2
        second = await c.post("/runs", json={"days": 2})
        assert second.status_code == 409
        await app.state.runs["task"]
        third = await c.post("/runs", json={"days": 1, "kind": "backfill"})
        assert third.status_code == 202


async def test_run_request_validation(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/runs", json={"days": 0})).status_code == 422
        assert (await c.post("/runs", json={"kind": "weird"})).status_code == 422


async def test_papers_tier_filter_must_be_integers(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/papers", params={"tier": "must_read"})).status_code == 422


def test_selection_rows_report_validation_errors_per_row():
    from pydantic import ValidationError

    from litwatch.api import SelectionRow, _brief
    from litwatch.selection import from_spec

    row = SelectionRow(name="Agents", subject="too short")
    with pytest.raises(ValidationError) as exc:
        from_spec(row.model_dump())
    assert "subject" in _brief(exc.value)
    ok = from_spec(
        SelectionRow(name="Agents and tools", subject="s" * 30, keywords="a, b").model_dump()
    )
    assert ok.slug == "agents-and-tools" and ok.keywords == ["a", "b"]
