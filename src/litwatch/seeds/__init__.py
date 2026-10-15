"""The taxonomy as YAML: the seed shipped with the package (`taxonomy.yaml`, built from
docs/specs/v1/taxonomy.md), and the format `litwatch taxonomy import` and `export` use. The
taxonomy lives in the database; these files are how it travels."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from litwatch.core.taxonomy import Taxonomy, TaxonomyClass

TAXONOMY = "taxonomy.yaml"


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
    if not isinstance(raw, dict):
        raise ValueError("a taxonomy is a mapping with `classes` and `topics`")
    if unknown := sorted(set(raw) - {"classes", "topics"}):
        raise ValueError(f"unknown taxonomy keys {unknown}; expected `classes` and `topics`")
    # Entries of the wrong shape are passed on as they are, for validation to report.
    subjects = {
        t.get("class"): t.get("subject") for t in raw.get("topics") or [] if isinstance(t, dict)
    }
    classes: list[Any] = []

    def walk(nodes: list[Any], parent: str | None) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                classes.append(node)
                continue
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
    followed = {t.slug for t in tree.topics}

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
