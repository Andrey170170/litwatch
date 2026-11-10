"""What `litwatch deep trace` and `deep notes` print: a paper's Deep attempts and one attempt's
trace, as the `deep` service recorded it (deep/README.md), its review rounds (a Deep read's,
docs/integrations.md), and the cards' process notes. For the agent reading what Deep did, not
for the owner. Long text is cut; `--json` gives it whole.

A trace written before P2 of docs/plans/deep-open-web.md has no failed call's reason, no fetch
record and no cell timeout mark; they print only where the trace has them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, tzinfo
from typing import Any

# How much of each kind of text is printed.
CODE_CHARS = 800
OUTPUT_CHARS = 800
TEXT_CHARS = 600
# Under an event, its details line up with its text, past the time.
INDENT = " " * 10


def cut(text: str, limit: int) -> str:
    """`text`, cut to `limit` characters with a note of how many more there were."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more characters]"


def _when(at: datetime | None, zone: tzinfo, day: bool = True) -> str:
    if at is None:
        return "-"
    return at.astimezone(zone).strftime("%Y-%m-%d %H:%M" if day else "%H:%M")


def _usage(usage: dict[str, Any] | None) -> str:
    if usage is None:
        return "usage unknown"
    parts = [f"{usage.get('turns', '?')} turns", f"{usage.get('cells', '?')} cells"]
    if "seconds" in usage:
        parts.append(f"{usage['seconds']} s")
    if "list_cost" in usage:
        parts.append(f"${usage['list_cost']:.2f}")
    return ", ".join(parts)


def _outcome(a: dict[str, Any]) -> str:
    if a["ended_at"] is None:
        return "running" if a["job_id"] else "submitted, no job yet"
    if a["wrote_card"]:
        return "wrote the card"
    if a["error"] is None:
        return "ended"
    return f"failed ({a['failure'] or 'unknown'}): {cut(a['error'], 120)}"


def attempt_lines(attempts: list[dict[str, Any]], zone: tzinfo) -> Iterator[str]:
    """One line per attempt, numbered from 1, oldest first."""
    for n, a in enumerate(attempts, 1):
        job = f"job {a['job_id']}" if a["job_id"] else "no job"
        trace = "trace" if a["traced"] else "no trace"
        yield (
            f"  {n:>2}  {_when(a['at'], zone)}  ended {_when(a['ended_at'], zone, day=False)}  "
            f"{job}  {_usage(a['usage'])}  {trace}  {_outcome(a)}"
        )


def _block(label: str, text: str, limit: int) -> Iterator[str]:
    """`text` under its label, indented, cut to `limit`; nothing when it is empty."""
    if not text:
        return
    lines = cut(text, limit).splitlines()
    yield f"{INDENT}{label}: {lines[0]}"
    for line in lines[1:]:
        yield f"{INDENT}{' ' * (len(label) + 2)}{line}"


def _fetched(record: dict[str, Any]) -> str:
    """Where a fetch went: each redirect, then the page that answered, or none."""
    steps = [f"{h['status']} {h['url']}" for h in record.get("hops", [])]
    if "status" in record:
        steps.append(f"{record['status']} {record['url']} · {record.get('bytes', 0)} bytes read")
    else:
        steps.append("no page")
    return " → ".join(steps)


def event_lines(trace: list[dict[str, Any]]) -> Iterator[str]:
    """The trace's events in order, each at its time since the job started."""
    turn = 0
    for e in trace:
        at = f"{e.get('t', 0) / 1000:>7.1f}s"
        kind = e.get("type")
        if kind == "assistant":
            turn += 1
            u = e.get("usage") or {}
            tokens = f"in {u.get('input', '?')} out {u.get('output', '?')}"
            tokens += f" reasoning {u.get('reasoning', '?')}"
            yield f"{at}  turn {turn} · {e.get('stop', '?')} · {tokens}"
            yield from _block("reasoning", e.get("thinking") or "", TEXT_CHARS)
            yield from _block("text", e.get("text") or "", TEXT_CHARS)
        elif kind == "cell":
            mark = " · timed out" if e.get("timedOut") else ""
            yield f"{at}  cell {e.get('n', '?')} · {e.get('ms', '?')} ms{mark}"
            yield from _block("code", e.get("code") or "", CODE_CHARS)
            yield from _block("output", e.get("output") or "", OUTPUT_CHARS)
            yield from _block("error", e.get("error") or "", OUTPUT_CHARS)
        elif kind == "host":
            ended = "ok" if e.get("ok") else "failed"
            if e.get("ok"):
                ended += f" · {e.get('chars', 0)} chars"
            elif e.get("error"):
                ended += f": {e['error']}"
            call = f"{e.get('name', '?')} {e.get('args', '')}"
            yield f"{at}  {call} · {e.get('ms', '?')} ms · {ended}"
            if "fetch" in e:
                yield f"{INDENT}went: {_fetched(e['fetch'])}"
        elif kind == "submit":
            if e.get("ok"):
                yield f"{at}  submit · ok"
            else:
                yield f"{at}  submit · rejected"
                for error in e.get("errors") or []:
                    yield f"{INDENT}- {cut(error, TEXT_CHARS)}"
        elif kind == "system":
            yield f"{at}  nudge: {cut(e.get('text') or '', TEXT_CHARS)}"
        else:
            yield f"{at}  {kind}: {cut(str(e), TEXT_CHARS)}"


