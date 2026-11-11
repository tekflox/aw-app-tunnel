"""The multi-worker half: exactly one worker binds, everyone reports the same
truth, and nothing is invented when the truth is unavailable.

Kanban ``4155bf3b-9510-81b9-b158-e7d877923019``. The bug was not a leaked
socket — it was N workers each running their own ``TunnelManager``, every one
of them calling ``start_all()``, N-1 losing each bind and then reporting that
loss as the tunnel's state to whoever's HTTP request happened to land there.
A fully working tunnel showed a permanent "address already in use".

"Two workers" here is two ``TunnelManager`` objects over the SAME store, each
with its own ``ctx`` and its own ``ctx.state`` facade instance, sharing kv and
the broadcast bus and contending for a REAL ``flock`` (see
``tests/fake_state.py`` for why that genuinely behaves like two processes).
Nothing is mocked at the socket layer: when a test says the leader bound the
port, something really is listening on it.
"""
from __future__ import annotations

import asyncio
import socket
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ctx import FakeCtx  # noqa: E402
from fake_state import FakeBroadcastBus, FakeState  # noqa: E402
from tunnel_app import forwarder  # noqa: E402
from tunnel_app.forwarder import LEASE_NAME, TunnelManager  # noqa: E402
from tunnel_app.routes import build_routes  # noqa: E402
from tunnel_app.store import TunnelStore  # noqa: E402


@dataclass
class Worker:
    ctx: FakeCtx
    store: TunnelStore
    manager: TunnelManager

    def client(self) -> TestClient:
        return TestClient(build_routes(self.ctx, self.store, self.manager))


class Cluster:
    """Workers of one notional core process group: one store, one kv, one bus,
    one lock dir."""

    def __init__(self, lock_dir: str) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock_dir = lock_dir
        self.kv: dict = {}
        self.bus = FakeBroadcastBus()
        self.workers: list[Worker] = []

    def worker(self) -> Worker:
        ctx = FakeCtx(self._conn,
                      state=FakeState(self._lock_dir, self.kv, self.bus))
        store = TunnelStore(ctx)
        worker = Worker(ctx, store, TunnelManager(ctx, store))
        self.workers.append(worker)
        return worker

    async def aclose(self) -> None:
        for worker in self.workers:
            await worker.manager.stop_all()
            worker.ctx.state.lease.release_all()


@pytest.fixture()
async def cluster(tmp_path):
    c = Cluster(str(tmp_path / "locks"))
    try:
        yield c
    finally:
        await c.aclose()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _is_listening(port: int) -> bool:
    """Ask the OS, not our bookkeeping: whether the port is bindable is the
    only claim about a listener that cannot be faked by the code under test."""
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def _tunnel(worker: Worker, **over) -> dict:
    body = {"name": over.pop("name", "t"),
            "listen_port": over.pop("listen_port", _free_port()),
            "dest_host": "127.0.0.1",
            "dest_port": over.pop("dest_port", _free_port())}
    body.update(over)
    return worker.store.create(body)


# ── leadership ──────────────────────────────────────────────────────────────

async def test_only_the_lease_holder_binds(cluster):
    leader, standby = cluster.worker(), cluster.worker()
    row = _tunnel(leader, name="vnc")

    assert await leader.manager.tick_once() is True
    assert await standby.manager.tick_once() is False

    assert leader.manager.state(row["id"]).listening is True
    assert _is_listening(row["listen_port"])
    # The whole point: the standby holds nothing and therefore cannot have a
    # bind error of its own to report.
    assert standby.manager._servers == {}
    assert standby.manager.is_leader is False
    assert standby.manager.state(row["id"]).last_error is None


async def test_the_standby_never_binds_even_after_many_ticks(cluster):
    """The old failure was reproducible from cold boot on every deploy — one
    tick proving it is not enough."""
    leader, standby = cluster.worker(), cluster.worker()
    row = _tunnel(leader)
    for _ in range(3):
        await leader.manager.tick_once()
        assert await standby.manager.tick_once() is False
    assert standby.manager._servers == {}
    assert leader.manager.state(row["id"]).last_error is None


async def test_the_leader_applies_an_edit_and_frees_the_old_port(cluster):
    """A PUT used to restart the listener only on the worker that served the
    request, leaving a sibling bound to the OLD port and destination
    indefinitely. The leader's reconcile is what makes an edit real."""
    leader, standby = cluster.worker(), cluster.worker()
    row = _tunnel(leader, listen_port=_free_port())
    old_port = row["listen_port"]
    await leader.manager.tick_once()
    await standby.manager.tick_once()
    assert _is_listening(old_port)

    new_port = _free_port()
    standby.store.update(row["id"], {"listen_port": new_port})
    await standby.manager.request_reconcile()   # standby owns nothing: a nudge
    assert _is_listening(old_port)              # ...which cannot bind for it

    await leader.manager.tick_once()
    assert _is_listening(new_port)
    assert not _is_listening(old_port)
    assert leader.manager.state(row["id"]).listening is True


