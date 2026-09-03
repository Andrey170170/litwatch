from datetime import UTC, datetime

from litwatch.arxiv_source import window_query
from litwatch.ingest import window


def test_window_query_covers_categories_and_dates():
    since = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 9, 3, 12, 30, tzinfo=UTC)
    q = window_query(["cs.CL", "cs.AI"], since, until)
    assert q == "(cat:cs.CL OR cat:cs.AI) AND submittedDate:[202609010000 TO 202609031230]"


def test_window_ends_now_and_spans_days():
    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    since, until = window(3, now)
    assert until == now and (until - since).days == 3
