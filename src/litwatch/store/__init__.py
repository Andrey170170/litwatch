"""PostgreSQL. The only package that knows SQL: migrations, and one module per aggregate
whose functions take a unit of the Database gate (`gates.database`) and return `core` types:
a `ReadUnit` where they only read, a `WriteUnit` where they write."""

from litwatch.store.db import LegacyDatabaseError, migrate

__all__ = ["LegacyDatabaseError", "migrate"]
