"""The fallback replay (docs/plans/failure-handling.md, P1): each role that writes asked, with the
pipeline's own request, of its own model and of each candidate fallback, over frozen cases, and
the answers set side by side.

A dry run: it reads papers, the taxonomy and published digests from the configured database,
papers it lacks from the archive's catalog, and texts the archive already holds (never asking
it to fetch one), and writes only to its output directory. Nothing is journalled or reused, so
every candidate is asked afresh, alone: a candidate has no fallback of its own.

A role's **ladder** is its candidates in order, the first the role's own model, which is the
reference the others are compared with. A **case** is one request of one role: a paper and a
topic for the graders (`judge`, `check`, `field_grade`), a paper and its area for `analyse` and
`brief`, a published daily digest for `writer`; in the `quality` set (ordinary papers, some
with the owner's Fit) or the `refusals` set (papers likely to be refused).

Spending is capped across every invocation into one directory. Before each call the harness
reserves the call's upper bound (its input at half a token per character, and the role's
`reserve_output_tokens`, at the candidate's prices), and starts it only while what the
directory's calls have cost plus what is reserved stays within the ceiling; a call whose charge
is unknown counts at its reservation. The run that reaches the ceiling stops there and records
the first call it did not make (`budget.jsonl`).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field, model_validator

from litwatch import analysis, grading
from litwatch.archive import ArchiveClient, ArchiveError, stored_text
from litwatch.config import Models, RoleConfig
from litwatch.core import Depth, Fit, Paper
from litwatch.core.digests import DigestPeriod
from litwatch.digests import writer
from litwatch.fulltext import brief
from litwatch.llm import CallError, Gateway, Provider, Request
from litwatch.store import digests as digests_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store

ROLES = ("analyse", "judge", "check", "field_grade", "brief", "writer")
GRADERS = ("judge", "check", "field_grade")
SETS = ("quality", "refusals")
# What a token is in characters: an estimate, and the reservation's upper bound.
CHARS_PER_TOKEN, RESERVE_CHARS_PER_TOKEN = 3.0, 2.0
# An answer that failed for want of the provider says nothing of the model: asked again.
RETRIED = ("timeout", "provider")
BUDGET = "budget.jsonl"


# -- the ladders -----------------------------------------------------------------------------------


class Candidate(BaseModel):
    """One model asked as a role, with its prices for the estimate and the reservation."""

    name: str
    role: RoleConfig
    price: tuple[float, float]  # USD per million input and output tokens

    @model_validator(mode="after")
    def _alone(self) -> Candidate:
        if self.role.fallback is not None or self.role.decisions:
            raise ValueError(f"{self.name}: a candidate is a chat model asked alone")
        return self


class Ladder(BaseModel):
    """A role's candidates, its own model first."""

    role: str
    output_tokens: int = Field(gt=0)  # expected a call, for the estimate
    reserve_output_tokens: int = Field(gt=0)  # a call's upper bound, for the ceiling
    candidates: list[Candidate] = Field(min_length=1)

    @model_validator(mode="after")
    def _named_once(self) -> Ladder:
        if self.role not in ROLES:
            raise ValueError(f"{self.role} is not a role that writes")
        names = [c.name for c in self.candidates]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.role}: a candidate named twice")
        return self

    @property
    def own(self) -> Candidate:
        return self.candidates[0]


def ladders(path: Path) -> dict[str, Ladder]:
    """The ladders of a TOML file: a table per role, holding its `output_tokens`,
    `reserve_output_tokens` and an array `candidates` of tables, each a role's configuration
    with a `name` and a `price`."""
    found = {}
    for role, table in tomllib.loads(path.read_text()).items():
        listed = [
            Candidate(
                name=c["name"],
                price=c["price"],
                role=RoleConfig.model_validate(
                    {k: v for k, v in c.items() if k not in ("name", "price")}
                ),
            )
            for c in table.get("candidates", [])
        ]
        rest = {k: v for k, v in table.items() if k != "candidates"}
        found[role] = Ladder(role=role, candidates=listed, **rest)
    return found


# -- the cases -------------------------------------------------------------------------------------


