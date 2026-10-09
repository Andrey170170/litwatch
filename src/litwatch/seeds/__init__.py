"""Topics as YAML: the starter set shipped with the package, and import and export of the
owner's own. Topics live in the database; these files are how they travel."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from litwatch.core import Topic


def from_spec(raw: dict[str, Any]) -> Topic:
    return Topic.model_validate({k: v for k, v in raw.items() if k != "revision"})


def load(path: Path) -> list[Topic]:
    """One topic per YAML file; a directory loads every `*.yaml` in it, sorted by name."""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    return [from_spec(yaml.safe_load(f.read_text()) or {}) for f in files]


def starter() -> list[Topic]:
    """The topics shipped with the package: two Watch, two Radar and three Pulse examples."""
    root = resources.files("litwatch.seeds")
    return [
        from_spec(yaml.safe_load(f.read_text()) or {})
        for f in sorted(root.iterdir(), key=lambda f: f.name)
        if f.name.endswith(".yaml")
    ]


def dump(topic: Topic) -> str:
    spec = topic.model_dump(mode="json", exclude={"revision"}, exclude_none=True)
    if not spec["keywords"]:
        del spec["keywords"]
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=96)
