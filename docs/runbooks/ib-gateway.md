# IB Gateway Runbook

Operational notes for the Interactive Brokers Gateway container (`ib-gateway`) on the
production VM.

---

## IB Gateway Watchdog (prod)

### What it does

The IB Gateway container can be "up" (Docker reports it running) while its IB _session_
is dead — the API socket stops responding after an IBKR-side disconnect, a daily
auto-restart/relogin, or a wedged IBC login. Docker's own restart policy does **not**
help here: it only restarts a container on _exit_, never on `unhealthy`. The watchdog
closes that gap.

A host **systemd oneshot + timer** (`msai-gateway-watchdog.{service,timer}`) fires every
~30s and runs `/usr/local/bin/gateway-watchdog.sh`. Each tick:

1. Reads the container's `docker inspect` running + health state.
2. Delegates the decision to a PURE app function via
   `docker exec msai-backend-1 python -m msai.cli system gateway-watchdog-tick …`.
   The CLI combines the host signals with the web process's in-memory IB probe
   (`GET /api/v1/account/health`) and the active-deployment count (DB-direct), runs the
   decision, persists anti-flap counters in Redis, and emits any alert.
3. Acts on the host **only** when the tick prints the `restart` action token —
   recreating the gateway with
   `COMPOSE_PROFILES=broker docker compose --project-name msai … up -d --force-recreate ib-gateway`.

Key safety properties:

