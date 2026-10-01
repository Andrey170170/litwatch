import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from litwatch import archive_delivery as delivery
from litwatch.archive_client import ArchiveUnavailable, ArchiveVersionUnknown
from litwatch.models import Paper


@pytest.fixture
def setup(monkeypatch):
    paper = Paper(
        id="2609.12345",
        title="Paper",
        abstract="Abstract",
        version=2,
        updated="2026-09-20T00:00:00+00:00",
    )
    row = {
        "id": 42,
        "lease_token": "lease",
        "attempts": 1,
        "kind": "pdf",
        "priority": 100,
        "remote_job_id": None,
        "payload": {"paper": paper.model_dump(), "source": "oai"},
    }
    claim = AsyncMock(side_effect=lambda *a, **kw: [row])
    complete = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery.archive_outbox, "claim", claim)
    monkeypatch.setattr(delivery.archive_outbox, "complete", complete)
    client = SimpleNamespace(
        upsert_paper=AsyncMock(),
        request=AsyncMock(return_value={"id": 7, "status": "pending"}),
        job=AsyncMock(return_value={"id": 7, "status": "complete"}),
        resolve=AsyncMock(side_effect=ArchiveVersionUnknown("unknown")),
    )
    conn = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value={"id": 42})))
    )
    return row, client, conn, complete


async def test_acceptance_is_not_availability_and_later_poll_completes(setup):
    row, client, conn, complete = setup
    assert await delivery.deliver_once(conn, client) == 1
    assert complete.call_args.kwargs["status"] == "delivered"
    assert complete.call_args.kwargs["remote_job_id"] == 7
    row["remote_job_id"] = 7
    await delivery.deliver_once(conn, client)
    assert complete.call_args.kwargs["status"] == "available"
    assert client.request.await_count == 1


async def test_delivered_urgent_job_is_promoted(setup):
    row, client, conn, complete = setup
    row.update(remote_job_id=7, priority=0)
    await delivery.deliver_once(conn, client)
    assert client.request.call_args.kwargs["priority"] == 0
    assert complete.call_args.kwargs["retry_seconds"] == 30


@pytest.mark.parametrize("remote,status", [("failed", "error"), ("unavailable", "unavailable")])
async def test_terminal_remote_states_do_not_restart_download(setup, remote, status):
    row, client, conn, complete = setup
    row["remote_job_id"] = 7
    client.job.return_value = {"id": 7, "status": remote}
    await delivery.deliver_once(conn, client)
    assert complete.call_args.kwargs["status"] == status
    client.request.assert_not_awaited()
    if remote == "failed":
        assert complete.call_args.kwargs["retry_seconds"] == 3600


async def test_outage_remains_durable_and_does_not_record_secrets(setup):
    row, client, conn, complete = setup
    client.request.side_effect = ArchiveUnavailable("secret-token")
    await delivery.deliver_once(conn, client)
    assert complete.call_args.kwargs["status"] == "error"
    assert "secret-token" not in complete.call_args.kwargs["error"]
    assert complete.call_args.kwargs["retry_seconds"] == 30


async def test_unknown_revision_resolves_at_observation_and_persists_under_lease(
    setup, monkeypatch
):
    row, client, conn, complete = setup
    row["payload"]["paper"]["version"] = None
    monkeypatch.setattr(
        delivery,
        "_fetch_history",
        lambda _: {
            "version": 3,
            "version_history": [
                {"version": 1, "date": "2026-09-10T12:00:00+00:00"},
                {"version": 2, "date": "2026-09-19T12:00:00+00:00"},
                {"version": 3, "date": "2026-09-21T12:00:00+00:00"},
            ],
        },
    )
    await delivery.deliver_once(conn, client)
    assert client.request.call_args.args[0].version == 2
    assert client.upsert_paper.call_args.args[0].version == 2
    query, params = conn.execute.call_args.args
    assert "lease_until>now()" in query and "lease_token=%s" in query
    assert params[0] == 2 and params[-1] == "lease"
    assert complete.call_args.kwargs["status"] == "delivered"


async def test_lost_resolution_lease_never_enqueues(setup, monkeypatch):
    row, client, conn, complete = setup
    row["payload"]["paper"]["version"] = None
    client.resolve.side_effect = None
    client.resolve.return_value = Paper.model_validate({**row["payload"]["paper"], "version": 2})
    conn.execute.return_value.fetchone.return_value = None
    await delivery.deliver_once(conn, client)
    client.request.assert_not_awaited()
    assert complete.call_args.kwargs["status"] == "error"


async def test_unknown_metadata_can_be_delivered_without_artifact_guess(setup):
    row, client, conn, complete = setup
    row["kind"] = "metadata"
    row["payload"]["paper"]["version"] = None
    await delivery.deliver_once(conn, client)
    client.upsert_paper.assert_awaited_once()
    client.resolve.assert_not_awaited()
    assert complete.call_args.kwargs["status"] == "available"


async def test_cancellation_leaves_lease_for_recovery(setup):
    row, client, conn, complete = setup
    client.request.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await delivery.deliver_once(conn, client)
    complete.assert_not_awaited()


async def test_dispatcher_reconnects_and_stops_without_logging_dsn(monkeypatch, caplog):
    stop = asyncio.Event()
    connection = SimpleNamespace(closed=False, close=AsyncMock())
    connect = AsyncMock(side_effect=[RuntimeError("secret-dsn"), connection])
    monkeypatch.setattr(delivery.db, "connect", connect)

    async def deliver(*args, **kwargs):
        stop.set()
        return 0

    monkeypatch.setattr(delivery, "deliver_once", deliver)
    await delivery.run_dispatcher("secret-dsn", object(), stop, interval_seconds=0.001)
    assert connect.await_count == 2
    connection.close.assert_awaited_once()
    assert "secret-dsn" not in caplog.text


async def test_versionless_rss_uses_snapshot_observation_time(setup, monkeypatch):
    row, client, conn, complete = setup
    row["payload"].update(source="atom", observed_at="2026-09-20T13:00:00+00:00")
    row["payload"]["paper"].update(version=None, published=None, updated=None)
    monkeypatch.setattr(
        delivery,
        "_fetch_history",
        lambda _: {
            "version_history": [
                {"version": 1, "date": "2026-09-19T12:00:00+00:00"},
                {"version": 2, "date": "2026-09-21T12:00:00+00:00"},
            ]
        },
    )
    await delivery.deliver_once(conn, client)
    assert client.request.call_args.args[0].version == 1
    client.upsert_paper.assert_not_awaited()
    assert complete.call_args.kwargs["status"] == "delivered"


async def test_resolved_canonical_metadata_outage_does_not_ack_resolution(setup):
    row, client, conn, complete = setup
    row["payload"]["paper"]["version"] = None
    client.resolve.side_effect = None
    client.resolve.return_value = Paper.model_validate({**row["payload"]["paper"], "version": 2})
    client.upsert_paper.side_effect = ArchiveUnavailable("outage")
    await delivery.deliver_once(conn, client)
    conn.execute.assert_not_awaited()
    client.request.assert_not_awaited()
    assert complete.call_args.kwargs["status"] == "error"
