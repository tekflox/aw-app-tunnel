"""Store + forwarder + routes against a SQLite-backed fake ctx.

The forwarder tests are the ones that matter: they stand up a REAL echo server,
a REAL tunnel listener, and push bytes through the whole path. A TCP proxy that
is only unit-tested against mocks proves nothing — the failure modes here are
all in the socket plumbing.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tunnel_app.forwarder import TunnelManager  # noqa: E402
from tunnel_app.routes import build_routes  # noqa: E402
from tunnel_app.store import TunnelError, TunnelNotFound, TunnelStore  # noqa: E402


class FakeCtx:
    """Only what TunnelStore/TunnelManager use: app_id, config, db."""

    def __init__(self, conn: sqlite3.Connection, config: dict | None = None) -> None:
        self.app_id = "tunnel"
        self.config = config or {}
        self.db = self._Db(conn)

    class _Db:
        def __init__(self, conn):
            self._conn = conn

        def table(self, name):
            return name

        def create(self, name, columns_sql):
            cols = (columns_sql
                    .replace("TIMESTAMPTZ", "TEXT")
                    .replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP")
                    .replace("BOOLEAN", "INTEGER"))
            self._conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({cols})')
            self._conn.commit()
            return name

        def execute(self, name, sql, params=None):
            stmt = sql.replace("{table}", f'"{name}"').replace("now()", "CURRENT_TIMESTAMP")
            cur = self._conn.execute(stmt, params or {})
            self._conn.commit()
            return cur.fetchall() if stmt.strip().lower().startswith("select") else cur


@pytest.fixture()
def ctx():
    return FakeCtx(sqlite3.connect(":memory:", check_same_thread=False))


@pytest.fixture()
def store(ctx):
    return TunnelStore(ctx)


@pytest.fixture()
def manager(ctx, store):
    return TunnelManager(ctx, store)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── store ───────────────────────────────────────────────────────────────────

def test_custom_tunnel_is_ready_remote_host_is_not(store):
    custom = store.create({"name": "a", "listen_port": 15001,
                           "dest_host": "127.0.0.1", "dest_port": 22})
    assert custom["ready"] is True
    assert custom["not_ready_reason"] is None

    remote = store.create({"name": "b", "listen_port": 15002, "dest_kind": "remote_host",
                           "dest_host": "127.0.0.1", "dest_port": 5900,
                           "remote_host_id": "abc123"})
    # Honest about the gap rather than binding a port that can never work.
    assert remote["ready"] is False
    assert "tcp_*" in remote["not_ready_reason"]


def test_remote_host_tunnel_requires_a_host_id(store):
    with pytest.raises(TunnelError):
        store.create({"name": "b", "listen_port": 15003, "dest_kind": "remote_host",
                      "dest_host": "127.0.0.1", "dest_port": 5900})


def test_duplicate_port_and_name_rejected(store):
    store.create({"name": "a", "listen_port": 15004,
                  "dest_host": "127.0.0.1", "dest_port": 22})
    with pytest.raises(TunnelError):
        store.create({"name": "other", "listen_port": 15004,
                      "dest_host": "127.0.0.1", "dest_port": 22})
    with pytest.raises(TunnelError):
        store.create({"name": "A", "listen_port": 15005,
                      "dest_host": "127.0.0.1", "dest_port": 22})


def test_editing_a_tunnel_can_keep_its_own_port(store):
    """The duplicate-port check must exclude the row being edited, or renaming a
    tunnel would fail against itself."""
    row = store.create({"name": "a", "listen_port": 15006,
                        "dest_host": "127.0.0.1", "dest_port": 22})
    updated = store.update(row["id"], {"name": "a-renamed"})
    assert updated["listen_port"] == 15006
    assert updated["name"] == "a-renamed"


@pytest.mark.parametrize("body", [
    {"name": "", "listen_port": 1, "dest_host": "h", "dest_port": 1},
    {"name": "n", "listen_port": 0, "dest_host": "h", "dest_port": 1},
    {"name": "n", "listen_port": 1, "dest_host": "h", "dest_port": 70000},
    {"name": "n", "listen_port": 1, "dest_host": "", "dest_port": 1},
    {"name": "n", "listen_port": 1, "dest_host": "h", "dest_port": 1, "dest_kind": "ftp"},
    {"name": "n", "listen_port": 1, "dest_host": "h", "dest_port": 1, "listen_host": "8.8.8.8"},
])
def test_bad_input_rejected(store, body):
    with pytest.raises(TunnelError):
        store.create(body)


def test_delete(store):
    row = store.create({"name": "a", "listen_port": 15007,
                        "dest_host": "127.0.0.1", "dest_port": 22})
    store.delete(row["id"])
    assert store.list() == []
    with pytest.raises(TunnelNotFound):
        store.get(row["id"])


# ── forwarder: real sockets ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bytes_flow_through_the_tunnel(store, manager):
    """The end-to-end path: client -> listener -> destination -> back."""
    async def echo(reader, writer):
        while True:
            data = await reader.read(1024)
            if not data:
                break
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    dest_port = _free_port()
    dest = await asyncio.start_server(echo, "127.0.0.1", dest_port)
    listen_port = _free_port()
    row = store.create({"name": "echo", "listen_port": listen_port,
                        "dest_host": "127.0.0.1", "dest_port": dest_port})
    assert (await manager.start(row["id"]))["started"] is True

    reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
    writer.write(b"hello tunnel")
    await writer.drain()
    assert await reader.read(1024) == b"HELLO TUNNEL"
    writer.close()

    await asyncio.sleep(0.05)
    st = manager.state(row["id"]).as_dict()
    assert st["listening"] is True
    assert st["total_connections"] == 1
    assert st["bytes_up"] == len(b"hello tunnel")
    assert st["bytes_down"] == len(b"HELLO TUNNEL")

    await manager.stop_all()
    dest.close()


@pytest.mark.asyncio
async def test_stop_frees_the_port(store, manager):
    """An update moves the listener; if stop() didn't really close, the rebind
    would fail with 'address in use' and every edit would break the tunnel."""
    listen_port = _free_port()
    row = store.create({"name": "t", "listen_port": listen_port,
                        "dest_host": "127.0.0.1", "dest_port": _free_port()})
    assert (await manager.start(row["id"]))["started"] is True
    await manager.stop(row["id"])
    # Same port must be bindable again immediately.
    assert (await manager.start(row["id"]))["started"] is True
    await manager.stop_all()


@pytest.mark.asyncio
async def test_not_ready_tunnel_never_binds(store, manager):
    row = store.create({"name": "rh", "listen_port": _free_port(),
                        "dest_kind": "remote_host", "dest_host": "127.0.0.1",
                        "dest_port": 5900, "remote_host_id": "h1"})
    result = await manager.start(row["id"])
    assert result["started"] is False
    assert manager.state(row["id"]).listening is False


@pytest.mark.asyncio
async def test_disabled_tunnel_never_binds(store, manager):
    row = store.create({"name": "off", "listen_port": _free_port(), "enabled": False,
                        "dest_host": "127.0.0.1", "dest_port": 22})
    assert (await manager.start(row["id"]))["reason"] == "disabled"
    assert manager.state(row["id"]).listening is False


@pytest.mark.asyncio
async def test_unreachable_destination_records_an_error_not_a_crash(store, manager):
    dest_port = _free_port()  # nothing listening there
    listen_port = _free_port()
    row = store.create({"name": "dead", "listen_port": listen_port,
                        "dest_host": "127.0.0.1", "dest_port": dest_port})
    await manager.start(row["id"])
    reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
    await reader.read(100)  # closed straight away
    writer.close()
    await asyncio.sleep(0.05)
    assert manager.state(row["id"]).last_error
    assert manager.state(row["id"]).listening is True  # listener survives
    await manager.stop_all()


@pytest.mark.asyncio
async def test_lan_destination_blocked_when_config_says_so(ctx, store, manager):
    ctx.config = {"allow_lan_destinations": False}
    listen_port = _free_port()
    row = store.create({"name": "lan", "listen_port": listen_port,
                        "dest_host": "127.0.0.1", "dest_port": _free_port()})
    await manager.start(row["id"])
    reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
    await reader.read(100)
    writer.close()
    await asyncio.sleep(0.05)
    assert "private address" in (manager.state(row["id"]).last_error or "")
    await manager.stop_all()


# ── HTTP surface ────────────────────────────────────────────────────────────

def test_crud_over_http(ctx, store, manager):
    client = TestClient(build_routes(ctx, store, manager))
    created = client.post("/tunnels", json={
        "name": "a", "listen_port": _free_port(),
        "dest_host": "127.0.0.1", "dest_port": 22}).json()
    assert client.get("/tunnels").json()["tunnels"][0]["name"] == "a"
    assert client.put(f"/tunnels/{created['id']}",
                      json={"name": "a2"}).json()["name"] == "a2"
    assert client.delete(f"/tunnels/{created['id']}").status_code == 200
    assert client.get("/tunnels").json()["tunnels"] == []
    asyncio.run(manager.stop_all())


def test_http_error_codes(ctx, store, manager):
    client = TestClient(build_routes(ctx, store, manager))
    assert client.post("/tunnels", json={"name": "x"}).status_code == 400
    assert client.put("/tunnels/nope", json={"name": "y"}).status_code == 404
    assert client.delete("/tunnels/nope").status_code == 404
    assert client.post("/tunnels/nope/start").status_code == 404


def test_settings_page_is_served(ctx, store, manager):
    client = TestClient(build_routes(ctx, store, manager))
    res = client.get("/ui/tunnels")
    assert res.status_code == 200
    assert "/api/apps/tunnel" in res.text
