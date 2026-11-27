"""`scripts/refresh-sandbox.sh --production`'s cleanup of the copy: what only means something
to production is cut loose. Its SQL, read from the script, run on a copy of the test schema."""

from pathlib import Path

from tests.sessions import Session

SCRIPT = Path(__file__).parent.parent / "scripts" / "refresh-sandbox.sh"


def cleanup() -> list[str]:
    """The statements of the script's cleanup transaction."""
    text = SCRIPT.read_text()
    body = text.split("<<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
    return [s for s in body.split(";\n") if s.strip()]


async def test_a_production_copy_keeps_no_live_command_state(conn: Session):
    """Production's `serve` rows go and its open command items end, so a sandbox `serve` never
    runs a command production's owner submitted; ended items and `serve`'s own stay as they
    are."""
    await conn.execute("INSERT INTO workers (name, kinds) VALUES ('prod:1', '{command:ping}')")
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, state) VALUES "
        "('command:digests', 'a', 'a', 'pending'), ('fulltext', '1', 'fulltext:1', 'pending')"
    )
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, state, lease_token, lease_until) "
        "VALUES ('command:ping', 'b', 'b', 'running', gen_random_uuid(), now() + interval '1h')"
    )
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, state, outcome) "
        "VALUES ('command:ping', 'd', 'd', 'ended', 'done')"
    )
    for statement in cleanup():
        await conn.execute(statement)
    assert await (await conn.execute("SELECT * FROM workers")).fetchall() == []
    found = await (
        await conn.execute("SELECT key, state, outcome FROM work_items ORDER BY key")
    ).fetchall()
    assert [(r["key"], r["state"], r["outcome"]) for r in found] == [
        ("1", "pending", None),
        ("a", "ended", "failed"),
        ("b", "ended", "failed"),
        ("d", "ended", "done"),
    ]
