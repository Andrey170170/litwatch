"""The monitoring side: `/metrics` and `/status` read the run records, a run whose process died
reads as failed, and a nightly that did not happen reads as late."""

import re
from datetime import UTC, datetime, time

import httpx
import pytest

from litwatch.api import create_app
from litwatch.api.monitor import Facts, components, create_monitor, due_night, metrics
from litwatch.config import Config, Schedule
from litwatch.store import db, runs

# A sample line of the exposition: a name, bounded labels, a number.
SAMPLE = re.compile(r'^[a-z_]+(\{[a-z_]+="[a-z0-9_./-]+"(,[a-z_]+="[a-z0-9_./-]+")*\})? \S+$')
STOPPED = {
    "awaiting_classification": 0,
    "analysis_failed": 0,
    "ungraded": 0,
    "fulltext_failed": 0,
    "digests_failed": 0,
}


@pytest.fixture
async def monitor(conn):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_monitor(create_app(dsn, "t0ken"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as c:
        yield c


def sample(text: str, name: str, job: str = "litwatch/nightly") -> float | None:
    for line in text.splitlines():
        if line.startswith(f'{name}{{job_id="{job}"}} '):
            return float(line.split()[-1])
    return None


def component(document: dict, id_: str) -> dict:
    return next(c for c in document["components"] if c["id"] == id_)


async def test_a_finished_nightly_is_measured_from_its_record(conn, monitor):
    run_id = await runs.start(conn, "nightly")
    await runs.finish(conn, run_id, counts={"classified": 42})

    text = (await monitor.get("/metrics")).text
    assert all(SAMPLE.match(line) for line in text.splitlines() if not line.startswith("#"))
    assert sample(text, "homelab_job_last_items") == 42
    assert sample(text, "homelab_job_running") == 0
    assert sample(text, "homelab_job_last_success_timestamp_seconds") >= sample(
        text, "homelab_job_last_run_timestamp_seconds"
    )
    assert sample(text, "homelab_job_last_run_timestamp_seconds", "litwatch/publications") is None
    started = (await runs.latest(conn, "nightly"))["started_at"].timestamp()
    assert sample(text, "homelab_job_last_run_timestamp_seconds") == pytest.approx(
        started, abs=1e-3
    )
    status = (await monitor.get("/status")).json()
    assert status["schema"] == "homelab.status/v1"
    assert component(status, "nightly")["status"] == "healthy"


async def test_a_nightly_whose_process_died_reads_failed_not_running(conn, monitor):
    # A process that opens its run and is killed: its session ends, the record stays open.
    other = await db.sibling(conn)
    await runs.start(other, "nightly")
    text = (await monitor.get("/metrics")).text
    assert sample(text, "homelab_job_running") == 1
    assert component((await monitor.get("/status")).json(), "nightly")["status"] == "healthy"

    await other.close()
    text = (await monitor.get("/metrics")).text
    assert sample(text, "homelab_job_running") == 0
    assert sample(text, "homelab_job_last_run_timestamp_seconds") is not None
    assert sample(text, "homelab_job_last_success_timestamp_seconds") is None
    nightly = component((await monitor.get("/status")).json(), "nightly")
    assert nightly["status"] == "failed"
    assert "ended without closing it" in nightly["reason"]


async def test_a_run_that_raised_reads_failed_with_its_error(conn, monitor):
    run_id = await runs.start(conn, "nightly")
    await runs.finish(conn, run_id, error="RuntimeError: boom")
    nightly = component((await monitor.get("/status")).json(), "nightly")
    assert nightly["status"] == "failed"
    assert "boom" in nightly["reason"]


SCHEDULED = Config(schedule=Schedule(enabled=True, timezone="America/Denver"))


def facts(now: datetime, success_started: datetime | None) -> Facts:
    ok = (
        {"started_at": success_started, "finished_at": success_started} if success_started else None
    )
    return Facts(
        now=now,
        jobs={"nightly": (ok, ok), "publications": (None, None)},
        status={"state": "quiet" if ok else "none", **STOPPED},
        passes={},
        cursors={},
        spend=[],
    )


def test_the_results_of_a_night_are_due_at_nine():
    # 02:00 nightly, 07:30 deadline, Denver (UTC-6 in October): due at 09:00, 15:00 UTC.
    fired, due = due_night(SCHEDULED, datetime(2026, 10, 20, 16, 0, tzinfo=UTC))
    assert (fired.astimezone(UTC), due.astimezone(UTC)) == (
        datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
    )
    # Before 09:00 the night before's is the one due.
    fired, _ = due_night(SCHEDULED, datetime(2026, 10, 20, 14, 0, tzinfo=UTC))
    assert fired.astimezone(UTC) == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    assert due_night(Config(), datetime(2026, 10, 20, 16, 0, tzinfo=UTC)) is None


def test_a_missed_night_reads_degraded_and_a_done_one_healthy():
    now = datetime(2026, 10, 20, 16, 0, tzinfo=UTC)
    tonight = datetime(2026, 10, 20, 8, 0, 5, tzinfo=UTC)
    last_night = datetime(2026, 10, 19, 8, 0, 5, tzinfo=UTC)
    assert components(facts(now, tonight), SCHEDULED)[0]["status"] == "healthy"
    late = components(facts(now, last_night), SCHEDULED)[0]
    assert late["status"] == "degraded"
    assert "expected one by 2026-10-20T09:00:00-06:00" in late["reason"]
    # The scheduler off (the switch night): no night is expected.
    assert components(facts(now, last_night), Config())[0]["status"] == "healthy"


def test_every_unhealthy_component_says_why():
    f = facts(datetime(2026, 10, 20, 16, 0, tzinfo=UTC), None)
    f.status |= {"analysis_failed": 2, "digests_failed": 1}
    f.probes = {"archive": "ConnectError: refused", "deep": None}
    found = {c["id"]: c for c in components(f, SCHEDULED)}
    assert {i: c["status"] for i, c in found.items()} == {
        "nightly": "degraded",
        "pipeline": "degraded",
        "digests": "degraded",
        "archive": "failed",
        "deep": "healthy",
        "budget": "healthy",
    }
    assert all(c.get("reason") for c in found.values() if c["status"] != "healthy")
    assert "2 analysis failed" in found["pipeline"]["reason"]
    assert all(SAMPLE.match(x) for x in metrics(f).splitlines() if not x.startswith("#"))


def test_the_deadline_moves_due():
    late_deadline = Config(schedule=Schedule(enabled=True, timezone="UTC", deadline=time(9, 0)))
    _, due = due_night(late_deadline, datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    assert due == datetime(2026, 10, 20, 10, 30, tzinfo=UTC)