async def test_an_unchanged_tunnel_is_not_rebound_every_tick(cluster):
    """Reconcile compares each listener against the fields it was bound FOR.
    Without that, every tick would tear down and rebind every listener, which
    drops live connections ten seconds at a time."""
    leader = cluster.worker()
    row = _tunnel(leader)
    await leader.manager.tick_once()
    server = leader.manager._servers[row["id"]]
    await leader.manager.tick_once()
    assert leader.manager._servers[row["id"]] is server


async def test_a_standby_takes_over_when_the_leader_lets_go(cluster):
    """Leader death drops the flock in the kernel and frees its sockets; a
    standby claims on its next tick. Bounded by one tick, not by a human."""
    leader, standby = cluster.worker(), cluster.worker()
    row = _tunnel(leader)
    await leader.manager.tick_once()
    assert await standby.manager.tick_once() is False

    # What the kernel does when the leader process dies.
    await leader.manager.stop_all()
    leader.ctx.state.lease.release(LEASE_NAME)

    assert await standby.manager.tick_once() is True
    assert standby.manager.state(row["id"]).listening is True
    assert _is_listening(row["listen_port"])


# ── the loop and the plugin wiring ──────────────────────────────────────────

async def test_the_tick_loop_binds_and_a_nudge_wakes_it_early(cluster):
    """``run_forever`` is what actually runs in production, and the broadcast
    nudge is what makes a mutation feel instant instead of tick-granular. The
    tick here is 30s, so anything that lands quickly landed because of the
    nudge — and the test still proves the nudge is only an optimisation,
    because the loop would have got there on its own."""
    leader, other = cluster.worker(), cluster.worker()
    first = _tunnel(leader)
    task = asyncio.create_task(leader.manager.run_forever(tick_s=30))
    try:
        for _ in range(100):
            if leader.manager.state(first["id"]).listening:
                break
            await asyncio.sleep(0.01)
        assert _is_listening(first["listen_port"])

        second = _tunnel(other, name="second")
        await other.manager.request_reconcile()   # a nudge over the bus
        for _ in range(100):
            if leader.manager.state(second["id"]).listening:
                break
            await asyncio.sleep(0.01)
        assert _is_listening(second["listen_port"])
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_activate_starts_the_tick_and_shutdown_gives_everything_back(
        cluster, tmp_path):
    """Activate must NOT bind directly (that is the N-workers-race this card
    is about), and deactivate must return both the ports and the lease — an
    app update is unload+load, so anything kept would make the next instance
    fail to bind or leave every sibling standing by forever."""
    from tunnel_app.plugin import TunnelAppPlugin

    worker = cluster.worker()
    ctx = worker.ctx
    ctx.routes = type("R", (), {"register": lambda self, app: None})()
    deactivate_hooks: list = []
    ctx.on_deactivate = deactivate_hooks.append

    plugin = TunnelAppPlugin()
    await plugin.activate(ctx)
    try:
        row = _tunnel(worker, name="via-plugin")
        assert plugin._tick is not None and not plugin._tick.done()
        for _ in range(200):
            if plugin.manager.state(row["id"]).listening:
                break
            await asyncio.sleep(0.01)
        assert _is_listening(row["listen_port"])
    finally:
        for hook in deactivate_hooks:
            await hook()

    assert plugin._tick is None
    assert plugin.manager._servers == {}
    assert not _is_listening(row["listen_port"])
    # The lease is back, so a fresh instance can lead immediately.
    assert cluster.worker().manager._claim_leadership() is True
    assert ctx.state.broadcast.closed is True


# ── status reporting: the symptom on the card ───────────────────────────────

async def test_a_non_leader_reports_the_leaders_status(cluster):
    leader, standby = cluster.worker(), cluster.worker()
    row = _tunnel(leader, name="mac-mini-vnc")
    await leader.manager.tick_once()        # binds + publishes
    await standby.manager.tick_once()

    payload = standby.client().get("/tunnels").json()["tunnels"][0]
    assert payload["id"] == row["id"]
    assert payload["listening"] is True
    assert payload["last_error"] is None
    assert payload["status"] == "listening"
    assert payload["status_source"] == "leader"


async def test_a_non_leader_with_no_snapshot_says_unknown_not_error(cluster):
    """THE regression test for this card. A worker that owns no listener and
    has no snapshot to read must report "unknown" — never its own empty local
    state, and never a bind error it produced by trying to bind a port a
    sibling already holds."""
    standby = cluster.worker()
    row = _tunnel(standby)
    await standby.manager.tick_once()        # it is the only worker: it leads
    # Now it is not: another worker takes the lease, and the snapshot lapses.
    other = cluster.worker()
    await standby.manager.stop_all()
    standby.ctx.state.lease.release(LEASE_NAME)
    assert other.manager._claim_leadership() is True
    await standby.manager.tick_once()
    cluster.kv.clear()
    # Exactly the stale local fiction the UI used to show as fact.
    standby.manager.state(row["id"]).last_error = (
        "bind 127.0.0.1:15900 failed: [Errno 98] address already in use")

    payload = standby.client().get("/tunnels").json()["tunnels"][0]
    assert payload["status"] == "unknown"
    assert payload["status_source"] == "unknown"
    assert payload["last_error"] is None
    assert payload["listening"] is False


