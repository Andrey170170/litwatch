"""Models scripted by the paper's title for the nightly steps (classify, analyse and the
graders), the configuration giving each role its own model, the two papers they answer about,
and a nightly run on them."""

import json

from litwatch.config import Config, Models, RoleConfig
from litwatch.jobs import nightly
from litwatch.llm import Gateway, Reply, Stop, Usage
from litwatch.store import runs
from tests.conftest import TREE, gate_on, paper
from tests.fakes import ScriptedProvider
from tests.sessions import Session, in_read

CLASSIFY = RoleConfig(provider="openrouter", model="test/decider")
# Each role its own model, so a scripted answer knows which role was asked.
ROLES = {
    "classify": CLASSIFY,
    "analyse": RoleConfig(provider="anthropic", model="test-analyse"),
    "judge": RoleConfig(provider="anthropic", model="test-judge"),
    "check": RoleConfig(provider="openrouter", model="test/check"),
    "area_check": RoleConfig(provider="openrouter", model="test/area-check"),
    "field_grade": RoleConfig(provider="anthropic", model="test-field"),
}
CONFIG = Config(models=Models(**ROLES))
# The roles that write, each with two fallbacks named after it, for the list tests; `judge`'s and
# `check`'s keep to their own labs.
LISTS = {
    name: ROLES[name].model_copy(
        update={
            "fallbacks": tuple(
                ROLES[name].model_copy(update={"model": f"{ROLES[name].model}-{n}"}) for n in (2, 3)
            )
        }
    )
    for name in ("analyse", "judge", "check", "field_grade")
}
LISTED = Config(models=Models(**ROLES | LISTS))
BY_MODEL = {m.model: name for name, r in (ROLES | LISTS).items() for m in r.models}
NAMES = {c.slug: c.name for c in TREE.classes}
GIST = (
    "Probes read a model's hidden steps from its activations and find that traces omit a third "
    "of them across four tasks."
)
ANALYSIS = {
    "gist": GIST,
    "contribution": "analysis",
    "releases_code": True,
    "releases_weights": False,
    "weight_reason": "A careful analysis with a fair comparison.",
    "weight": "solid",
}
A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes on activations.")
B = paper("2610.00002", title="Is the chain of thought faithful?", abstract="We test traces.")


def decision(primary: str = "other", **probabilities: float) -> dict:
    return (
        dict.fromkeys(NAMES, 0.0)
        | probabilities
        | {"primary_class": {"option": NAMES[primary], "probability": 0.9}}
    )


class Scripted:
    """Scripted roles, answering by the paper's title: `classify` and `analyse`, and the
    graders (`fits` by (role, title); related unless scripted, and `area_check` sure it holds).
    `said` by (model, title) comes first, for a model of a role's list; a `Reply` answers as
    it is (a refusal)."""

    def __init__(self) -> None:
        self.decisions: dict[str, dict] = {}
        self.analyses: dict[str, dict] = {}
        self.fits: dict[tuple[str, str], dict | Exception] = {}
        self.said: dict[tuple[str, str], dict | Exception | Reply] = {}
        self.asked: list[tuple[str, str]] = []  # (role, title)
        self.models_asked: list[tuple[str, str]] = []  # (model, title)
        self.users: list[str] = []  # every request's user message

    def respond(self, role: RoleConfig, request) -> str | Reply:
        title = request.user.split("Title: ", 1)[1].split("\n", 1)[0]
        step = BY_MODEL[role.model]
        self.asked.append((step, title))
        self.models_asked.append((role.model, title))
        self.users.append(request.user)
        if (role.model, title) in self.said:
            answer = self.said[(role.model, title)]
            if isinstance(answer, Exception):
                raise answer
            return answer if isinstance(answer, Reply) else json.dumps(answer)
        match step:
            case "classify":
                answer = self.decisions.get(title) or decision()
            case "analyse":
                answer = self.analyses.get(title) or ANALYSIS
            case "area_check":
                answer = self.fits.get((step, title)) or {"holds": 0.9}
            case _:
                answer = self.fits.get((step, title)) or {
                    "reason": "On the subject.",
                    "fit": "related",
                }
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)

    def gateway(self, config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))


async def night(conn: Session, models: Scripted, config: Config = CONFIG) -> dict:
    async with gate_on(conn) as db:
        run_id = await nightly.run(db, config, fetch=False, gateway=models.gateway)
    return (await in_read(conn, runs.get, run_id))["counts"]


async def rows(conn: Session, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


def refusal(model: str = "m") -> Reply:
    return Reply("", Stop.REFUSED, Usage(model=model, cost=0.001), refusal="bio")


async def calls_for(conn: Session, paper_id: str, role: str) -> list[str]:
    """The models the journal shows `role` asked about the paper, in order."""
    found = await rows(
        conn,
        "SELECT usage->>'model' AS model FROM inference_calls "
        "WHERE paper_id = %s AND operation = %s ORDER BY id",
        paper_id,
        role,
    )
    return [r["model"] for r in found]
