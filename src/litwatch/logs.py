"""Logging for the CLI and the service: plain lines by default, or one JSON object per line with
`LITWATCH_LOG_FORMAT=json`, as the image sets (the homelab's telemetry convention: `ts`, `level`,
`msg`, and `component`, the logger's name; a record's `fields`, given as `extra={"fields": ...}`,
join the object as context fields)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

# The convention's level names.
LEVELS = {"WARNING": "warn", "CRITICAL": "error"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": LEVELS.get(record.levelname, record.levelname.lower()),
            "msg": record.getMessage(),
            "component": record.name,
        }
        line |= getattr(record, "fields", {})
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


def setup(level: int = logging.INFO) -> None:
    """Send every logger's records, uvicorn's included, to stderr in the configured format."""
    handler = logging.StreamHandler()
    if os.environ.get("LITWATCH_LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # The MCP SDK logs every stateless request at info; /mcp logs its own line per call.
    logging.getLogger("mcp").setLevel(logging.WARNING)
