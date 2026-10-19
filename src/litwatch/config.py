"""Configuration: `litwatch.toml` for everything that is not a secret, the environment or
secret files for keys. Topics are not configuration; they live in the database."""

from __future__ import annotations

import contextlib
import os
import tomllib
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from litwatch.core.classification import CHOSEN, Thresholds

ROLES = (
    "classify",
    "analyse",
    "judge",
    "check",
    "area_check",
    "field_grade",
    "brief",
    "writer",
)
# The roles answered by a decision model with probabilities (models.md, roles): no fallbacks.
DECISION_ROLES = ("classify", "area_check")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleConfig(_Section):
    """Where one gateway role is served. Pipeline code names the role, never the model."""

    provider: Literal["anthropic", "openrouter"]
    model: str
    effort: str | None = None
    # OpenRouter only: the providers allowed to serve the model, tried in order. Empty uses
    # the built-in list for the model, or leaves the choice to OpenRouter.
    routing: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=180.0, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    # The model is served by OpenRouter's Decisions endpoint (Luna Decisions, Jev). A decision
    # role without it asks a chat model, with a strict schema.
    decisions: bool = False
    # The models asked in turn when this one fails for the paper, in order (models.md, roles):
    # each with its provider, model and effort, and none with fallbacks of its own.
    fallbacks: tuple[RoleConfig, ...] = ()

    @model_validator(mode="after")
    def _decisions_on_openrouter(self) -> RoleConfig:
        if self.decisions and self.provider != "openrouter":
            raise ValueError("only OpenRouter serves a Decisions endpoint")
        return self

    @model_validator(mode="after")
    def _plain_fallbacks(self) -> RoleConfig:
        for f in self.fallbacks:
            if f.fallbacks or f.decisions:
                raise ValueError(f"fallback {f.label}: a chat model without fallbacks of its own")
        if len({m.label for m in self.models}) < len(self.models):
            raise ValueError(f"{self.label}: a model is listed twice")
        return self

    @property
    def models(self) -> tuple[RoleConfig, ...]:
        """The role's list: its own model, then its fallbacks, in the order asked."""
        return (self, *self.fallbacks)

    @property
    def lab(self) -> str:
        """The organisation that trained the model, not the account it is reached through."""
        if self.provider == "anthropic":
            return "anthropic"
        return self.model.split("/", 1)[0]

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" + (f"@{self.effort}" if self.effort else "")


def _model(
    provider: Literal["anthropic", "openrouter"], model: str, effort: str | None = None
) -> RoleConfig:
    return RoleConfig(provider=provider, model=model, effort=effort)


class Models(_Section):
    """Each role's model and its fallbacks. The fallbacks were chosen by the failure-handling
    replay (evals/2026-10-19-fallback-ladders.md): `field_grade` has none, as no candidate grades
    as Haiku does."""

    # Chosen in N1 (evals/2026-10-14-classifiers.md).
    classify: RoleConfig = RoleConfig(
        provider="openrouter", model="typesafe/jev-1.13", decisions=True
    )
    analyse: RoleConfig = RoleConfig(
        provider="anthropic",
        model="claude-haiku-5-5",
        fallbacks=(
            _model("openrouter", "openai/gpt-6-luna"),
            _model("openrouter", "deepseek/deepseek-v4.1-flash"),
        ),
    )
    judge: RoleConfig = RoleConfig(
        provider="anthropic",
        model="claude-opus-5-5",
        effort="medium",
        fallbacks=(
            _model("anthropic", "claude-sonnet-5-5", "medium"),
            _model("openrouter", "openai/gpt-6.1-sol", "medium"),
            _model("openrouter", "moonshotai/kimi-k3", "medium"),
        ),
    )
    check: RoleConfig = RoleConfig(
        provider="openrouter",
        model="meta/muse-spark-1.3-contributor",
        effort="high",
        fallbacks=(
            _model("openrouter", "deepseek/deepseek-v4.1-flash-1017", "high"),
            _model("openrouter", "google/gemini-3.8-flash", "high"),
        ),
    )
    # A decision model asked whether an Area verdict's Fit holds.
    area_check: RoleConfig = RoleConfig(
        provider="openrouter", model="typesafe/jev-1.13", decisions=True
    )
    field_grade: RoleConfig = RoleConfig(provider="anthropic", model="claude-haiku-5-5")
    # Briefs, Weight and Gists from the full text (pipeline.md, section 6): Sonnet chosen in the
    # Sonnet replay (evals/2026-10-16-sonnet-briefs.md).
    brief: RoleConfig = RoleConfig(
        provider="anthropic",
        model="claude-sonnet-5-5",
        effort="high",
        fallbacks=(_model("openrouter", "openai/gpt-6.1-sol", "high"),),
    )
    writer: RoleConfig = RoleConfig(
        provider="anthropic",
        model="claude-opus-5-5",
        effort="medium",
        fallbacks=(
            _model("anthropic", "claude-sonnet-5-5", "medium"),
            _model("openrouter", "moonshotai/kimi-k3", "medium"),
        ),
    )

    @model_validator(mode="after")
    def _two_labs_grade(self) -> Models:
        # A Focus grade comes from two labs by fixed lists (D2); a fallback may share its own
        # role's lab (D1).
        shared = {m.lab for m in self.judge.models} & {m.lab for m in self.check.models}
        if shared:
            raise ValueError(
                f"judge's and check's lists must share no lab; both have {sorted(shared)}"
            )
        return self

    @model_validator(mode="after")
    def _no_decision_fallbacks(self) -> Models:
        for name in ROLES:
            role = self.role(name)
            if role.fallbacks and (name in DECISION_ROLES or role.decisions):
                raise ValueError(f"{name}: a decision role has no fallbacks")
        return self

    def role(self, name: str) -> RoleConfig:
        if name not in ROLES:
            raise KeyError(f"unknown role {name!r}")
        return getattr(self, name)