class Case(BaseModel):
    """One request of one role. `subject` is the topic graded, the area, or the digest's id;
    `label` the owner's Fit for the topic, where the owner gave one; `note` why it is in its
    set."""

    set: Literal["quality", "refusals"]
    role: str
    paper: str | None = None  # None for a digest
    subject: str
    label: Fit | None = None
    note: str | None = None

    @property
    def key(self) -> str:
        return f"{self.role}/{self.paper or ''}/{self.subject}"


def cases(path: Path) -> list[Case]:
    """The cases of a CSV file with the columns of `Case`, empty cells absent."""
    with path.open(newline="") as f:
        rows = [{k: v for k, v in r.items() if v != ""} for r in csv.DictReader(f)]
    found = [Case.model_validate(r) for r in rows]
    if len({c.key for c in found}) != len(found):
        raise ValueError(f"{path}: a case listed twice")
    if unknown := {c.role for c in found} - set(ROLES):
        raise ValueError(f"{path}: no role {', '.join(sorted(unknown))}")
    return found


async def papers(
    conn: psycopg.AsyncConnection, archive: ArchiveClient | None, ids: list[str]
) -> dict[str, Paper]:
    """The papers by id: the database's, else the archive catalog's."""
    found = {p.id: p for p in await papers_store.get_many(conn, ids)}
    for pid in ids:
        if pid not in found and archive is not None and (p := await archive.paper(pid)):
            found[pid] = p
    return found


async def requests(
    conn: psycopg.AsyncConnection, archive: ArchiveClient | None, chosen: list[Case]
) -> dict[str, Request | str]:
    """Each case's request, built as the pipeline builds it, or why it has none."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        raise ValueError("no taxonomy in the database")
    topics = {t.slug: t for t in tree.topics}
    classes = {c.slug for c in tree.classes}
    found_papers = await papers(conn, archive, sorted({c.paper for c in chosen if c.paper}))
    found: dict[str, Request | str] = {}
    for c in chosen:
        if c.role == "writer":
            found[c.key] = await _digest(conn, tree, topics, int(c.subject))
        elif (paper := found_papers.get(c.paper or "")) is None:
            found[c.key] = "the paper is in neither the database nor the archive"
        elif c.role in GRADERS:
            topic = topics.get(c.subject)
            name = tree.get(c.subject).name if topic else ""
            found[c.key] = grading.request(paper, topic, name) if topic else f"no topic {c.subject}"
        elif c.subject not in classes:
            found[c.key] = f"no class {c.subject}"
        elif c.role == "analyse":
            found[c.key] = analysis.request(paper, tree.get(c.subject))
        else:
            found[c.key] = await _brief(archive, paper, tree.get(c.subject))
    return found


async def _brief(archive: ArchiveClient | None, paper: Paper, area: Any) -> Request | str:
    if archive is None:
        return "no archive to read the text from"
    try:
        text = await stored_text(archive, paper)
    except ArchiveError as exc:
        return f"the archive: {exc}"
    if text is None:
        return "the archive holds no text of this version"
    return brief.request(paper, area, text.body, text.source)


async def _digest(
    conn: psycopg.AsyncConnection, tree: Any, topics: dict[str, Any], digest_id: int
) -> Request | str:
    """A published daily digest's request, over its papers as it showed them."""
    d = await digests_store.get(conn, digest_id)
    if d is None or d["text"] is None or d["period"] != DigestPeriod.DAY.value:
        return "not a written daily digest"
    topic = topics.get(d["topic"])
    if topic is None or topic.depth == Depth.FIELD:
        return "not a Focus or Area topic's"
    focus = topic.depth == Depth.FOCUS
    found = await digests_store.as_shown(conn, digest_id, focus)
    build = writer.focus_day if focus else writer.area_day
    return build(topic, tree.get(topic.slug).name, d["day"], found)


# -- the run ---------------------------------------------------------------------------------------


class Row(BaseModel):
    """One call: its case, the candidate asked, and what came back."""

    set: str
    role: str
    case: str
    paper: str | None
    candidate: str
    model: str  # `provider:model@effort`
    request: str  # `asked`: an answer to another request or configuration is not mixed in
    outcome: str  # answered, or a `CallError` kind
    detail: str | None = None
    output: dict[str, Any] | None = None
    cost: float | None = None  # None: unknown, never zero
    reserved: float = 0.0  # what it counts for against the ceiling while its cost is unknown
    input_tokens: int | None = None
    output_tokens: int | None = None  # thinking included
    reasoning_tokens: int | None = None
    seconds: float | None = None


