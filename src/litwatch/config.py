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


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    screen_model: str = "openai/gpt-5.6-luna"
    screen_effort: str = "low"
    rescreen_model: str = "openai/gpt-5.6-sol"
    rescreen_effort: str = "low"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is not set")
        return cls(
            openrouter_api_key=key,
            screen_model=os.environ.get("LITWATCH_SCREEN_MODEL", cls.screen_model),
            screen_effort=os.environ.get("LITWATCH_SCREEN_EFFORT", cls.screen_effort),
            rescreen_model=os.environ.get("LITWATCH_RESCREEN_MODEL", cls.rescreen_model),
            rescreen_effort=os.environ.get("LITWATCH_RESCREEN_EFFORT", cls.rescreen_effort),
        )
