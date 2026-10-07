"""Runtime settings from the environment. Secrets never live in files inside the repo."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from litwatch.routing import DEFAULT_PROVIDERS, parse_providers


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader: KEY=VALUE lines, no export, no quoting rules beyond stripping."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _secret(name: str) -> str | None:
    """`NAME` from the environment, or the contents of the file named by `NAME_FILE`."""
    if value := os.environ.get(name):
        return value
    if path := os.environ.get(f"{name}_FILE"):
        return Path(path).read_text().strip()
    return None


# Deep-card extension requested per selection slug; the names are litwatch-deep's EXTENSIONS.
DEFAULT_DEEP_EXTENSIONS = {"cot-faithfulness": "faithfulness", "cot-monitoring": "monitoring"}


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    screen_model: str = "openai/gpt-5.6-luna"
    screen_effort: str = "low"
    screen_timeout_seconds: float = 120.0
    rescreen_model: str = "z-ai/glm-5.3"
    rescreen_effort: str = "high"
    rescreen_max_completion_tokens: int = 8192
    rescreen_ignored_keywords: tuple[str, ...] = ("benchmark",)
    # A different model family on purpose: its errors are less correlated with Luna's.
    # The `-contributor` endpoint is far cheaper but needs OpenRouter's training opt-in.
    confirm_model: str = "meta/muse-spark-1.3"
    confirm_effort: str = "high"
    # Digest prose. Kimi K3 won the blind three-way pick against GLM 5.3 and Sol (2026-09-03).
    digest_model: str = "moonshotai/kimi-k3"
    digest_effort: str = "low"
    # OpenRouter providers per model, tried in order and exclusively; see routing.py.
    providers: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_PROVIDERS))
    database_url: str | None = None
    # Routing (phase 3). Unset Zotero credentials disable routing; unset WebDAV disables PDFs.
    zotero_user_id: str | None = None
    zotero_api_key: str | None = None
    webdav_url: str | None = None
    webdav_username: str | None = None
    webdav_password: str | None = None
    # Deep processing (phase 4): the litwatch-deep service; unset disables it.
    deep_url: str | None = None
    # Tier 1 items of these selections join this collection once enriched, so paperflow-lite
    # picks them up. Personal routing stays out of the lab-facing selection spec on purpose.
    inbox_collection: str | None = None
    inbox_selections: tuple[str, ...] = ("cot-faithfulness",)
    # Deep card extensions per selection (`slug=extension,...`); a tier 1 in a selection without
    # an extension still gets the core card. The cap protects the subscription from a loose rubric.
    deep_extensions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DEEP_EXTENSIONS))
    deep_max_per_run: int = 10
    # An edited or new selection re-judges papers published within this many days.
    rescreen_window_days: int = 30
    categories: tuple[str, ...] = ("cs.CL", "cs.AI", "cs.LG")
    screen_concurrency: int = 16
    # Papers judged under an older selection version are re-screened in the background, a
    # batch at a time and with few calls in flight, giving way to any run that asks.
    backlog_enabled: bool = True
    backlog_batch: int = 40
    backlog_concurrency: int = 4
    backlog_interval_seconds: float = 300.0
    backlog_retry_seconds: float = 6 * 3600.0
    intake_source: str = "oai"
    archive_url: str | None = None
    archive_token: str | None = field(default=None, repr=False)
    archive_wait_seconds: float = 60.0
    archive_dispatch_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        key = _secret("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY (or OPENROUTER_API_KEY_FILE) is not set")
        return cls(
            openrouter_api_key=key,
            screen_model=os.environ.get("LITWATCH_SCREEN_MODEL", cls.screen_model),
            screen_effort=os.environ.get("LITWATCH_SCREEN_EFFORT", cls.screen_effort),
            screen_timeout_seconds=float(os.environ.get("LITWATCH_SCREEN_TIMEOUT_SECONDS", "120")),
            rescreen_model=os.environ.get("LITWATCH_RESCREEN_MODEL", cls.rescreen_model),
            rescreen_effort=os.environ.get("LITWATCH_RESCREEN_EFFORT", cls.rescreen_effort),
            rescreen_max_completion_tokens=int(
                os.environ.get("LITWATCH_RESCREEN_MAX_COMPLETION_TOKENS", "8192")
            ),
            rescreen_ignored_keywords=_csv(
                "LITWATCH_RESCREEN_IGNORED_KEYWORDS", cls.rescreen_ignored_keywords
            ),
            confirm_model=os.environ.get("LITWATCH_CONFIRM_MODEL", cls.confirm_model),
            confirm_effort=os.environ.get("LITWATCH_CONFIRM_EFFORT", cls.confirm_effort),
            digest_model=os.environ.get("LITWATCH_DIGEST_MODEL", cls.digest_model),
            digest_effort=os.environ.get("LITWATCH_DIGEST_EFFORT", cls.digest_effort),
            providers=parse_providers(os.environ.get("LITWATCH_PROVIDERS", ""), DEFAULT_PROVIDERS),
            database_url=_database_url(),
            zotero_user_id=os.environ.get("LITWATCH_ZOTERO_USER_ID"),
            zotero_api_key=_secret("ZOTERO_API_KEY"),
            webdav_url=_secret("WEBDAV_URL"),
            webdav_username=_secret("WEBDAV_USERNAME"),
            webdav_password=_secret("WEBDAV_PASSWORD"),
            deep_url=os.environ.get("LITWATCH_DEEP_URL"),
            inbox_collection=os.environ.get("LITWATCH_INBOX_COLLECTION"),
            inbox_selections=_csv("LITWATCH_INBOX_SELECTIONS", cls.inbox_selections),
            deep_extensions=_mapping("LITWATCH_DEEP_EXTENSIONS", dict(DEFAULT_DEEP_EXTENSIONS)),
            deep_max_per_run=int(os.environ.get("LITWATCH_DEEP_MAX_PER_RUN", "10")),
            rescreen_window_days=int(os.environ.get("LITWATCH_RESCREEN_WINDOW_DAYS", "30")),
            categories=tuple(
                os.environ.get("LITWATCH_CATEGORIES", ",".join(cls.categories)).split(",")
            ),
            screen_concurrency=int(os.environ.get("LITWATCH_SCREEN_CONCURRENCY", "16")),
            backlog_enabled=os.environ.get("LITWATCH_BACKLOG_ENABLED", "1") not in ("0", "false"),
            backlog_batch=int(os.environ.get("LITWATCH_BACKLOG_BATCH", "40")),
            backlog_concurrency=int(os.environ.get("LITWATCH_BACKLOG_CONCURRENCY", "4")),
            backlog_interval_seconds=float(
                os.environ.get("LITWATCH_BACKLOG_INTERVAL_SECONDS", "300")
            ),
            backlog_retry_seconds=float(
                os.environ.get("LITWATCH_BACKLOG_RETRY_SECONDS", str(6 * 3600))
            ),
            intake_source=os.environ.get("LITWATCH_INTAKE_SOURCE", "oai"),
            archive_url=os.environ.get("LITWATCH_ARCHIVE_URL"),
            archive_token=_secret("LITWATCH_ARCHIVE_TOKEN"),
            archive_wait_seconds=float(os.environ.get("LITWATCH_ARCHIVE_WAIT_SECONDS", "60")),
            archive_dispatch_seconds=float(
                os.environ.get("LITWATCH_ARCHIVE_DISPATCH_SECONDS", "5")
            ),
        )

    def providers_for(self, model: str) -> tuple[str, ...]:
        return self.providers.get(model, ())

    @property
    def archive_enabled(self) -> bool:
        return bool(self.archive_url and self.archive_token)

    @property
    def routing_enabled(self) -> bool:
        return bool(self.zotero_user_id and self.zotero_api_key)

    @property
    def webdav_enabled(self) -> bool:
        return bool(self.webdav_url and self.webdav_username and self.webdav_password)

    @property
    def deep_enabled(self) -> bool:
        return bool(self.deep_url)

    def require_database(self) -> str:
        if not self.database_url:
            raise SystemExit("LITWATCH_DATABASE_URL or LITWATCH_DB_* is not set")
        return self.database_url


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _mapping(name: str, default: dict[str, str]) -> dict[str, str]:
    """`a=b,c=d` from the environment, or the default when unset."""
    raw = os.environ.get(name)
    if raw is None:
        return dict(default)
    pairs = (x.split("=", 1) for x in raw.split(",") if "=" in x)
    return {k.strip(): v.strip() for k, v in pairs}


def _database_url() -> str | None:
    """A full DSN, or one assembled from LITWATCH_DB_HOST/NAME/USER and a password file."""
    if url := os.environ.get("LITWATCH_DATABASE_URL"):
        return url
    host = os.environ.get("LITWATCH_DB_HOST")
    if not host:
        return None
    user = os.environ.get("LITWATCH_DB_USER", "litwatch")
    name = os.environ.get("LITWATCH_DB_NAME", "litwatch")
    port = os.environ.get("LITWATCH_DB_PORT", "5432")
    password = _secret("LITWATCH_DB_PASSWORD") or ""
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"
