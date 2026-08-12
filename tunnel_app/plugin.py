"""Entrypoint referenced by aw-app.json's runtime.entrypoint
("tunnel_app.plugin:TunnelAppPlugin").

Listeners are started on activate and torn down through ``ctx.on_deactivate``.
That teardown is not optional bookkeeping: an app update is unload+load, so a
listener left running would still hold the port when the new instance tries to
bind it, and every tunnel would come back "address in use" after the first
update.

No ``service:manage``: the tunnels are asyncio servers inside this process, not
subprocesses, so the runtime's service supervisor has nothing to supervise.
``net:outbound`` is the capability that matters — every tunnel dials out.
"""
from __future__ import annotations

import logging

from . import routes as routes_mod
from .forwarder import TunnelManager
from .store import TunnelStore

log = logging.getLogger("aw_apps.tunnel")


class TunnelAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx
        self.store = TunnelStore(ctx)
        self.manager = TunnelManager(ctx, self.store)
        ctx.routes.register(routes_mod.build_routes(ctx, self.store, self.manager))
        ctx.on_deactivate(self.manager.stop_all)
        await self.manager.start_all()
        log.info("aw-app-tunnel activated (%d tunnel(s))", len(self.store.list()))

    async def on_config_saved(self, ctx) -> None:
        # allow_lan_destinations / connect_timeout_s are read per-connection off
        # ctx.config, so a save needs no restart — but the manager holds the old
        # ctx, so re-point it.
        self.ctx = ctx
        self.manager._ctx = ctx
        log.info("aw-app-tunnel config saved")

    async def deactivate(self) -> None:
        log.info("aw-app-tunnel deactivated")
