# IB Gateway prod compose: clients must target the gnzsnz socat proxy port, not the loopback bind

**Branch:** `fix/ib-port-prod-compose-4004` (PR #\_\_)
**Date:** 2026-05-12
**Surfaced by:** paper live drill preflight (council Option-3 broker-side validation)

## Problem

Backend + live-supervisor in production could not complete an IB API handshake against `ib-gateway:4002`. TCP socket connection succeeded (port listener present), but `ib_async.IB.connectAsync` timed out after 20 s. No data flow, no error in Nautilus layer — silent infrastructure failure.

Empirical contrast from the prod VM, same backend container, same gateway container:

```
ib_async.connectAsync("ib-gateway", 4002) → TimeoutError after 20s
ib_async.connectAsync("ib-gateway", 4004) → server_version=178, managedAccounts=[6 paper subs]
```

This would silently block any real-money trading attempt at the first broker-connect step. It survived four deploy-pipeline slices because no end-to-end broker drill had ever fired against the prod compose stack.

## Root Cause

The `ghcr.io/gnzsnz/ib-gateway:10.43.1c` image runs two processes inside the container:

1. **IB Gateway (Java)** binds to `127.0.0.1:4002` (paper) or `127.0.0.1:4001` (live). It refuses API connections from any non-loopback IP — including the docker bridge IPs of sibling containers.
2. **socat proxy** listens on `0.0.0.0:4004` (paper) or `0.0.0.0:4003` (live) and re-originates each connection as `127.0.0.1:4002`/`4001`. The proxy is what makes IB Gateway reachable from other containers on the docker network.

`docker-compose.dev.yml` always used `IB_PORT=${IB_PORT:-4004}` — works for every dev session.
`docker-compose.prod.yml` (introduced in PR #50, commit `6900210`) used `IB_PORT=${IB_PORT:-4002}` with a comment claiming "Pre-2026-05-09 this defaulted to 4004 — never matched any ib-gateway listener." That diagnosis was wrong: 4004 IS the only port that works for clients on the docker network.

Additionally, prod compose conflated two distinct concepts in a single `IB_PORT` variable:

- **Client-side connect port** (what backend/live-supervisor target on `ib-gateway`): must be 4004 (paper socat) / 4003 (live socat).
- **Gateway-internal API port** (what `gnzsnz` tells IB Gateway to bind on container-local loopback): must be 4002 (paper) / 4001 (live).

The prod compose set both to `${IB_PORT:-4002}`. The Pydantic `Settings.ib_port` default also defaulted to 4002, which would have re-introduced the bug in any non-docker context (local dev without compose, one-shot CLI tools, etc.).

## Solution

1. **`docker-compose.prod.yml` (6 edits):**
   - Backend + live-supervisor: `IB_PORT: ${IB_PORT:-4004}` (was `:-4002`).
   - ib-gateway: `IB_API_PORT: ${IB_API_PORT:-4002}` — decoupled from `IB_PORT`. Operator must now set BOTH `IB_PORT=4003` and `IB_API_PORT=4001` to flip to live, instead of the single ambiguous knob.
   - Host port mapping: exposes socat port (4004), not the loopback bind (4002), so `curl 127.0.0.1:4004` on the VM actually reaches a working endpoint.
   - Removed the misleading `Pre-2026-05-09` comment from PR #50.
2. **`backend/src/msai/core/config.py`:** `Settings.ib_port` default changed from 4002 to 4004 (gnzsnz socat paper port). The Pydantic alias still accepts both `IB_PORT` and `IB_GATEWAY_PORT_PAPER` for backwards compatibility.
3. **Stale-doc sweep (13 files):** all client-side "connect to gateway on port 4002" references updated to 4004 (or annotated as gateway-internal-bind where the 4002 reference is intentional, e.g., the healthcheck which probes the loopback bind from inside the container).

The runtime validator (`backend/src/msai/services/nautilus/ib_port_validator.py`) was already correct: `IB_PAPER_PORTS = (4002, 4004)` and `IB_LIVE_PORTS = (4001, 4003)`. No code-level validator change was needed — the fix was entirely "stop pointing clients at the wrong port."

## Prevention

Three layers of regression guard land with this PR:

1. **`backend/tests/unit/test_compose_prod_ib_port.py`** — parses `docker-compose.prod.yml` as YAML and asserts structurally on the defaults + port mapping + decoupling + comment removal. Runs in CI on every PR.
2. **`backend/tests/unit/core/test_config.py::test_ib_port_default_is_socat_proxy_paper_port`** — asserts the Pydantic default is 4004 (with `_env_file=None` so the worktree's `.env` doesn't leak in).
3. **`tests/infra/test_workflow_deploy.sh`** — bash-level grep assertions on the literal compose YAML strings, matching the existing deploy-pipeline test convention.

## Operator runbook (post-merge)

**Paper mode (default):** no env overrides needed. Compose defaults are correct.

**Live mode flip:** set THREE env vars in `/run/msai.env` (or the durable runtime-config source — see deferred follow-up):

```bash
TRADING_MODE=live
IB_PORT=4003          # client-side socat port for live
IB_API_PORT=4001      # gateway internal loopback bind for live
```

## Deferred follow-up

The compose defaults are correct for paper, but the live-mode flip currently requires either (a) a manual `/run/msai.env` edit (rejected as "hot-patch state" per Contrarian) or (b) something not yet built. Before the first real-money drill, decide a durable runtime-config path:

- Add `TRADING_MODE` / `IB_PORT` / `IB_API_PORT` to `scripts/msai-render-env.service` as OPTIONAL_RUNTIME_CONFIG (separate from secrets).
- Or use a systemd `Environment=` directive in `docker-compose-msai.service`.
- Or operator-managed `/etc/msai/runtime.env` overlay.

Outside the scope of this PR.

## Lessons

- **gnzsnz/ib-gateway hides a socat proxy that is load-bearing for cross-container clients.** The image's port semantics are NOT just `4002 = paper, 4001 = live` — they're `gateway binds 4002/4001 on loopback; socat exposes 4004/4003 to the docker network`. Any documentation that omits this distinction is going to mislead.
- **Empirical diff between dev and prod is the canary.** Dev compose worked. Prod compose didn't. The diff (4004 vs 4002) was the bug; the misleading commit comment in PR #50 was the trap.
- **Council preflight gates earn their keep.** The 5-advisor council's blocking objection ("any failure in broker resolution stops the drill") is exactly what surfaced this — the drill itself was the test.
