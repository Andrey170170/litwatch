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