async def test_the_leader_serves_its_own_state_when_the_snapshot_is_gone(cluster):
    """The one worker allowed to answer from local state is the one actually
    holding the listeners — Redis being down must not blind it to itself."""
    leader = cluster.worker()
    _tunnel(leader)
    await leader.manager.tick_once()
    cluster.kv.clear()

    payload = leader.client().get("/tunnels").json()["tunnels"][0]
    assert payload["status"] == "listening"
    assert payload["status_source"] == "local"


# ── manual stop ─────────────────────────────────────────────────────────────

async def test_a_manual_stop_survives_reconcile_and_a_restart(cluster):
    leader = cluster.worker()
    row = _tunnel(leader)
    await leader.manager.tick_once()
    assert _is_listening(row["listen_port"])

    leader.store.set_manual_stopped(row["id"], True)
    await leader.manager.tick_once()
    assert not _is_listening(row["listen_port"])
    assert leader.manager.state(row["id"]).last_error is None

    # Further ticks must not resurrect it...
    await leader.manager.tick_once()
    assert not _is_listening(row["listen_port"])

    # ...and neither must a fresh process (the deliberate semantic change:
    # a manual stop is durable now, where the old in-memory one was not).
    await leader.manager.stop_all()
    leader.ctx.state.lease.release(LEASE_NAME)
    fresh = cluster.worker()
    assert await fresh.manager.tick_once() is True
    assert not _is_listening(row["listen_port"])

    # Start clears it, on the normal route.
    fresh.store.set_manual_stopped(row["id"], False)
    await fresh.manager.tick_once()
    assert _is_listening(row["listen_port"])


async def test_stop_on_a_non_leader_still_stops_the_tunnel(cluster, monkeypatch):
    """The correctness hole the lighter "fix the status only" option left open:
    POST /stop was a no-op on the N-1 workers that held no listener."""
    monkeypatch.setattr(forwarder, "APPLY_WAIT_S", 0.3)
    leader, standby = cluster.worker(), cluster.worker()
    row = _tunnel(leader)
    await leader.manager.tick_once()
    await standby.manager.tick_once()

    res = standby.client().post(f"/tunnels/{row['id']}/stop").json()
    assert res.get("pending") is True          # honest: queued, not yet applied
    assert standby.store.get(row["id"])["manual_stopped"] is True
    assert _is_listening(row["listen_port"])   # the leader has not ticked yet

    await leader.manager.tick_once()
    assert not _is_listening(row["listen_port"])


# ── errors that must NOT be suppressed ──────────────────────────────────────

async def test_a_real_bind_conflict_on_the_leader_still_surfaces(cluster):
    """A port held by something that is not a sibling worker is a real
    failure. Suppressing it to quieten this card's noise would hide the only
    case where the error was ever true."""
    leader = cluster.worker()
    port = _free_port()
    squatter = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    try:
        row = _tunnel(leader, listen_port=port)
        assert await leader.manager.tick_once() is True
        st = leader.manager.state(row["id"])
        assert st.listening is False
        assert "bind" in (st.last_error or "")
        assert "address already in use" in st.last_error.lower()
        # And it is published, not kept private to this worker.
        snap = await leader.manager.published_status()
        assert "bind" in snap["tunnels"][row["id"]]["last_error"]
        assert leader.client().get("/tunnels").json()["tunnels"][0]["status"] == "error"
    finally:
        squatter.close()
        await squatter.wait_closed()


async def test_two_enabled_rows_on_one_port_get_an_honest_error(cluster):
    """The rows that started this card: ``macbook-vnc`` and ``mac-mini-vnc``
    both on 15900, predating both the store's duplicate-port check and the
    unique index, so neither ever caught them. The loser used to report a raw
    OS bind error naming nothing. Insert them the only way they can exist —
    straight into the table, around the validation."""
    leader = cluster.worker()
    port = _free_port()
    for tunnel_id, name in (("a-winner", "macbook-vnc"), ("b-loser", "mac-mini-vnc")):
        leader.ctx.db.execute(
            leader.store._table,
            "INSERT INTO {table} (id, name, enabled, listen_host, listen_port, "
            " dest_kind, dest_host, dest_port, remote_host_id) "
            "VALUES (:id, :name, 1, '127.0.0.1', :port, 'custom', "
            "        '127.0.0.1', 5900, '')",
            {"id": tunnel_id, "name": name, "port": port},
        )

    await leader.manager.tick_once()

    assert leader.manager.state("a-winner").listening is True
    loser = leader.manager.state("b-loser")
    assert loser.listening is False
    assert "macbook-vnc" in (loser.last_error or "")
    assert "address already in use" not in (loser.last_error or "").lower()
