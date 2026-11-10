"""A Deep read as /mcp gives it (mcp.md, "A paper"; docs/specs/deep-read.md, Beyond the web
app): in `get_paper`, its At a glance and its sections, one line each; in `read_deep`, one
section as text. Tables come as Markdown tables, the paper's figures as their numbers and
captions, a diagram as what each of its nodes and edges does. Prose keeps the read's inline
marks (**bold**, *italic*, `code`, $tex$, links), which are Markdown's own.

It reads a stored read through `core.deepread`, taking a missing field as absent: litwatch
stores a read unvalidated beyond its format tag."""

from __future__ import annotations

from typing import Any

from litwatch.core import deepread

KINDS = {
    "glance": "at a glance",
    "problem": "problem",
    "method": "method",
    "finding": "finding",
    "claims": "claims",
    "lens": "lens",
    "limits": "limitations",
    "related": "related work",
    "repro": "reproducibility",
}


def _row(cells: list[Any]) -> str:
    return "| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |"


def _facts(facts: list[dict[str, Any]]) -> list[str]:
    """Facts by name; a tag says what is there (yes) or missing (no)."""
    out = []
    for fact in facts:
        tags = [
            t.get("text", "") + (f" ({t['state']})" if t.get("state") else "")
            for t in fact.get("tags") or []
        ]
        said = " · ".join(x for x in (fact.get("text"), ", ".join(tags)) if x)
        out.append(f"- {fact.get('name', '')}: {said}")
    return out


def _table(block: dict[str, Any]) -> list[str]:
    """A Markdown table. A group heading is a row of its own in bold, a totals row is bold, and
    a cell's note follows the table under the cell's mark."""
    columns = block.get("columns") or []
    out = [_row(columns), _row(["---"] * len(columns))]
    notes: list[str] = []
    for row in block.get("rows") or []:
        if "group" in row:
            out.append(_row([f"**{row['group']}**", *[""] * (len(columns) - 1)]))
            continue
        cells = []
        for cell in row.get("cells") or []:
            if isinstance(cell, dict):
                notes.append(str(cell.get("note", "")))
                cell = f"{cell.get('value', '')} [{len(notes)}]"
            cells.append(f"**{cell}**" if row.get("sum") and cell != "" else cell)
        out.append(_row(cells))
    out += [f"[{n}] {note}" for n, note in enumerate(notes, 1)]
    if caption := block.get("caption"):
        out.append(f"Table: {caption}")
    return out


def _diagram(block: dict[str, Any]) -> list[str]:
    """A diagram as what it says: its caption, then each node and each edge with its note."""
    nodes = block.get("nodes") or []
    names = {n.get("id"): n.get("label", n.get("id")) for n in nodes}

    def where(part: dict[str, Any]) -> str:
        return f" ({part['where']})" if part.get("where") else ""

    out = [f"Diagram: {block.get('caption') or 'drawn by the read'}"]
    if lanes := [lane.get("text", "") for lane in block.get("lanes") or []]:
        out.append(f"Lanes: {', '.join(lanes)}")
    for n in nodes:
        sub = f" ({n['sub']})" if n.get("sub") else ""
        out.append(f"- **{n.get('label', n.get('id'))}**{sub}: {n.get('tip', '')}{where(n)}")
    for e in block.get("edges") or []:
        ends = f"{names.get(e.get('from'), e.get('from'))} → {names.get(e.get('to'), e.get('to'))}"
        label = f" ({e['label']})" if e.get("label") else ""
        title = f"{e['title']}: " if e.get("title") else ""
        out.append(f"- **{ends}**{label}: {title}{e.get('tip', '')}{where(e)}")
    return out


def _block(block: dict[str, Any]) -> list[str]:
    kind = block.get("type")
    if kind == "p":
        return [block.get("text", "")]
    if kind == "h3":
        return [f"### {block.get('text', '')}"]
    if kind == "list":
        return [f"- {item}" for item in block.get("items") or []]
    if kind == "why":
        return [f"> **Why (the paper's reason):** {block.get('text', '')}"]
    if kind == "note":
        return [
            f"> **Note (the read's own, on {block.get('where', '?')}):** {block.get('text', '')}"
        ]
    if kind == "eq":
        caption = [block["caption"]] if block.get("caption") else []
        return [f"$$ {block.get('tex', '')} $$", *caption]
    if kind == "fig":
        return [f"[{block.get('number', 'Figure')}: {block.get('caption', '')}]"]
    if kind == "xp":
        label = f" {block['label']}" if block.get("label") else ""
        parts = (("Did", "did"), ("Found", "found"), ("Means", "means"), ("Not shown", "not_shown"))
        return [
            f"**Experiment{label}: {block.get('question', '')}**",
            *(f"- {name}: {block[key]}" for name, key in parts if block.get(key)),
        ]
    if kind == "table":
        return _table(block)
    if kind == "diagram":
        return _diagram(block)
    return [f"[{kind}: a block this server does not know]"]


