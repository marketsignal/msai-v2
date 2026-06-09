# PRD Discussion: Dashboard Account Selector

**Status:** Complete
**Started:** 2026-06-08
**Participants:** User (operator), Claude

## Original User Stories

PR4 from the multi-account-broker-fleet run plan: "Dashboard account selector (UI fleet view + filters)." Derived stories (to be refined):

- As an operator managing multiple broker accounts (LVP / HVP / fund), I want to filter the live-trading fleet view by account so I can focus on one account's deployments and their per-account state (halt, restart-paused, heartbeat).
- As an operator, I want the dashboard to make clear which account its numbers reflect, so I'm not misled when multiple accounts exist.
- As an operator, I want my account selection to persist across navigation/reload so I don't re-select it every time.

## Grounded context (verified 2026-06-08)

- `/api/v1/live/status` already returns per-deployment account context: `account_id`, `ib_login_key`, `ibg_client_id`, `fleet_halted`, `account_halted`, `auto_restart_paused`, `last_heartbeat_age_s`. The live-trading page currently renders all deployments in ONE flat table — no per-account grouping/filter.
- `/api/v1/broker-accounts` lists registered broker accounts (id, ib_account_id, ib_login_key, label, trading_mode, status).
- **Constraint (verified during the 2026-06-08 HVP drill):** `/api/v1/account/summary` + `/api/v1/account/portfolio` are **gateway-bound** — they reflect ONLY the single IB account the gateway is currently logged into (`settings.ib_host:ib_port`). They cannot show balances/positions for an arbitrary account the gateway isn't connected to. So an account selector can scope DEPLOYMENT/fleet data freely, but NOT account balances.
- Legacy deployments may have `account_id=null` / `broker_account_id=null` (pre-fleet topology).

## Discussion Log

### Round 1 (2026-06-08) — operator answers

- **Scope:** A **GLOBAL account selector at the top of the MSAI dashboard** — selecting an account re-scopes everything account-related. Backtesting is excluded (account-agnostic). Live-trading dashboard + live-trading page change per account. The operator wants UI-design expertise on the **deploy-target** flow.
- **Behavior:** `"All"` default + single-account filter, **persisted** across navigation/reload.
- **Balances:** Filter deployment/fleet data per-account (real, from `/live/status`); balance cards stay **gateway-bound, clearly labeled "connected account only — <id>"** (no faked per-account balances).
- **NEW concern raised by operator — deploy-target:** "When I deploy a portfolio to an account, I need to select the target. Is it the globally-selected account, or a separate target picker? I need UI-design expertise. The API and CLI should also be able to select the target." → take to council.
- **Source/legacy (Q4):** operator deferred to council.

### Open design questions → COUNCIL (operator-requested)

1. **Deploy-target UX + real-money safety.** Should the single global top-bar account selector serve BOTH as the view-filter context AND the deploy target, or should view-context and deploy-target be **separate** (deploy target explicit + confirmed in the deploy dialog)? Real-money risk: a global selector that implicitly sets the deploy target could deploy to the FUND account by accident (the exact wrong-account class we hit during the 2026-06-08 HVP drill). API/CLI already take `broker_account_id` on `start-portfolio`; the UX is the open part. Must stay consistent across UI/API/CLI.
2. **Selector source + legacy handling.** Populate from registered `/broker-accounts` (+ `"All"`, + an `"Unassigned"` bucket for legacy deployments with `account_id=null`), vs only accounts seen in deployments, vs the union of both.

### Round 2 (2026-06-08) — COUNCIL verdict (5 advisors + Codex chairman, all CONDITIONAL/converged)

**Chairman recommendation:** Global top-bar selector = **viewing scope ONLY**. Deploy target = **separate explicit field** in the deploy dialog, defaulted from the global selector only when it is a single concrete account, requiring explicit confirmation. `All`/`Unassigned` **disable deploy**. UI/API/CLI align: deploy requires explicit `broker_account_id`; no implicit fallback (selector / gateway login / persisted UI state / `all`). Selector population = **union** (registered `/broker-accounts` + accounts in `/live/status` + `All` + `Unassigned`); unknown deployment-only ids shown explicitly as unknown/retired, not hidden.

