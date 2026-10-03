# Research: ib-gateway-watchdog

**Date:** 2026-06-10
**Feature:** prod-only always-on IB Gateway watchdog (host systemd timer → thin `msai` CLI tick → restart/alert)
**Researcher:** research-first agent

This is an **infrastructure** feature. There are **no new pip dependencies** — the
research targets are OS/runtime behavior (systemd, Docker health/restart semantics,
the gnzsnz image) plus two already-pinned libs (Typer, httpx). Where the answer was
already settled empirically in prod drills or is fixed by project code, the brief
**corroborates against primary docs** and cites the in-repo file that pins the behavior.

## Targets Touched

| Target                    | Our pin / source                              | Latest stable       | Breaking changes | Source                                                   |
| ------------------------- | --------------------------------------------- | ------------------- | ---------------- | -------------------------------------------------------- |
| systemd (timer + oneshot) | host VM (Ubuntu 24.04, systemd 255)           | 255 (LTS)           | None relevant    | [freedesktop / Arch](#1-systemd--timer--oneshot-service) |
| Docker health + restart   | Compose v2.40 (per repo note)                 | v2.x                | None relevant    | [Docker docs](#2-docker-health--restart-semantics)       |
| gnzsnz/ib-gateway         | `ghcr.io/gnzsnz/ib-gateway:10.43.1c` (pinned) | 10.47.1d / 10.45.1g | n/a (pinned)     | [gnzsnz repo](#3-gnzsnzib-gateway-image)                 |
| Typer                     | `typer>=0.15.0` (pyproject)                   | 0.15.x              | None relevant    | [in-repo CLI](#4-typer-cli)                              |
| httpx                     | `httpx>=0.28.0` (pyproject)                   | 0.28.x              | None relevant    | [in-repo CLI](#5-httpx)                                  |

---

## Per-Target Analysis

### 1. systemd (timer + oneshot service)

**Versions:** host = Ubuntu 24.04 / systemd 255. No version delta to manage (OS-pinned).

**Key findings (corroborated against the two in-repo precedents):**

- The repo already has BOTH systemd patterns this feature needs:
  - `scripts/msai-render-env.service` — `Type=oneshot`, `RemainAfterExit=yes`, `StartLimitIntervalSec=900` / `StartLimitBurst=5` declared in **[Unit]** (the brief verified this — `StartLimit*` belongs in [Unit], not [Service]), `Restart=on-failure`, `RestartSec=30`, installed idempotently by `deploy-on-vm.sh` (`install -m 0644` → `daemon-reload` → `systemctl enable`).
  - `scripts/backup-to-blob.{service,timer}` — `Type=oneshot` service triggered by a separate `.timer` using `OnCalendar=*-*-* 02:07:00 UTC` + `Persistent=true` + `RandomizedDelaySec=300`, enabled via `systemctl enable --now backup-to-blob.timer` (note: enabling the **timer**, not the service).

- **`OnCalendar` is the WRONG tool for a ~30s cadence.** `OnCalendar` is wall-clock-scheduled (best for nightly/hourly). For a fast periodic watchdog use the monotonic pair:
  - `OnBootSec=<initial delay, e.g. 60s>` — first fire after boot (give the stack + IBC ~60–120s to come up; the gateway healthcheck has `start_period: 180s`, so OnBootSec should be ≥ that or the first ticks will always see `unhealthy`).
  - `OnUnitInactiveSec=30s` — **fire 30s AFTER the previous run FINISHED.** This is the correct knob for a oneshot watchdog: `OnUnitActiveSec` measures from the previous _activation start_ and is ill-suited to oneshot units (which never stay "active" — they go activating→inactive immediately; systemd issue #21600 documents this mismatch). `OnUnitInactiveSec` measures from when the unit went inactive, which is exactly "30s after the tick completed."

- **Overlap prevention is automatic — do NOT hand-roll a lockfile.** A timer activates its service unit; if that unit is still active/activating, systemd does nothing rather than starting a second copy. Combined with `OnUnitInactiveSec` (next fire only scheduled after the prior run finishes), a slow tick simply delays the next one — it can never overlap. `RefuseManualStart` is **not** needed for serialization; it only blocks operator `systemctl start` of the service directly. (We may still want operators to run the tick manually for debugging → leave it OFF and rely on the CLI being separately runnable.)

- **`AccuracySec`**: defaults to 1min, which would smear a 30s timer. Set `AccuracySec=1s` (or a few seconds) on the watchdog timer so the 30s cadence is honored. (Tighter accuracy = more wakeups = more power, irrelevant on an always-on VM.)

- **Failure handling:** keep `StartLimitIntervalSec` / `StartLimitBurst` in **[Unit]** (mirror render-env). For a watchdog, do **not** set an aggressive `Restart=` on the oneshot itself — the tick's own decision FSM owns retry/cooldown semantics (the PRD's anti-flap K-in-window + cooldown). A crashing tick should fail the oneshot and let the timer re-fire on the next interval; the StartLimit guards against a tight crash-loop. Mirror `StandardOutput=journal` / `StandardError=journal` (from backup-to-blob) so `journalctl -u msai-gateway-watchdog` is the operator's audit trail.

- **Idempotent install:** mirror `deploy-on-vm.sh` Phase 8 exactly — `cp`/`install` the `.service` + `.timer` into `/etc/systemd/system/`, write any RG/KV drop-in under `*.service.d/`, then `systemctl daemon-reload` and `systemctl enable --now msai-gateway-watchdog.timer`. `daemon-reload` before `enable --now` is required so systemd picks up the edited unit on re-deploy.

**Sources:**

1. [systemd.timer — Arch Wiki + SUSE "Working with systemd Timers"](https://wiki.archlinux.org/title/Systemd/Timers) — accessed 2026-06-10 (OnBootSec/OnUnitActiveSec/OnUnitInactiveSec semantics; timer→service serialization)
2. [systemd issue #21600 — OnUnitActiveSec + Type=oneshot](https://github.com/systemd/systemd/issues/21600) — accessed 2026-06-10 (why OnUnitActiveSec is a poor fit for oneshot; use OnUnitInactiveSec)
3. In-repo precedents (read, not assumed): `scripts/msai-render-env.service`, `scripts/backup-to-blob.{service,timer}`, `scripts/deploy-on-vm.sh:204-235,432-448`

**Design impact:** Use `OnBootSec` (≥ gateway `start_period`, ~180s) + `OnUnitInactiveSec=30s` + `AccuracySec=1s`, NOT `OnCalendar`. No lockfile — systemd serializes the oneshot. Enable the **timer** (`enable --now …​.timer`), not the service. Put StartLimit in [Unit]. Anti-flap/cooldown lives in the CLI's decide() FSM, not in systemd `Restart=`.

**Test implication:** Unit-test the decide() FSM across ticks (the FSM, not systemd). For the unit files, add a shell-level idempotency test (or a `deploy-on-vm.sh` dry-run assertion) that a second install + `daemon-reload` + `enable` is a no-op and that the enabled unit is the `.timer`. There is no good way to unit-test 30s cadence in CI — assert the timer file's `OnUnitInactiveSec`/`AccuracySec` literals via a file-content test.

---

### 2. Docker health + restart semantics

**Versions:** Docker Compose v2.40 (the repo's `msai-render-env.service` comment cites v2.40 interpolation behavior). No delta to manage.

**Key findings:**

- **CONFIRMED — `restart: unless-stopped` acts ONLY on container EXIT, never on `unhealthy`.** Docker's restart policy fires when the container's main process exits; a container that is `Up (unhealthy)` (process alive, healthcheck failing) is **never** restarted by the policy. This is a long-standing, documented Docker limitation (unchanged as of 2025). This is precisely the gap the watchdog fills (PRD §Problem + acceptance criterion 2). Native alternatives (autoheal sidecar, Swarm reschedule) exist but the PRD deliberately chose a host watchdog so the _restart action_ (which needs `/run/msai.env` creds + the broker profile) runs on the host.

- **Reading health:** `docker inspect --format '{{.State.Health.Status}}' ib-gateway` returns one of `starting` (within `start_period`), `healthy`, `unhealthy`, or empty (`<no value>` / no healthcheck defined). The watchdog must treat `starting` as "not yet down" (don't restart during the 180s `start_period`) and an empty/missing result as a distinct error (container not found / no healthcheck). The repo's `ib-gateway` healthcheck (docker-compose.prod.yml:586-611) is a project-defined `/dev/tcp/localhost/$IB_API_PORT` probe of the **internal** API port (4002 paper / 4001 live) with `interval:15s timeout:5s retries:5 start_period:180s` — i.e. health = "IB Gateway's loopback API listener is up," which is exactly the failure class the drills saw (`Up (unhealthy)`, 4001 refusing).

- **Running-vs-exists:** combine `docker inspect --format '{{.State.Running}}'` (true/false) and `{{.State.Health.Status}}`. A `down`'d or restart-policy-exhausted container shows `Running=false`; an up-but-dead one shows `Running=true, Health=unhealthy` — the PRD's two distinct "down" sub-cases.

- **Recreate semantics:** `docker compose up -d --force-recreate ib-gateway` stops + recreates only that service container (other services untouched), re-reading env. With this repo's prod invocation it must carry `COMPOSE_PROFILES=broker` (the service is in the `broker` profile) AND both env files: `--env-file /run/msai.env --env-file /run/msai-images.env -f docker-compose.prod.yml` (matches the PRD §Act line and the deploy's compose invocation). Without `COMPOSE_PROFILES=broker`, compose treats `ib-gateway` as out-of-scope and the recreate is a no-op. Note (verified in render-env.service comment): Compose v2.40 interpolates `${VAR:?}` guards for ALL services _before_ profile filtering, so the env files must satisfy every `:?` guard or the command fails before doing anything — fail-safe with an alert if `/run/msai.env` is missing (PRD open risk #1).

**Sources:**

1. [Docker docs — restart policy & HEALTHCHECK behavior](https://docs.docker.com/reference/dockerfile/) + [Docker auto-heal limitation writeup (oneuptime, 2026-02)](https://oneuptime.com/blog/post/2026-02-08-how-to-set-up-docker-container-auto-healing-without-orchestration/view) — accessed 2026-06-10 (restart policy acts on exit only, not unhealthy)
2. [willfarrell/autoheal pattern + Last9 "Docker Status Unhealthy"](https://last9.io/blog/docker-status-unhealthy-how-to-fix-it/) — accessed 2026-06-10 (reading State.Health.Status; why unhealthy needs external remediation)
3. In-repo (read): `docker-compose.prod.yml:536-612` (the actual ib-gateway healthcheck + profile + ports)

**Design impact:** Watchdog "down" = `Running=false` OR `Health=unhealthy`, but `Health=starting` is NOT down (respect the 180s start_period). Restart command MUST set `COMPOSE_PROFILES=broker` and pass both `--env-file` files or it is a silent no-op. Fail-safe (alert, no crash-loop) when `/run/msai.env` is absent — the `${VAR:?}` guards make a partial-env recreate abort anyway.

**Test implication:** decide() FSM tests must cover `starting` → no-action, `Running=false` → idle-down path, `Running=true & unhealthy` → unhealthy-running path (acceptance criterion 2), and missing/`<no value>` → error/fail-safe. Host-script test should assert the constructed recreate command literally contains `COMPOSE_PROFILES=broker` and both `--env-file` paths.

---

### 3. gnzsnz/ib-gateway image

**Versions:** pinned `ghcr.io/gnzsnz/ib-gateway:10.43.1c` (docker-compose.prod.yml:537). Latest stable upstream is `10.47.1d` (latest) / `10.45.1g` (stable) as of 2026-06. We are intentionally pinned — no bump in scope for this PR.

**Key findings (corroborated against gnzsnz README/repo; much already proven in HVP/LVP drills):**

- **socat relay (confirmed):** live = listen `0.0.0.0:4003` → forward `127.0.0.1:4001`; paper = listen `0.0.0.0:4004` → forward `127.0.0.1:4002`. IB Gateway binds loopback-only (4001/4002) and refuses non-127.0.0.1 connections, so socat is mandatory for any cross-container/host reach. This matches docker-compose.prod.yml:575-582 (host maps the socat port 4003/4004, NOT the loopback bind) and the `IB_PORT`/`IB_API_PORT` decoupling comments.
- **TRADING_MODE → ports:** `TRADING_MODE=live` ⇒ internal 4001; `paper` ⇒ 4002. The repo decouples this from the client-side socat port via two explicit knobs (`IB_API_PORT` = internal bind, `IB_PORT` = socat proxy port) — flipping prod to live requires BOTH `IB_PORT=4003` AND `IB_API_PORT=4001` (compose comment lines 564-579; matches CLAUDE.md env-var notes and the "prod gateway was on PAPER" memory lesson).
- **Healthcheck nuance (IMPORTANT — do not assume the gnzsnz default):** the gnzsnz upstream Dockerfile examined does **not** ship a HEALTHCHECK; this repo defines its OWN in docker-compose.prod.yml:586-611 — a portable `bash /dev/tcp/localhost/$$IB_API_PORT` TCP probe of the internal API port (no `nc` dependency). So the watchdog's `docker inspect … Health.Status` reads the **project's** probe, which keys off `IB_API_PORT` (4002/4001 by mode). This is the right signal (it tests the loopback listener socat forwards to), but the watchdog author must NOT assume an upstream healthcheck exists — it's repo-owned and mode-sensitive.
- **IBC login latency:** IBC takes ~60–120s to log in before the port opens (compose comment + gnzsnz docs), hence `start_period:180s`. The watchdog's `OnBootSec` and grace window must absorb this or every cold-start tick reads `unhealthy`/`starting`.
- **Version-specific gotcha:** `10.26.1k`+ can run live AND paper in parallel; we run single-mode. `EXISTING_SESSION_DETECTED_ACTION: primary` + the one-session-per-login IB rule (CLAUDE.md gotcha #3) means a restart that races an existing IB session can churn/disconnect — the watchdog's "never restart out from under a live deployment" rule (PRD goal 4) directly mitigates the worst case; the persistent-auth-failure STOP path mitigates the `PROD_PAPER_INVALID_CHOICE`/IB-Key churn case.

**Sources:**

1. [gnzsnz/ib-gateway-docker README (master)](https://github.com/gnzsnz/ib-gateway-docker/blob/master/README.md) — accessed 2026-06-10 (socat listen/forward ports, TRADING_MODE→port, IBC, version matrix)
2. [gnzsnz/ib-gateway GHCR versions / releases](https://github.com/gnzsnz/ib-gateway-docker/releases) — accessed 2026-06-10 (10.43.1c is a real tag; latest 10.47.1d / stable 10.45.1g)
3. In-repo (read): `docker-compose.prod.yml:536-611` (project-owned healthcheck + IB_PORT/IB_API_PORT decoupling)

**Design impact:** The watchdog reads the **project-defined** `/dev/tcp/$IB_API_PORT` healthcheck, which is mode-sensitive (4002 paper / 4001 live). It must respect `start_period`/IBC latency (≥180s) before treating a freshly-recreated gateway as "down," and must NOT restart while a live deployment is active (one-session-per-login churn risk).

**Test implication:** FSM tests should parametrize across paper(4002)/live(4001) to ensure the decision is port-agnostic (it consumes the inspect result, not the port). Add a test asserting the watchdog does NOT restart when `/live/status` shows an active deployment even if `Health=unhealthy` (acceptance criterion 4). No need to test the image itself.

---

### 4. Typer (CLI)

**Versions:** ours = `typer>=0.15.0` (pyproject), latest 0.15.x. No delta.

**Key findings (grounded in the existing `system` sub-app):**

- The `system` sub-app already exists (`cli.py:102` `system_app = typer.Typer(...)`, registered at `cli.py:120` `app.add_typer(system_app, name="system")`). Adding `gateway-watchdog-tick` is one more `@system_app.command("gateway-watchdog-tick")` — same pattern as `system health` (cli.py:1435) and `system smoke-alert` (cli.py:1499).
- **JSON output convention exists:** `system health` already emits machine-readable JSON via the in-repo `_emit_json(...)` helper. The new tick should reuse `_emit_json` to print the decision + action (e.g. `{"state": "...", "action": "restart|alert|none", "reason": "...", "signals": {...}}`) so the host script can parse it (and so the CLI UC can observe a human/machine-readable line).
- **Exit codes:** Typer maps an uncaught exception → non-zero; a clean return → 0. The existing `smoke-alert` command deliberately uses `raise typer.Exit(code=2)` for downstream-failure visibility. For the tick, the action is the host script's job — the CLI should exit 0 when it successfully _produced a decision_ (even a "restart" decision), and use a non-zero exit only for tick-internal failure (e.g. couldn't reach the local API to gather signals AND couldn't form a fail-safe decision). The host script keys off the JSON `action` field, not the exit code, for what to do; exit code is for "did the tick itself run."

**Sources:**

1. In-repo (read, authoritative): `backend/src/msai/cli.py:102,120,1435-1496` (`system` sub-app + `_emit_json` + exit-code conventions)
2. [Typer docs — commands, sub-apps (add_typer), Exit codes](https://typer.tiangolo.com/) — accessed 2026-06-10 (command/sub-app + `typer.Exit` semantics; no change since 0.12)

**Design impact:** Add `@system_app.command("gateway-watchdog-tick")` reusing `_emit_json`. CLI exit 0 = "tick ran and produced a decision" (including a restart decision); non-zero = tick-internal failure. Decision/action carried in the JSON body, NOT the exit code — the host script parses `action`.

**Test implication:** Unit-test the command via Typer's `CliRunner` asserting the emitted JSON has `state`/`action`/`reason` keys and exit 0 across the FSM matrix; assert non-zero exit only on the signal-gather-failure-with-no-fallback case. (The pure `decide()` function gets its own exhaustive FSM tests — acceptance criterion 5.)

---

### 5. httpx

**Versions:** ours = `httpx>=0.28.0` (pyproject), latest 0.28.x. No delta.

**Key findings:**

- `system health` already uses `httpx.get(url, headers=_api_headers(), timeout=5.0)` with specific exception handling (`httpx.ConnectError`, `httpx.TimeoutException`, `httpx.RequestError`) — cli.py:1488-1495. The new tick's two probes (`/api/v1/account/health` for `gateway_connected`/`consecutive_failures`, `/api/v1/live/status` for active-deployment) should reuse this exact pattern and the `_api_base()`/`_api_headers()` helpers (MSAI_API_KEY auth).
- **Important behavioral note already encoded in the codebase:** `/api/v1/account/health` returns **HTTP 200 even when the gateway is down**, with `status: "unhealthy"` / `gateway_connected:false` in the body (cli.py:1443-1448 docstring). So the tick must derive "down" from the **body fields**, never from `response.is_success`. This is the single most important httpx-related correctness point for the feature.
- **Where should the HTTP live — host curl vs CLI?** Per the PRD design, signal-gather is split: the host script does `docker inspect` (needs the Docker socket, host-only) AND the localhost `curl`s, then passes them to the CLI's `decide()`. BUT cleaner per the DRY/"not duplicated in bash" PRD intent: have the **CLI** do the httpx GETs (it already has the helpers + the 200-but-unhealthy handling) and accept the `docker inspect` results as CLI args/stdin from the host. That keeps the fragile "200-but-unhealthy" logic in tested Python, not bash. Either way, use `timeout=5.0` (matches the existing probe) and the same three specific exceptions; a probe failure degrades to a `{"error": ...}` signal that `decide()` treats as "down/unknown" (fail-toward-alert, not crash).

**Sources:**

1. In-repo (read, authoritative): `backend/src/msai/cli.py:1443-1496` (httpx usage, timeout, exception handling, and the 200-but-unhealthy contract)
2. [httpx docs — timeouts & exceptions](https://www.python-httpx.org/) — accessed 2026-06-10 (ConnectError/TimeoutException/RequestError hierarchy; `timeout=` default behavior; unchanged in 0.28)

**Design impact:** Reuse `_api_base()`/`_api_headers()`/`timeout=5.0` and the three specific httpx exceptions. **Derive "gateway down" from the response BODY (`status`/`gateway_connected`), never `response.is_success`** — `/account/health` returns 200 while unhealthy. Prefer doing the HTTP in the (tested) CLI and passing `docker inspect` output in from the host, keeping the 200-but-unhealthy logic out of bash.

**Test implication:** Tests must include a probe that returns HTTP 200 with `gateway_connected:false` and assert the tick classifies it as down (not healthy). Include ConnectError/TimeoutException cases that degrade to a fail-toward-alert signal rather than an unhandled exception.

---

## Not Researched (OS/internal)

- **`curl` / `docker inspect` invocation in the host shell script** — OS tooling, no library to research. Behavior covered under §2.
- **`AlertService` / `/api/v1/alerts/` persistence** — existing internal MSAI code (services/alerting.py), already used by `smoke-alert`; reuse, not research.
- **MSAI `decide()` FSM, `IBProbe`, `/account/health` endpoint internals** — internal app code the feature builds on; behavior read from `cli.py` and cited inline, not external research.
- **Azure Key Vault / `/run/msai.env` rendering** — owned by the existing `msai-render-env.service`; the watchdog only consumes the rendered file. No new research.

## Open Risks

1. **`start_period` vs grace double-count.** The gateway healthcheck has `start_period:180s` and IBC login is 60–120s. If `OnBootSec` < 180s OR the FSM's grace (≈2 ticks / 60–90s) doesn't account for `Health=starting`, every cold start will read "down" and trigger a needless recreate — _which resets start_period and can loop_. Mitigation: treat `Health=starting` as explicitly NOT-down, and set `OnBootSec` ≥ 180s. (Flagged for design.)
2. **`COMPOSE_PROFILES=broker` omission = silent no-op recreate.** If the host script forgets the profile env, `docker compose up -d --force-recreate ib-gateway` exits 0 having done nothing, and the watchdog "thinks" it remediated while the gateway stays down. Mitigation: assert profile in the command string + verify post-recreate that the container's `created`/`started` timestamp advanced.
3. **`/run/msai.env` missing → `${VAR:?}` abort.** PRD open risk #1. The recreate aborts (good — fail-closed) but the watchdog must catch the non-zero compose exit and emit a CRITICAL alert rather than crash-looping the oneshot into StartLimit.
4. **200-but-unhealthy contract drift.** The feature's correctness hinges on `/account/health` returning 200 while unhealthy (cli.py docstring). If that endpoint is ever "fixed" to return non-200 when down, a tick that (incorrectly) keyed off status code would silently flip behavior. Mitigation: derive from body fields + a regression test pinning the 200-but-unhealthy contract.
5. **One-session-per-login churn (HVP-class).** A restart that races an existing IB session (or a persistently failing login) churns IBKR sessions and worsens recovery (PRD §Problem; CLAUDE.md gotcha #3). Mitigated by the never-restart-under-live-deployment rule + the persistent-failure STOP+CRITICAL path, but the _thresholds_ (K, window, cooldown) are unproven against real HVP auth-failure timing — validate in the prod drill, not just unit tests.
6. **gnzsnz image pin drift.** We're on `10.43.1c`; upstream is at `10.47.1d`. Not in scope, but a future bump could change IBC login timing or the (project-owned) healthcheck assumptions — re-verify `start_period` after any image bump.
