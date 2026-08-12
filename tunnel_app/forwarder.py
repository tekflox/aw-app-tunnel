"""The TCP proxy itself — one asyncio listener per enabled, ready tunnel.

Deliberately protocol-blind: bytes in, bytes out, no inspection. That is what
lets one tunnel carry RFB, SSH, Postgres wire protocol or anything else without
this file knowing which.

Lifecycle is owned by ``TunnelManager`` below: the runtime starts it on
``activate`` and stops it via ``ctx.on_deactivate``, so a workspace restart or
an app update never leaves an orphan listener holding a port.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .store import TunnelError, TunnelStore

log = logging.getLogger("aw_apps.tunnel")

BUF = 65536


class TunnelRuntimeState:
    """Live counters for one tunnel — what the settings page shows."""

    def __init__(self) -> None:
        self.listening = False
        self.active = 0
        self.total = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.last_error: str | None = None
        self.last_connection_at: float | None = None

    def as_dict(self) -> dict:
        return {
            "listening": self.listening,
            "active_connections": self.active,
            "total_connections": self.total,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "last_error": self.last_error,
            "last_connection_at": self.last_connection_at,
        }


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                on_bytes) -> None:
    try:
        while True:
            chunk = await reader.read(BUF)
            if not chunk:
                break
            on_bytes(len(chunk))
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass  # peer closed or write failed — the pair is tearing down anyway
    finally:
        try:
            writer.close()
        except Exception:
            pass


class TunnelManager:
    def __init__(self, ctx, store: TunnelStore) -> None:
        self._ctx = ctx
        self._store = store
        self._servers: dict[str, asyncio.AbstractServer] = {}
        self._state: dict[str, TunnelRuntimeState] = {}

    # ── config knobs, read per-connection so a save takes effect at once ────

    def _allow_lan(self) -> bool:
        cfg = getattr(self._ctx, "config", {}) or {}
        return bool(cfg.get("allow_lan_destinations", True))

    def _connect_timeout(self) -> float:
        cfg = getattr(self._ctx, "config", {}) or {}
        return float(cfg.get("connect_timeout_s") or 10)

    # ── state ───────────────────────────────────────────────────────────────

    def state(self, tunnel_id: str) -> TunnelRuntimeState:
        return self._state.setdefault(tunnel_id, TunnelRuntimeState())

    def status(self) -> list[dict]:
        out = []
        for row in self._store.list():
            out.append({**row, **self.state(row["id"]).as_dict()})
        return out

    # ── start / stop ────────────────────────────────────────────────────────

    async def start(self, tunnel_id: str) -> dict:
        """(Re)start one tunnel's listener. Idempotent: an already-running
        listener is stopped first, so this doubles as 'apply my edit'."""
        await self.stop(tunnel_id)
        row = self._store.get(tunnel_id)
        st = self.state(tunnel_id)

        if not row["enabled"]:
            st.last_error = None
            return {"started": False, "reason": "disabled"}
        if not row["ready"]:
            # Never bind a port for a destination we cannot actually reach —
            # a listener that accepts and then always fails is worse than no
            # listener, because the consumer's error points at the wrong layer.
            st.last_error = row["not_ready_reason"]
            return {"started": False, "reason": row["not_ready_reason"]}

        async def handle(client_r: asyncio.StreamReader,
                         client_w: asyncio.StreamWriter) -> None:
            st.total += 1
            st.active += 1
            st.last_connection_at = time.time()
            dest_r = dest_w = None
            try:
                self._store.check_destination_allowed(row, self._allow_lan())
                dest_r, dest_w = await asyncio.wait_for(
                    asyncio.open_connection(row["dest_host"], row["dest_port"]),
                    timeout=self._connect_timeout(),
                )
                st.last_error = None

                def up(n: int) -> None:
                    st.bytes_up += n

                def down(n: int) -> None:
                    st.bytes_down += n

                await asyncio.gather(
                    _pipe(client_r, dest_w, up),
                    _pipe(dest_r, client_w, down),
                    return_exceptions=True,
                )
            except (TunnelError, asyncio.TimeoutError, OSError) as e:
                st.last_error = str(e) or type(e).__name__
                log.warning("tunnel %s: %s", row["name"], st.last_error)
            finally:
                st.active -= 1
                for w in (client_w, dest_w):
                    if w is not None:
                        try:
                            w.close()
                        except Exception:
                            pass

        try:
            server = await asyncio.start_server(
                handle, row["listen_host"], row["listen_port"])
        except OSError as e:
            st.listening = False
            st.last_error = f"bind {row['listen_host']}:{row['listen_port']} failed: {e}"
            log.warning("tunnel %s: %s", row["name"], st.last_error)
            return {"started": False, "reason": st.last_error}

        self._servers[tunnel_id] = server
        st.listening = True
        st.last_error = None
        log.info("tunnel %s listening on %s:%s -> %s:%s", row["name"],
                 row["listen_host"], row["listen_port"],
                 row["dest_host"], row["dest_port"])
        return {"started": True}

    async def stop(self, tunnel_id: str) -> None:
        server = self._servers.pop(tunnel_id, None)
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
        self.state(tunnel_id).listening = False

    async def start_all(self) -> None:
        for row in self._store.list():
            await self.start(row["id"])

    async def stop_all(self) -> None:
        for tunnel_id in list(self._servers):
            await self.stop(tunnel_id)
