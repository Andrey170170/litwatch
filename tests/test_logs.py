"""JSON logs as the homelab's telemetry convention names their fields."""

import json
import logging

from litwatch.logs import JsonFormatter


def test_a_record_is_one_json_object_with_the_conventions_fields():
    record = logging.LogRecord("litwatch.jobs", logging.WARNING, "x.py", 1, "%s left", (3,), None)
    line = json.loads(JsonFormatter().format(record))
    assert line["level"] == "warn"
    assert line["msg"] == "3 left"
    assert line["component"] == "litwatch.jobs"
    assert line["ts"].endswith("+00:00")


def test_a_records_fields_join_the_object_as_context():
    record = logging.LogRecord("litwatch.api.mcp", logging.INFO, "x.py", 1, "done", (), None)
    record.fields = {"event": "mcp.call", "tool": "get_paper", "outcome": "ok"}
    line = json.loads(JsonFormatter().format(record))
    assert (line["msg"], line["event"], line["tool"], line["outcome"]) == (
        "done", "mcp.call", "get_paper", "ok",
    )  # fmt: skip
