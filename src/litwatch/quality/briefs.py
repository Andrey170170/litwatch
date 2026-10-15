"""The Brief harness (docs/agents/verification.md): the `brief` role over the preference set's
papers, each read from an archive as a full-text pass reads it, and a report that puts each
paper's abstract beside its overstatement line, Brief and Gists for the owner to read. It writes
nothing to the database. Scoring is the rule checks and the owner's recorded judgments; the
judge model and the comparison with approved outputs join once the preference set has some."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable
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
from litwatch.core import Paper, Taxonomy
from litwatch.fulltext import brief
from litwatch.llm import CallError, Gateway

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
    return [Entry.model_validate(e) for e in yaml.safe_load(path.read_text())["papers"]]


class Result(BaseModel):
    """One paper's answer, or why there is none."""

    paper_id: str
    version: int | None = None
    source: str | None = None
    chars: int | None = None
    output: brief.Output | None = None
    model: str | None = None
    cost: float | None = None
    seconds: float | None = None
    error: str | None = None
    request: str  # `fingerprint`: answers to another request are not mixed into one run


def results(path: Path) -> dict[str, Result]:
    if not path.exists():
        return {}
    found = (Result.model_validate_json(line) for line in path.read_text().splitlines() if line)
    return {r.paper_id: r for r in found}


def spent(path: Path) -> float:
    """What every call of the run cost, failed ones and those answered again included."""
    if not path.exists():
        return 0.0
    lines = (line for line in path.read_text().splitlines() if line)
    return sum(Result.model_validate_json(line).cost or 0 for line in lines)


def fingerprint(gateway: Gateway) -> str:
    """The role's configuration and its instructions, hashed."""
    role = gateway.models.role(brief.ROLE).model_dump_json()
    return hashlib.sha256(f"{role}\n{brief.SYSTEM}".encode()).hexdigest()[:12]


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


async def run(
    gateway: Gateway,
    tree: Taxonomy,
    papers: list[tuple[Paper, Entry]],
    archive: ArchiveClient,
    out: Path,
    *,
    concurrency: int = 4,
    wait_seconds: float = 20 * 60,
    poll: float = 20.0,
    progress: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Write the Brief of every paper not yet answered in `out`, appending each answer as it
    arrives; a paper whose text could not be had is left to the next run. Refuses to resume a
    run made with another configuration of the role."""
    asked = fingerprint(gateway)
    done = {pid for pid, r in results(out).items() if r.output is not None}
    if other := {r.request for r in results(out).values()} - {asked}:
        raise ValueError(
            f"{out} holds answers to another request ({sorted(other)}); use a new file"
        )
    gate = asyncio.Semaphore(concurrency)
    found: list[Result] = []

    async def one(paper: Paper, entry: Entry) -> None:
        area = tree.get(entry.area) if entry.area else None
        text = await _text(archive, paper, wait_seconds, poll)
        if isinstance(text, str):
            result = Result(paper_id=paper.id, error=text, request=asked)
        else:
            async with gate:
                started = time.monotonic()
                try:
                    answer = await gateway.ask(
                        brief.ROLE,
                        brief.request(paper, area, text.body, text.source),
                        paper_id=paper.id,
                    )
                except CallError as exc:
                    cost = exc.usage.cost if exc.usage else None
                    result = Result(paper_id=paper.id, error=str(exc), cost=cost, request=asked)
                else:
                    result = Result(
                        paper_id=paper.id,
                        output=answer.value,
                        model=answer.model,
                        cost=answer.usage.cost,
                        request=asked,
                    )
                result.seconds = round(time.monotonic() - started, 2)
            result.version, result.source, result.chars = text.version, text.source, len(text.body)
        with out.open("a", encoding="utf-8") as f:
            f.write(result.model_dump_json() + "\n")
        found.append(result)
        if progress:
            progress(result)

    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.gather(*(one(p, e) for p, e in papers if p.id not in done))
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
    cost: float,
) -> str:
    """The report, as markdown, from the run alone: no calls. `cost` is what the run spent."""
    answered = [found[e.id] for e in entries if e.id in found and found[e.id].output]
    lines = [f"# {title}", ""]
    models = sorted({r.model for r in answered if r.model})
    lines += [
        f"{len(answered)} of {len(entries)} papers answered by {', '.join(models) or 'no model'}, "
        f"${cost:.2f} in all. Each paper's text was read from the archive as a full-text pass "
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
            lines.append(f"| {e.id} | {r.error if r else 'not run'} | {_owner(e)} | | | |")
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
            f"{r.source} · {r.chars:,} characters · {r.model} · ${r.cost or 0:.2f}",
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