# A review's scores, in the rubric's order (docs/specs/deep-read.md); any other follows by name.
SCORES = ("faithfulness", "clarity", "depth", "calibration")


def _scores(scores: Any) -> str:
    """A review's scores, each by its name. Stored JSON keeps no order of its own."""
    if not isinstance(scores, dict):
        return str(scores)
    names = [n for n in SCORES if n in scores] + sorted(n for n in scores if n not in SCORES)
    return ", ".join(f"{name} {scores[name]}" for name in names)


def review_lines(reviews: list[Any]) -> Iterator[str]:
    """The attempt's review rounds in order: each one's verdict, scores and failed checks, then
    its findings, most important first as the reviewer gave them. A round is printed by the
    fields it has (docs/integrations.md gives the shape), and whole when it is not an object."""
    for n, review in enumerate(reviews, 1):
        if not isinstance(review, dict):
            yield f"  round {n}: {cut(str(review), TEXT_CHARS)}"
            continue
        verdict = {True: "passed", False: "failed"}.get(review.get("passed"), "no verdict")
        scores = f" · {_scores(review['scores'])}" if "scores" in review else ""
        yield f"  round {review.get('round', n)} · {verdict}{scores}"
        checks = review.get("checks")
        if isinstance(checks, dict):
            if failed := sorted(name for name, ok in checks.items() if ok is False):
                yield f"{INDENT}checks failed: {', '.join(failed)}"
        elif checks is not None:
            yield f"{INDENT}checks: {cut(str(checks), TEXT_CHARS)}"
        for finding in review.get("findings") or []:
            if not isinstance(finding, dict):
                yield f"{INDENT}- {cut(str(finding), TEXT_CHARS)}"
                continue
            about = finding.get("about")
            where = finding.get("where")
            head = " ".join(x for x in (f"[{about}]" if about else "", where or "") if x)
            said = cut(str(finding.get("text", "")), TEXT_CHARS)
            yield f"{INDENT}- {head}: {said}" if head else f"{INDENT}- {said}"
            yield from _block("rewrite", str(finding.get("rewrite") or ""), TEXT_CHARS)


def note_lines(notes: list[dict[str, Any]], indent: str = "  ") -> Iterator[str]:
    """Process notes, one line each: kind, source, the note and its URL."""
    for note in notes:
        url = f" <{note['url']}>" if note.get("url") else ""
        said = f"{note.get('kind', '?')} ({note.get('source', '?')}): {note.get('note', '')}"
        yield f"{indent}{said}{url}"


def trace_text(
    paper_id: str,
    title: str | None,
    attempts: list[dict[str, Any]],
    chosen: int,
    trace: list[dict[str, Any]] | None,
    notes: list[dict[str, Any]],
    zone: tzinfo,
    reviews: list[Any] | None = None,
) -> str:
    """The paper's attempts, then attempt `chosen` (numbered from 1): its trace, its `reviews`
    (None: no review ran), and `notes`, the card's process notes, which the caller gives only
    when this attempt wrote the card."""
    a = attempts[chosen - 1]
    lines = [f"{paper_id} · {title or '(not in the collection)'}", "Attempts, oldest first:"]
    lines += attempt_lines(attempts, zone)
    lines += ["", f"Attempt {chosen} (id {a['id']}): {_outcome(a)}"]
    lines.append(f"  {_usage(a['usage'])}")
    if trace is None:
        lines.append(
            "  No trace was kept: its job never ran, was lost, never ended or is still running, "
            "or it ran before traces were kept per attempt, when a later job's replaced its own."
        )
    elif not trace:
        lines.append("  Its trace is empty.")
    else:
        lines += event_lines(trace)
    if reviews is not None:
        lines += ["", f"Reviews: {len(reviews)} round" + ("" if len(reviews) == 1 else "s")]
        lines += review_lines(reviews)
    if notes:
        lines += ["", "Process notes on the card:", *note_lines(notes)]
    return "\n".join(lines) + "\n"


def notes_text(cards: list[dict[str, Any]], zone: tzinfo) -> str:
    """The cards' process notes, newest card first, each under its paper."""
    if not cards:
        return "No process notes.\n"
    lines: list[str] = []
    for c in cards:
        lines.append(f"{_when(c['written_at'], zone)}  {c['paper_id']} · {c['title']}")
        lines += note_lines(c["notes"], indent="    ")
    return "\n".join(lines) + "\n"
