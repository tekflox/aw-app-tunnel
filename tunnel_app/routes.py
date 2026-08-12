"""tunnel_app's FastAPI sub-app, mounted at ``/api/apps/tunnel``.

    GET    /tunnels              list + live counters (listening, conns, bytes)
    POST   /tunnels              create (starts it if enabled + ready)
    PUT    /tunnels/{id}         update (restarts the listener)
    DELETE /tunnels/{id}         delete (stops the listener first)
    POST   /tunnels/{id}/start   (re)start — also the "apply" button
    POST   /tunnels/{id}/stop    stop without disabling
    GET    /remote-hosts         linked hosts, for the destination picker
    GET    /panel/tunnels        the settings page (see tunnels_ui.py)
"""
from __future__ import annotations

import logging

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .forwarder import TunnelManager
from .remote_hosts import list_linked_hosts
from .store import LISTEN_HOSTS, TunnelError, TunnelNotFound, TunnelStore
from .tunnels_ui import TUNNELS_UI_HTML

log = logging.getLogger("aw_apps.tunnel")


def build_routes(ctx, store: TunnelStore, manager: TunnelManager) -> FastAPI:
    app = FastAPI(title="tunnel")

    @app.get("/tunnels")
    async def list_tunnels() -> dict:
        return {"tunnels": manager.status(), "listen_hosts": LISTEN_HOSTS}

    @app.post("/tunnels")
    async def create_tunnel(body: dict = Body(...)) -> dict:
        try:
            row = store.create(body)
        except TunnelError as e:
            raise HTTPException(400, str(e))
        await manager.start(row["id"])
        return {**row, **manager.state(row["id"]).as_dict()}

    @app.put("/tunnels/{tunnel_id}")
    async def update_tunnel(tunnel_id: str, body: dict = Body(...)) -> dict:
        try:
            row = store.update(tunnel_id, body)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        except TunnelError as e:
            raise HTTPException(400, str(e))
        # An edit that moves the port or destination must not leave the old
        # listener up — start() stops first, so this is the apply path too.
        await manager.start(tunnel_id)
        return {**row, **manager.state(tunnel_id).as_dict()}

    @app.delete("/tunnels/{tunnel_id}")
    async def delete_tunnel(tunnel_id: str) -> dict:
        try:
            await manager.stop(tunnel_id)
            store.delete(tunnel_id)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        return {"ok": True}

    @app.post("/tunnels/{tunnel_id}/start")
    async def start_tunnel(tunnel_id: str) -> dict:
        try:
            store.get(tunnel_id)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        return await manager.start(tunnel_id)

    @app.post("/tunnels/{tunnel_id}/stop")
    async def stop_tunnel(tunnel_id: str) -> dict:
        try:
            store.get(tunnel_id)
        except TunnelNotFound:
            raise HTTPException(404, "Tunnel not found")
        await manager.stop(tunnel_id)
        return {"stopped": True}

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
