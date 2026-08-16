---
repo: architecture
path: docs/architecture/aw-app-tunnel.md
source: generated
edited: false
checksum: sha256:0a5ef7d7bc560c77295cbe0d7f7dc5c2a6610079d4220bb7f1d4b464af10df43
---
# Tunnel

- **repo**: aw-app-tunnel
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Named local TCP endpoints that forward to a destination, so consumers point at 127.0.0.1:<port> and never learn what is on the other side. Two destination kinds: a custom IP:port (a plain TCP proxy, working today) and a linked aw-remote-host (dialled BY that machine over the /link WebSocket it already holds open, so the workspace reaches its loopback and LAN with no inbound port and no VPN). Each tunnel chooses its own bind address, which is what decides who may use it.

## Connections
- `db` → **postgres** — app-owned tables in the workspace schema
- `http` → **aw-workspace** — routes mounted at /api/apps/tunnel

## MCP tools
_none exposed_

## Requirements
_none documented_
