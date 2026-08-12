"""Linked aw-remote-host lookup, for the destination picker.

Talks to aw-backend's ``GET /api/workspaces/{slug}/remote-hosts`` with this
workspace's own ``awlk_`` credential — the same call
``aw-app-remote-host-cli``'s client makes. Credentials come from the
environment, falling back to ``<AW_WORKSPACE_HOME>/.env``, which the
remote-host-cli plugin republishes on every activate.

Failure is never fatal here: this only populates a dropdown, so a missing
credential or an unreachable backend degrades to an empty list plus a reason,
and the UI falls back to a free-text host id.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("aw_apps.tunnel")

DEFAULT_BACKEND_URL = "http://127.0.0.1:9025"
WORKSPACE_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")


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


async def list_linked_hosts() -> dict:
    backend = (_env("AW_BACKEND_URL") or DEFAULT_BACKEND_URL).rstrip("/")
    workspace = _env("AW_WORKSPACE")
    token = _env("AW_WORKSPACE_HOST_TOKEN")
    if not (workspace and token):
        return {"hosts": [], "error": "no workspace host credential published"}
    url = f"{backend}/api/workspaces/{workspace}/remote-hosts"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            resp.raise_for_status()
            return {"hosts": resp.json().get("hosts", []), "error": None}
    except Exception as e:  # noqa: BLE001 — a dropdown is not worth a 500
        log.warning("tunnel: remote-host lookup failed: %s", e)
        return {"hosts": [], "error": str(e)}
