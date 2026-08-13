"""Dial a TCP address FROM a linked aw-remote-host, over its /link tunnel.

A ``custom`` tunnel opens the socket itself. A ``remote_host`` tunnel cannot:
the address is on the host's own network (its loopback, its LAN), which this
workspace has no route to. The host, though, already holds an outbound
WebSocket to aw-backend — so it dials on our behalf and relays the bytes.

The chain, once, so the failure modes are legible:

    tunnel listener  ->  this module  ->  wss aw-backend .../tcp
                     ->  /link (tcp_open/tcp_data/tcp_close)
                     ->  aw-remote-host  ->  net.Dial(host:port)

Everything here speaks raw bytes. The base64 lives on the /link hop only,
because that hop is a JSON protocol; the consumer's socket never sees it.

Auth is this workspace's own ``awlk_`` host credential — the same one the
remote-host exec calls use, read from the environment or
``<AW_WORKSPACE_HOME>/.env``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import quote

import websockets

log = logging.getLogger("aw_apps.tunnel")

WORKSPACE_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
DEFAULT_BACKEND_URL = "http://127.0.0.1:9025"


class RemoteDialError(RuntimeError):
    """Could not establish the relayed connection — reported to the client as
    a closed socket, never as a hang."""


def _env(name: str) -> str:
    """os.environ first, then <AW_WORKSPACE_HOME>/.env — an app process and a
    cross-container caller see different environments but the same file."""
    value = os.environ.get(name)
    if value:
        return value
    env_file = Path(os.environ.get(
        "AW_WORKSPACE_ENV_FILE", f"{WORKSPACE_DIR}/.aw-workspace/.env"))
    if not env_file.is_file():
        return ""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == name:
            return val.strip().strip('"').strip("'")
    return ""


def bridge_url(remote_host_id: str, host: str, port: int) -> str:
    backend = (_env("AW_BACKEND_URL") or DEFAULT_BACKEND_URL).rstrip("/")
    workspace = _env("AW_WORKSPACE")
    if not workspace:
        raise RemoteDialError("AW_WORKSPACE is not published in this workspace")
    ws_base = backend.replace("https://", "wss://").replace("http://", "ws://")
    return (f"{ws_base}/api/workspaces/{quote(workspace)}"
            f"/remote-hosts/{quote(remote_host_id)}/tcp"
            f"?host={quote(host)}&port={port}")


async def open_bridge(remote_host_id: str, host: str, port: int):
    """Open the relay WebSocket. Raises RemoteDialError if it cannot."""
    token = _env("AW_WORKSPACE_HOST_TOKEN")
    if not token:
        raise RemoteDialError(
            "AW_WORKSPACE_HOST_TOKEN is not published — a remote-host tunnel "
            "needs the credential the /link handshake minted for this workspace")
    url = bridge_url(remote_host_id, host, port)
    try:
        return await websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {token}"},
            max_size=None, open_timeout=15,
        )
    except Exception as e:  # noqa: BLE001 — every failure is "cannot dial"
        raise RemoteDialError(f"relay connect failed: {e}") from e


async def pump(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter,
               bridge, on_bytes_up, on_bytes_down) -> None:
    """Pipe the local client and the relay together until either end goes.

    Byte counters are handed in so the tunnel's live stats read the same
    whether the destination was dialled locally or through a host.
    """

    async def up() -> None:
        while True:
            chunk = await client_reader.read(65536)
            if not chunk:
                break
            on_bytes_up(len(chunk))
            await bridge.send(chunk)

    async def down() -> None:
        async for message in bridge:
            if isinstance(message, str):
                # The relay only ever sends binary; a text frame means the
                # far side is speaking a different protocol than we think.
                log.warning("tunnel: unexpected text frame on the relay")
                continue
            on_bytes_down(len(message))
            client_writer.write(message)
            await client_writer.drain()

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
    finally:
        for task in tasks:
            task.cancel()