def asked(candidate: Candidate, request: Request) -> str:
    """The request and the candidate's configuration, hashed."""
    material = json.dumps(
        [candidate.role.model_dump(mode="json"), request.key_material()], sort_keys=True
    )
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def rows(out: Path) -> list[Row]:
    """Every call recorded in `out`, in order, those asked again included."""
    found = []
    for path in sorted(out.glob("*.jsonl")):
        if path.name == BUDGET:
            continue
        found += [Row.model_validate_json(line) for line in path.read_text().splitlines() if line]
    return found


def latest(found: list[Row]) -> dict[tuple[str, str], Row]:
    """The latest call of each case and candidate."""
    return {(r.case, r.candidate): r for r in found}


def spent(found: list[Row]) -> float:
    """What the calls cost, each unknown charge at its reservation."""
    return sum(r.reserved if r.cost is None else r.cost for r in found)


def _tokens(request: Request, per_token: float) -> float:
    return (len(request.system) + len(request.user)) / per_token


def estimate(ladder: Ladder, candidate: Candidate, request: Request) -> float:
    """What a call is expected to cost."""
    price_in, price_out = candidate.price
    tokens = _tokens(request, CHARS_PER_TOKEN)
    return (tokens * price_in + ladder.output_tokens * price_out) / 1e6


def reservation(ladder: Ladder, candidate: Candidate, request: Request) -> float:
    """A call's upper bound, as far as its output stays within `reserve_output_tokens`."""
    price_in, price_out = candidate.price
    tokens = _tokens(request, RESERVE_CHARS_PER_TOKEN)
    return (tokens * price_in + ladder.reserve_output_tokens * price_out) / 1e6


@dataclass
class Budget:
    """The ceiling, what has been spent and what calls in flight have reserved."""

    ceiling: float
    spent: float
    reserved: float = 0.0
    stopped: bool = False

    def reserve(self, amount: float) -> bool:
        """Whether a call of upper bound `amount` may start; once one may not, none may."""
        if self.stopped or self.spent + self.reserved + amount > self.ceiling:
            self.stopped = True
            return False
        self.reserved += amount
        return True

    def settle(self, amount: float, cost: float | None) -> None:
        self.reserved -= amount
        self.spent += amount if cost is None else cost


@dataclass(frozen=True)
class Todo:
    case: Case
    ladder: Ladder
    candidate: Candidate
    request: Request


def todo(
    chosen: list[Case],
    ladders_: dict[str, Ladder],
    built: Mapping[str, Request | str],
    done: list[Row],
    only: list[str] | None = None,
) -> list[Todo]:
    """The calls left, case by case so that a run stopped early leaves whole cases: every
    case with a request, of every candidate (or those named in `only`), not yet answered or
    failed in a way that asking again would only repeat."""
    last = latest(done)
    found = []
    for c in chosen:
        request = built.get(c.key)
        if c.role not in ladders_ or not isinstance(request, Request):
            continue
        ladder = ladders_[c.role]
        for candidate in ladder.candidates:
            if only and candidate.name not in only:
                continue
            r = last.get((c.key, candidate.name))
            if r and r.request == asked(candidate, request) and r.outcome not in RETRIED:
                continue
            found.append(Todo(c, ladder, candidate, request))
    return found


def gateway(role: str, candidate: Candidate, providers: Mapping[str, Provider]) -> Gateway:
    """A gateway that serves `role` with the candidate alone, and validation's detail kept."""
    models = Models().model_copy(update={role: candidate.role})
    return Gateway(models, dict(providers), debug=True)


@dataclass
class Ran:
    """A run's calls, and where it stopped when the ceiling stopped it."""

    rows: list[Row] = field(default_factory=list)
    stopped_at: Todo | None = None


