"""PostgreSQL. The only package that knows SQL: migrations, and one module per aggregate
whose functions take a connection and return `core` types."""

from litwatch.store.db import LegacyDatabaseError, adopt_0x, connect, migrate

__all__ = ["LegacyDatabaseError", "adopt_0x", "connect", "migrate"]
