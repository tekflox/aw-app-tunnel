"""Tunnel definitions — this app's own Postgres table.

A tunnel is a *named local TCP endpoint that forwards to a destination*. The
consumer connects to ``listen_host:listen_port`` and never learns which kind of
destination is on the other side — that indirection is the whole point of the
app, and it is what lets e.g. aw-app-remote-screen store a ``tunnel_id``
instead of a ``host:port`` it may not be able to reach.

Two destination kinds, deliberately NOT equal in cost:

``custom``
    A reachable ``dest_host:dest_port``. Pure app code — ``forwarder.py`` opens
    the connection itself. Works today.

``remote_host``
    ``dest_host:dest_port`` **as seen from** a linked aw-remote-host machine,
    reached over the ``/link`` WebSocket that host already holds open. This
    needs ``tcp_open``/``tcp_data``/``tcp_close`` frames that do not exist yet
    (the link speaks ``http_req``/``ws_*``/``pty_*``/``cmd`` only, and its Go
    side forwards to a fixed local target). Storable so the inventory can be
    entered, and reported as ``ready: false`` with a reason — never started
    silently.

``listen_host`` is the security decision, per tunnel:

``127.0.0.1``
    Only this workspace container. Tier-1 (in-process) apps — aw-app-remote-
    screen among them — share that loopback, so this covers them.
``0.0.0.0``
    Also reachable from Tier-2 app containers (browser, code-server), which have
    their own network namespace and therefore CANNOT see the workspace's
    loopback. The cost is that *every* container on that network can use it.
"""
from __future__ import annotations

import ipaddress
import uuid as _uuid

TABLE_SUFFIX = "tunnels"

TABLE_COLUMNS_SQL = """
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    listen_host    TEXT NOT NULL DEFAULT '127.0.0.1',
    listen_port    INTEGER NOT NULL,
    dest_kind      TEXT NOT NULL DEFAULT 'custom',
    dest_host      TEXT NOT NULL DEFAULT '',
    dest_port      INTEGER NOT NULL DEFAULT 0,
    remote_host_id TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
"""

DEST_KINDS = ("custom", "remote_host")

# Destination kinds whose transport actually exists today.
READY_DEST_KINDS = ("custom",)

NOT_READY_REASON = (
    "Remote-host tunnels need tcp_* frames on the /link tunnel, which "
    "aw-remote-host and aw-backend do not implement yet. Saved, not started."
)

LISTEN_HOSTS = {
    "127.0.0.1": "This workspace only (Tier-1 apps share this loopback)",
    "0.0.0.0": "Also other app containers on the workspace network",
}


class TunnelError(ValueError):
    """Bad input from a caller — routes.py turns this into a 400."""


class TunnelNotFound(LookupError):
    """No row with that id — routes.py turns this into a 404."""


