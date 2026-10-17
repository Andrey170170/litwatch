"""The service's monitoring side, served on its own internal port (`litwatch serve --metrics-port`):
`/metrics` in Prometheus' text format and `/status` as a `homelab.status/v1` document, following
the owner's homelab telemetry convention (docs/plans/v1-deploy.md). Neither asks for a token,
so the port is never routed through a public proxy. Both read the database when asked, so a
job run by another process (`litwatch nightly` in a terminal) shows as well as the scheduler's."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from croniter import croniter
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from litwatch import store
from litwatch.api.deps import budget_lines
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core.budget import Spend
from litwatch.events import Integration
from litwatch.integrations.deep import Deep
from litwatch.store import fulltext, outbox, reading, runs
from litwatch.store.outbox import Cursor

SERVICE = "litwatch"
# The scheduler's jobs, and the count in each run's record that says how much it handled.
JOBS = {"nightly": "classified", "publications": "looked_up"}
# The owner reads at 09:00; the nightly's deadline is 07:30 by default, its digests done by it.
READY_AFTER_DEADLINE = timedelta(minutes=90)
# Probes run together, inside the ten seconds Prometheus and Vergil wait for an answer.
PROBE_SECONDS = 3.0
# A lag past this many events, on an integration that is not failing, is worth a look.
LAG_DEGRADED = 500


@dataclass
class Job:
    latest: dict | None  # the newest run
    success: dict | None  # the newest that finished without an error
    running: bool  # some run is open and alive


@dataclass
class Facts:
    """What both endpoints are built from, read once per request."""

    now: datetime
    jobs: dict[str, Job]
    status: dict[str, Any]  # the status line's (`store.reading.status`)
    passes: dict[tuple[str, str], int]
    cursors: dict[str, Cursor]  # of the loaded integrations
    spend: list[Spend]
    probes: dict[str, str | None] = field(default_factory=dict)  # name: error, None if it answers


async def facts(
    conn: psycopg.AsyncConnection,
    config: Config,
    integrations: list[Integration],
    archive: ArchiveClient | None,
) -> Facts:
    now = datetime.now(UTC)
    jobs = {
        k: Job(
            await runs.latest(conn, k),
            await runs.last_success(conn, k),
            await runs.running(conn, k),
        )
        for k in JOBS
    }
    found = Facts(
        now=now,
        jobs=jobs,
        status=await reading.status(conn),
        passes=await fulltext.count_by_state(conn),
        cursors=await outbox.cursors(conn, [i.name for i in integrations]),
        spend=await budget_lines(conn, config.budget),
    )
    probes = {}
    if archive is not None:
        probes["archive"] = archive.indexed()
    for deep in (i for i in integrations if isinstance(i, Deep)):
        probes["deep"] = deep.client.health()
    answers = await asyncio.gather(*map(_probe, probes.values()))
    found.probes = dict(zip(probes, answers, strict=True))
    return found


async def _probe(call: Any) -> str | None:
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            await call
    except TimeoutError:
        return f"no answer within {PROBE_SECONDS:g} s"
    except Exception as exc:  # noqa: BLE001 - any failure is the observation
        return f"{type(exc).__name__}: {exc}"
    return None


# -- /metrics --------------------------------------------------------------------------------------


def _ts(at: datetime | None) -> float | None:
    return at.timestamp() if at else None


def _escaped(value: str) -> str:
    """A label value as the text format wants it."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metrics(f: Facts) -> str:
    """The exposition: the homelab's `homelab_job_*` family for the scheduler's jobs, then
    litwatch's own gauges. Labels are bounded: job, step, pass kind and state, integration,
    provider."""
    out: list[str] = []

    def gauge(name: str, help_: str, rows: list[tuple[dict[str, str], float | None]]) -> None:
        out.extend([f"# HELP {name} {help_}", f"# TYPE {name} gauge"])
        for labels, value in rows:
            if value is None:
                continue
            text = ",".join(f'{k}="{_escaped(v)}"' for k, v in labels.items())
            number = repr(float(value))  # every digit: a timestamp needs ten
            out.append(f"{name}{{{text}}} {number}" if text else f"{name} {number}")

    def per_job(get: Any) -> list[tuple[dict[str, str], float | None]]:
        return [({"job_id": f"{SERVICE}/{k}"}, get(k, f.jobs[k])) for k in JOBS]

    def duration(_: str, job: Job) -> float | None:
        run = job.latest
        if run is None or run["finished_at"] is None:
            return None
        return (run["finished_at"] - run["started_at"]).total_seconds()

    def items(kind: str, job: Job) -> float | None:
        run = job.latest
        if run is None or run["finished_at"] is None:
            return None
        return (run["counts"] or {}).get(JOBS[kind], 0)

    def last_success(_: str, job: Job) -> float | None:
        # 0 before the first success, so `last_run > last_success` holds for a first failure.
        if job.latest is None:
            return None
        return _ts(job.success["finished_at"]) if job.success else 0.0

    gauge(
        "homelab_job_last_run_timestamp_seconds",
        "Start of the last run.",
        per_job(lambda _, job: _ts(job.latest and job.latest["started_at"])),
    )
    gauge(
        "homelab_job_last_success_timestamp_seconds",
        "End of the last successful run.",
        per_job(last_success),
    )
    gauge("homelab_job_last_duration_seconds", "Duration of the last run.", per_job(duration))
    gauge("homelab_job_last_items", "Items handled by the last run.", per_job(items))
    gauge(
        "homelab_job_running",
        "1 while a run is in progress.",
        per_job(lambda _, job: float(job.running)),
    )
    s = f.status
    gauge(
        "litwatch_stuck_papers",
        "Papers stopped at a step after repeated failures, awaiting the owner.",
        [
            ({"step": "classification"}, s["awaiting_classification"]),
            ({"step": "analysis"}, s["analysis_failed"]),
            ({"step": "grading"}, s["ungraded"]),
            ({"step": "fulltext"}, s["fulltext_failed"]),
        ],
    )
    gauge(
        "litwatch_digests_failed",
        "Digests that failed and are not written yet.",
        [({}, s["digests_failed"])],
    )
    gauge(
        "litwatch_fulltext_passes",
        "Full-text passes not done, by kind and state.",
        [({"kind": k, "state": st}, n) for (k, st), n in sorted(f.passes.items())],
    )
    gauge(
        "litwatch_integration_lag_events",
        "Outbox events an integration has not handled yet.",
        [({"integration": n}, c.lag) for n, c in sorted(f.cursors.items())],
    )
    gauge(
        "litwatch_integration_failing",
        "1 while an integration is stopped on a failing event.",
        [({"integration": n}, float(c.failing is not None)) for n, c in sorted(f.cursors.items())],
    )
    gauge(
        "litwatch_spend_month_dollars",
        "Month-to-date model spend, by provider.",
        [({"provider": x.provider}, x.spent) for x in f.spend],
    )
    gauge(
        "litwatch_spend_projected_dollars",
        "Model spend projected to the month's end, by provider.",
        [({"provider": x.provider}, x.projected) for x in f.spend],
    )
    gauge(
        "litwatch_budget_month_dollars",
        "The monthly budget, by provider.",
        [({"provider": x.provider}, x.budget) for x in f.spend],
    )
    gauge(
        "litwatch_up",
        "1 when a service litwatch depends on answers.",
        [({"dependency": n}, float(err is None)) for n, err in sorted(f.probes.items())],
    )
    return "\n".join(out) + "\n"


