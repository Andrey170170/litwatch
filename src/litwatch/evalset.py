"""Labeled evaluation set on disk: YAML with paper metadata and expected flags."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from litwatch.models import Flags, Paper

SCHEMA_VERSION = 1


class LabeledPaper(BaseModel):
    paper: Paper
    expected: Flags | None = None
    note: str | None = None


class EvalSet(BaseModel):
    schema_version: int = SCHEMA_VERSION
    papers: list[LabeledPaper] = Field(default_factory=list)


def _to_entry(item: LabeledPaper) -> dict:
    entry = item.paper.model_dump(exclude_none=True)
    entry["expected"] = item.expected.model_dump() if item.expected else None
    if item.note:
        entry["note"] = item.note
    return entry


def _from_entry(entry: dict) -> LabeledPaper:
    entry = dict(entry)
    expected = entry.pop("expected", None)
    note = entry.pop("note", None)
    return LabeledPaper(
        paper=Paper(**entry),
        expected=Flags(**expected) if expected else None,
        note=note,
    )


def load(path: Path) -> EvalSet:
    raw = yaml.safe_load(path.read_text()) or {}
    if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError(f"unsupported eval set schema {raw.get('schema_version')}")
    return EvalSet(papers=[_from_entry(e) for e in raw.get("papers", [])])


def save(path: Path, evalset: EvalSet) -> None:
    doc = {
        "schema_version": evalset.schema_version,
        "papers": [_to_entry(p) for p in evalset.papers],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
