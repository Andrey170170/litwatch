"""Migrations: a fresh database, an adopted 0.x one, and the refusal in between."""

import pytest

from litwatch import store


async def tables(conn) -> set[str]:
    rows = await (
        await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    ).fetchall()
    return {r["tablename"] for r in rows}


async def test_migrations_apply_once(conn):
    assert await store.migrate(conn) == []
    assert {"papers", "runs", "inference_calls", "outbox"} <= await tables(conn)


async def test_a_0x_database_is_refused_until_it_is_adopted(conn):
    # Rebuild the schema as 0.x left it: no migration record, its own verdicts and selections.
    for table in await tables(conn):
        await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    await conn.execute(
        """
        CREATE TABLE papers (id text PRIMARY KEY, title text NOT NULL, abstract text NOT NULL,
            authors jsonb NOT NULL DEFAULT '[]', categories text[] NOT NULL DEFAULT '{}',
            primary_category text, published timestamptz, updated timestamptz, url text,
            fetched_at timestamptz NOT NULL DEFAULT now(), tier text,
            metadata_source text NOT NULL DEFAULT 'arxiv', version integer,
            version_history jsonb NOT NULL DEFAULT '[]', doi text, journal_ref text, license text,
            search tsvector GENERATED ALWAYS AS
                (to_tsvector('english', title || ' ' || abstract)) STORED);
        CREATE TABLE runs (id bigserial PRIMARY KEY, kind text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
            window_from timestamptz, window_to timestamptz, fetched integer NOT NULL DEFAULT 0,
            inserted integer NOT NULL DEFAULT 0, updated integer NOT NULL DEFAULT 0,
            cost numeric(10, 4) NOT NULL DEFAULT 0, error text, intake jsonb);
        CREATE TABLE selections (slug text PRIMARY KEY, name text NOT NULL);
        CREATE TABLE verdicts (paper_id text NOT NULL REFERENCES papers (id),
            selection text NOT NULL, tier smallint NOT NULL, PRIMARY KEY (paper_id, selection));
        CREATE INDEX verdicts_selection_tier_idx ON verdicts (selection, tier);
        INSERT INTO papers (id, title, abstract, tier) VALUES ('1', 't', 'a', '2');
        INSERT INTO verdicts VALUES ('1', 'cot-faithfulness', 2);
        """
    )
    with pytest.raises(store.LegacyDatabaseError):
        await store.migrate(conn)
    assert "schema_migrations" not in await tables(conn)

    await store.adopt_0x(conn)
    names = await tables(conn)
    assert {"legacy_verdicts", "legacy_selections", "outbox", "papers"} <= names
    kept = await (await conn.execute("SELECT id, tier FROM papers")).fetchall()
    assert kept == [{"id": "1", "tier": "2"}]
    assert (await (await conn.execute("SELECT counts FROM runs")).fetchall()) == []
    with pytest.raises(RuntimeError, match="already"):
        await store.adopt_0x(conn)