async def run(
    work: list[Todo],
    providers: Mapping[str, Provider],
    out: Path,
    ceiling: float,
    *,
    concurrency: int = 8,
    progress: Callable[[Row], None] | None = None,
) -> Ran:
    """Make the calls in `work`, in order, appending each to `out/<role>.jsonl` as it ends,
    while the ceiling allows (see the module's docstring)."""
    out.mkdir(parents=True, exist_ok=True)
    budget = Budget(ceiling, spent(rows(out)))
    gate = asyncio.Semaphore(concurrency)
    ran = Ran()

    async def one(t: Todo) -> None:
        async with gate:
            hold = reservation(t.ladder, t.candidate, t.request)
            if not budget.reserve(hold):
                if ran.stopped_at is None or work.index(t) < work.index(ran.stopped_at):
                    ran.stopped_at = t
                return
            started = time.monotonic()
            row = await _ask(t, providers)
            row.reserved = hold
            row.seconds = round(time.monotonic() - started, 2)
            budget.settle(hold, row.cost)
            with (out / f"{t.case.role}.jsonl").open("a", encoding="utf-8") as f:
                f.write(row.model_dump_json() + "\n")
            ran.rows.append(row)
            if progress:
                progress(row)

    await asyncio.gather(*(one(t) for t in work))
    if (t := ran.stopped_at) is not None:
        stop = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "ceiling": ceiling,
            "spent": round(budget.spent, 4),
            "first_not_run": {"case": t.case.key, "candidate": t.candidate.name},
            "not_run": len(work) - len(ran.rows),
        }
        with (out / BUDGET).open("a", encoding="utf-8") as f:
            f.write(json.dumps(stop) + "\n")
    return ran


async def _ask(t: Todo, providers: Mapping[str, Provider]) -> Row:
    def row(outcome: str, usage: Any, **rest: Any) -> Row:
        return Row(
            set=t.case.set,
            role=t.case.role,
            case=t.case.key,
            paper=t.case.paper,
            candidate=t.candidate.name,
            model=t.candidate.role.label,
            request=asked(t.candidate, t.request),
            outcome=outcome,
            **_usage(usage),
            **rest,
        )

    try:
        answer = await gateway(t.case.role, t.candidate, providers).ask(
            t.case.role, t.request, paper_id=t.case.paper
        )
    except CallError as exc:
        return row(exc.kind, exc.usage, detail=exc.detail)
    return row("answered", answer.usage, output=answer.value.model_dump(mode="json"))


def _usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    return {
        "cost": usage.cost,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


# -- the report ------------------------------------------------------------------------------------

POSITIVE = (Fit.CENTRAL.value, Fit.RELATED.value)
# What two answers to one case are compared on, by role.
COMPARED: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(GRADERS, ("Fit", "side")),
    "analyse": ("Weight", "contribution"),
    "brief": ("Weight", "overstatement line"),
    "writer": (),
}


def _same(role: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, bool]:
    """Whether two answers to one case agree on each of the role's `COMPARED`: for a grader the
    Fit, and its side of the line between related and adjacent."""
    if role in GRADERS:
        return {
            "Fit": a["fit"] == b["fit"],
            "side": (a["fit"] in POSITIVE) == (b["fit"] in POSITIVE),
        }
    if role == "analyse":
        return {
            "Weight": a["weight"] == b["weight"],
            "contribution": a["contribution"] == b["contribution"],
        }
    if role == "brief":
        line = (a["overstatement"] is None) == (b["overstatement"] is None)
        return {"Weight": a["weight"] == b["weight"], "overstatement line": line}
    return {}


def _share(hits: int, n: int) -> str:
    return f"{hits}/{n}" if n else "–"


def _dollars(found: list[Row]) -> str:
    """What the calls cost; a lower bound where a charge is unknown."""
    known = sum(r.cost for r in found if r.cost is not None)
    return f"{'≥ ' if any(r.cost is None for r in found) else ''}${known:.2f}"