class Intake(_Section):
    categories: tuple[str, ...] = ("cs.CL", "cs.AI", "cs.LG")
    # Nightly runs look back a few days so announcement lag and outages overlap.
    window_days: int = Field(default=3, gt=0)


class Classification(_Section):
    """Where membership is drawn for classes without a threshold of their own (pipeline.md,
    section 2); the defaults were chosen in N1."""

    default_threshold: float = Field(default=CHOSEN.default, gt=0, lt=1)
    focus_threshold: float = Field(default=CHOSEN.focus, gt=0, lt=1)  # at or under a Focus topic
    uncertain_floor: float = Field(default=CHOSEN.floor, gt=0, lt=1)

    @model_validator(mode="after")
    def _floor_below(self) -> Classification:
        if self.uncertain_floor >= min(self.default_threshold, self.focus_threshold):
            raise ValueError("uncertain_floor must be below both thresholds")
        return self

    @property
    def thresholds(self) -> Thresholds:
        return Thresholds(
            default=self.default_threshold, focus=self.focus_threshold, floor=self.uncertain_floor
        )


class Grading(_Section):
    """How Area verdicts are checked (pipeline.md, section 4)."""

    # A "yes" from `area_check` below this probability escalates to `check`, as a "no" does.
    area_check_min_confidence: float = Field(default=0.7, gt=0, le=1)


class Digests(_Section):
    """How long the night's digests wait for the full-text passes they depend on (digests.md,
    timing)."""

    wait_minutes: float = Field(default=120, ge=0)


class Budget(_Section):
    """A monthly budget per provider account, in USD (models.md, budgets). Projected spend over
    it turns the status line amber; nothing is downgraded or stopped. None is no budget."""

    # The owner's caps since 2026-10-16.
    anthropic_monthly: float | None = Field(default=100, gt=0)
    openrouter_monthly: float | None = Field(default=25, gt=0)

    @property
    def by_provider(self) -> dict[str, float]:
        found = {"anthropic": self.anthropic_monthly, "openrouter": self.openrouter_monthly}
        return {k: v for k, v in found.items() if v is not None}


def system_zone() -> tzinfo:
    """The server's own zone with its daylight-saving rules. `TZ` decides when set, as it
    does for the process: a zone name gives that zone, anything else (a POSIX rule) the
    process's local time, which follows it. Unset, the zone `/etc/localtime` links to, else
    the one `/etc/timezone` names; when neither names one, the current fixed offset."""
    if tz := os.environ.get("TZ", "").lstrip(":"):
        try:
            return ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            return datetime.now().astimezone().tzinfo
    names = [os.path.realpath("/etc/localtime").partition("zoneinfo/")[2]]
    with contextlib.suppress(OSError):
        names.append(Path("/etc/timezone").read_text().strip())
    for name in filter(None, names):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return datetime.now().astimezone().tzinfo


