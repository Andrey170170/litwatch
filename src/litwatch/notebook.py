"""Trace export: the deep-processing log of one paper as a Jupyter notebook, for reading."""

from __future__ import annotations

import json
from typing import Any


def _md(source: str) -> dict[str, Any]:
    return {"cell_type": "markdown", "metadata": {}, "source": source}


def _code(source: str, output: str, error: str | None, n: int) -> dict[str, Any]:
    outputs: list[dict[str, Any]] = []
    if output:
        outputs.append({"output_type": "stream", "name": "stdout", "text": output})
    if error:
        outputs.append({"output_type": "stream", "name": "stderr", "text": error})
    return {
        "cell_type": "code",
        "execution_count": n,
        "metadata": {},
        "source": source,
        "outputs": outputs,
    }


def trace_notebook(row: dict[str, Any]) -> dict[str, Any]:
    """Notebook cells from a paper row's `deep_trace`; assistant turns become markdown."""
    stats = row.get("deep_stats") or {}
    cells = [
        _md(
            f"# {row['title']}\n\narXiv {row['id']} · {row.get('enrichment_model') or ''} · "
            f"{stats.get('cells', '?')} cells, {stats.get('turns', '?')} turns, "
            f"{stats.get('seconds', '?')}s, list cost ${stats.get('list_cost', 0):.2f}"
            + (f"\n\n**Error:** {row['enrichment_error']}" if row.get("enrichment_error") else "")
        )
    ]
    for ev in row.get("deep_trace") or []:
        kind = ev["type"]
        if kind == "assistant":
            parts = []
            if ev.get("thinking"):
                parts.append(
                    f"<details><summary>thinking</summary>\n\n{ev['thinking']}\n\n</details>"
                )
            if ev.get("text"):
                parts.append(ev["text"])
            if parts:
                cells.append(_md("\n\n".join(parts)))
        elif kind == "cell":
            cells.append(_code(ev["code"], ev.get("output") or "", ev.get("error"), ev["n"]))
        elif kind == "host":
            status = "ok" if ev.get("ok") else "failed"
            cells.append(
                _md(
                    f"`{ev['name']}` {ev.get('args', '')} → {status}, "
                    f"{ev.get('ms')} ms, {ev.get('chars', 0)} chars"
                )
            )
        elif kind == "submit":
            if ev["ok"]:
                cells.append(_md("**submit: accepted**"))
            else:
                cells.append(
                    _md(
                        "**submit: rejected**\n\n"
                        + "\n".join(f"- {e}" for e in ev.get("errors") or [])
                    )
                )
        elif kind == "system":
            cells.append(_md(f"*system:* {ev['text']}"))
    if row.get("enrichment"):
        cells.append(
            _md("## Result\n\n```json\n" + json.dumps(row["enrichment"], indent=1) + "\n```")
        )
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "javascript", "display_name": "litwatch-deep"},
            "language_info": {"name": "javascript"},
        },
        "cells": cells,
    }