def _table(head: list[str], body: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    return lines + ["| " + " | ".join(str(c) for c in row) + " |" for row in body]


def tables(ladders_: dict[str, Ladder], chosen: list[Case], found: list[Row]) -> str:
    """Per role and set, each candidate's outcomes, its agreement with the owner's Fit (over
    the labelled cases it answered) and with the role's own model (over the cases both
    answered), and its cost, as markdown, from the recorded calls alone. Outcomes are each
    case's latest call; costs are every call's."""
    last = latest(found)
    lines: list[str] = []
    for role, ladder in ladders_.items():
        for which in SETS:
            mine = [c for c in chosen if c.role == role and c.set == which]
            if mine:
                lines += ["", f"### `{role}`, {which} set", ""]
                lines += _role_table(ladder, mine, last, found)
    return "\n".join(lines).lstrip("\n") + "\n"


def _role_table(
    ladder: Ladder, mine: list[Case], last: dict[tuple[str, str], Row], found: list[Row]
) -> list[str]:
    keys = {c.key for c in mine}
    own = ladder.own.name
    owner = {c.key: c.label.value for c in mine if c.label is not None}
    head = ["Candidate", "Model", "Cases", "Answered", "Refused", "Invalid", "Truncated"]
    head += ["No answer"] + (["Fit as owner", "Side as owner"] if owner else [])
    head += [f"{what} as {own}" for what in COMPARED[ladder.role]] + ["Cost", "Cost an answer"]
    body = []
    for cand in ladder.candidates:
        outcome = {c.key: r for c in mine if (r := last.get((c.key, cand.name)))}
        kinds = Counter(r.outcome for r in outcome.values())
        row: list[object] = [cand.name, f"`{cand.role.label}`", len(outcome)]
        row += [kinds[k] for k in ("answered", "refused", "invalid", "truncated")]
        row.append(kinds["timeout"] + kinds["provider"])
        answered = {k: r for k, r in outcome.items() if r.outcome == "answered"}
        if owner:
            pairs = [
                (owner[k], r.output["fit"]) for k, r in answered.items() if k in owner and r.output
            ]
            row.append(_share(sum(a == b for a, b in pairs), len(pairs)))
            sides = sum((a in POSITIVE) == (b in POSITIVE) for a, b in pairs)
            row.append(_share(sides, len(pairs)))
        for what in COMPARED[ladder.role]:
            agreed = [
                _same(ladder.role, ref.output, r.output)[what]
                for k, r in answered.items()
                if (ref := last.get((k, own))) and ref.output is not None and r.output is not None
            ]
            row.append("–" if cand.name == own else _share(sum(agreed), len(agreed)))
        row.append(_dollars([r for r in found if r.candidate == cand.name and r.case in keys]))
        known = [r.cost for r in answered.values() if r.cost is not None]
        row.append(f"${sum(known) / len(known):.4f}" if known else "–")
        body.append(row)
    return _table(head, body)


def _said(role: str, r: Row) -> str:
    """What an answer says, for the owner to read: the Fit and its reason; the Weight and the
    Gist; or the overstatement line, the Weight and the Brief's first 80 words."""
    if r.outcome != "answered" or r.output is None:
        return f"*{r.outcome}*" + (f": {r.detail}" if r.detail else "")
    o = r.output
    if role in GRADERS:
        return f"**{o['fit']}**. {o['reason']}"
    if role == "analyse":
        return f"**{o['weight']}**. {o['gist']}"
    if role == "brief":
        words = o["brief"].split()
        excerpt = " ".join(words[:80]) + (" …" if len(words) > 80 else "")
        line = o["overstatement"] or "no overstatement line"
        return f"**{o['weight']}**. {line} {excerpt}"
    return f"{len(o['text'].split())} words. {o['gist']}"


def answers(
    ladders_: dict[str, Ladder],
    chosen: list[Case],
    found: list[Row],
    titles: dict[str, str] | None = None,
) -> str:
    """Every case of the refusals set with a note or a candidate that did not answer, each
    candidate's latest answer to it, as markdown, from the recorded calls alone."""
    last = latest(found)
    lines: list[str] = []
    for c in chosen:
        ladder = ladders_.get(c.role)
        if ladder is None or c.set != "refusals":
            continue
        said = [(cand, last.get((c.key, cand.name))) for cand in ladder.candidates]
        if not c.note and all(r is None or r.outcome == "answered" for _, r in said):
            continue
        title = (titles or {}).get(c.paper or "", "")
        lines += [
            "",
            f"#### `{c.role}` · {c.paper} · {c.subject}" + (f" · {title}" if title else ""),
        ]
        if c.note:
            lines += ["", c.note]
        lines.append("")
        lines += [f"- {cand.name}: {_said(c.role, r) if r else '*not asked*'}" for cand, r in said]
    return "\n".join(lines).lstrip("\n") + "\n"
