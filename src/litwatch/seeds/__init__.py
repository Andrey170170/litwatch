"""Topics as YAML: the starter set shipped with the package, and import and export of the
owner's own. Topics live in the database; these files are how they travel.

`taxonomy.yaml` is v1's seed: the class tree with its followed topics (docs/specs/v1/taxonomy.md).
Until the topics table is reshaped (N2) it is read from here, not from the database."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from litwatch.core import Topic
from litwatch.core.taxonomy import Taxonomy, TaxonomyClass

TAXONOMY = "taxonomy.yaml"


def from_spec(raw: dict[str, Any]) -> Topic:
    return Topic.model_validate({k: v for k, v in raw.items() if k != "revision"})


def load(path: Path) -> list[Topic]:
    """One topic per YAML file; a directory loads every `*.yaml` in it, sorted by name."""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    return [from_spec(yaml.safe_load(f.read_text()) or {}) for f in files if _is_topic(f.name)]


def _is_topic(name: str) -> bool:
    """Topic files are the YAML files beside the taxonomy, not the taxonomy itself."""
    return name.endswith(".yaml") and name != TAXONOMY


def starter() -> list[Topic]:
    """The topics shipped with the package: two Watch, two Radar and three Pulse examples."""
    root = resources.files("litwatch.seeds")
    return [
        from_spec(yaml.safe_load(f.read_text()) or {})
        for f in sorted(root.iterdir(), key=lambda f: f.name)
        if _is_topic(f.name)
    ]


def dump(topic: Topic) -> str:
    spec = topic.model_dump(mode="json", exclude={"revision", "position"}, exclude_none=True)
    if not spec["keywords"]:
        del spec["keywords"]
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=96)


def taxonomy(path: Path | None = None) -> Taxonomy:
    """The taxonomy in `path`, or the seed shipped with the package. Fails with every problem."""
    text = (
        path.read_text()
        if path
        else resources.files("litwatch.seeds").joinpath(TAXONOMY).read_text()
    )
    return taxonomy_from_spec(yaml.safe_load(text) or {})


def taxonomy_from_spec(raw: dict[str, Any]) -> Taxonomy:
    """Nested classes (`children`) flattened parents first; a followed class takes its topic's
    subject as its description and may not carry one of its own."""
    subjects = {t["class"]: t.get("subject") for t in raw.get("topics") or []}
    classes: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]], parent: str | None) -> None:
        for node in nodes:
            spec = {k: v for k, v in node.items() if k != "children"}
            if spec.get("slug") in subjects:
                if "description" in spec:
                    raise ValueError(
                        f"class {spec['slug']!r} is followed; its description is its topic's "
                        "subject"
                    )
                spec["description"] = subjects[spec["slug"]]
            classes.append({**spec, "parent": parent})
            walk(node.get("children") or [], spec.get("slug"))

    walk(raw.get("classes") or [], None)
    return Taxonomy.model_validate({"classes": classes, "topics": raw.get("topics") or []})


def dump_taxonomy(tree: Taxonomy) -> str:
    """The inverse of `taxonomy_from_spec`."""
    followed = {t.class_ for t in tree.topics}

    def node(c: TaxonomyClass) -> dict[str, Any]:
        spec: dict[str, Any] = {"slug": c.slug, "name": c.name}
        if c.slug not in followed:
            spec["description"] = c.description
        if c.threshold is not None:
            spec["threshold"] = c.threshold
        if children := tree.children(c.slug):
            spec["children"] = [node(child) for child in children]
        return spec

    topics = [
        t.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
        for t in tree.topics
    ]
    spec = {"classes": [node(c) for c in tree.children(None)], "topics": topics}
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=96)
