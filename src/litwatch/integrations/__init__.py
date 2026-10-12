"""Integrations: everything outside the core that reacts to events. Each is off unless
configured, and each is one module with a `load` function:

    def load(name: str, options: dict[str, Any], config: Config) -> Integration

A `[integrations.<name>]` section of `litwatch.toml` loads one. `use` says which module: a
built-in by its short name, or any importable module by dotted path; it defaults to the
section's name, so `[integrations.zotero]` loads the built-in. `enabled = false` turns a
section off. Everything else in the section is the module's options; secrets come from the
environment. `docs/integrations.md` is the guide to writing one.
"""

from __future__ import annotations

import importlib
from typing import Any

import psycopg

from litwatch.config import Config, secret
from litwatch.core import Grade
from litwatch.events import Integration
from litwatch.store import word as word_store

BUILT_IN = ("zotero", "webdav", "archive", "deep", "webhook")


class IntegrationError(ValueError):
    """An integration is configured in a way that cannot work."""


def _module(use: str) -> Any:
    if use in BUILT_IN:
        return importlib.import_module(f"litwatch.integrations.{use}")
    if "." not in use:
        raise IntegrationError(f"no built-in integration {use!r}; built-in: {', '.join(BUILT_IN)}")
    return importlib.import_module(use)


def load(config: Config) -> list[Integration]:
    """The integrations the configuration enables, in its order."""
    loaded: list[Integration] = []
    for name, section in config.integrations.items():
        options = dict(section)
        if not options.pop("enabled", True):
            continue
        use = options.pop("use", name)
        try:
            integration = _module(use).load(name, options, config)
        except (ImportError, KeyError, ValueError) as exc:
            raise IntegrationError(f"[integrations.{name}]: {exc}") from exc
        if not isinstance(integration, Integration):
            raise IntegrationError(f"{use}.load did not return an Integration")
        loaded.append(integration)
    names = [i.name for i in loaded]
    if len(set(names)) != len(names):
        raise IntegrationError(f"two integrations share a name: {names}")
    for integration in loaded:
        for needed in getattr(integration, "requires", ()):
            if needed not in names:
                raise IntegrationError(f"{integration.name} needs the {needed} integration too")
    return loaded


def option(options: dict[str, Any], key: str, *, env: str | None = None) -> Any:
    """A setting from the section, else from the environment variable `env` (or the file its
    `_FILE` variable names); None if neither."""
    if key in options:
        return options[key]
    return secret(env) if env else None


def required(options: dict[str, Any], key: str, *, env: str | None = None) -> Any:
    value = option(options, key, env=env)
    if value in (None, ""):
        raise IntegrationError(f"missing `{key}`" + (f" (or ${env})" if env else ""))
    return value


def required_secret(env: str) -> str:
    """A secret from the environment variable, or the file its `_FILE` variable names. Never
    from `litwatch.toml`."""
    value = secret(env)
    if not value:
        raise IntegrationError(f"missing ${env} (or ${env}_FILE)")
    return value


async def must_read_and_saved(conn: psycopg.AsyncConnection, paper_id: str) -> bool:
    """What an integration acting on `must_read` checks when it handles the event, rather than
    trusting what the event said: the owner's word or a later verdict may have moved the
    paper since, and the owner may have unsaved it."""
    row = await word_store.library_row(conn, paper_id)
    grades = await word_store.effective_for(conn, paper_id)
    return bool(row and row["saved"]) and Grade.MUST_READ in grades.values()
