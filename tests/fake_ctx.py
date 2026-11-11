"""The SQLite-backed stand-in for an ``AppContext``, shared by the test
modules here.

``state`` defaults to ``None`` on purpose: a manager with no ``ctx.state``
behaves as a single process that owns its own listeners (see
``TunnelManager.is_leader``), which is what the store/forwarder/HTTP tests
want. The leadership tests hand it a real :class:`tests.fake_state.FakeState`.
"""
from __future__ import annotations

import sqlite3


class FakeCtx:
    """Only what TunnelStore/TunnelManager use: app_id, config, db, state."""

    def __init__(self, conn: sqlite3.Connection, config: dict | None = None,
                 state=None) -> None:
        self.app_id = "tunnel"
        self.config = config or {}
        self.db = self._Db(conn)
        if state is not None:
            self.state = state

    class _Db:
        def __init__(self, conn):
            self._conn = conn

        def table(self, name):
            return name

        def create(self, name, columns_sql):
            cols = (columns_sql
                    .replace("TIMESTAMPTZ", "TEXT")
                    .replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP")
                    .replace("BOOLEAN", "INTEGER")
                    .replace("DEFAULT FALSE", "DEFAULT 0"))
            self._conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({cols})')
            self._conn.commit()
            return name

        def execute(self, name, sql, params=None):
            stmt = sql.replace("{table}", f'"{name}"').replace("now()", "CURRENT_TIMESTAMP")
            cur = self._conn.execute(stmt, params or {})
            self._conn.commit()
            return cur.fetchall() if stmt.strip().lower().startswith("select") else cur
