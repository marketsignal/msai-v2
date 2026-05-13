# Fix: prod compose IB_PORT default routes to wrong socket

## Goal

Backend + live-supervisor in `docker-compose.prod.yml` must connect to IB Gateway through the gnzsnz `socat` proxy port (4004 paper / 4003 live), not the IB Gateway's internal-loopback API port (4002 paper / 4001 live). Right now they target the loopback port and the API handshake times out — broker connectivity is fundamentally broken on prod. (Workers don't open IB connections; they read Parquet/Postgres only.)

## Architecture

The gnzsnz `ib-gateway:10.43.1c` image runs two processes:

1. **IB Gateway (Java)** binds to `127.0.0.1:4002` (paper) or `127.0.0.1:4001` (live) _inside the container only_. It refuses API connections from non-loopback IPs.
2. **socat proxy** listens on `0.0.0.0:4004` (paper) or `0.0.0.0:4003` (live) and forwards each connection to `127.0.0.1:4002`/`4001`. The proxy re-originates as localhost, so IB Gateway accepts the connection.

So **clients on the docker network must target 4004/4003 (socat), never 4002/4001 (gateway loopback bind)**.

`docker-compose.dev.yml` correctly uses 4004 (paper default), works for every dev session. `docker-compose.prod.yml` (introduced in PR #50 `6900210`) used 4002 with a misleading comment claiming "Pre-2026-05-09 this defaulted to 4004 — never matched any ib-gateway listener." That comment is wrong: 4004 IS what works.

## Tech Stack

- `docker-compose.prod.yml` (3 services consume `IB_PORT`; 1 service consumes `IB_API_PORT`)
- `backend/src/msai/core/config.py` (`Settings.ib_port` Pydantic default + alias choices)
- `tests/infra/test_workflow_deploy.sh` (grep-based regression assertions on compose semantics)
- Operator docs: `vm-setup.md`, `release-signoff-checklist.md`, `ib-gateway-troubleshooting.md`, `system-topology.md`, `CLAUDE.md`, `scripts/verify-paper-soak.sh`
- gnzsnz/ib-gateway image (version-pinned `10.43.1c`)

## Approach Comparison

| Axis                  | **Default: decouple `IB_PORT` (client) from `IB_API_PORT` (gateway)**                                               | Alternative: hot-patch `/run/msai.env` on the VM                           |
| --------------------- | ------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Complexity            | Single compose edit + test assertion + comment cleanup                                                              | Zero code change; manual env edit on every VM                              |
| Blast Radius          | All prod deploys benefit from correct default                                                                       | Single VM, lost on next render-env-from-kv.sh run                          |
| Reversibility         | Compose `IB_PORT` is a knob with a CORRECT default; `git revert` if needed. Overriding to `4002` re-breaks (don't). | Same                                                                       |
| Time to Validate      | Redeploy + resume paper drill (~10 min)                                                                             | Redeploy + resume paper drill (~10 min)                                    |
| User/Correctness Risk | LOWER — eliminates "forgot to set the override" failure mode for every future operator + every future deploy        | HIGHER — Contrarian's "hot-patched state" objection from the drill council |

**Chosen Default: in-tree compose fix.**

## Contrarian Verdict — PRE-DONE

Per `feedback_skip_phase3_brainstorm_when_council_predone`: the 5-advisor + Codex chairman council ran during the paper-drill kickoff earlier this session, and its blocking objection explicitly read **"Any failure in broker resolution, migrations, stale worker imports, or heartbeat freshness stops the drill and becomes the next fix."** The drill's preflight surfaced this exact failure (Step 3, IB Gateway handshake timeout on `ib-gateway:4002`). The council's recommended path is what this PR implements; Phase 3.1 / 3.1b / 3.1c are not re-run.

Pragmatist's vote explicitly named this risk: "DUP733213 market-data entitlement gap (per reference_ib_entitlements.md) may bite again — use a paper account with confirmed data, not that one." Contrarian's blocking objection: "Treat 'no live bars' as an infrastructure failure, not a strategy result." Both maps directly onto the IB_PORT bug — broker resolution failing is infrastructure, not strategy.

## Root Cause (Phase 2 RCA, documented)

**Reproduce:** From a backend container on prod, `ib_async.IB.connectAsync("ib-gateway", 4002)` times out after 20s. From the same container, `ib_async.IB.connectAsync("ib-gateway", 4004)` returns server_version=178 and 6 paper sub-accounts.

**Isolate:** `docker top msai-ib-gateway-1` shows `socat TCP-LISTEN:4004,fork TCP:127.0.0.1:4002`. Backend at `172.18.0.x` connects to `ib-gateway:4002` → docker bridge routes the SYN → IB Gateway accepts TCP but is bound to 127.0.0.1 only, so the API handshake never completes. Backend connects to `ib-gateway:4004` → socat accepts → forwards as `127.0.0.1` → IB Gateway accepts → handshake completes.

**Identify:** PR #50 (`6900210`) set `IB_PORT: ${IB_PORT:-4002}` in prod compose with a comment claiming the prior default of 4004 "never matched any ib-gateway listener." That diagnosis was wrong — 4004 IS what works because that's the socat port. Dev compose's `IB_PORT: ${IB_PORT:-4004}` has worked through every session since the broker profile landed.

**Verify:** Empirically confirmed in the paper-drill preflight on commit `613186e`.

## Fix Design

### `docker-compose.prod.yml` (6 edits)

1. **Header comment (line 37):** `IB_PORT : 4002 paper (default), 4001 live` → `IB_PORT : 4004 paper (default), 4003 live` (and add a short note that this is the socat proxy port).
2. **`backend.environment.IB_PORT` (line 153):** `${IB_PORT:-4002}` → `${IB_PORT:-4004}`. Comment claiming "Pre-2026-05-09 this defaulted to 4004 — never matched any ib-gateway listener" is wrong and gets removed/replaced.
3. **`live-supervisor.environment.IB_PORT` (line 356):** same change.
4. **`ib-gateway.environment.IB_API_PORT` (line 490):** decouple from `IB_PORT`. New value `${IB_API_PORT:-4002}` — operator overrides `IB_API_PORT=4001` for live mode, separate from the client `IB_PORT=4003` override. Update the explanatory comment block (lines 482–489) to reflect the new pattern.
5. **Host port mapping (line 496):** `"127.0.0.1:${IB_PORT:-4002}:${IB_PORT:-4002}"` → `"127.0.0.1:${IB_PORT:-4004}:${IB_PORT:-4004}"`. The reachable-from-host port should be the socat proxy port, not the loopback-only gateway port — that way `curl 127.0.0.1:4004` on the VM actually probes a working endpoint.
6. **Healthcheck (line 518):** keep referencing `$$IB_API_PORT` (the env var name is now decoupled but the value is still 4002 paper / 4001 live, matching IB Gateway's actual loopback bind — probing from inside the container against localhost is correct).

**Operator flip paper → live:** instead of one env override (`IB_PORT=4001`) the operator now sets three explicit vars: `TRADING_MODE=live`, `IB_API_PORT=4001`, `IB_PORT=4003`. Slight ergonomic regression for explicit correctness. Document this in the operator runbook.

### `backend/src/msai/core/config.py` (1 edit, Codex iter-1 P2 follow-up)

The `Settings.ib_port` Pydantic field currently defaults to `4002`. That default fires when no env var is set — e.g. a local dev runs `uvicorn msai.main:app` directly. The default must match the empirical truth (gnzsnz socat proxy port):

```python
ib_port: int = Field(
    default=4004,  # gnzsnz socat proxy port for paper. Live = 4003 (explicit override).
    validation_alias=AliasChoices("IB_PORT", "IB_GATEWAY_PORT_PAPER"),
)
```

### Stale operator-doc sweep (Codex iter-1 P2 + P3)

Every doc that references raw port 4002 / 4001 in a "what the client uses to reach the gateway" context needs the socat-proxy port instead. Found references:

- `docs/runbooks/vm-setup.md:88` — `.env` example sets `IB_PORT=4002` → change to `IB_PORT=4004` plus add `IB_API_PORT=4002`.
- `docs/release-signoff-checklist.md:68` — paper-soak command line `IB_PORT=4002` → `IB_PORT=4004`; live command `IB_PORT=4001` → `IB_PORT=4003 IB_API_PORT=4001`.
- `scripts/verify-paper-soak.sh:134` — error message mentions `IB_PORT=4001` for live → update to the new two-var idiom.
- `docs/runbooks/ib-gateway-troubleshooting.md:7,27` — "Port 4002: IB API (paper trading)" + diagnostic curl on 4002 → 4004 with a note that the loopback-only 4002 inside the container is the gateway's actual bind.
- `docs/architecture/system-topology.md:21,84-85,91,105,118,160` — port table + diagrams referencing 4002/4001 in client-connect contexts (e.g. "live-supervisor → ib-gateway:4002") → 4004/4003. Healthcheck row stays at 4002 (loopback bind probe is correct).
- `CLAUDE.md:233-234` — `IB_GATEWAY_PORT_PAPER=4002` → `IB_GATEWAY_PORT_PAPER=4004` (this IS a Pydantic alias for `ib_port` per `core/config.py:91-94`, so setting it actually flips the client). `IB_GATEWAY_PORT_LIVE=4001` → `IB_GATEWAY_PORT_LIVE=4003`, **but** annotate that `IB_GATEWAY_PORT_LIVE` is currently documentation-only — to actually flip the client to live the operator must set `IB_PORT=4003` explicitly. The plan does NOT add an `IB_GATEWAY_PORT_LIVE` Pydantic alias in this PR (that'd be a behavior change).
- `CLAUDE.md:269`, `docs/architecture/00-developer-journey.md:135`, `tests/e2e/use-cases/instruments/symbol-onboarding.md:113-114`, `tests/e2e/use-cases/instruments/databento-registry-bootstrap.md:144`, `backend/tests/e2e/test_instruments_refresh_ib_smoke.py:4-5` — additional stale 4002/4001 client-side refs caught by Codex iter-3 P3, fix in the same sweep.
- `docs/architecture/live-trading-subsystem.md:129` — "paper_trading=True requires IB_PORT=4002" → "paper_trading=True requires IB_PORT in {4002, 4004}" (matches `IB_PAPER_PORTS` in `ib_port_validator.py`, which already accepts both).
- `docs/architecture/how-live-portfolios-and-ib-accounts.md:190` — same correction.
- `tests/e2e/use-cases/live/registry-backed-deploy.md:19` — `IB_GATEWAY_PORT_PAPER=4002` precondition → `IB_GATEWAY_PORT_PAPER=4004`.
- `backend/src/msai/live_supervisor/__main__.py:89` — comment "ib_port to 4002 (paper)" → "ib_port to 4004 (paper, socat) — IB Gateway's loopback bind is 4002 but cross-container clients must use the socat proxy port."
- `README.md:80` (port table showing host mapping `127.0.0.1:4002 → 4002`) and `README.md:261` ("IB port 4002 (paper) by default") — update to socat ports.

The runtime validator (`backend/src/msai/services/nautilus/ib_port_validator.py:29-30`) already accepts both port pairs — no code change needed there, just the stale-doc sweep.

### Durable live-mode override path (OUT OF SCOPE)

Codex iter-2 P2: `TRADING_MODE` / `IB_API_PORT` / `IB_PORT` are NOT durable through `scripts/msai-render-env.service` (its `REQUIRED_SECRETS` / `OPTIONAL_SECRETS` only cover Key-Vault-stored secrets, not plain configuration). For PAPER mode this PR is sufficient because the compose defaults are correct; for LIVE mode an operator would currently have to manually edit `/run/msai.env`, which is the rejected hot-patch path.

This PR explicitly does NOT address the durable live-flip path. That requires an architectural decision (systemd `Environment=` in `docker-compose-msai.service`, a separate non-secret config renderer, KV-based runtime knobs, or operator-managed `/etc/msai/runtime.env` overlay) and belongs in a separate PR. The plan's live-flip recipe (`TRADING_MODE=live IB_PORT=4003 IB_API_PORT=4001`) is documented as "what to set" — the "where to set it" is the deferred follow-up.

**Deferred follow-up (post-merge):** decide the durable runtime-config path before the live-trading drill.

## Test Design (Phase 4 TDD)

Three layers of test (Codex iter-1 P2: expand coverage):

### Layer A — `tests/infra/test_workflow_deploy.sh` grep assertions (compose source-of-truth)

1. `IB_PORT:-4004` appears in prod compose for backend service block.
2. `IB_PORT:-4004` appears for live-supervisor service block.
3. `IB_API_PORT:-4002` (decoupled from IB_PORT) appears for ib-gateway service block.
4. The host port mapping line is `127.0.0.1:${IB_PORT:-4004}:${IB_PORT:-4004}` (exposes socat, not the loopback bind).
5. The stale-comment about "Pre-2026-05-09 this defaulted to 4004 — never matched any ib-gateway listener" is GONE.

### Layer B — `docker compose config` rendering assertions (Codex iter-1 P2)

Invocation: `COMPOSE_PROFILES=broker docker compose --env-file <stub> -f docker-compose.prod.yml config`. The `broker` profile is REQUIRED — without it, `live-supervisor` and `ib-gateway` are filtered out and the rendered YAML omits exactly the services we're trying to assert on (Codex iter-5 P2).

The stub env file (a temp file generated by the test) sets all `:?`-guarded required vars to dummy values so `docker compose config` succeeds. As of this PR the full list is: `POSTGRES_PASSWORD`, `REPORT_SIGNING_SECRET`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `CORS_ORIGINS`, `IB_ACCOUNT_ID`, `TWS_USERID`, `TWS_PASSWORD`, `MSAI_HOSTNAME` (Caddy needs this), and the image-version vars `MSAI_REGISTRY` / `MSAI_BACKEND_IMAGE` / `MSAI_FRONTEND_IMAGE` / `MSAI_GIT_SHA`. The test should derive the list from a regex over the compose file — occurrence-level, not line-level (a single line can contain multiple `:?` guards). Use `grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*:\?' docker-compose.prod.yml | sed 's/^\${//; s/:?$//'` (or a Python regex) so the stub list stays in sync if a new required var is added later.

**Ambient env isolation:** the test MUST `unset IB_PORT IB_API_PORT TRADING_MODE` (and any other overridable runtime var being asserted on) in the subshell that invokes `docker compose config`. Compose's `--env-file` is overridden by the shell's actual env, so a stray `IB_PORT=4003` in the CI runner's env would contaminate the "default paper" scenario. Use `env -i` with an explicit allowlist, or `unset` immediately before the invocation.

Parse the rendered YAML (don't grep raw strings — Compose normalizes port mappings into structured `{host_ip, target, published, protocol}` entries, so the raw `127.0.0.1:4004:4004` form does NOT survive rendering). Use `yq -r` or a Python helper. Assert, for two scenarios:

- **Default paper (no port overrides):** `services.backend.environment.IB_PORT == "4004"`; `services["live-supervisor"].environment.IB_PORT == "4004"`; `services["ib-gateway"].environment.IB_API_PORT == "4002"`; `services["ib-gateway"].ports[0]` has `host_ip: 127.0.0.1, target: 4004, published: 4004`.
- **Explicit live (`IB_PORT=4003 IB_API_PORT=4001 TRADING_MODE=live` set in the stub env):** backend `IB_PORT == "4003"`; live-supervisor `IB_PORT == "4003"`; ib-gateway `IB_API_PORT == "4001"`; ib-gateway port `target: 4003, published: 4003`.

This is the regression guard that catches the negative case Codex flagged: `IB_API_PORT: ${IB_PORT...}` re-coupling the two vars again. The test belongs in `tests/infra/test_workflow_deploy.sh` alongside the grep assertions (same test surface, adds `docker compose config` + structured YAML parsing).

### Layer C — Python-level default test

Add a single test in `backend/tests/unit/core/test_config.py` that asserts the Pydantic-level default for `ib_port` is 4004. Must explicitly disable `.env` loading (otherwise the worktree's `.env` would leak in and the test wouldn't be testing the default). Pattern:

```python
def test_ib_port_default_is_socat_proxy_paper_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IB_PORT", raising=False)
    monkeypatch.delenv("IB_GATEWAY_PORT_PAPER", raising=False)
    settings = Settings(_env_file=None)
    assert settings.ib_port == 4004
```

Catches the case where a local dev runs the backend outside docker compose and inherits the Pydantic default.

We will NOT write a full integration test that spins up the broker profile and probes the port — that's what Phase 5.4's E2E (re-running the paper drill) validates, end-to-end on the real prod VM after deploy.

## E2E Use Cases (Phase 3.2b)

Project type: **fullstack**. Interface for these UCs: **API + operational**.

### UC1 — broker resolution smoke from backend to IB Gateway

**Intent:** A backend deployment with the broker profile active must reach IB Gateway through the socat proxy and complete an API handshake.

**Setup:** Deploy this fix to prod via the existing CI pipeline. Bring up the broker profile.

**Steps:**

1. Operator: `COMPOSE_PROFILES=broker docker compose up -d ib-gateway` on prod VM.
2. Wait for `msai-ib-gateway-1` health: `healthy`.
3. From inside `msai-backend-1`, run an ib_async connect probe to `ib-gateway:${IB_PORT}` (default 4004 after fix).

**Verification:** Probe returns `server_version > 0` and a non-empty `managedAccounts` list. The probe IS the regression guard.

**Persistence:** N/A — broker session is stateless from the outside.

### UC2 — paper live drill resumes after the fix

**Intent:** Continue the paper live drill from the earlier session, now that the IB_PORT bug is fixed. The drill at minimum must reach step 3c (data entitlement probe for AAPL/SPY).

**Setup:** This fix merged + redeployed to prod.

**Steps:**

1. Bring up broker profile.
2. Run AAPL/SPY market-data entitlement probe inside `msai-backend-1` against `ib-gateway:${IB_PORT}`.

**Verification:** Probe reports `SUBSCRIBED` for AAPL and SPY with non-zero bid/ask/last from IB.

**Persistence:** N/A.

## Plan Review (Phase 3.3)

Will be filled by Codex + main-agent review.

## Implementation Order

1. **Layer A failing tests:** add the 5 grep assertions to `tests/infra/test_workflow_deploy.sh` and run it — expect FAIL (RED).
2. **Layer B failing tests:** add the two `docker compose config` rendering assertions (default paper + explicit live) to `tests/infra/test_workflow_deploy.sh` — expect FAIL.
3. **Layer C failing test:** add a Python unit test asserting `Settings().ib_port == 4004` in `backend/tests/unit/core/test_config.py` — expect FAIL.
4. **Fix:** edit `docker-compose.prod.yml` per Fix Design (6 edits — comment, backend IB_PORT default, live-supervisor IB_PORT default, decouple ib-gateway IB_API_PORT, host port mapping, remove the misleading "Pre-2026-05-09" comment).
5. **Fix:** edit `backend/src/msai/core/config.py` — change `ib_port` default from 4002 to 4004 with a comment.
6. **Fix:** sweep the stale docs enumerated above (10 files).
7. Re-run Layers A + B + C — expect PASS (GREEN) for all three.
8. Lint check the YAML (`docker compose -f docker-compose.prod.yml config -q` against a stub env, idempotent re-run after step 7).
9. Code review (Codex + PR Toolkit) — Phase 5.1.
10. Simplify pass — Phase 5.2.
11. Verify-app suite — Phase 5.3.
12. Open PR (DO NOT merge until CI green).
13. After CI: ask user for merge approval.
14. After merge: redeploy hits prod, then resume the paper drill UC1 + UC2 as Phase 5.4 evidence.
