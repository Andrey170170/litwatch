"""Configuration: `litwatch.toml` for everything that is not a secret, the environment or
secret files for keys. Topics are not configuration; they live in the database."""

from __future__ import annotations

import contextlib
import os
import tomllib
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROLES = ("classify", "triage", "judge", "check", "review", "writer")


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

    @model_validator(mode="after")
    def _decisions_on_openrouter(self) -> RoleConfig:
        if self.decisions and self.provider != "openrouter":
            raise ValueError("only OpenRouter serves a Decisions endpoint")
        return self

    @property
    def lab(self) -> str:
        """The organisation that trained the model, not the account it is reached through."""
        if self.provider == "anthropic":
            return "anthropic"
        return self.model.split("/", 1)[0]

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" + (f"@{self.effort}" if self.effort else "")


class Models(_Section):
    # Chosen in N1 (evals/2026-10-14-classifiers.md).
    classify: RoleConfig = RoleConfig(provider="openrouter", model="typesafe/jev-1.13", decisions=True)
    triage: RoleConfig = RoleConfig(provider="anthropic", model="claude-haiku-5-5", effort="medium")
    judge: RoleConfig = RoleConfig(provider="anthropic", model="claude-opus-5-5", effort="medium")
    check: RoleConfig = RoleConfig(
        provider="openrouter", model="meta/muse-spark-1.3-contributor", effort="high"
    )
    review: RoleConfig = RoleConfig(provider="anthropic", model="claude-opus-5-5", effort="medium")
    writer: RoleConfig = RoleConfig(provider="anthropic", model="claude-opus-5-5", effort="medium")

    @model_validator(mode="after")
    def _two_labs_grade(self) -> Models:
        if self.judge.lab == self.check.lab:
            raise ValueError(
                f"judge and check must come from different labs; both are {self.judge.lab!r}"
            )
        return self

    def role(self, name: str) -> RoleConfig:
        if name not in ROLES:
            raise KeyError(f"unknown role {name!r}")
        return getattr(self, name)


class Intake(_Section):
    categories: tuple[str, ...] = ("cs.CL", "cs.AI", "cs.LG")
    # Nightly runs look back a few days so announcement lag and outages overlap.
    window_days: int = Field(default=3, gt=0)


class Limits(_Section):
    """Work in flight at once, per step."""

    triage: int = Field(default=16, gt=0)  # calls
    # Candidates graded at once. Each asks the judge and the check together, which are
    # different labs, so this is also the most calls any one lab sees at a time.
    grading: int = Field(default=6, gt=0)
    # A candidate that failed grading this many runs is shown to the owner ungraded.
    grading_attempts: int = Field(default=3, gt=0)
    # A paper whose triage failed this many runs is not retried: it waits for the owner.
    triage_stuck_after: int = Field(default=3, gt=0)
    writing: int = Field(default=4, gt=0)  # takes written at once
    # A pair the writer failed on this many runs enters its issue without a take.
    take_attempts: int = Field(default=3, gt=0)


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
    nightly: str = "0 6 * * *"
    weekly: str = "0 7 * * 1"
    # Publication lookup for library papers: new saves, and monthly re-checks (spec section 3).
    publications: str = "30 9 * * *"
    # Decides which calendar date an issue belongs to. None is the server's own zone.
    timezone: str | None = None

    @field_validator("nightly", "weekly", "publications")
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

    def today(self) -> date:
        return datetime.now(self.zone).date()

    def window(self, start: date, days: int = 1) -> tuple[datetime, datetime]:
        """From midnight of `start` to midnight `days` later, in the configured zone."""
        begin = datetime.combine(start, time.min, self.zone)
        return begin, begin + timedelta(days=days)

    def week_of(self, day: date) -> date:
        """The Monday of the week the day falls in."""
        return day - timedelta(days=day.weekday())


class Config(_Section):
    intake: Intake = Intake()
    models: Models = Models()
    limits: Limits = Limits()
    schedule: Schedule = Schedule()
    integrations: dict[str, dict] = Field(default_factory=dict)


def load(path: Path | None = None) -> Config:
    """The file named, or `$LITWATCH_CONFIG`, or `./litwatch.toml`; defaults when absent."""
    explicit = path or (Path(p) if (p := os.environ.get("LITWATCH_CONFIG")) else None)
    path = explicit or Path("litwatch.toml")
    if not path.exists():
        if explicit:
            raise SystemExit(f"configuration file {path} does not exist")
        return Config()
    return Config.model_validate(tomllib.loads(path.read_text()))


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
    """A full DSN, or one assembled from LITWATCH_DB_HOST/NAME/USER and a password file."""
    if url := os.environ.get("LITWATCH_DATABASE_URL"):
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
