"""The silver class sample (decision 55): draw it, have two labs sort it into the seed tree,
and compare their primary classes. See README.md for the strata and what each file holds.

    uv run python evals/labels/silver.py draw
    uv run python evals/labels/silver.py label opus|sol     # title and abstract from $TEXT_DSN
    uv run python evals/labels/silver.py compare
"""

from __future__ import annotations

import asyncio
import csv
import os
import random
import re
import sys
from pathlib import Path

import psycopg
from pydantic import BaseModel, ValidationError

from litwatch import seeds
from litwatch.config import RoleConfig
from litwatch.core.taxonomy import Taxonomy
from litwatch.llm.anthropic import AnthropicProvider
from litwatch.llm.openrouter import OpenRouterProvider
from litwatch.llm.types import ProviderError, Request, Stop

HERE = Path(__file__).parent
SEED = 20261014
SAMPLE = HERE / "silver-sample.csv"
MAX_DISPUTED = 20
LABELLERS = {
    "opus": RoleConfig(provider="anthropic", model="claude-opus-5-5", effort="medium"),
    "sol": RoleConfig(provider="openrouter", model="openai/gpt-6.1-sol", effort="medium"),
}
BROAD = ["interpretability", "alignment-and-safety", "reasoning-and-rl", "agents",
         "efficiency-and-architecture"]  # fmt: skip


def rows(path: Path) -> list[dict[str, str]]:
    with path.open() as f:
        return list(csv.DictReader(f))


def write(path: Path, items: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="") as f:
        out = csv.DictWriter(f, fieldnames=fields)
        out.writeheader()
        out.writerows(items)


def draw() -> None:
    """200 papers from 0.14's screened papers: 20 CoT positives, 10 owner-labelled papers,
    20 positives of each broad selection, and 70 papers no selection placed above tier 4."""
    rng = random.Random(SEED)
    tiers = rows(HERE / "tiers-0.14.csv")
    owner = sorted({r["paper_id"] for r in rows(HERE / "fit.csv") if r["cohort"] == "owner"})

    def positive(r: dict[str, str], col: str) -> bool:
        return r[col] in ("1", "2")

    strata: list[tuple[str, list[str], int]] = [
        ("cot", [r["paper_id"] for r in tiers
                 if positive(r, "cot-faithfulness") or positive(r, "cot-monitoring")], 20),
        ("owner-labelled", owner, 10),
        *[(col, [r["paper_id"] for r in tiers if positive(r, col)], 20) for col in BROAD],
        ("unselected", [r["paper_id"] for r in tiers
                        if all(r[c] in ("", "4") for c in r if c != "paper_id")], 70),
    ]  # fmt: skip
    taken: dict[str, str] = {}
    for name, pool, n in strata:
        free = sorted(set(pool) - set(taken))
        taken.update(dict.fromkeys(rng.sample(free, n), name))
    write(
        SAMPLE, [{"paper_id": p, "stratum": s} for p, s in taken.items()], ["paper_id", "stratum"]
    )
    print(f"drew {len(taken)} papers into {SAMPLE.name}")


class Sorting(BaseModel):
    primary: str
    others: list[str]
    reason: str


def system_prompt(tree: Taxonomy) -> str:
    lines = [
        f"{'  ' * (tree.level(c.slug) - 1)}- {c.slug}: {c.name}. {c.description}"
        for c in tree.classes
    ]
    return (
        "You sort arXiv papers into the class tree below. Each line gives a class's slug, its name "
        "and what it covers; indentation shows the tree, and a paper in a class is also in all of "
        "its ancestors.\n\nFor the paper you are given, answer with:\n"
        "- primary: the slug of the one class the paper belongs to most. Choose the most specific "
        "class that fits; choose a parent only when none of its children fits.\n"
        "- others: the slugs of any other classes the paper also clearly belongs to, as specific "
        "as fits. Leave out the ancestors of classes you named. Often empty.\n"
        "Write each class as its bare slug, the word before the colon (for example `circuits`), "
        "never as a path.\n"
        "- reason: one sentence on the choice of primary class.\n\n"
        "Judge by the paper's main contribution, not by every subject it mentions.\n\n"
        "The tree:\n" + "\n".join(lines)
    )


def slug_of(answer: str, tree: Taxonomy) -> str | None:
    """The class an answer names: a bare slug, or a path whose parts are the class's ancestry
    (`interpretability/circuits`). Anything else names no class."""
    *path, slug = re.split(r"[/:]", answer.strip())
    if slug not in {c.slug for c in tree.classes}:
        return None
    ancestry: list[str] = []
    parent = tree.get(slug).parent
    while parent is not None:
        ancestry.insert(0, parent)
        parent = tree.get(parent).parent
    return slug if not path or path == ancestry[-len(path) :] else None


