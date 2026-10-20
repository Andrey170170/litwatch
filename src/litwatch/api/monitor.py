"""The service's monitoring side, served on its own internal port (`litwatch serve --metrics-port`):
`/metrics` in Prometheus' text format and `/status` as a `homelab.status/v1` document, following
the owner's homelab telemetry convention (docs/plans/v1-deploy.md). Neither asks for a token,
so the port is never routed through a public proxy. Both read the database when asked, so a
job run by another process (`litwatch nightly` in a terminal) shows as well as the scheduler's;
the MCP server's calls are counted in this process, where it runs."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
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
from litwatch.config import ROLES, Config, secret
from litwatch.core import FAILED_RUNS
from litwatch.core.budget import Spend
from litwatch.events import Integration
from litwatch.integrations.deep import Deep
from litwatch.llm.gateway import KEYS
from litwatch.store import calls, failures, fulltext, outbox, reading, runs
from litwatch.store import integrations as integrations_store
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
# Many papers stopped at once: at one step in one night, more than this many, or this share of
# the papers the night classified if that is more (data.md, the status document).
STOPPED_AT_ONCE = 5
STOPPED_SHARE = 0.02
# The steps a paper stops at, as the status document names them, in the pipeline's order.
STEPS = {
    "classification": "classification",
    "analysis": "analysis",
    "grading": "grading",
    "fulltext": "the Brief and Weight passes",
}


@dataclass
class Job:
    latest: dict | None  # the newest run
    success: dict | None  # the newest that finished without an error
    running: bool  # the newest run is open and alive (runs of a kind never overlap)


@dataclass
class Night:
    """The latest nightly that finished, as the status document reads it."""

    run_id: int
    classified: int
    stopped: dict[str, int]  # by `STEPS` key: the distinct papers that stopped there during it
    calls: dict[str, dict[str, int]]  # by provider: calls `failed` without an answer, `replied`


# How a call to the MCP server ended: answered, nothing found, still being fetched (ask again),
# or failed with a server error. Only the last is a failure.
MCP_OUTCOMES = ("ok", "not_found", "fetching", "error")
# How far back the `mcp` component looks for failed calls.
MCP_WINDOW = timedelta(hours=1)


@dataclass
class McpCalls:
    """The MCP server's calls in this process (it runs inside `serve`), for `/metrics` and the
    `mcp` component: counts by tool and outcome, time spent per tool, the failures of the last
    `MCP_WINDOW` and the newest call that did not fail."""

    tools: tuple[str, ...]
    counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    seconds: dict[str, float] = field(default_factory=dict)
    failed: deque[tuple[datetime, str]] = field(default_factory=deque)  # oldest first
    answered_at: datetime | None = None

    def record(self, tool: str, outcome: str, seconds: float, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.counts[tool, outcome] += 1
        self.seconds[tool] = self.seconds.get(tool, 0.0) + seconds
        if outcome == "error":
            self.failed.append((now, tool))
        else:
            self.answered_at = now
        while self.failed and self.failed[0][0] < now - MCP_WINDOW:
            self.failed.popleft()

    def component(self, now: datetime) -> dict[str, Any]:
        """Degraded when calls in the last `MCP_WINDOW` failed and none answered since the last
        of them; a paper not found or still being fetched is an answer (decision 68)."""
        failed = [(at, tool) for at, tool in self.failed if at >= now - MCP_WINDOW]
        last = failed[-1][0] if failed else None
        if last is None or (self.answered_at is not None and self.answered_at > last):
            return _component("mcp", "healthy", None, now)
        by_tool = Counter(tool for _, tool in failed)
        tools = ", ".join(f"{tool} {n}" for tool, n in sorted(by_tool.items()))
        reason = (
            f"{len(failed)} calls failed in the last hour ({tools}), the last at "
            f"{last.isoformat()}, none answered since; expected an answer after a failure"
        )
        return _component("mcp", "degraded", reason, now)


@dataclass
class Facts:
    """What both endpoints are built from, read once per request."""

    now: datetime
    jobs: dict[str, Job]
    # the status line's (`store.reading.status`), but counting what is stopped whether or not
    # the owner acknowledged it: the owner's Acknowledge is for the app's run status alone
    status: dict[str, Any]
    passes: dict[tuple[str, str], int]
    cursors: dict[str, Cursor]  # of the loaded integrations
    spend: list[Spend]
    probes: dict[str, str | None] = field(default_factory=dict)  # name: error, None if it answers
    night: Night | None = None
    observed: dict[str, dict[str, Any]] = field(default_factory=dict)  # `calls.observations`
    signin: dict[str, Any] | None = None  # the Deep service's, as its health check reported it
    deep_failure: dict[str, Any] | None = None  # `integrations.service_failure`
    unkeyed: dict[str, str] = field(default_factory=dict)  # provider: why it has no key
    mcp: McpCalls | None = None  # while /mcp is served


async def facts(
    conn: psycopg.AsyncConnection,
    config: Config,
    integrations: list[Integration],
    archive: ArchiveClient | None,
) -> Facts:
    now = datetime.now(UTC)
    jobs = {}
    for k in JOBS:
        latest = await runs.latest(conn, k)
        running = bool(latest and latest["finished_at"] is None and latest["alive"])
        jobs[k] = Job(latest, await runs.last_success(conn, k), running)
    found = Facts(
        now=now,
        jobs=jobs,
        status=await reading.status(conn) | await reading.stopped(conn, unacknowledged=False),
        passes=await fulltext.count_by_state(conn),
        cursors=await outbox.cursors(conn, [i.name for i in integrations]),
        spend=await budget_lines(conn, config.budget),
        night=await _night(conn),
        observed=await calls.observations(conn),
        deep_failure=await integrations_store.service_failure(conn),
        unkeyed=_unkeyed(),
    )
    probes = {}
    if archive is not None:
        probes["archive"] = archive.indexed()
    for deep in (i for i in integrations if isinstance(i, Deep)):
        probes["deep"] = deep.client.health()
    answers = dict(zip(probes, await asyncio.gather(*map(_probe, probes.values())), strict=True))
    found.probes = {name: error for name, (error, _) in answers.items()}
    if "deep" in answers and answers["deep"][0] is None:
        found.signin = (answers["deep"][1] or {}).get("auth") or {}
    return found


def _unkeyed() -> dict[str, str]:
    """Each provider whose key is not in the environment, with why; an unreadable key file is a
    reason, not an error, so the rest of the status still answers."""
    out = {}
    for name, variable in KEYS.items():
        try:
            if not secret(variable):
                out[name] = f"neither {variable} nor {variable}_FILE is set"
        except OSError as e:
            out[name] = f"{variable}_FILE cannot be read ({e.strerror})"
    return out


async def _night(conn: psycopg.AsyncConnection) -> Night | None:
    """The latest finished nightly: from its start to its end, its digest wait included. One
    whose process died reads failed on `nightly`, and has no known end."""
    run = await runs.latest_finished(conn, "nightly")
    if run is None:
        return None
    during = (run["started_at"], run["finished_at"], FAILED_RUNS)
    stopped: dict[str, int] = dict(await failures.stopped_between(conn, *during))
    stopped["fulltext"] = await fulltext.stopped_between(conn, *during)
    classified = (run["counts"] or {}).get("classified", 0)
    return Night(run["id"], classified, stopped, await calls.outcomes(conn, run))


async def _probe(call: Any) -> tuple[str | None, Any]:
    """The error, None if it answered in time, and its answer."""
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            return None, await call
    except TimeoutError:
        return f"no answer within {PROBE_SECONDS:g} s", None
    except Exception as exc:  # noqa: BLE001 - any failure is the observation
        return f"{type(exc).__name__}: {exc}", None


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
        "1 while an integration is stopped on a failing event or a failing setup.",
        [
            ({"integration": n}, float(c.failing is not None or c.setup_failures > 0))
            for n, c in sorted(f.cursors.items())
        ],
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
    if f.mcp is not None:
        out.extend(_mcp_metrics(f.mcp))
    return "\n".join(out) + "\n"


def _mcp_metrics(calls: McpCalls) -> list[str]:
    """The MCP server's counter and summary, every tool and outcome listed from zero."""
    out = [
        "# HELP litwatch_mcp_calls_total MCP tool calls, by tool and how they ended.",
        "# TYPE litwatch_mcp_calls_total counter",
    ]
    for tool in calls.tools:
        for outcome in MCP_OUTCOMES:
            n = calls.counts[tool, outcome]
            out.append(f'litwatch_mcp_calls_total{{tool="{tool}",outcome="{outcome}"}} {n}')
    out += [
        "# HELP litwatch_mcp_call_seconds Time spent answering MCP tool calls, by tool.",
        "# TYPE litwatch_mcp_call_seconds summary",
    ]
    for tool in calls.tools:
        n = sum(calls.counts[tool, outcome] for outcome in MCP_OUTCOMES)
        spent = repr(calls.seconds.get(tool, 0.0))
        out.append(f'litwatch_mcp_call_seconds_sum{{tool="{tool}"}} {spent}')
        out.append(f'litwatch_mcp_call_seconds_count{{tool="{tool}"}} {n}')
    return out


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
    # One iterator, stepped back: rebuilding it a second before a fire loops forever when the
    # fire was moved by a spring-forward gap.
    fires = croniter(config.schedule.nightly, now.astimezone(zone))
    while True:
        fired = fires.get_prev(datetime)
        deadline = datetime.combine(fired.date(), config.schedule.deadline, zone)
        if deadline <= fired:
            deadline += timedelta(days=1)
        if deadline + READY_AFTER_DEADLINE <= now:
            return fired, deadline + READY_AFTER_DEADLINE


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

    out.append(_pipeline(f.night, now))
    failed = s["digests_failed"]
    out.append(
        _component(
            "digests",
            "degraded" if failed else "healthy",
            f"{failed} digests failed; expected none" if failed else None,
            now,
        )
    )
    for name in sorted({m.provider for r in ROLES for m in config.models.role(r).models}):
        reasons = _provider(name, f.observed.get(name, {}), f.night, f.unkeyed.get(name))
        status = "degraded" if reasons else "healthy"
        out.append(_component(f"provider-{name}", status, "; ".join(reasons) or None, now))
    for name, c in sorted(f.cursors.items()):
        if c.setup_failures:
            reason = (
                f"setup failed {c.setup_failures} times: {c.setup_error}; expected it to succeed"
            )
            out.append(_component(f"integration-{name}", "degraded", reason, now))
        elif c.failing is not None:
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
        reasons = [f"does not answer: {err}; expected an answer within {PROBE_SECONDS:g} s"]
        reasons = reasons if err else []
        if name == "deep":
            reasons += _deep(f)
        # Without the archive nothing is read or taken in; without deep only cards wait.
        status = "healthy" if not reasons else ("failed" if name == "archive" else "degraded")
        out.append(_component(name, status, "; ".join(reasons) or None, now))
    over = [x for x in f.spend if x.over]
    reason = "; ".join(
        f"{x.provider} projected ${x.projected:.2f} this month, budget ${x.budget:.2f}"
        for x in over
    )
    out.append(_component("budget", "degraded" if over else "healthy", reason or None, now))
    if f.mcp is not None:
        out.append(f.mcp.component(now))
    return out


