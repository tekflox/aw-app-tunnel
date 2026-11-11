"""Entrypoint referenced by aw-app.json's runtime.entrypoint
("tunnel_app.plugin:TunnelAppPlugin").

Activate starts ONE task — the forwarder's leadership + reconcile tick — and
nothing else. It deliberately does not bind anything itself: core loads a
Tier-1 app into every uvicorn worker, so an unconditional ``start_all()`` here
is N workers racing for each port, N-1 of them recording a bind error nobody
asked for. See ``forwarder.py``'s module docstring.

Teardown is not optional bookkeeping: an app update is unload+load, so a
listener left running would still hold the port when the new instance tries to
bind it, and every tunnel would come back "address in use" after the first
update. The lease has to go back for the same reason — a held flock fd
surviving into a process whose app is no longer active would keep every other
worker standing by forever.

``state:own`` is what buys the lease/kv/broadcast this coordination needs. No
``service:manage``: the tunnels are asyncio servers inside this process, not
subprocesses, so the runtime's service supervisor has nothing to supervise.
``net:outbound`` is the capability that matters for the data path — every
tunnel dials out.
"""
from __future__ import annotations

import asyncio
import logging

from . import routes as routes_mod
from .forwarder import TunnelManager
from .store import TunnelStore

log = logging.getLogger("aw_apps.tunnel")


class TunnelAppPlugin:
    def __init__(self) -> None:
        self._tick: asyncio.Task | None = None

    async def activate(self, ctx) -> None:
        self.ctx = ctx
        self.store = TunnelStore(ctx)
        self.manager = TunnelManager(ctx, self.store)
        ctx.routes.register(routes_mod.build_routes(ctx, self.store, self.manager))
        ctx.on_deactivate(self._shutdown)
        self._tick = asyncio.create_task(
            self.manager.run_forever(), name="tunnel-forwarder-tick")
        log.info("aw-app-tunnel activated (%d tunnel(s), forwarder tick started)",
                 len(self.store.list()))

    async def on_config_saved(self, ctx) -> None:
        # allow_lan_destinations / connect_timeout_s are read per-connection off
        # ctx.config, so a save needs no restart — but the manager holds the old
        # ctx, so re-point it. The tick reads ctx through the manager on every
        # pass, so it picks this up without being restarted.
        self.ctx = ctx
        self.manager._ctx = ctx
        log.info("aw-app-tunnel config saved")

    async def _shutdown(self) -> None:
        """Order matters: stop reconciling before unbinding, or the tick we are
        tearing down re-binds what we just closed."""
        if self._tick is not None:
            self._tick.cancel()
            try:
                await self._tick
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — shutdown path
                log.debug("aw-app-tunnel: forwarder tick raised on cancel",
                          exc_info=True)
            self._tick = None
        await self.manager.stop_all()
        state = self.manager._state_facade()
        if state is not None:
            try:
                state.lease.release_all()
            except Exception:  # noqa: BLE001
                log.debug("aw-app-tunnel: lease release failed", exc_info=True)
            try:
                await state.broadcast.aclose()
            except Exception:  # noqa: BLE001
                log.debug("aw-app-tunnel: broadcast teardown failed", exc_info=True)

    async def deactivate(self) -> None:
        log.info("aw-app-tunnel deactivated")
