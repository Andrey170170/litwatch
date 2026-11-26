"""Models with known costs: every call priced at `COST`, a billed call in the journal
(`charged`), in a scheduled configuration."""

import json

from litwatch.config import Config, Models, RoleConfig
from litwatch.llm import Gateway, Reply, Request
from litwatch.store import runs
from tests.briefs import ANSWER
from tests.conftest import started
from tests.fakes import ScriptedProvider
from tests.scripted import ROLES, Scripted
from tests.sessions import Session, in_write
from tests.working import quiet

BRIEF = RoleConfig(provider="anthropic", model="test-brief")
QUIET = quiet()  # the schedule on (the workers run), with no fire near now
CONFIG = Config(models=Models(**ROLES, brief=BRIEF), schedule=QUIET)
ZONE = CONFIG.schedule.zone
COST = 0.01  # what every call costs


class Costed(ScriptedProvider):
    """A scripted provider whose every reply costs `COST`, keeping the limits each call was sent
    with (`max_price`)."""

    def __init__(self, name: str, respond) -> None:
        super().__init__(name, respond)
        self.limits: list[dict | None] = []

    async def complete(self, role: RoleConfig, request: Request, *, max_price=None) -> Reply:
        self.limits.append(max_price)
        reply = await super().complete(role, request)
        reply.usage.cost = COST
        return reply


class Priced:
    """The graders (`Scripted`) and `brief`, each call costing `COST`."""

    def __init__(self) -> None:
        self.scripted = Scripted()
        self.providers: dict[str, Costed] = {}

    def respond(self, role: RoleConfig, request) -> str | Reply:
        if role.model == BRIEF.model:
            return json.dumps(ANSWER)
        return self.scripted.respond(role, request)

    def gateway(self, config: Config, journal) -> Gateway:
        self.providers = {n: Costed(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, self.providers, journal, retry_delays=(0.0,))

    def sent(self, role: str) -> list[str]:
        """The titles `role` was asked about."""
        return [title for r, title in self.scripted.asked if r == role]


async def charged(
    conn: Session,
    model: str,
    *,
    cost: float,
    tokens_in: int,
    tokens_out: int,
    cached: int = 0,
    by="X",
    job: int | None = None,
) -> None:
    """A billed OpenRouter call of `model` in the journal, on a run that is not a nightly's (the
    `job`'s, when given)."""
    run_id = await started(conn, "job" if job else "fulltext", None, None, job)
    usage = {
        "model": model,
        "via": "openrouter",
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "cached_tokens": cached,
        "cost": cost,
        "provider": by,
    }
    await conn.execute(
        "INSERT INTO inference_calls (run_id, operation, usage) VALUES (%s, 'check', %s)",
        (run_id, json.dumps(usage)),
    )
    await in_write(conn, runs.finish, run_id)