def _pipeline(night: Night | None, now: datetime) -> dict[str, Any]:
    """Degraded when many papers stopped at one step in the latest finished nightly; a paper's own
    failure is the app's, so a few never turn it."""
    if night is None:
        return _component("pipeline", "healthy", None, now)
    most = max(STOPPED_AT_ONCE, STOPPED_SHARE * night.classified)
    over = [
        f"{night.stopped[k]} at {what}"
        for k, what in STEPS.items()
        if night.stopped.get(k, 0) > most
    ]
    if not over:
        return _component("pipeline", "healthy", None, now)
    reason = (
        f"papers stopped in nightly {night.run_id}, which classified {night.classified}: "
        f"{', '.join(over)}; expected at most {most:g} at a step"
    )
    return _component("pipeline", "degraded", reason, now)


def _provider(
    name: str, seen: dict[str, Any], night: Night | None, unkeyed: str | None
) -> list[str]:
    """Why the provider is failed, if it is: no key in the environment, an account error with no
    reply since, or the latest finished nightly's calls to it failed without one reply."""
    reasons = []
    if unkeyed:
        reasons.append(f"no key: {unkeyed}; expected one, as a configured role uses it")
    error_at, replied_at = seen.get("account_error_at"), seen.get("replied_at")
    if error_at and (replied_at is None or replied_at < error_at):
        reasons.append(
            f"{seen['account_error']} at {error_at.isoformat()}, no reply since; "
            "expected a reply after it"
        )
    ran = night.calls.get(name, {}) if night else {}
    if ran.get("failed") and not ran.get("replied"):
        reasons.append(
            f"{ran['failed']} calls in nightly {night.run_id} failed and none had a reply; "
            "expected a reply"
        )
    return reasons


def _deep(f: Facts) -> list[str]:
    """Why the Deep service is failed, but for not answering: its sign-in when it answered, a
    service failure litwatch saw with no card made since."""
    reasons = []
    signin = f.signin
    if signin is not None and signin.get("state") != "ok":
        said = signin.get("state") or "not reported"
        detail = f" ({signin['detail']})" if signin.get("detail") else ""
        reasons.append(f"sign-in {said}{detail}; expected ok")
    if failure := f.deep_failure:
        said = f": {failure['error']}" if failure["error"] else ""
        reasons.append(
            f"service failure at {failure['at'].isoformat()}{said}, no card made since; "
            "expected a card after it"
        )
    return reasons


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
            found = await facts(conn, st.config, st.integrations, st.archive)
        found.mcp = st.mcp_calls
        return found

    @monitor.get("/metrics", response_class=PlainTextResponse)
    async def get_metrics() -> PlainTextResponse:
        return PlainTextResponse(metrics(await read()), media_type="text/plain; version=0.0.4")

    @monitor.get("/status")
    async def get_status() -> dict[str, Any]:
        return status_document(await read(), app.state.config)

    return monitor
