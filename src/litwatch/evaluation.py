"""Run the cascade over the labeled set and score per-selection, per-tier precision and recall."""

import asyncio
from dataclasses import dataclass, field

from litwatch.evalset import LabeledPaper
from litwatch.models import TIERS, ScreenResult, StageRole
from litwatch.screen import Cascade


@dataclass
class PRF:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class Scored:
    item: LabeledPaper
    result: ScreenResult | None
    error: str | None = None

    def predicted(self, slug: str, role: StageRole = StageRole.CONFIRM) -> int | None:
        if self.result is None:
            return None
        verdict = self.result.verdicts_after(role).get(slug)
        return verdict.tier if verdict else None


@dataclass
class SelectionReport:
    slug: str
    scored: list[Scored]  # only papers labeled for this selection
    per_tier: dict[int, PRF] = field(default_factory=dict)
    per_stage: dict[StageRole, dict[int, PRF]] = field(default_factory=dict)

    @property
    def misses(self) -> list[Scored]:
        """Expected tier 1 papers that did not land in tier 1: the recall bar."""
        return [
            s
            for s in self.scored
            if s.item.expected_tier(self.slug) == 1 and s.predicted(self.slug) != 1
        ]

    @property
    def recommended_recall(self) -> float | None:
        """Recall of expected tier 1/2 papers into tier 1/2: the lab-visible reading list."""
        want = [s for s in self.scored if (s.item.expected_tier(self.slug) or 4) <= 2]
        hit = [s for s in want if (s.predicted(self.slug) or 4) <= 2]
        return len(hit) / len(want) if want else None


@dataclass
class Report:
    scored: list[Scored]
    selections: dict[str, SelectionReport] = field(default_factory=dict)

    @property
    def errors(self) -> list[Scored]:
        return [s for s in self.scored if s.error]

    @property
    def rescreened(self) -> int:
        return sum(1 for s in self.scored if s.result and s.result.rescreened)

    @property
    def confirmed(self) -> int:
        return sum(1 for s in self.scored if s.result and s.result.confirmed)

    @property
    def cost_by_model(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for s in self.scored:
            for u in s.result.usage if s.result else []:
                totals[u.model] = totals.get(u.model, 0.0) + (u.cost or 0.0)
        return totals


def score_tiers(pairs: list[tuple[int, int | None]]) -> dict[int, PRF]:
    per = {t: PRF() for t in TIERS}
    for expected, predicted in pairs:
        for tier in TIERS:
            if expected == tier and predicted == tier:
                per[tier].tp += 1
            elif predicted == tier:
                per[tier].fp += 1
            elif expected == tier:
                per[tier].fn += 1
    return per


def score_selection(slug: str, scored: list[Scored]) -> SelectionReport:
    labeled = [s for s in scored if s.item.expected_tier(slug) is not None]
    report = SelectionReport(slug=slug, scored=labeled)

    def pairs(role: StageRole) -> list[tuple[int, int | None]]:
        return [(s.item.expected_tier(slug) or 4, s.predicted(slug, role)) for s in labeled]

    report.per_tier = score_tiers(pairs(StageRole.CONFIRM))
    for role in StageRole:
        report.per_stage[role] = score_tiers(pairs(role))
    return report


async def run(cascade: Cascade, items: list[LabeledPaper], concurrency: int = 8) -> Report:
    labeled = [i for i in items if i.expected]
    sem = asyncio.Semaphore(concurrency)

    async def one(item: LabeledPaper) -> Scored:
        async with sem:
            try:
                return Scored(item=item, result=await cascade.screen(item.paper))
            except Exception as exc:  # noqa: BLE001 - the report records failures per paper
                return Scored(item=item, result=None, error=f"{type(exc).__name__}: {exc}")

    scored = list(await asyncio.gather(*(one(i) for i in labeled)))
    report = Report(scored=scored)
    for s in cascade.selections:
        report.selections[s.slug] = score_selection(s.slug, scored)
    return report
