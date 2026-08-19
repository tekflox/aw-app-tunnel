---
repo: architecture
path: docs/architecture/aw-app-tunnel.md
source: generated
edited: false
checksum: sha256:8ab7ec46af7f4e2640931070c15ddc9e6aaaddcb2a063b1d8fad9a990c4478c6
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
### Porta de escuta e nome são únicos, e editar um túnel não colide consigo mesmo
- Given os túneis cadastrados, cada um querendo bind numa porta local
- When a unicidade é verificada na criação e na edição (repos/aw-app-tunnel/tunnel_app/store.py, via repos/aw-app-tunnel/tests/test_tunnel.py::test_duplicate_port_and_name_rejected:104 e test_editing_a_tunnel_can_keep_its_own_port:115)
- Then porta repetida é recusada, nome repetido é recusado sem diferenciar maiúscula de minúscula ("a" e "A" colidem), e a checagem exclui a própria linha em edição — sem essa exclusão renomear um túnel falharia contra ele mesmo, que é o bug que só aparece depois, quando alguém tenta a primeira edição. Duas linhas na mesma porta produziriam um bind que funciona para uma e falha para a outra, dependendo de quem subiu primeiro
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-tunnel/tests/test_tunnel.py` (passing)

### Túnel para remote_host exige o id do host, e destino inválido é recusado na entrada
- Given dois tipos de destino possíveis (host customizado e remote_host), sendo que o segundo não significa nada sem saber qual host
- When a validação de entrada roda (repos/aw-app-tunnel/tunnel_app/store.py, via test_remote_host_tunnel_requires_a_host_id:98 e test_bad_input_rejected:133)
- Then dest_kind=remote_host sem remote_host_id levanta TunnelError, e também são recusados nome vazio, porta 0, porta acima de 65535, host de destino vazio, dest_kind desconhecido e um listen_host que não seja local — recusar na entrada é o que evita uma linha salva que nunca poderá escutar, e o listen_host restrito é o que impede o túnel de ser aberto para fora da máquina sem ninguém ter pedido isso
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-tunnel/tests/test_tunnel.py` (passing)

### A ponte aponta para o relay do backend, com host e porta como parâmetros
- Given o destino vive atrás de um remote host, alcançável só pelo relay TCP do backend
- When a URL do WebSocket de ponte é montada (repos/aw-app-tunnel/tunnel_app/remote_dial.py::bridge_url, via repos/aw-app-tunnel/tests/test_tunnel.py::test_bridge_url_targets_the_backend_relay:223)
- Then a URL é wss://&lt;backend&gt;/api/workspaces/&lt;ws&gt;/remote-hosts/&lt;id&gt;/tcp com host e port em query string, derivada de AW_BACKEND_URL e AW_WORKSPACE — o esquema vira wss e não ws, porque o tráfego sai da máquina. Este é o ponto em que o app deixa de ser local: tudo que passa por aqui atravessa o backend, e errar a montagem produz um túnel que abre e nunca entrega byte nenhum
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-tunnel/tests/test_tunnel.py` (passing)

### Sem token de host a ponte falha dizendo qual variável falta
- Given a ponte precisa de AW_WORKSPACE_HOST_TOKEN para se autenticar no relay, e a variável pode simplesmente não estar no ambiente
- When a abertura da ponte é tentada sem ela (repos/aw-app-tunnel/tunnel_app/remote_dial.py::open_bridge, exercitado em repos/aw-app-tunnel/tests/test_tunnel.py:215-221)
- Then sobe RemoteDialError nomeando AW_WORKSPACE_HOST_TOKEN no texto, em vez de tentar conectar e falhar com um erro de WebSocket genérico — a mensagem carregar o nome exato da variável é a diferença entre um diagnóstico de um minuto e uma caçada, e num app de rede quase toda falha se parece com problema de conectividade até que algo diga o contrário
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-tunnel/tests/test_tunnel.py` (passing)
