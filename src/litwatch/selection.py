"""Selections: the human-written specs that say what a topic is and what makes a paper tier 1.

A selection is authored in Notion (or, for seeds and experiments, a YAML file), synced into the
`selections` table, and rendered into the screener prompt. Its `version` is a content hash, so a
verdict records which wording it was judged under and an edit re-screens only what it touches.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from litwatch.models import Paper

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_TEXT = 2000  # a Notion text property holds this much; keeps rubrics short on purpose


class Status(StrEnum):
    DRAFT = "draft"  # validated and stored, not screened
    ACTIVE = "active"  # screened, routed, projected
    PAUSED = "paused"  # existing verdicts kept, no new judgements


class Selection(BaseModel):
    slug: str = Field(pattern=SLUG_RE.pattern)
    name: str = Field(min_length=2, max_length=80)
    subject: str = Field(min_length=20, max_length=MAX_TEXT)
    rubric: str | None = Field(default=None, max_length=MAX_TEXT, description="Tier 1 rubric")
    keywords: list[str] = Field(default_factory=list, description="Regexes for the recall audit")
    owner: str | None = None
    status: Status = Status.ACTIVE
    digest: bool = Field(default=False, description="Gets its own digest per run")
    notion_id: str | None = None

    @field_validator("rubric", mode="before")
    @classmethod
    def _blank_rubric_is_none(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @field_validator("subject", mode="before")
    @classmethod
    def _strip_subject(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("keywords", mode="before")
    @classmethod
    def _keywords(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [k.strip() for k in re.split(r"[,\n]", value)]
        items = [str(k).strip() for k in value if str(k).strip()]
        for pattern in items:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"keyword {pattern!r} is not a valid regex: {exc}") from exc
        return items

    @property
    def has_rubric(self) -> bool:
        return self.rubric is not None

    @property
    def max_tier(self) -> int:
        """Without a rubric nobody has said what is required reading, so tier 1 is unreachable."""
        return 1 if self.has_rubric else 2

    @property
    def field(self) -> str:
        """The slug as a structured-output field name."""
        return self.slug.replace("-", "_")

    @property
    def version(self) -> str:
        """Content hash of what the screener sees; owner, status, digest, and ids do not count."""
        content = json.dumps(
            [self.name, self.subject, self.rubric, self.keywords], ensure_ascii=False
        )
        return hashlib.sha256(content.encode()).hexdigest()[:12]

    def keyword_hit(self, paper: Paper, *, ignored_keywords: tuple[str, ...] = ()) -> bool:
        """Match authored regexes, optionally excluding exact patterns for a caller's policy."""
        ignored = {k.casefold() for k in ignored_keywords}
        keywords = [k for k in self.keywords if k.casefold() not in ignored]
        if not keywords:
            return False
        pattern = re.compile("|".join(f"(?:{k})" for k in keywords), re.IGNORECASE)
        return bool(pattern.search(f"{paper.title}\n{paper.abstract}"))


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not SLUG_RE.match(slug):
        raise ValueError(f"cannot derive a slug from {name!r}")
    return slug


def versions_hash(versions: dict[str, str]) -> str:
    """Hash of a set of (slug, version) pairs: the identity of one screener prompt."""
    content = json.dumps(sorted(versions.items()))
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def selections_version(selections: list[Selection]) -> str:
    """Hash of the whole active set: the screener prompt changes when it changes."""
    return versions_hash({s.slug: s.version for s in selections})


def active(selections: list[Selection]) -> list[Selection]:
    return [s for s in selections if s.status == Status.ACTIVE]


# -- prompt rendering ------------------------------------------------------------------------------


def render_selections(selections: list[Selection]) -> str:
    blocks = []
    for s in selections:
        lines = [f"## {s.name} (`{s.slug}`)", f"Subject: {s.subject}"]
        if s.rubric:
            lines.append(f"Tier 1 rubric: {s.rubric}")
        else:
            lines.append("No tier 1 rubric: the best tier for this selection is 2.")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# -- YAML specs: seeds shipped with the package, and files for trying a rubric before Notion -------


def from_spec(raw: dict[str, Any]) -> Selection:
    raw = dict(raw)
    if not raw.get("slug") and raw.get("name"):
        raw["slug"] = slugify(raw["name"])
    return Selection.model_validate(raw)


def load_specs(path: Path) -> list[Selection]:
    """One selection per YAML file; a directory loads every `*.yaml` in it, sorted by name."""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    return [from_spec(yaml.safe_load(f.read_text()) or {}) for f in files]


def seeds() -> list[Selection]:
    """The selections shipped with the package: what the Notion data source starts from."""
    root = resources.files("litwatch.seeds")
    return [
        from_spec(yaml.safe_load(f.read_text()) or {})
        for f in sorted(root.iterdir(), key=lambda f: f.name)
        if f.name.endswith(".yaml")
    ]