def _is_private(host: str) -> bool:
    """True for loopback/RFC1918/link-local. A hostname (not an IP literal)
    returns False — we deliberately do NOT resolve it here: resolution at
    validation time and resolution at connect time can disagree (DNS rebinding),
    so a name is judged when the connection is actually made, not now."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


class TunnelStore:
    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._table = ctx.db.table(f"app__{ctx.app_id}__{TABLE_SUFFIX}")
        ctx.db.create(self._table, TABLE_COLUMNS_SQL)

    # ── reads ───────────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        rows = self._ctx.db.execute(
            self._table,
            "SELECT id, name, enabled, listen_host, listen_port, dest_kind, "
            "dest_host, dest_port, remote_host_id FROM {table} "
            "ORDER BY listen_port",
        )
        return [self._public(r) for r in rows]

    def get(self, tunnel_id: str) -> dict:
        rows = self._ctx.db.execute(
            self._table,
            "SELECT id, name, enabled, listen_host, listen_port, dest_kind, "
            "dest_host, dest_port, remote_host_id FROM {table} WHERE id = :id",
            {"id": tunnel_id},
        )
        if not rows:
            raise TunnelNotFound(tunnel_id)
        return self._public(rows[0])

    @staticmethod
    def _public(row) -> dict:
        dest_kind = row[5]
        ready = dest_kind in READY_DEST_KINDS
        return {
            "id": row[0],
            "name": row[1],
            "enabled": bool(row[2]),
            "listen_host": row[3],
            "listen_port": row[4],
            "dest_kind": dest_kind,
            "dest_host": row[6],
            "dest_port": row[7],
            "remote_host_id": row[8],
            "ready": ready,
            "not_ready_reason": None if ready else NOT_READY_REASON,
        }

    # ── validation ──────────────────────────────────────────────────────────

    def _validate(self, body: dict, *, exclude_id: str | None = None) -> dict:
        name = (body.get("name") or "").strip()
        if not name:
            raise TunnelError("name is required")

        dest_kind = (body.get("dest_kind") or "custom").strip()
        if dest_kind not in DEST_KINDS:
            raise TunnelError(f"dest_kind must be one of {', '.join(DEST_KINDS)}")

        listen_host = (body.get("listen_host") or "127.0.0.1").strip()
        if listen_host not in LISTEN_HOSTS:
            raise TunnelError(
                f"listen_host must be one of {', '.join(LISTEN_HOSTS)}")

        try:
            listen_port = int(body.get("listen_port") or 0)
            dest_port = int(body.get("dest_port") or 0)
        except (TypeError, ValueError):
            raise TunnelError("ports must be numbers")
        for label, port in (("listen_port", listen_port), ("dest_port", dest_port)):
            if not 1 <= port <= 65535:
                raise TunnelError(f"{label} must be between 1 and 65535")

        dest_host = (body.get("dest_host") or "").strip()
        if not dest_host:
            raise TunnelError("dest_host is required")

        remote_host_id = (body.get("remote_host_id") or "").strip()
        if dest_kind == "remote_host" and not remote_host_id:
            raise TunnelError("remote_host_id is required for a remote_host tunnel")

        # Two tunnels on the same port would race for the listener, and the
        # loser's failure surfaces as an unrelated "address in use" much later.
        for other in self.list():
            if other["id"] == exclude_id:
                continue
            if other["listen_port"] == listen_port:
                raise TunnelError(
                    f"listen_port {listen_port} is already used by {other['name']!r}")
            if other["name"].lower() == name.lower():
                raise TunnelError(f"a tunnel named {name!r} already exists")

        return {
            "name": name,
            "enabled": bool(body.get("enabled", True)),
            "listen_host": listen_host,
            "listen_port": listen_port,
            "dest_kind": dest_kind,
            "dest_host": dest_host,
            "dest_port": dest_port,
            "remote_host_id": remote_host_id,
        }

    def check_destination_allowed(self, row: dict, allow_lan: bool) -> None:
        """Raise if this tunnel's destination is barred by app config. Called at
        CONNECT time, not validation time — see ``_is_private``."""
        if row["dest_kind"] != "custom":
            return
        if not allow_lan and _is_private(row["dest_host"]):
            raise TunnelError(
                f"destination {row['dest_host']} is a private address and "
                "allow_lan_destinations is off")

    # ── writes ──────────────────────────────────────────────────────────────

    def create(self, body: dict) -> dict:
        fields = self._validate(body)
        tunnel_id = str(_uuid.uuid4())
        self._ctx.db.execute(
            self._table,
            "INSERT INTO {table} (id, name, enabled, listen_host, listen_port, "
            " dest_kind, dest_host, dest_port, remote_host_id) "
            "VALUES (:id, :name, :enabled, :listen_host, :listen_port, "
            "        :dest_kind, :dest_host, :dest_port, :remote_host_id)",
            {"id": tunnel_id, **fields},
        )
        return self.get(tunnel_id)

    def update(self, tunnel_id: str, body: dict) -> dict:
        current = self.get(tunnel_id)
        merged = {**current, **{k: v for k, v in body.items() if v is not None}}
        fields = self._validate(merged, exclude_id=tunnel_id)
        self._ctx.db.execute(
            self._table,
            "UPDATE {table} SET name = :name, enabled = :enabled, "
            "listen_host = :listen_host, listen_port = :listen_port, "
            "dest_kind = :dest_kind, dest_host = :dest_host, "
            "dest_port = :dest_port, remote_host_id = :remote_host_id, "
            "updated_at = now() WHERE id = :id",
            {"id": tunnel_id, **fields},
        )
        return self.get(tunnel_id)

    def delete(self, tunnel_id: str) -> None:
        self.get(tunnel_id)  # 404 before we touch anything
        self._ctx.db.execute(
            self._table, "DELETE FROM {table} WHERE id = :id", {"id": tunnel_id})
