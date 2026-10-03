# PRD — Always-On IB Gateway Watchdog (prod)

> Status: design approved by operator 2026-06-10 (this session). Brainstorm PRE-DONE.
> Scope: **prod only.** Standalone small PR — the operational insert chosen **before PR5** (per-account risk caps).

## Problem

The operator's requirement: _"the IB Gateway always should be up. There should be something that keeps it up and connected. That's the whole point: these always have to be on."_

Today, when **idle** (no live deployment running), **nothing watches, remediates, or alerts** on a down-or-disconnected prod IB Gateway:

- `ib-gateway` + `live-supervisor` are `restart: unless-stopped` — Docker's restart policy only acts on **container EXIT**, not on a container that is `Up` but **unhealthy**.
- The node-level `IBDisconnectHandler` (2-min grace → halt) and the PR-1b data-stale monitor only run **inside a live deployment**.
- `IBProbe` (30s) exposes `gateway_connected` on `/health` + `/api/v1/account/health`, and `AlertService` can email + persist to `/api/v1/alerts/` — but nothing wires an **idle gateway-down** condition to an alert or a restart.

**Hard evidence (2026-06-09/10 HVP/LVP drills):** the prod gateway sat `Up (unhealthy)` for minutes with the internal IB API on 4001 refusing connections while the container kept running. So **"always on" must key off IB-session health, not `docker ps`.** Separately, blindly restart-looping a gateway whose login is _persistently_ failing auth (the HVP `PROD_PAPER_INVALID_CHOICE` / IB-Key case) just churns IBKR sessions and makes recovery worse — so remediation must distinguish a **transient** down (restart) from a **persistent** failure (escalate + stop).

## Goals

1. Keep prod's IB Gateway **session** up and connected, autonomously, when idle.
2. Detect the failure classes Docker's restart policy misses: **container up but IB API dead**, and **idle-down** (`down`'d / restart-policy exhausted).
3. **Alert** (not restart-loop) on persistent auth/config failure.
4. Never restart out from under a running live deployment — coordinate with the node's own halt.
5. Reuse existing infrastructure (the `IBProbe`/`AlertService` signal + `/api/v1/account/health` + `/api/v1/live/status`; the systemd pattern of `msai-render-env.service`).

## Non-goals

- **Does NOT fix** auth/config failures (HVP-class) — it **escalates** them (CRITICAL alert) and stops restarting.
- **Prod only** — dev gateway stays operator-managed per session (and one-session-per-login means dev can't be live alongside prod anyway).
- Does NOT restart out from under a running node (halt-first).
- Does NOT touch the IB one-session-per-login rule — only ever recreates prod's single `ib-gateway`; never spawns a duplicate.
- NOT per-account risk caps / fleet ledger — that's PR5.

## Design (approved)

### Home: host systemd timer + app signal

A systemd `msai-gateway-watchdog.service` + `.timer` on the prod VM (shipped by the deploy into `/opt/msai/scripts` + systemd, exactly like `msai-render-env.service`), firing every ~30s. Rationale: the **restart action** (`docker compose … up -d --force-recreate ib-gateway` from `/run/msai.env`) is inherently a host operation (Docker + creds live on the host), and the host layer already has a systemd precedent. Isolated from the trading supervisor's blast radius. Decision logic is testable app code (not bash).

### Per-tick flow

1. **Gather signals** (host script):
   - `docker inspect` health + running state of `ib-gateway`.
   - `curl localhost:8000/api/v1/account/health` → `gateway_connected`, `consecutive_failures` (MSAI_API_KEY from `/run/msai.env`).
   - `curl localhost:8000/api/v1/live/status` → is any deployment active (non-stopped)?
2. **Decide** via a **pure, unit-tested** function in app code: `msai.services.gateway_watchdog.decide(signals) -> Action`. The host invokes it through a thin `msai system gateway-watchdog-tick` CLI so the logic is **not duplicated in bash** and is unit-testable.
3. **Act** (host script): restart = `COMPOSE_PROFILES=broker docker compose --env-file /run/msai.env --env-file /run/msai-images.env -f docker-compose.prod.yml up -d --force-recreate ib-gateway`; alert via the existing `AlertService` (email + `/api/v1/alerts/`).

### Decision state machine

- **Healthy** → reset restart counter; no action.
- **Down/unhealthy for ≥ grace** (≈2 consecutive ticks / 60–90s, to ignore momentary blips):
  - **Live deployment active?** → **alert immediately, do NOT restart**; wait until the deployment is halted/stopped (the node's own 2-min disconnect-grace halts it); _then_ take the restart path.
  - **Idle** → restart path.
- **Restart path (anti-flap):**
  - restarts-in-window `< K` (≈3 in 15 min) → recreate `ib-gateway` + **WARNING** alert + increment counter.
  - restarts-in-window `≥ K` with no recovery → **STOP restarting**, **CRITICAL** alert (_"persistent gateway auth/config failure — operator needed"_), enter ≈30-min cooldown, then one retry + re-alert.
- Recovery (healthy tick) → counter resets.
- All thresholds (`grace`, `K`, `window`, `cooldown`, `poll interval`) are **env-tunable**.

### "Down" definition

`ib-gateway` not running **OR** container healthcheck `unhealthy` **OR** `/account/health` unreachable / `gateway_connected:false` for ≥ grace.

## Acceptance criteria

1. Idle + gateway down → watchdog recreates it and a WARNING alert appears in `/api/v1/alerts/`.
2. Idle + gateway unhealthy (container up, 4001 dead) → watchdog recreates it (this is the case Docker's restart policy misses).
3. Persistent failure (≥ K restarts, no recovery) → watchdog STOPS restarting and emits a CRITICAL alert; no restart-storm.
4. Live deployment active + gateway down → watchdog alerts but does NOT restart until the deployment is halted/stopped.
5. Decision function is pure + unit-tested across the full matrix (healthy / idle-down / unhealthy-running / live-active-down / persistent-fail / cooldown).
6. Watchdog ships via the deploy (systemd unit + timer installed on the VM, idempotently).

## Surface coverage decision

Project exposes `[API, CLI, UI]` (per `CLAUDE.md ## E2E Configuration`).

- **CLI: Covered** — the watchdog tick is invoked as `msai system gateway-watchdog-tick`; an operator can run it manually and observe its decision/action. CLI UC targets this.
- **API: Covered (read-side)** — the watchdog's observable effect is an alert in `GET /api/v1/alerts/` after a remediation; an operator/integrator polling alerts sees the gateway recovered + the WARNING/CRITICAL alert. API UC targets the alert trail.
- **UI: N/A** — this is an unattended operational watchdog with no new user-facing page; its surfaced effect (alerts) is already viewable on the existing alerts surface. No new UI element. (If alerts have a UI page, the existing page renders the new alerts with no code change.)

## Open risks

- Restart command depends on `/run/msai.env` + `/run/msai-images.env` being present (rendered by `msai-render-env.service`) — watchdog must fail-safe (alert, don't crash-loop) if they're missing.
- systemd timer cadence vs IBProbe 30s refresh — avoid double-counting; grace window absorbs the skew.
- Must respect that the gateway is in the `broker` compose profile (operator-managed, deploy-excluded) — the watchdog IS the automated operator for restart; it must set `COMPOSE_PROFILES=broker`.
