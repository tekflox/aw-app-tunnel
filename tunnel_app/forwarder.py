"""The TCP proxy itself — one asyncio listener per enabled, ready tunnel.

Deliberately protocol-blind: bytes in, bytes out, no inspection. That is what
lets one tunnel carry RFB, SSH, Postgres wire protocol or anything else without
this file knowing which.

**Exactly one worker binds.** Core runs several uvicorn workers
(``AW_WORKSPACE_WORKERS``) and a Tier-1 in-process app loads into every one of
them, each with its own ``TunnelManager`` and its own private ``_servers`` /
``_state``. Before this file grew a leadership loop, every worker called
``start_all()`` on boot: one won each port and the other N-1 logged "address
already in use" — harmless for connectivity (the OS routes connections to
whichever fd actually holds the listener) but *not* for truth, because
``GET /tunnels`` lands on whichever worker serves that one request and reported
that worker's private ``last_error``. A fully working tunnel therefore showed a
permanent, invented bind error in the Settings UI. That fabrication is the bug
behind Kanban ``4155bf3b-9510-81b9-b158-e7d877923019``.

So: every worker runs :meth:`TunnelManager.run_forever`, which each tick claims
one app-owned flock lease (``ctx.state.lease``). The holder reconciles the
listeners it is running against the store and publishes one status snapshot to
``ctx.state.kv``; every other worker holds zero listeners and reports nothing of
its own. HTTP mutations write the store (shared, every worker sees it) and then
nudge the leader over ``ctx.state.broadcast`` — a latency optimisation only:
broadcast is dropped outright when Redis is down, so the tick is the correctness
guarantee, never the nudge.

``start()``/``stop()`` stay exactly what they were — leader-local primitives
that bind and unbind one port. Leadership lives in the loop and in the routes,
not inside them.

This pins ALL listeners to one worker, which is safe only because the data path
has no worker affinity: a ``custom`` tunnel dials outbound TCP per connection
and a ``remote_host`` tunnel opens a FRESH WebSocket to aw-backend per
connection (``remote_dial.open_bridge``), so any worker could serve any
connection. If ``remote_dial`` ever moves to one shared in-process ``/link``
connection, that stops being true and this flips to per-tunnel,
ownership-based leases — the same constraint that made
agents-platform-multitenant's own ``tunnels.py`` use per-client ownership
instead of a global leader.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import remote_dial
from .store import TunnelError, TunnelStore

log = logging.getLogger("aw_apps.tunnel")

BUF = 65536

#: ``ctx.state.lease`` name. One holder across every worker; the held
#: open-file-description IS the liveness proof, so a leader that dies (crash,
#: kill, reload) drops it in the kernel and a standby claims it on its next
#: tick. Claimed ONLY from :meth:`TunnelManager.tick_once` — never through
#: ``lease.is_held()``, which momentarily ACQUIRES a free lease to answer and
#: can therefore steal a concurrent claim (src/apps/state_facade.py:372-385).
LEASE_NAME = "forwarder-leader"

#: ``ctx.state.kv`` name the leader publishes every tunnel's live state under,
#: so a request served by any worker reports the same truth.
STATUS_KV_NAME = "status"

#: ``ctx.state.broadcast`` topic a mutation publishes to make the leader
#: reconcile now instead of on its next tick. Best-effort by design.
RECONCILE_TOPIC = "reconcile"

#: Seconds between leadership claims / reconcile passes. Also the upper bound
#: on leader takeover after the holder dies.
TICK_S = 10.0

#: TTL on the published status. 3x the tick so one slow pass does not flap the
#: UI to "unknown"; ``ctx.state.kv`` has no immortal keys, so some TTL is
#: mandatory rather than a choice.
STATUS_TTL_S = TICK_S * 3

#: How long a mutation waits for the leader's published status to reflect it
#: before answering "accepted, pending". Short: this is a UI nicety on the
#: non-leader path, not a correctness mechanism.
APPLY_WAIT_S = 2.5
APPLY_POLL_S = 0.25

#: Fields whose value decides what the listener IS. A change to any of them
#: needs a rebind — including the destination ones, because the connection
#: handler closes over the row it was started with.
_BIND_FIELDS = ("enabled", "ready", "listen_host", "listen_port",
                "dest_kind", "dest_host", "dest_port", "remote_host_id")


def _bind_key(row: dict) -> tuple:
    return tuple(row.get(f) for f in _BIND_FIELDS)


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


def unknown_state() -> dict:
    """The shape a worker reports when it genuinely does not know: no
    listener of its own, and no published snapshot from the leader. Zeroed
    counters and ``last_error: None`` — never a locally invented error."""
    return TunnelRuntimeState().as_dict()


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
        # What each running listener was bound FOR, captured at bind time, so
        # reconcile can tell "already correct" from "needs a rebind" without
        # tearing down every listener on every pass.
        self._bound: dict[str, tuple] = {}
        self._is_leader = False
        self._tick_s = TICK_S
        self._nudge = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._in_reconcile = False
        self._subscribed_to: object | None = None
        self._state_failures = 0
        self._publish_task: asyncio.Task | None = None

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

    @property
    def is_leader(self) -> bool:
        """Whether THIS worker owns the listeners, as of its last tick. A
        cached fact, never a probe — see :data:`LEASE_NAME`.

        With no ``ctx.state`` to coordinate through there is nothing to lose a
        claim to, so this process owns whatever it bound — the same answer
        :meth:`_claim_leadership` gives, and the pre-leadership behaviour."""
        return self._state_facade() is None or self._is_leader

    def local_status(self) -> dict[str, dict]:
        """This worker's own view, id -> counters. Only ever published or
        served by the leader; on any other worker it is the fiction this card
        exists to stop reporting."""
        return {row["id"]: self.state(row["id"]).as_dict()
                for row in self._store.list()}

    # ── ctx.state plumbing ──────────────────────────────────────────────────
    #
    # ``_ctx`` is re-pointed by plugin.on_config_saved, so every pass reads it
    # through self rather than capturing it when the task starts.

    def _state_facade(self):
        """``ctx.state`` or ``None``. Attribute access itself raises
        ``PermissionError`` when ``state:own`` was not granted (the facade is
        handed out only after the capability check), which is exactly the
        failure an app update that dropped the grant produces — so catch it
        here once rather than at a dozen call sites."""
        try:
            return getattr(self._ctx, "state", None)
        except Exception as exc:  # noqa: BLE001
            self._note_state_failure("state", exc)
            return None

    def _note_state_failure(self, op: str, exc: Exception) -> None:
        """A ctx.state call raised. Loud once (it is almost always a missing
        ``state:own`` grant on an app update, which no amount of retrying
        fixes), quiet afterwards so a persistent fault cannot flood the log
        every tick."""
        self._state_failures += 1
        if self._state_failures == 1:
            log.error(
                "tunnel: ctx.state.%s raised (%s) — the app needs the "
                "'state:own' capability granted; falling back to every worker "
                "binding its own listeners, which is noisy but keeps tunnels "
                "up", op, exc)
        else:
            log.debug("tunnel: ctx.state.%s raised again: %s", op, exc)

    async def published_status(self) -> dict | None:
        """The leader's last published snapshot, or ``None`` on a miss (no
        leader has published yet, the key's TTL lapsed, or Redis is down).
        ``None`` means "unknown", which is a different answer from "broken"."""
        state = self._state_facade()
        if state is None:
            return None
        try:
            snap = await state.kv.get(STATUS_KV_NAME)
        except Exception as exc:  # noqa: BLE001 — degrade to unknown, never 500
            self._note_state_failure("kv.get", exc)
            return None
        return snap if isinstance(snap, dict) else None

    async def publish_status(self) -> None:
        state = self._state_facade()
        if state is None:
            return
        payload = {"at": time.time(), "tunnels": self.local_status()}
        try:
            await state.kv.put(STATUS_KV_NAME, payload, ttl=STATUS_TTL_S)
        except Exception as exc:  # noqa: BLE001
            self._note_state_failure("kv.put", exc)

    def _publish_soon(self) -> None:
        """Refresh the published status after a transition that happened
        inside a connection handler. Fire-and-forget with a single coalescing
        slot: the byte path must never await Redis."""
        if not self._is_leader or self._state_facade() is None:
            return
        if self._publish_task is not None and not self._publish_task.done():
            return
        self._publish_task = asyncio.create_task(self.publish_status())

    # ── leadership + reconcile loop ─────────────────────────────────────────

    def _claim_leadership(self) -> bool:
        state = self._state_facade()
        if state is None:
            # No facade: a unit-test ctx, or a 'state:own' grant that did not
            # land on an app update. Fail OPEN deliberately — every worker
            # binding its own listeners is the noisy pre-leadership behaviour,
            # and a tunnel app that binds nothing at all is a far harder
            # outage than that noise. _state_facade already logged it loudly.
            return True
        try:
            return state.lease.claim(LEASE_NAME)
        except Exception as exc:  # noqa: BLE001
            self._note_state_failure("lease.claim", exc)
            return True

    async def tick_once(self) -> bool:
        """One pass: claim leadership, and if we hold it, make reality match
        the store and publish what reality now is. Returns whether this worker
        is the binder."""
        leader = self._claim_leadership()
        was_leader = self._is_leader
        self._is_leader = leader
        if not leader:
            if self._servers:
                # Can only happen if this worker held the lease and lost it
                # (release, or a reload that dropped the fd). Give the ports
                # back rather than keeping a listener no one reports on.
                log.info("tunnel: not the forwarder leader — releasing %d "
                         "listener(s)", len(self._servers))
                await self.stop_all()
            return False
        if not was_leader:
            log.info("tunnel: this worker is now the forwarder leader")
        await self.reconcile()
        await self.publish_status()
        return True

    async def run_forever(self, tick_s: float | None = None) -> None:
        """The task plugin.activate starts. Never returns; cancelled on
        deactivate."""
        if tick_s is not None:
            self._tick_s = tick_s
        while True:
            self._nudge.clear()
            await self._ensure_subscribed()
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a bad tick must not kill the loop
                log.exception("tunnel: reconcile tick failed")
            try:
                await asyncio.wait_for(self._nudge.wait(), timeout=self._tick_s)
            except asyncio.TimeoutError:
                pass

    async def _ensure_subscribed(self) -> None:
        """Subscribe to the reconcile nudge, and re-subscribe if ctx was
        re-pointed under us by on_config_saved (the broadcaster hangs off the
        ctx, so the old subscription would be on a stale facade)."""
        state = self._state_facade()
        if state is None or self._subscribed_to is self._ctx:
            return
        try:
            await state.broadcast.subscribe(RECONCILE_TOPIC, self._on_nudge)
        except Exception as exc:  # noqa: BLE001 — the tick is the guarantee
            self._note_state_failure("broadcast.subscribe", exc)
            return
        self._subscribed_to = self._ctx

    async def _on_nudge(self, _payload: dict) -> None:
        self._nudge.set()

    async def request_reconcile(self) -> None:
        """Ask whoever holds the lease to apply the store's current contents
        now. The leader does it inline (so a mutation it served answers with
        real state); everyone else publishes a best-effort nudge."""
        state = self._state_facade()
        if state is not None:
            try:
                await state.broadcast.publish(RECONCILE_TOPIC, {"at": time.time()})
            except Exception as exc:  # noqa: BLE001
                self._note_state_failure("broadcast.publish", exc)
        self._nudge.set()
        # The property, not the raw flag: routes.py decides whether to wait for
        # a published snapshot off the same answer, so the two must agree about
        # who applies the change.
        if self.is_leader:
            await self.reconcile()
            await self.publish_status()

    async def await_published(self, tunnel_id: str, accept,
                             timeout: float | None = None) -> dict | None:
        """Poll the published status for up to ``timeout`` seconds, waiting for
        one tunnel's entry to satisfy ``accept``. ``None`` if it never does —
        the caller then answers "accepted, pending" rather than inventing a
        state. Only useful on a non-leader worker; the leader has already
        applied the change inline by the time it calls this."""
        # Read at call time, not as a default argument, so a test (or a future
        # config knob) can shorten it without reloading the module.
        deadline = time.monotonic() + (APPLY_WAIT_S if timeout is None else timeout)
        while True:
            snap = await self.published_status()
            live = (snap or {}).get("tunnels", {}).get(tunnel_id)
            if live is not None and accept(live):
                return live
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(APPLY_POLL_S)

    def _port_conflicts(self, rows: list[dict]) -> dict[str, str]:
        """Loser tunnel id -> the name of the tunnel that owns the port.

        The store rejects a duplicate ``listen_port`` on create/update and
        migration 0001 pins it with a unique index, but neither check ran
        retroactively: rows that predate them can still share a port, and that
        is what started this card (``macbook-vnc`` and ``mac-mini-vnc`` both on
        15900 for six days, the loser reporting a raw OS bind error that named
        nothing). Resolve it here instead, deterministically by id so the same
        tunnel wins on every worker and every boot, and tell the loser WHY.
        """
        by_port: dict[int, list[dict]] = {}
        for row in rows:
            by_port.setdefault(row["listen_port"], []).append(row)
        losers: dict[str, str] = {}
        for candidates in by_port.values():
            if len(candidates) < 2:
                continue
            winner, *rest = sorted(candidates, key=lambda r: r["id"])
            for row in rest:
                losers[row["id"]] = winner["name"]
        return losers

    async def reconcile(self) -> None:
        """Make this worker's listeners match the store. Leader-only — the
        caller decides that; this method does not claim anything."""
        async with self._reconcile_lock:
            self._in_reconcile = True
            try:
                await self._reconcile_locked()
            finally:
                self._in_reconcile = False

    async def _reconcile_locked(self) -> None:
        rows = {row["id"]: row for row in self._store.list()}

        # Deleted while we were holding the port.
        for tunnel_id in list(self._servers):
            if tunnel_id not in rows:
                await self.stop(tunnel_id)
                self._state.pop(tunnel_id, None)
                self._bound.pop(tunnel_id, None)

        # Only rows that would really bind can own a port. A disabled, stopped
        # or transport-less row must never make a working tunnel the loser.
        eligible = [r for r in rows.values()
                    if r["enabled"] and r["ready"] and not r.get("manual_stopped")]
        conflicts = self._port_conflicts(eligible)

        for tunnel_id, row in rows.items():
            if not row["enabled"] or row.get("manual_stopped"):
                if tunnel_id in self._servers:
                    await self.stop(tunnel_id)
                self._bound.pop(tunnel_id, None)
                self.state(tunnel_id).last_error = None
                continue

            conflict = conflicts.get(tunnel_id)
            if conflict:
                if tunnel_id in self._servers:
                    await self.stop(tunnel_id)
                self._bound.pop(tunnel_id, None)
                st = self.state(tunnel_id)
                st.listening = False
                self._set_error(
                    row, st,
                    f"listen_port {row['listen_port']} is also configured on "
                    f"tunnel {conflict!r}, which holds it — give this tunnel a "
                    f"different local port")
                continue

            if (tunnel_id in self._servers
                    and self._bound.get(tunnel_id) == _bind_key(row)):
                continue  # already listening for exactly this config
            await self.start(tunnel_id)

    def _set_error(self, row: dict, st: TunnelRuntimeState, message: str) -> None:
        """Record an error, and log it only when it is news. Reconcile retries
        a failing tunnel every tick; logging each attempt would bury the log in
        the same line every 10 seconds."""
        if st.last_error != message:
            log.warning("tunnel %s: %s", row["name"], message)
        st.last_error = message

    # ── start / stop ────────────────────────────────────────────────────────

    async def start(self, tunnel_id: str) -> dict:
        """(Re)start one tunnel's listener. Idempotent: an already-running
        listener is stopped first, so this doubles as 'apply my edit'.

        A leader-local primitive — it binds a port in THIS process and knows
        nothing about leadership. Callers that are not the reconcile loop
        should go through :meth:`request_reconcile`."""
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
            bridge = None

            def up(n: int) -> None:
                st.bytes_up += n

            def down(n: int) -> None:
                st.bytes_down += n

            try:
                self._store.check_destination_allowed(row, self._allow_lan())
                if row["dest_kind"] == "remote_host":
                    # The address is on the HOST's network, not ours — it dials
                    # for us over the /link tunnel it already holds open.
                    bridge = await remote_dial.open_bridge(
                        row["remote_host_id"], row["dest_host"], row["dest_port"])
                    st.last_error = None
                    await remote_dial.pump(client_r, client_w, bridge, up, down)
                else:
                    dest_r, dest_w = await asyncio.wait_for(
                        asyncio.open_connection(row["dest_host"], row["dest_port"]),
                        timeout=self._connect_timeout(),
                    )
                    st.last_error = None
                    await asyncio.gather(
                        _pipe(client_r, dest_w, up),
                        _pipe(dest_r, client_w, down),
                        return_exceptions=True,
                    )
            except (TunnelError, remote_dial.RemoteDialError,
                    asyncio.TimeoutError, OSError) as e:
                st.last_error = str(e) or type(e).__name__
                log.warning("tunnel %s: %s", row["name"], st.last_error)
                # A connection error is a transition the UI should see without
                # waiting out a whole tick.
                self._publish_soon()
            finally:
                st.active -= 1
                if bridge is not None:
                    try:
                        await bridge.close()
                    except Exception:
                        pass
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
            self._bound.pop(tunnel_id, None)
            # NOT suppressed, on purpose: on the leader, a port already taken
            # is a real failure (a non-worker process holds it) and the only
            # honest thing to report. The per-worker noise this card is about
            # came from the other N-1 workers, which no longer bind at all.
            self._set_error(
                row, st,
                f"bind {row['listen_host']}:{row['listen_port']} failed: {e}")
            await self._publish_after_transition()
            return {"started": False, "reason": st.last_error}

        self._servers[tunnel_id] = server
        self._bound[tunnel_id] = _bind_key(row)
        st.listening = True
        st.last_error = None
        log.info("tunnel %s listening on %s:%s -> %s:%s", row["name"],
                 row["listen_host"], row["listen_port"],
                 row["dest_host"], row["dest_port"])
        await self._publish_after_transition()
        return {"started": True}

    async def stop(self, tunnel_id: str) -> None:
        server = self._servers.pop(tunnel_id, None)
        self._bound.pop(tunnel_id, None)
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
        self.state(tunnel_id).listening = False
        await self._publish_after_transition()

    async def _publish_after_transition(self) -> None:
        """Bind/unbind changed what we report. Suppressed mid-reconcile: that
        pass publishes once at the end instead of once per tunnel."""
        if self._in_reconcile or not self._is_leader:
            return
        await self.publish_status()

    # No start_all(): binding every row unconditionally is precisely what N
    # workers must not do. :meth:`reconcile` is the only bulk path, and it
    # only runs on the lease holder.

    async def stop_all(self) -> None:
        for tunnel_id in list(self._servers):
            await self.stop(tunnel_id)
        self._bound.clear()