def _blocks(blocks: list[dict[str, Any]]) -> list[str]:
    """Blocks in reading order, a blank line between them."""
    out: list[str] = []
    for block in blocks:
        out += [*([""] if out else []), *_block(block)]
    return out


def _glance(section: dict[str, Any]) -> list[str]:
    out = [f"Key claim: {section.get('claim', '')}"]
    out += [f"- {point}" for point in section.get("points") or []]
    if facts := section.get("facts"):
        out += ["", "Facts:", *_facts(facts)]
    return out


def _body(read: dict[str, Any], section: dict[str, Any]) -> list[str]:
    """One section's content, by its kind."""
    kind = section.get("kind")
    if kind == "glance":
        return _glance(section)
    if kind == "claims":
        out = []
        for c in section.get("claims") or []:
            out.append(f"- **{c.get('verdict', '?')}**: {c.get('text', '')}")
            out.append(f"  Evidence ({c.get('where', '?')}): {c.get('evidence', '')}")
        return out
    if kind == "limits":
        return [
            f"- ({deepread.LIMITATION_BY.get(x.get('by'), x.get('by'))}) {x.get('text', '')}"
            for x in section.get("limitations") or []
        ]
    if kind == "related":
        out = []
        for work in section.get("prior_work") or []:
            arxiv = f" (arXiv:{work['arxiv_id']})" if work.get("arxiv_id") else ""
            out.append(f"- {work.get('title', '')}{arxiv}: {work.get('relation', '')}")
        if among := read.get("among_collection"):
            out += ["", "Among litwatch's collection:"]
            for a in among:
                note = f": {a['note']}" if a.get("note") else ""
                out.append(
                    f"- {a.get('title', '')} (arXiv:{a.get('arxiv_id', '?')}), "
                    f"{a.get('relation', '?')}{note}"
                )
        return out
    if kind == "repro":
        return _facts(section.get("facts") or [])
    return _blocks(section.get("blocks") or [])


def _note_lines(read: dict[str, Any]) -> list[str]:
    return [
        f"- {n.get('kind', '?')} ({n.get('source', '?')}): {n.get('note', '')}"
        + (f" <{n['url']}>" if n.get("url") else "")
        for n in read.get("process_notes") or []
    ]


def summary(read: dict[str, Any]) -> list[str]:
    """What `get_paper` shows of a read: whether it failed its review, its At a glance, its
    sections by id with their takeaways, and its process notes."""
    out = []
    if deepread.review_note(read) is not None:
        out.append("It did not pass its review (its process notes say on what).")
    if glance := deepread.section(read, "glance"):
        out += ["", "### At a glance", *_glance(glance)]
    out += ["", "### Sections (read one with read_deep(paper_id, section=its id))"]
    for s in deepread.sections(read):
        takeaway = f": {s['takeaway']}" if s.get("takeaway") else ""
        out.append(f"- `{s.get('id', '?')}` · {s.get('title', '')}{takeaway}")
    if notes := _note_lines(read):
        out += ["", "### Process notes (what the run could not do)", *notes]
    if url := read.get("code_url"):
        out += ["", f"Code: {url}"]
    return out


def ids(read: dict[str, Any]) -> list[str]:
    """The ids of the read's sections, in order."""
    return [s["id"] for s in deepread.sections(read) if s.get("id")]


def section_text(read: dict[str, Any], section_id: str) -> list[str] | None:
    """One section of the read as text: its title, kind and takeaway, then its content. None
    when the read has no section of that id."""
    found = next((s for s in deepread.sections(read) if s.get("id") == section_id), None)
    if found is None:
        return None
    kind = found.get("kind", "")
    named = KINDS.get(kind, kind)
    if kind == "lens" and found.get("topic"):
        named += f" for the topic {found['topic']}"
    out = [f"## {found.get('title', section_id)} ({named})"]
    if found.get("takeaway"):
        out.append(f"In one line: {found['takeaway']}")
    return [*out, "", *_body(read, found)]
