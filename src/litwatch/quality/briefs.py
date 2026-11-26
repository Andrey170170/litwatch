"""The Brief harness (docs/agents/verification.md): the `brief` role over the preference set's
papers, each read from an archive as a full-text pass reads it, and a report that puts each
paper's abstract beside its overstatement line, Brief and Gists for the owner to read. It writes
nothing production reads: its calls go in an evaluation run's journal, which nothing reuses.
`left` is what a run resumes with, and `ask` writes the Briefs. Scoring is the rule checks and
the owner's recorded judgments; the judge model and the comparison with approved outputs join
once the preference set has some."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from pydantic import BaseModel

from litwatch.archive import (
    URGENT,
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    PaperText,
    read_text,
)
from litwatch.config import Models, RoleConfig
from litwatch.core import Paper, Taxonomy
from litwatch.core.budget import CallStart
from litwatch.fulltext import brief, trim
from litwatch.llm import CallError, ModelGate, Usage
from litwatch.llm.gateway import Journal

# The Brief asked for, and the Gist's hard limit (`brief.SYSTEM`, `brief.Output`).
ASKED_BRIEF_WORDS = (120, 200)
GIST_LIMIT = 30
# Writing to a reader or judging the paper for one (`brief.SYSTEM`, the objective voice).
VOICE = re.compile(
    r"\byou(r)?\b|\bfor (practitioners|anyone|those|researchers|builders)\b|must.read|"
    r"worth reading|required reading|^this paper",
    re.IGNORECASE,
)


class Entry(BaseModel):
    """One paper of the preference set with the owner's recorded judgments: whether the abstract
    overstates (None until read), the facts a Brief must keep and the claims none may make."""

    id: str
    area: str | None = None  # the class its Weight is judged in, as a full-text pass is given it
    overstates: bool | None = None
    key_facts: list[str] = []
    must_not: list[str] = []
    note: str | None = None


def load(path: Path) -> list[Entry]:
    return [
        Entry.model_validate(e) for e in yaml.safe_load(path.read_text(encoding="utf-8"))["papers"]
    ]


class Result(BaseModel):
    """One paper's answer, or why there is none."""

    paper_id: str
    version: int | None = None
    source: str | None = None
    chars: int | None = None
    output: brief.Output | None = None
    model: str | None = None
    cost: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None  # thinking included
    seconds: float | None = None
    error: str | None = None
    request: str  # `fingerprint`: answers to another request are not mixed into one run
    inputs: str | None = None  # `inputs`: an answer to other inputs is written again


def results(path: Path) -> dict[str, Result]:
    if not path.exists():
        return {}
    found = (
        Result.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )
    return {r.paper_id: r for r in found}


def spent(path: Path) -> list[float | None]:
    """What each call of the run cost, failed ones and those answered again included; None
    where the charge is unknown."""
    if not path.exists():
        return []
    lines = (line for line in path.read_text(encoding="utf-8").splitlines() if line)
    return [Result.model_validate_json(line).cost for line in lines]


def dollars(costs: list[float | None]) -> str:
    """What `costs` add up to, marked as a lower bound where a charge is unknown."""
    if costs and all(c is None for c in costs):
        return "cost unknown"
    known = sum(c for c in costs if c is not None)
    return f"{'≥ ' if None in costs else ''}${known:.2f}"


def inputs(paper: Paper, entry: Entry) -> str:
    """What one paper's request is made from besides its text, which its version stands for (its
    last update where the version is unknown), hashed."""
    asked = repr((paper.title, paper.abstract, paper.version, paper.updated, entry.area))
    return hashlib.sha256(asked.encode()).hexdigest()[:12]


# What the model is sent of a paper's text: as passes send it, as read, or also without the
# appendix (docs/plans/model-spend.md, P1).
TEXTS: dict[str, Callable[[str, str], str]] = {
    "pass": trim.without_references,
    "as-read": lambda text, _source: text,
    "no-appendix": trim.without_appendix,
}


def fingerprint(role: RoleConfig, text: str = "pass") -> str:
    """The role's configuration, its fallbacks included, its instructions and the text variant,
    hashed."""
    configured = role.model_dump_json()
    return hashlib.sha256(f"{configured}\n{brief.SYSTEM}\n{text}".encode()).hexdigest()[:12]