def texts(ids: list[str]) -> dict[str, tuple[str, str]]:
    """Title and abstract from a read-only source: the development copy of 0.14's papers."""
    with psycopg.connect(os.environ["TEXT_DSN"]) as conn:
        found = conn.execute(
            "SELECT id, title, abstract FROM papers WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    return {pid: (title, abstract) for pid, title, abstract in found}


async def label(name: str) -> None:
    """Sort every paper of the sample not yet in `silver-<name>.csv`; a run can be resumed."""
    role, tree = LABELLERS[name], seeds.taxonomy()
    provider = (
        AnthropicProvider(os.environ["ANTHROPIC_API_KEY"])
        if role.provider == "anthropic"
        else OpenRouterProvider(os.environ["OPENROUTER_API_KEY"])
    )
    out = HERE / f"silver-{name}.csv"
    done = rows(out) if out.exists() else []
    todo = [
        r["paper_id"] for r in rows(SAMPLE) if r["paper_id"] not in {d["paper_id"] for d in done}
    ]
    text, system = texts(todo), system_prompt(tree)
    gate, cost = asyncio.Semaphore(8), [0.0]

    async def one(pid: str) -> dict[str, str] | None:
        title, abstract = text[pid]
        request = Request(
            system=system, user=f"Title: {title}\n\nAbstract: {abstract}", output=Sorting
        )
        for _ in range(3):
            try:
                async with gate:
                    reply = await provider.complete(role, request)
            except ProviderError as exc:
                print(f"{pid}: {exc}", file=sys.stderr)
                continue
            cost[0] += reply.usage.cost or 0.0
            if reply.stop != Stop.COMPLETE:
                print(f"{pid}: {reply.stop.value}", file=sys.stderr)
                continue
            try:
                sorting = Sorting.model_validate_json(reply.text)
            except ValidationError as exc:
                print(f"{pid}: malformed answer: {exc}", file=sys.stderr)
                continue
            named = [slug_of(s, tree) for s in (sorting.primary, *sorting.others)]
            primary, *others = named
            if None in named:
                print(
                    f"{pid}: unknown classes {[sorting.primary, *sorting.others]}", file=sys.stderr
                )
            else:
                others = [s for s in dict.fromkeys(others) if s != primary]
                return {"paper_id": pid, "primary": primary, "others": " ".join(others),
                        "reason": sorting.reason}  # fmt: skip
        print(f"{pid}: no usable answer", file=sys.stderr)
        return None

    results = [r for r in await asyncio.gather(*(one(p) for p in todo)) if r]
    order = {r["paper_id"]: i for i, r in enumerate(rows(SAMPLE))}
    merged = sorted(done + results, key=lambda r: order[r["paper_id"]])
    write(out, merged, ["paper_id", "primary", "others", "reason"])
    print(f"{name}: {len(results)} sorted, {len(todo) - len(results)} failed, ${cost[0]:.2f}")


def closure(answer: dict[str, str], tree: Taxonomy) -> set[str]:
    """Every class a labeller put the paper in: those named and all their ancestors."""
    found: set[str] = set()
    for slug in [answer["primary"], *answer["others"].split()]:
        while slug is not None and slug not in found:
            found.add(slug)
            slug = tree.get(slug).parent
    return found


def compare() -> None:
    """Agreed primary classes go to silver.csv, with the classes both put the paper in; at most 20
    disagreements, drawn with the seed and spread across top-level classes, go to disputed.csv
    for the owner."""
    tree = seeds.taxonomy()

    def top(slug: str) -> str:
        while (parent := tree.get(slug).parent) is not None:
            slug = parent
        return slug

    a = {r["paper_id"]: r for r in rows(HERE / "silver-opus.csv")}
    b = {r["paper_id"]: r for r in rows(HERE / "silver-sol.csv")}
    agreed, differ = [], []
    # A paper one labeller refused stays unresolved and is not asked of the owner.
    missing = sorted(
        r["paper_id"] for r in rows(SAMPLE) if r["paper_id"] not in a.keys() & b.keys()
    )
    for pid in a.keys() & b.keys():
        if a[pid]["primary"] == b[pid]["primary"]:
            # Membership is inherited, so each labeller's classes are closed over ancestors: a
            # class both put the paper in is a silver member, one only one did is unsettled and
            # counts neither way.
            x, y = closure(a[pid], tree), closure(b[pid], tree)
            agreed.append(
                {"paper_id": pid, "primary": a[pid]["primary"], "members": " ".join(sorted(x & y)),
                 "unsettled": " ".join(sorted(x ^ y))}
            )  # fmt: skip
        else:
            differ.append(pid)
    # Round-robin over the top-level class Opus named, in tree order, each queue shuffled.
    rng = random.Random(SEED)
    queues: dict[str, list[str]] = {}
    for pid in sorted(differ):
        queues.setdefault(top(a[pid]["primary"]), []).append(pid)
    for q in queues.values():
        rng.shuffle(q)
    picked: list[str] = []
    while len(picked) < MAX_DISPUTED and any(queues.values()):
        for c in tree.children(None):
            if queues.get(c.slug) and len(picked) < MAX_DISPUTED:
                picked.append(queues[c.slug].pop())
    write(HERE / "silver.csv", sorted(agreed, key=lambda r: r["paper_id"]),
          ["paper_id", "primary", "members", "unsettled"])  # fmt: skip

    def settled(pid: str) -> dict[str, str]:
        """Memberships do not depend on the primary class: both labellers' agreement counts."""
        if pid in missing:
            return {"members": "", "unsettled": ""}
        x, y = closure(a[pid], tree), closure(b[pid], tree)
        return {"members": " ".join(sorted(x & y)), "unsettled": " ".join(sorted(x ^ y))}

    disputed = [
        {"paper_id": pid, "opus": a.get(pid, {}).get("primary", ""),
         "sol": b.get(pid, {}).get("primary", ""),
         "status": "asked" if pid in picked else "unresolved", **settled(pid)}
        for pid in sorted(differ + missing)
    ]  # fmt: skip
    write(HERE / "disputed.csv", disputed,
          ["paper_id", "opus", "sol", "status", "members", "unsettled"])  # fmt: skip
    print(
        f"{len(agreed)} agreed, {len(differ)} differ, {len(missing)} with one label, "
        f"{len(picked)} asked of the owner"
    )


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["draw"]:
            draw()
        case ["label", name] if name in LABELLERS:
            asyncio.run(label(name))
        case ["compare"]:
            compare()
        case _:
            sys.exit(__doc__)