- **Halt-awareness** — the watchdog NEVER restarts the gateway while a live deployment is
  active (status in `running, ready, starting, building, stopping`), or when the
  live-status DB query fails (conservative: it can't confirm idle → it does not restart).
  It alerts instead; the node's own disconnect-handler halts the deployment.
- **Anti-flap** — after `restart_cap` restarts within `window_secs`, it ESCALATEs:
  stops auto-restarting, emits a CRITICAL alert, and enters a cooldown. After the cooldown
  it makes exactly ONE retry; if that still fails it re-escalates + re-cooldowns.
- **Restart authority stays on the host** — no Docker socket is mounted into any app
  container.
- **Reboot-resilient** — the recreate needs the image refs, but `/run/msai-images.env` is
  tmpfs and only written at deploy. `deploy-on-vm.sh` mirrors it to the non-volatile
  `/opt/msai/msai-images.env`; after a reboot clears `/run`, the watchdog falls back to that
  copy so it can still recreate an unhealthy gateway. The service is also ordered
  `After=msai-render-env.service` so the first post-reboot tick doesn't false-alarm before
  `/run/msai.env` is regenerated.
- **Auth-aware** — the tick reads gateway health from `/api/v1/account/health` using
  `MSAI_API_KEY`. If that probe is rejected with **401/403** (e.g. `MSAI_API_KEY` unset in a
  JWT-only prod), the watchdog treats it as a _watchdog misconfiguration_, NOT gateway-down:
  it emits a throttled CRITICAL ("set `MSAI_API_KEY`") and decides NONE, so it never
  force-recreates a healthy gateway because it couldn't authenticate to its own API. Other
  non-2xx (500/503) still count as down. → set `MSAI_API_KEY` in Key Vault for prod.
- **Recovery** — once the gateway is confirmed healthy again after a prior escalation, it
  emits an info "recovered" alert and resets all counters.

The decision tokens (last stdout line of the tick) are: `restart`, `none`, `alert_only`,
`escalate`, `recovered`. All alerts land in `GET /api/v1/alerts/` (and email when SMTP is
configured).

### Env-tunable thresholds

All optional; defaults match the PRD and are baked into the backend CLI
(`_watchdog_config_from_env`), so leaving them unset is the normal case. The host
timer runs the tick via `docker exec` into the `backend` container, so the CLI reads
these from the **backend container environment**.

To actually change one in prod, set the matching **Key Vault secret** (lowercase,
dashes — e.g. `msai-watchdog-grace-secs`) and redeploy: `msai-render-env.service`
(OPTIONAL_SECRETS) writes it into `/run/msai.env`, which `docker-compose.prod.yml`
interpolates into the backend service env (`${MSAI_WATCHDOG_GRACE_SECS:-}`). An
empty/unset value is treated as "use the default" — so an absent KV secret is a
harmless skip, not a misconfiguration.

| Env var                             | KV secret name                      | Default | Meaning                                                              |
| ----------------------------------- | ----------------------------------- | ------- | -------------------------------------------------------------------- |
| `MSAI_WATCHDOG_GRACE_SECS`          | `msai-watchdog-grace-secs`          | `60`    | Ignore a down/unhealthy gateway for this long before acting (blips). |
| `MSAI_WATCHDOG_RESTART_CAP`         | `msai-watchdog-restart-cap`         | `3`     | Restarts allowed within the window before escalating + stopping.     |
| `MSAI_WATCHDOG_WINDOW_SECS`         | `msai-watchdog-window-secs`         | `900`   | Rolling window (15 min) the restart cap is counted over.             |
| `MSAI_WATCHDOG_COOLDOWN_SECS`       | `msai-watchdog-cooldown-secs`       | `1800`  | After escalation, stop auto-restarting for this long (30 min).       |
| `MSAI_WATCHDOG_ALERT_THROTTLE_SECS` | `msai-watchdog-alert-throttle-secs` | `1800`  | Min interval between repeating same-reason alerts (anti-storm).      |

`starting` health is treated as NOT-down — the watchdog respects the gateway's ~180s
`start_period` / IBC login window after a (re)create rather than restart-storming.

### Reading the logs / status

```bash
# Recent watchdog ticks (host journal) — follow live:
journalctl -u msai-gateway-watchdog -f

# Last 100 lines:
journalctl -u msai-gateway-watchdog -n 100 --no-pager

# Is the timer armed + when does it next fire?
systemctl status msai-gateway-watchdog.timer
systemctl list-timers msai-gateway-watchdog.timer

# Dedicated host-side log file (tick stderr + recreate output):
tail -n 100 /var/log/msai-gateway-watchdog.log

# The watchdog's user-observable effect — alerts feed:
curl -s -H "X-API-Key: $MSAI_API_KEY" http://127.0.0.1:8000/api/v1/alerts/ | jq .
```

Each tick logs a line like
`… gateway-watchdog: running=true health=unhealthy → action=restart`.

### Interpreting the CRITICAL escalation

A CRITICAL alert titled **"IB Gateway persistent failure — operator needed"** (or
**"IB Gateway STILL failing after cooldown retry"**) means the watchdog recreated the
gateway up to the restart cap (and made its one post-cooldown retry) and it still did not
come back healthy. Auto-restart is now STOPPED until the cooldown elapses.

This almost always indicates an **IBKR auth/config problem**, not a transient outage —
recreating the container cannot fix it. Most common causes:

- **Paper-vs-live login mismatch** — the gateway is pointed at the wrong port/account
  (paper `4002`/`DU…` vs live `4001`/`U…`). Cross-check `IB_PORT` + the TWS login vs the
  intended live account (LVP `U4705114` local / HVP `U4715997` prod). See
  `feedback_prod_gateway_on_paper_not_live_hvp` — `gateway_connected:true` is NOT enough;
  verify the account _identity_.
- **IB Key 2FA** — IBKR is prompting for second-factor approval on the mobile IB Key app
  and the login is stuck pending. Approve it (or disable 2FA for the test login per the
  account setup).
- **Credentials rotated / expired** in Key Vault — the rendered `/run/msai.env` carries a
  stale `TWS_USERID`/`TWS_PASSWORD`.

After fixing the underlying issue, recreate manually and confirm:

```bash
COMPOSE_PROFILES=broker docker compose --project-name msai \
    --env-file /run/msai.env --env-file /run/msai-images.env \
    -f /opt/msai/docker-compose.prod.yml up -d --force-recreate ib-gateway

curl -s -H "X-API-Key: $MSAI_API_KEY" http://127.0.0.1:8000/api/v1/account/health | jq .
```

Once `gateway_connected:true`, the next watchdog tick emits an info "IB Gateway
recovered" alert and resets the counters.

A separate CRITICAL titled **"IB Gateway watchdog host-action FAILED"** means the _host_
action itself broke (missing rendered env, the tick exec failed, or the
`docker compose --force-recreate` returned non-zero) — distinct from "gateway won't come
up". Check `/var/log/msai-gateway-watchdog.log` + `journalctl -u msai-gateway-watchdog`.

### Manual diagnostic (without waiting for the timer)

Run a watchdog tick by hand to see the decision it would make. `--dry-run` persists the
counters + emits alerts but suppresses the real recreate, and the inject flags simulate a
down gateway so you see the _actionable_ decision immediately:

```bash
docker exec msai-backend-1 python -m msai.cli system gateway-watchdog-tick \
    --dry-run --inject-health down --inject-idle
```

The tick prints a human-readable summary line `decision=RESTART reason=idle-down-restart restart_count=1` followed by the bare action token on the VERY LAST line (e.g. `restart`) — the host script greps that final token, so read the summary line above it for the decision detail.
Run it again to watch `restart_count` advance, and use the companion reset to clear the
persisted counters before a clean test:

```bash
docker exec msai-backend-1 python -m msai.cli system gateway-watchdog-reset
```

> Note: the inject flags take the **bare boolean form** (`--inject-idle`, not
> `--inject-idle=true`) — Typer's dual-bool flags reject `=value`.