class _Spent:
    """The calls' journal (`journal`, an evaluation run's, when given), which adds up each
    paper's charges as it goes, so a refusal the fallback answered counts in what its paper
    cost; a call with no known charge (none reported, or none recorded, as after a timeout)
    makes the total unknown. Nothing is reused: an evaluation asks afresh, and nothing batches
    (`observes`)."""

    observes: bool = False

    def __init__(self, journal: Journal | None = None) -> None:
        self.journal = journal
        self.run_id: int | None = journal.run_id if journal else 0  # no journal: rows of its own
        self.calls: dict[int, tuple[str | None, float | None]] = {}  # paper, charge, by call id

    def cost(self, paper_id: str) -> float | None:
        charges = [cost for paper, cost in self.calls.values() if paper == paper_id]
        return None if None in charges else sum(charges)  # type: ignore[arg-type]

    async def cached(self, key: str) -> None:
        return None

    async def start(self, call: CallStart) -> int:
        call_id = await self.journal.start(call) if self.journal else len(self.calls)
        self.calls[call_id] = (call.paper_id, None)
        return call_id

    async def record(self, call_id: int, usage: Usage) -> None:
        if self.journal:
            await self.journal.record(call_id, usage)
        self.calls[call_id] = (self.calls[call_id][0], usage.cost)

    async def accept(self, call_id: int, output: dict) -> None:
        if self.journal:
            await self.journal.accept(call_id, output)

    async def reject(
        self,
        call_id: int,
        error: str,
        reply: str | None = None,
        *,
        account: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        if self.journal:
            await self.journal.reject(call_id, error, reply, account=account, usage=usage)
        if usage is not None:
            self.calls[call_id] = (self.calls[call_id][0], usage.cost)

    async def refused(self, key: str, paper_id: str | None) -> bool:
        return False

    async def in_batch(self, key: str, work_id: int | None, owned: list[int]) -> None:
        return None


def _usage(usage: Usage | None) -> dict:
    if usage is None:
        return {}
    return {
        "cost": usage.cost,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
    }


async def _text(
    archive: ArchiveClient, paper: Paper, wait_seconds: float, poll: float
) -> PaperText | str:
    """The paper's text, asked for at urgent priority and waited for while the archive fetches
    it, or why there is none."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if (text := await read_text(archive, paper, URGENT)) is not None:
                return text
        except ArchiveUnavailable:
            pass  # tried again, as a pass is
        except ArchiveError as exc:  # TextUnavailable included
            return str(exc)
        if time.monotonic() > deadline:
            return "the archive is still fetching its text, or cannot be reached"
        await asyncio.sleep(poll)


def left(out: Path, asked: str, papers: list[tuple[Paper, Entry]]) -> list[tuple[Paper, Entry]]:
    """The papers not yet answered in `out`, to resume a run: a paper whose title, abstract,
    version, update or area changed since its answer is written again. Refuses to resume a run
    made with another configuration of the role or text variant (another `fingerprint`,
    `asked`)."""
    found = results(out)
    if other := {r.request for r in found.values()} - {asked}:
        raise ValueError(
            f"{out} holds answers to another request ({sorted(other)}); use a new file"
        )
    done = {pid for pid, r in found.items() if r.output and r.inputs == inputs(*_of(papers, pid))}
    return [(p, e) for p, e in papers if p.id not in done]


def _of(papers: list[tuple[Paper, Entry]], pid: str) -> tuple[Paper, Entry]:
    return next((p, e) for p, e in papers if p.id == pid)


async def ask(
    models: ModelGate,
    role: RoleConfig,
    tree: Taxonomy,
    papers: list[tuple[Paper, Entry]],
    archive: ArchiveClient,
    keep: Callable[[Result], Awaitable[None]],
    *,
    journal: Journal | None = None,
    concurrency: int = 4,
    text: str = "pass",
    wait_seconds: float = 20 * 60,
    poll: float = 20.0,
) -> list[Result]:
    """Write the Brief of every paper through `models`, as the `brief` role configured `role`,
    each answer kept (`keep`) as it arrives with what its calls were charged, its calls in
    `journal` when given; a paper whose text could not be had is answered with why, and left to
    the next run. One that fails to be kept stops the rest."""
    spent = _Spent(journal)
    gateway = models.on(spent, models=Models().model_copy(update={brief.ROLE: role}))
    asked = fingerprint(role, text)
    prepare = TEXTS[text]
    gate = asyncio.Semaphore(concurrency)
    found: list[Result] = []

    async def one(paper: Paper, entry: Entry) -> None:
        area = tree.get(entry.area) if entry.area else None
        given = inputs(paper, entry)
        read = await _text(archive, paper, wait_seconds, poll)
        if isinstance(read, str):
            result = Result(  # no call, so nothing charged
                paper_id=paper.id, error=read, cost=0.0, request=asked, inputs=given
            )
        else:
            async with gate:
                started = time.monotonic()
                try:
                    answer = await gateway.ask(
                        brief.ROLE,
                        brief.request(paper, area, read.body, read.source, prepare=prepare),
                        paper_id=paper.id,
                    )
                except CallError as exc:
                    result = Result(
                        paper_id=paper.id,
                        error=str(exc),
                        **_usage(exc.usage),
                        request=asked,
                        inputs=given,
                    )
                else:
                    result = Result(
                        paper_id=paper.id,
                        output=answer.value,
                        model=answer.model,
                        **_usage(answer.usage),
                        request=asked,
                        inputs=given,
                    )
                result.seconds = round(time.monotonic() - started, 2)
                result.cost = spent.cost(paper.id)
            result.version, result.source = read.version, read.source
            result.chars = len(prepare(read.body, read.source))
        await keep(result)
        found.append(result)

    async with asyncio.TaskGroup() as group:
        for p, e in papers:
            group.create_task(one(p, e))
    return found


# -- the report ------------------------------------------------------------------------------------


def _words(text: str) -> int:
    return len(text.split())


def checks(o: brief.Output) -> list[str]:
    """The rule checks that fail: the asked lengths and the objective voice."""
    failed = []
    low, high = ASKED_BRIEF_WORDS
    if not low <= _words(o.brief) <= high:
        failed.append(f"Brief of {_words(o.brief)} words (asked {low}–{high})")
    if _words(o.gist) > GIST_LIMIT:
        failed.append(f"Gist of {_words(o.gist)} words (limit {GIST_LIMIT})")
    for name, text in (("Brief", o.brief), ("Gist", o.gist)):
        if m := VOICE.search(text):
            failed.append(f"{name} voice: “{m.group(0)}”")
    return failed


def _owner(entry: Entry) -> str:
    return {None: "not read yet", True: "overstates", False: "does not overstate"}[entry.overstates]


def report(
    title: str,
    entries: list[Entry],
    papers: dict[str, Paper],
    found: dict[str, Result],
    abstract_gists: dict[str, str],
    spent: list[float | None],
) -> str:
    """The report, as markdown, from the run alone: no calls. `spent` is what each call of the
    run cost, as `spent` reads it. An
    answer to other inputs than the paper's and entry's now is left out."""
    stale = {
        e.id
        for e in entries
        if e.id in found and (e.id not in papers or found[e.id].inputs != inputs(papers[e.id], e))
    }
    found = {pid: r for pid, r in found.items() if pid not in stale}
    answered = [found[e.id] for e in entries if e.id in found and found[e.id].output]
    lines = [f"# {title}", ""]
    models = sorted({r.model for r in answered if r.model})
    lines += [
        f"{len(answered)} of {len(entries)} papers answered by {', '.join(models) or 'no model'}, "
        f"{dollars(spent)} in all. Each paper's text was read from the archive as a full-text pass "
        "reads it. The owner marks whether the abstract overstates in the preference set "
        "(`evals/preferences/briefs.yaml`); the milestone check is that the overstatement line "
        "appears where the owner agrees and nowhere else, and that no Gist repeats a claim its "
        "Brief corrects.",
        "",
        "| Paper | Overstatement line | Owner | Brief words | Gist words | Checks |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for e in entries:
        r = found.get(e.id)
        if r is None or r.output is None:
            why = "answered for other inputs: run it again" if e.id in stale else "not run"
            lines.append(f"| {e.id} | {r.error if r else why} | {_owner(e)} | | | |")
            continue
        o = r.output
        failed = checks(o)
        lines.append(
            f"| {e.id} | {'yes' if o.overstatement else 'no'} | {_owner(e)} | {_words(o.brief)} | "
            f"{_words(o.gist)} | {'; '.join(failed) or 'pass'} |"
        )
    for e in entries:
        p, r = papers.get(e.id), found.get(e.id)
        if p is None or r is None or r.output is None:
            continue
        o = r.output
        lines += [
            "",
            f"## {p.id} · {p.title}",
            "",
            f"[arXiv]({p.url}) · area {e.area or 'not given'} · v{r.version or '?'} from its "
            f"{r.source} · {r.chars:,} characters · {r.model} · {dollars([r.cost])}",
            "",
            f"**Abstract.** {p.abstract}",
            "",
            f"**Overstatement.** {o.overstatement or 'none'}",
            "",
            f"**Brief.** {o.brief}",
            "",
            f"**Gist from the full text.** {o.gist}",
            "",
            f"**Gist from the abstract.** {abstract_gists.get(e.id) or 'none written'}",
            "",
            f"**Weight.** {o.weight.value}: {o.weight_reason}",
            "",
            f"**Owner.** {_owner(e)}" + (f". {e.note}" if e.note else ""),
        ]
        for heading, items in (("Must keep", e.key_facts), ("Must not say", e.must_not)):
            if items:
                lines += ["", f"{heading}:", *(f"- {i}" for i in items)]
    return "\n".join(lines) + "\n"
