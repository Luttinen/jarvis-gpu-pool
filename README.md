# jarvis-gpu-pool

Use the gaming PCs in your home as one GPU pool.

> Licensed under the [MIT License](LICENSE).

A coordinator sends work such as LLM chat, vision or ComfyUI workflows to whichever
machine is free. If the right machine is asleep, the job is queued, the machine is
woken with Wake-on-LAN, and the job runs once the machine is back. When it has been
idle for a while, it goes back to sleep. Everything uses only the Python standard
library, so a node can run on any Python ≥ 3.10, including the embedded Python that
ships with ComfyUI.

It was built for two Windows PCs on a home network and Tailscale, and has been in
daily use there. Measured on that setup: wake from sleep to accepting jobs takes
about 16 s.

## Features

- **Capability routing.** The coordinator doesn't need to know IP addresses. It asks
  for a capability (`llm.chat`, `comfy.aja`, …), and the router picks a live node that
  has it. It prefers nodes with no game running, then the lowest load, then the
  highest priority, then the most free VRAM.
- **Parallel fan-out.** `NodeRekisteri.rinnakkain()` spreads a batch of tasks over
  every capable node. It allows one GPU job and four light jobs per node at a time,
  and retries once on another node.
- **Wake-on-LAN.** Magic packets go out from the coordinator and are also relayed
  through every other live node (`laite.herata`). This still works when the
  coordinator is on another network, for example a VPS on Tailscale.
- **Keep-awake.** A Windows PC woken over the network falls back asleep after about
  2 minutes without user input ("unattended sleep"). Nodes call
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`, the same mechanism
  media players use, during each job and for 15 minutes after it. No power settings
  are changed.
- **Sleep and shutdown over SSH.** The sleep timer is started with WMI
  (`Win32_Process.Create`) so it survives the SSH session. Windows OpenSSH kills a
  session's child processes when the connection closes. The user sees a warning on
  screen, and the sleep can be cancelled.
- **Idle watcher.** A machine the pool woke up is put back to sleep (or the user is
  asked first) after 20 idle minutes. It is never touched while a game is running,
  jobs are pending, or someone has used the mouse or keyboard in the last 10 minutes.
- **SQLite job queue.** Job states are `jonossa` (queued) → `odottaa_heratysta`
  (waiting for wake-up) → `kaynnissa` (running) → `valmis` (done), `epaonnistui`
  (failed) or `peruttu` (cancelled). After a coordinator crash, running jobs are put
  back in the queue, up to 3 attempts.
- **HMAC-signed requests.** Every request carries an HMAC-SHA256 of method, path,
  timestamp, nonce and body hash, made with a shared token. The token never goes over
  the network. A 60 s time window and single-use nonces stop replays. Nodes accept
  only loopback, Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48) and private LAN
  addresses.
- **One-command remote install.** `jarvis_core.kayttoonotto.asenna()` zips the node,
  copies it over `scp`, and runs a PowerShell installer. The installer registers a
  logon scheduled task, adds a firewall rule that allows only Tailscale and LAN
  addresses, and waits for `/health`. The token is passed base64-encoded inside the
  script and never shows up on a command line, in logs or in the returned report.

## Layout

```
jarvis_core/    coordinator-side core (stdlib only)
  protokolla.py   signing, verification, address filter, token handling
  reititin.py     node registry: heartbeat polling, selection, calls, fan-out
  jono.py         SQLite job queue with wake-up
  kayttoonotto.py remote node install over SSH (Windows)
jarvis_node/    the node: runs on every machine (stdlib only)
  node.py         threaded HTTP server, GPU job queue, file store
  kyvyt.py        capabilities: Ollama, ComfyUI, services, Wake-on-LAN, keep-awake
  jarjestelma.py  metrics: CPU, RAM, GPU (nvidia-smi), active game, user idle time
jarvis_pool/    coordinator glue
  devices.py      device registry, WoL, SSH, sleep/shutdown, wake-and-wait
  pool.py         Pool: own node + router + queue + idle watcher
