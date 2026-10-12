"""webhook: POSTs every event that matches a filter to a URL, as JSON. n8n workflows, and
through them the Notion export, hang off this.

The body is the event: `{"id", "type", "paper_id", "payload", "created_at"}`. A receiver reads
anything more through litwatch's API. The event id is also sent as `X-Litwatch-Event`, so a
receiver can drop the repeat it may get after a failure; with a secret configured the body
is signed, `X-Litwatch-Signature: sha256=<HMAC-SHA256 of the body, hex>`. Any answer but a
2xx is a failure and the event is sent again after a backoff.

Several webhooks are several sections with `use = "webhook"`.

Options: `url` (or $LITWATCH_WEBHOOK_URL), `events` (patterns such as "paper.*"; default
every event) and `secret_env`, the environment variable holding the signing secret.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
from typing import Any

import httpx
import psycopg

from litwatch.config import Config
from litwatch.core import Event
from litwatch.integrations import IntegrationError, required, required_secret


def body(event: Event) -> bytes:
    return json.dumps(event.model_dump(mode="json"), separators=(",", ":")).encode()


def signature(secret: str, content: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), content, hashlib.sha256).hexdigest()


class Webhook:
    def __init__(
        self,
        url: str,
        *,
        name: str = "webhook",
        patterns: tuple[str, ...] = ("*",),
        secret: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.patterns = patterns
        self.secret = secret
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self.description = f"POSTs {', '.join(patterns)} events to a URL."

    def wants(self, event: Event) -> bool:
        return any(fnmatch.fnmatchcase(event.type, p) for p in self.patterns)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        content = body(event)
        headers = {"Content-Type": "application/json", "X-Litwatch-Event": str(event.id)}
        if self.secret:
            headers["X-Litwatch-Signature"] = signature(self.secret, content)
        resp = await self.http.post(self.url, content=content, headers=headers)
        if not resp.is_success:
            # The URL may carry a token; the status says enough.
            raise RuntimeError(f"the webhook answered HTTP {resp.status_code}")


def load(name: str, options: dict[str, Any], config: Config) -> Webhook:
    patterns = options.get("events", ["*"])
    if isinstance(patterns, str) or not patterns:
        raise IntegrationError("`events` is a list of event-type patterns")
    secret_env = options.get("secret_env")
    return Webhook(
        required(options, "url", env="LITWATCH_WEBHOOK_URL"),
        name=name,
        patterns=tuple(patterns),
        secret=required_secret(secret_env) if secret_env else None,
    )
