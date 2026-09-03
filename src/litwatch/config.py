"""Runtime settings from the environment. Secrets never live in files inside the repo."""

import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    screen_model: str = "openai/gpt-5.6-luna"
    screen_effort: str = "low"
    rescreen_model: str = "openai/gpt-5.6-sol"
    rescreen_effort: str = "low"
    database_url: str | None = None
    categories: tuple[str, ...] = ("cs.CL", "cs.AI", "cs.LG")
    screen_concurrency: int = 8

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
            rescreen_model=os.environ.get("LITWATCH_RESCREEN_MODEL", cls.rescreen_model),
            rescreen_effort=os.environ.get("LITWATCH_RESCREEN_EFFORT", cls.rescreen_effort),
            database_url=_database_url(),
            categories=tuple(
                os.environ.get("LITWATCH_CATEGORIES", ",".join(cls.categories)).split(",")
            ),
            screen_concurrency=int(os.environ.get("LITWATCH_SCREEN_CONCURRENCY", "8")),
        )

    def require_database(self) -> str:
        if not self.database_url:
            raise SystemExit("LITWATCH_DATABASE_URL or LITWATCH_DB_* is not set")
        return self.database_url


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