examples/       devices.example.json, coordinator.py
tests/          pytest suite (no network needed; nodes bind to port 0 on loopback)
```

The original code was written in Finnish, so most identifiers in `jarvis_core` and
`jarvis_node` are Finnish. The new glue in `jarvis_pool` is in English. Glossary:

| Finnish | English | Finnish | English |
|---|---|---|---|
| kyky / kyvyt | capability / capabilities | syke | heartbeat |
| node / NodeRekisteri | node / node registry (router) | tyo / tyot | job / jobs |
| kutsu | call | jono | queue |
| herätä / heraata | wake up | pida_hereilla | keep awake |
| laite / laitteet | device / devices | palvelu | service |
| aivot | brain (= coordinator) | peli_aktiivinen | game running |
| elossa / epavarma / poissa | alive / uncertain / gone | aikaraja_s | timeout (s) |

## Quick start

### 1. On each worker PC

```bash
pip install .            # or just copy jarvis_core/ and jarvis_node/
cp jarvis_node/node.example.json node.json   # edit services, port, games
python -c "import secrets; print(secrets.token_hex(32))" > node_token.txt
python jarvis_node/node.py --config node.json
```

Use the same `node_token.txt` on every machine, or set `JARVIS_NODE_TOKEN`. Never
commit it. For Windows PCs, `kayttoonotto.asenna()` does all of this over SSH.

### 2. On the coordinator

```bash
cp examples/devices.example.json devices.json   # addresses, MAC, SSH user
python examples/coordinator.py devices.json
```

```python
from jarvis_pool import Pool
from jarvis_pool.devices import load_registry

pool = Pool(lambda: load_registry("devices.json"), data_dir="data", notify=print)
pool.start()
print(pool.status_text())
pool.enqueue("comfy.aja", {"workflow": {...}}, description="picture", target="gaming-pc")
pool.wake_and_wait("gaming-pc")
pool.power_off("gaming-pc", "sleep")      # or "shutdown" / "cancel"
```

To call a node directly, use `pool.router.kutsu("llm.chat", {...})`. To spread a
batch over all capable nodes, use `pool.router.rinnakkain([...])`.

## Configuration

`devices.json` (coordinator):

| key | meaning |
|---|---|
| `paikallinen` | `true` for the coordinator's own machine |
| `os` | `windows` / `linux` |
| `tailscale`, `lan` | addresses, tried in this order |
| `node_portti` | node port (default 8791); devices without it are not nodes |
| `mac` | MAC address for Wake-on-LAN |
| `ssh_kayttaja`, `ssh_portti`, `ssh_alias` | SSH access for install and power-off |
| `automaattisammutus` | `true`: sleep automatically after the idle period |
| `automaattisammutus_tapa` | `nukku` (sleep, default) or `sammuta` (shutdown) |
| `sovellukset.<app>.odota_s` | how long a queued job waits for the machine and app |

`node.json` (node): `node_id`, `prioriteetti` (lower = preferred), `kuuntele` (bind
addresses), `portti`, `palvelut` (services: `portti`, `terveys` health path,
`tehtava` scheduled task, `tunniste` command-line pattern, `kaynnistys` executable,
`argumentit`, `odota_s`), `pelit` (game process names), `token_tiedosto`.

## Node HTTP API

| method | path | |
|---|---|---|
| GET | `/health` | `{"ok", "node_id", "versio"}`, no auth |
| GET | `/manifest` | hardware and capabilities |
| GET | `/syke` | heartbeat: CPU, RAM, GPU/VRAM, services, loaded models, jobs, game, idle time, keep-awake left |
| POST | `/call` | `{"kyky", "args", "aikaraja_s"}` → `202 {"tyo_id"}` |
| GET | `/tyo/<id>` | job state, progress, result |
| POST | `/tyo/<id>/peru` | cancel |
| GET | `/f/<id>` | download a result file (e.g. a ComfyUI image) |

Signed requests carry these headers: `X-Jarvis-Lahettaja` (sender),
`X-Jarvis-Ts`, `X-Jarvis-Nonce` and `X-Jarvis-Allekirjoitus` (signature). The signed
message is `METHOD\npath?query\nts\nnonce\nsha256(body)`.

Built-in capabilities are `jarjestelma.tila`, `jarjestelma.pida_hereilla` and
`laite.herata`. With Ollama configured you also get `llm.chat`, `llm.vision` and
`llm.mallit`. With ComfyUI configured you get `comfy.aja` (a GPU job: one at a time
per node). With any service configured you get `palvelu.kaynnista` and
`palvelu.sammuta` (start and stop that service).

## Tests

```bash
pip install pytest
python -m pytest
```

The suite needs no network or SSH. Real nodes bind to port 0 on loopback, and SSH,
sockets and clocks are replaced with fakes.

## Security notes

- Anyone with the token can run every capability on every node, including starting
  and stopping configured services. Treat the token like a password.
- The address filter and the generated firewall rule are a second layer, not a
  replacement for the token.
- `/health` is unauthenticated. It reveals only the node id and version.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Jarno Eemeli.