# -- /status ---------------------------------------------------------------------------------------


def _component(id_: str, status: str, reason: str | None, now: datetime) -> dict[str, Any]:
    found = {"id": id_, "status": status, "observed_at": now.isoformat()}
    return found | ({"reason": reason} if reason else {})


def due_night(config: Config, now: datetime) -> tuple[datetime, datetime] | None:
    """The latest scheduled night whose results are due by now: when its nightly fired, and
    when its results were due (its deadline plus `READY_AFTER_DEADLINE`). None when nothing is
    scheduled."""
    if not config.schedule.enabled:
        return None
    zone = config.schedule.zone
    at = now.astimezone(zone)
    while True:
        fired = croniter(config.schedule.nightly, at).get_prev(datetime)
        deadline = datetime.combine(fired.date(), config.schedule.deadline, zone)
        if deadline <= fired:
            deadline += timedelta(days=1)
        if deadline + READY_AFTER_DEADLINE <= now:
            return fired, deadline + READY_AFTER_DEADLINE
        at = fired - timedelta(seconds=1)  # tonight's is not due yet: the night before's


def components(f: Facts, config: Config) -> list[dict[str, Any]]:
    """The status document's components, each with the observed and expected value in its
    reason whenever it is not healthy."""
    now, s, out = f.now, f.status, []
    ok = f.jobs["nightly"].success
    night = due_night(config, now)
    if s["state"] == "failed":
        out.append(
            _component(
                "nightly",
                "failed",
                f"last run {s['run_id']}: {s['error']}; expected it to finish without an error",
                now,
            )
        )
    elif night and (ok is None or ok["started_at"] < night[0]):
        # Running or not: past the time the owner reads, the night's results are late.
        last = ok["finished_at"].isoformat() if ok else "never"
        running = "; a nightly is still running" if s["state"] == "running" else ""
        reason = f"last successful nightly {last}; expected one by {night[1].isoformat()}{running}"
        out.append(_component("nightly", "degraded", reason, now))
    else:
        out.append(_component("nightly", "healthy", None, now))

    stuck = {
        "awaiting classification": s["awaiting_classification"],
        "analysis failed": s["analysis_failed"],
        "ungraded": s["ungraded"],
        "full-text pass failed or awaiting text": s["fulltext_failed"],
    }
    named = [f"{n} {what}" for what, n in stuck.items() if n]
    out.append(
        _component(
            "pipeline",
            "degraded" if named else "healthy",
            f"papers stopped: {', '.join(named)}; expected none" if named else None,
            now,
        )
    )
    failed = s["digests_failed"]
    out.append(
        _component(
            "digests",
            "degraded" if failed else "healthy",
            f"{failed} digests failed; expected none" if failed else None,
            now,
        )
    )
    for name, c in sorted(f.cursors.items()):
        if c.failing is not None:
            reason = (
                f"stopped on event {c.failing} after {c.failures} failures: {c.last_error}; "
                "expected no failing event"
            )
            out.append(_component(f"integration-{name}", "degraded", reason, now))
        elif c.lag > LAG_DEGRADED:
            reason = f"{c.lag} events behind; expected at most {LAG_DEGRADED}"
            out.append(_component(f"integration-{name}", "degraded", reason, now))
        else:
            out.append(_component(f"integration-{name}", "healthy", None, now))
    for name, err in sorted(f.probes.items()):
        # Without the archive nothing is read or taken in; without deep only cards wait.
        status = "healthy" if err is None else ("failed" if name == "archive" else "degraded")
        out.append(
            _component(
                name,
                status,
                f"does not answer: {err}; expected an answer within {PROBE_SECONDS:g} s"
                if err
                else None,
                now,
            )
        )
    over = [x for x in f.spend if x.over]
    reason = "; ".join(
        f"{x.provider} projected ${x.projected:.2f} this month, budget ${x.budget:.2f}"
        for x in over
    )
    out.append(_component("budget", "degraded" if over else "healthy", reason or None, now))
    return out


def status_document(f: Facts, config: Config) -> dict[str, Any]:
    return {
        "schema": "homelab.status/v1",
        "observed_at": f.now.isoformat(),
        "components": components(f, config),
    }


def create_monitor(app: FastAPI) -> FastAPI:
    """The monitoring app beside `app`, reading its database, configuration, integrations and
    archive."""
    monitor = FastAPI(title="litwatch monitoring", docs_url=None, redoc_url=None, openapi_url=None)

    async def read() -> Facts:
        st = app.state
        async with await store.connect(st.dsn) as conn:
            return await facts(conn, st.config, st.integrations, st.archive)

    @monitor.get("/metrics", response_class=PlainTextResponse)
    async def get_metrics() -> PlainTextResponse:
        return PlainTextResponse(metrics(await read()), media_type="text/plain; version=0.0.4")

    @monitor.get("/status")
    async def get_status() -> dict[str, Any]:
        return status_document(await read(), app.state.config)

    return monitor