class Schedule(_Section):
    # Off until cutover: jobs run by hand before then (spec decision 22).
    enabled: bool = False
    # Results are read at 09:00 (docs/plans/model-spend.md).
    nightly: str = "0 2 * * *"
    # Publication lookup for library papers: new saves, and monthly re-checks (spec section 3).
    publications: str = "30 9 * * *"
    # The nightly's deadline, a time of day (docs/specs/v1/models.md, batches): its batches
    # are cancelled then and what is left goes as normal calls.
    deadline: time = time(7, 30)
    # The zone the cron times are read in. None is the server's own zone.
    timezone: str | None = None

    @field_validator("nightly", "publications")
    @classmethod
    def _cron(cls, value: str) -> str:
        # Checked at load, so one bad expression stops the start rather than one job.
        if not croniter.is_valid(value):
            raise ValueError(f"not a cron expression: {value!r}")
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        # Checked at load too: a zone that cannot be found would fail every tick.
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"not a time zone: {value!r}") from exc
        return value

    @property
    def zone(self) -> tzinfo:
        return ZoneInfo(self.timezone) if self.timezone else system_zone()

    def night(self, now: datetime) -> tuple[datetime, datetime] | None:
        """The night a nightly started at `now` (aware) belongs to: the nightly time that last
        fired and the first deadline after it, when `now` is before that deadline; else None,
        and the run does not batch. A run started again the same night keeps its deadline."""
        local = (now.astimezone(UTC) + timedelta(microseconds=1)).astimezone(self.zone)
        fired = croniter(self.nightly, local).get_prev(datetime)
        deadline = datetime.combine(fired.date(), self.deadline, self.zone)
        if deadline <= fired:
            deadline = datetime.combine(fired.date() + timedelta(days=1), self.deadline, self.zone)
        return (fired, deadline) if now < deadline else None


class Config(_Section):
    # Debug mode keeps what a model replied when the reply could not be used, and the
    # validator's messages with it (`inference_calls.reply`). `LITWATCH_DEBUG=1` turns it on too.
    debug: bool = False
    intake: Intake = Intake()
    models: Models = Models()
    classification: Classification = Classification()
    grading: Grading = Grading()
    digests: Digests = Digests()
    budget: Budget = Budget()
    schedule: Schedule = Schedule()
    integrations: dict[str, dict] = Field(default_factory=dict)


def load(path: Path | None = None) -> Config:
    """The file named, or `$LITWATCH_CONFIG`, or `./litwatch.toml`; defaults when absent.
    `$LITWATCH_DEBUG` turns debug mode on whatever the file says."""
    explicit = path or (Path(p) if (p := os.environ.get("LITWATCH_CONFIG")) else None)
    path = explicit or Path("litwatch.toml")
    if not path.exists():
        if explicit:
            raise SystemExit(f"configuration file {path} does not exist")
        config = Config()
    else:
        config = Config.model_validate(tomllib.loads(path.read_text()))
    if os.environ.get("LITWATCH_DEBUG", "").lower() in ("1", "true", "yes", "on"):
        config = config.model_copy(update={"debug": True})
    return config


# -- secrets ---------------------------------------------------------------------------------------


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader: KEY=VALUE lines; values already in the environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def secret(name: str) -> str | None:
    """`NAME` from the environment, or the contents of the file named by `NAME_FILE`."""
    if value := os.environ.get(name):
        return value
    if path := os.environ.get(f"{name}_FILE"):
        return Path(path).read_text().strip()
    return None


def database_url() -> str:
    """A full DSN (itself a secret, so also from a file), or one assembled from
    LITWATCH_DB_HOST/NAME/USER and a password file."""
    if url := secret("LITWATCH_DATABASE_URL"):
        return url
    host = os.environ.get("LITWATCH_DB_HOST")
    if not host:
        raise SystemExit("LITWATCH_DATABASE_URL or LITWATCH_DB_* is not set")
    user = os.environ.get("LITWATCH_DB_USER", "litwatch")
    name = os.environ.get("LITWATCH_DB_NAME", "litwatch")
    port = os.environ.get("LITWATCH_DB_PORT", "5432")
    password = secret("LITWATCH_DB_PASSWORD") or ""
    # A generated password may hold `@`, `/` or `#`, which would change what the URL means.
    user, password, name = (quote(part, safe="") for part in (user, password, name))
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"