**Council blocking objections (all folded into scope per operator decision "Full safety scope"):**
1. API rejects absent/`all`/`unassigned`/ambiguous deploy target with 422.
2. Deploy dialog shows human labels, not raw UUIDs (e.g. `REAL FUND - Uxxxx - LIVE MONEY`).
3. **Server-side confirmation gate** for real-money: `confirm_account_id` on `/start-portfolio` must equal the resolved `ib_account_id` (UI-only confirm insufficient; enforced for UI+API+CLI).
4. **`BrokerAccount` gains an explicit account-class / `is_real_money` field**, surfaced in `BrokerAccountResponse` + enforced server-side. Do NOT infer real-money from `ib_account_id` string prefix in the frontend (KEY finding: today LVP+HVP both have `trading_mode=live`; only the id string separates the fund).
5. Unknown deployment account ids render as explicit unknown/retired.
6. `Unassigned` is filter-only; legacy `account_id=null` needs migration or operator-visible handling.
7. Divergence audit/metric when the dialog deploy target ≠ active global selector.

**Operator scope decision (2026-06-08): FULL SAFETY SCOPE** — PR4 ships the view selector + deploy-dialog explicit target + ALL of the backend safety layer above (account-class field, server-side `confirm_account_id` gate, divergence audit). Rationale: shipping a multi-account deploy UI without the server-side intent check would reproduce the exact wrong-account class hit during the 2026-06-08 HVP drill.

## Refined Understanding

### Personas

- **Fleet operator** (primary): manages multiple broker accounts (LVP test, HVP test, real FUND) from the dashboard; needs to focus views per account AND deploy strategies to a chosen account without ever hitting the fund by accident.
- **API integrator / CLI operator**: deploys via `POST /live/start-portfolio` / `msai live start-portfolio`; the safety contract (explicit target + real-money confirm) must hold identically here.

### User Stories (Refined)

- **US-001** — As a fleet operator, I select an account in a global top-bar selector and all account-scoped views (live-trading fleet, dashboard cards) re-scope to it; `All` is the default and my selection persists across navigation/reload. Backtesting is unaffected (account-agnostic).
- **US-002** — As a fleet operator, when I deploy a portfolio I choose the target account in a **separate, explicit, confirmed** field (pre-filled from the selector only when it's a single account); `All`/`Unassigned` disable the Deploy action.
- **US-003** — As any operator (UI/API/CLI), deploying to a **real-money** account requires confirming the account identity server-side (`confirm_account_id` == resolved `ib_account_id`); a mismatch or absent/ambiguous target is rejected (422).
- **US-004** — As a fleet operator, the selector lists the union of registered accounts + accounts seen in deployments + `All` + `Unassigned`, with unmistakable real-money labels and unknown/retired ids shown explicitly.
- **US-005** — As an operator, balance cards remain labeled as the gateway-connected account only (no faked per-account balances), because IB account summary/portfolio are gateway-bound.

### Non-Goals

- Per-account real balances for non-connected accounts (gateway-bound; out of scope — would need gateway-per-account or a snapshot cache).
- Multi-account simultaneous deploy / fan-out (`All` is never a deploy target).
- Backtesting account scoping (account-agnostic).

### Key Decisions

- Global selector = view scope; deploy target = separate explicit confirmed field. (council)
- `is_real_money`/account_class on `BrokerAccount` + server-side `confirm_account_id` gate + divergence audit are IN scope (full safety). (operator)
- Union population + `Unassigned`/unknown-retired visibility; filter-only buckets non-deployable. (council)
- Migration is additive (new nullable/defaulted column) per `rules/database.md`.

### Open Questions (Remaining)

- [ ] Exact real-money confirmation UX in the dialog (typed account-id echo vs explicit checkbox + label) — design phase.
- [ ] Whether `account_class` is a boolean `is_real_money` or an enum (`test`/`real`/`paper`) — design phase; enum is more future-proof.
- [ ] Whether any null-account legacy deployment rows actually exist on prod/dev (council "missing evidence") — verify at implementation.
