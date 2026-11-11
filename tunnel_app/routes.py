"""tunnel_app's FastAPI sub-app, mounted at ``/api/apps/tunnel``.

    GET    /tunnels              list + live counters (listening, conns, bytes)
    POST   /tunnels              create (the leader starts it if enabled + ready)
    PUT    /tunnels/{id}         update (the leader restarts the listener)
    DELETE /tunnels/{id}         delete (the leader stops the listener)
    POST   /tunnels/{id}/start   (re)start — also the "apply" button
    POST   /tunnels/{id}/stop    stop without disabling (durable, see store)
    GET    /remote-hosts         linked hosts, for the destination picker
    GET    /panel/tunnels        the settings page (see tunnels_ui.py)

**Every route here can land on any worker, and only one worker binds.** So no
route touches a listener directly. Reads merge the store's rows (shared) with
the leader's published snapshot (``ctx.state.kv``); writes change the store and
ask the leader to reconcile. A worker that is neither the leader nor able to
read a snapshot reports ``status: "unknown"`` — never its own empty local
state dressed up as fact, which is what made a perfectly healthy tunnel show a
permanent bind error in the Settings UI (Kanban
``4155bf3b-9510-81b9-b158-e7d877923019``).
"""
from __future__ import annotations

import logging
import time

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .forwarder import TunnelManager, unknown_state
from .remote_hosts import list_linked_hosts
from .store import LISTEN_HOSTS, TunnelError, TunnelNotFound, TunnelStore
from .tunnels_ui import TUNNELS_UI_HTML

log = logging.getLogger("aw_apps.tunnel")

#: Where the live half of a merged row came from. The UI needs this to tell
#: "not listening" (a fact) from "nobody has told me yet" (not a fact).
SOURCE_LEADER = "leader"
SOURCE_LOCAL = "local"
SOURCE_UNKNOWN = "unknown"


def _status_label(row: dict, live: dict, source: str) -> str:
    if source == SOURCE_UNKNOWN:
        return "unknown"
    if live.get("listening"):
        return "listening"
    if not row["enabled"]:
        return "disabled"
    if row.get("manual_stopped"):
        return "stopped"
    if row.get("not_ready_reason"):
        return "not_ready"
    if live.get("last_error"):
        return "error"
    return "stopped"


def _landed(row: dict):
    """What "my change took effect" looks like in the leader's snapshot, given
    the row as it now stands. A row that cannot listen (disabled, manually
    stopped, no transport) has landed once it is NOT listening; anything else
    has landed once it is listening or has a reason why it isn't."""
    if not row["enabled"] or row.get("manual_stopped") or not row["ready"]:
        return lambda live: not live.get("listening")
    return lambda live: bool(live.get("listening") or live.get("last_error"))


def _merge(row: dict, snapshot: dict | None, manager: TunnelManager) -> dict:
    """One tunnel as this worker can honestly describe it."""
    published = (snapshot or {}).get("tunnels", {})
    live = published.get(row["id"])
    if live is not None:
        source = SOURCE_LEADER
    elif manager.is_leader:
        # We ARE the worker holding the listeners, so our own state is the
        # truth — this is the only case where local state may be served.
        live = manager.state(row["id"]).as_dict()
        source = SOURCE_LOCAL
    else:
        live = unknown_state()
        source = SOURCE_UNKNOWN
    at = (snapshot or {}).get("at")
    return {
        **row,
        **live,
        "status": _status_label(row, live, source),
        "status_source": source,
        "status_age_s": (round(max(0.0, time.time() - at), 1)
                         if source == SOURCE_LEADER and isinstance(at, (int, float))
                         else None),
    }


def build_routes(ctx, store: TunnelStore, manager: TunnelManager) -> FastAPI:
    app = FastAPI(title="tunnel")

    async def _view(tunnel_id: str) -> dict:
        snapshot = await manager.published_status()
        return _merge(store.get(tunnel_id), snapshot, manager)

    async def _apply(tunnel_id: str) -> dict:
        """Push the store change to whoever owns the listeners, then answer
        with real state if it arrives quickly."""
        await manager.request_reconcile()
        if manager.is_leader:
            # We applied it inline, so our own state already IS the answer.
            return await _view(tunnel_id)
        live = await manager.await_published(tunnel_id, _landed(store.get(tunnel_id)))
        view = await _view(tunnel_id)
        if live is None:
            # Saved and queued, but we cannot yet see it applied. Say exactly
            # that instead of guessing either way.
            view["accepted"] = True
            view["pending"] = True
        return view

    @app.get("/tunnels")
    async def list_tunnels() -> dict:
        snapshot = await manager.published_status()
        return {
            "tunnels": [_merge(row, snapshot, manager) for row in store.list()],
            "listen_hosts": LISTEN_HOSTS,
        }

    @app.post("/tunnels")
    async def create_tunnel(body: dict = Body(...)) -> dict:
        try:
            row = store.create(body)
        except TunnelError as e:
            raise HTTPException(400, str(e))
        return await _apply(row["id"])

    @app.put("/tunnels/{tunnel_id}")
    async def update_tunnel(tunnel_id: str, body: dict = Body(...)) -> dict:
        try:
            store.update(tunnel_id, body)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        except TunnelError as e:
            raise HTTPException(400, str(e))
        # An edit that moves the port or destination must not leave the old
        # listener up. The leader compares each listener against the fields it
        # was bound for, so reconciling is the apply path.
        return await _apply(tunnel_id)

    @app.delete("/tunnels/{tunnel_id}")
    async def delete_tunnel(tunnel_id: str) -> dict:
        try:
            store.delete(tunnel_id)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        # The row is gone; reconcile is what closes the socket, wherever it is.
        await manager.request_reconcile()
        return {"ok": True}

    @app.post("/tunnels/{tunnel_id}/start")
    async def start_tunnel(tunnel_id: str) -> dict:
        try:
            store.set_manual_stopped(tunnel_id, False)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        view = await _apply(tunnel_id)
        if view["status"] == "listening":
            return {"started": True, **view}
        if view.get("pending"):
            return {"started": None, "accepted": True, "pending": True,
                    "reason": "applying on the worker that owns the listeners",
                    **view}
        return {"started": False,
                "reason": (view.get("last_error") or view.get("not_ready_reason")
                           or view["status"]),
                **view}

    @app.post("/tunnels/{tunnel_id}/stop")
    async def stop_tunnel(tunnel_id: str) -> dict:
        try:
            store.set_manual_stopped(tunnel_id, True)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        view = await _apply(tunnel_id)
        return {"stopped": not view.get("listening", False), **view}

    @app.get("/remote-hosts")
    async def remote_hosts() -> dict:
        """Linked aw-remote-host machines, so the destination picker can offer
        real ids instead of asking the user to paste a uuid. Returns an empty
        list (never a 500) when the credential isn't published — the picker
        just falls back to a free-text field."""
        return await list_linked_hosts()

    # NOT under /ui/ — core owns GET /api/apps/{slug}/ui/{path:path} for
    # component-mode ESM bundles and shadows anything an app mounts there.
    @app.get("/panel/tunnels", response_class=HTMLResponse)
    async def tunnels_ui() -> HTMLResponse:
        return HTMLResponse(TUNNELS_UI_HTML)

    return app
