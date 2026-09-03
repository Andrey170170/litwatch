"""Run the screener over the labeled set and score per-tier precision and recall."""

import asyncio
from dataclasses import dataclass, field

from litwatch.evalset import LabeledPaper
from litwatch.models import Flags, ScreenResult, Tier
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

    @property
    def expected_tier(self) -> Tier:
        assert self.item.expected is not None
        return self.item.expected.tier

    @property
    def predicted_tier(self) -> Tier | None:
        return self.result.tier if self.result else None

    @property
    def first_stage_tier(self) -> Tier | None:
        if self.result is None:
            return None
        return (self.result.first_stage or self.result.output).tier


@dataclass
class Report:
    scored: list[Scored]
    per_tier: dict[Tier, PRF] = field(default_factory=dict)
    per_tier_first_stage: dict[Tier, PRF] = field(default_factory=dict)

    @property
    def misses(self) -> list[Scored]:
        """Expected must_read papers that did not land in must_read: the recall bar."""
        return [
            s
            for s in self.scored
            if s.expected_tier == Tier.MUST_READ and s.predicted_tier != Tier.MUST_READ
        ]

    @property
    def errors(self) -> list[Scored]:
        return [s for s in self.scored if s.error]

    @property
    def rescreened(self) -> int:
        return sum(1 for s in self.scored if s.result and s.result.rescreened)

    @property
    def cost_by_model(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for s in self.scored:
            for u in s.result.usage if s.result else []:
                totals[u.model] = totals.get(u.model, 0.0) + (u.cost or 0.0)
        return totals


def score_tiers(pairs: list[tuple[Tier, Tier | None]]) -> dict[Tier, PRF]:
    per = {t: PRF() for t in Tier}
    for expected, predicted in pairs:
        for tier in Tier:
            if expected == tier and predicted == tier:
                per[tier].tp += 1
            elif predicted == tier:
                per[tier].fp += 1
            elif expected == tier:
                per[tier].fn += 1
    return per


def flag_agreement(scored: list[Scored]) -> dict[str, float | None]:
    """Per-flag accuracy, useful for seeing which flag drives tier errors."""
    counts: dict[str, list[int]] = {k: [0, 0] for k in Flags.model_fields}
    for s in scored:
        if s.result is None or s.item.expected is None:
            continue
        predicted = s.result.output.flags
        for key in counts:
            counts[key][1] += 1
            if getattr(predicted, key) == getattr(s.item.expected, key):
                counts[key][0] += 1
    return {k: (ok / n if n else None) for k, (ok, n) in counts.items()}


async def run(cascade: Cascade, items: list[LabeledPaper], concurrency: int = 8) -> Report:
    labeled = [i for i in items if i.expected is not None]
    sem = asyncio.Semaphore(concurrency)

    async def one(item: LabeledPaper) -> Scored:
        async with sem:
            try:
                return Scored(item=item, result=await cascade.screen(item.paper))
            except Exception as exc:  # noqa: BLE001 - the report records failures per paper
                return Scored(item=item, result=None, error=f"{type(exc).__name__}: {exc}")

    scored = list(await asyncio.gather(*(one(i) for i in labeled)))
    report = Report(scored=scored)
    report.per_tier = score_tiers([(s.expected_tier, s.predicted_tier) for s in scored])
    report.per_tier_first_stage = score_tiers(
        [(s.expected_tier, s.first_stage_tier) for s in scored]
    )
    return report
