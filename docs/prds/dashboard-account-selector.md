# PRD: Dashboard Account Selector

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-06-08
**Last Updated:** 2026-06-08

---

## 1. Overview

MSAI now manages multiple Interactive Brokers accounts (low-value test LVP, high-value test HVP, and eventually the real FUND account). The dashboard currently shows all live deployments in one flat view and a single gateway-bound account summary, with no way to focus on one account. This feature adds a **global account selector at the top of the dashboard** that re-scopes all account-related views to the chosen account, plus a **separate, explicit, confirmed deploy-target** mechanism (with a server-side real-money confirmation gate) so an operator can never deploy a strategy to the wrong account — especially the fund — by accident. It is the fleet-management front-end that pairs with PR5 (per-account hard caps).

## 2. Goals & Success Metrics

### Goals

- Let a fleet operator focus the dashboard (live-trading fleet view + dashboard cards) on a single broker account, with an "All" default that preserves today's view.
- Make deploying a portfolio to a specific account **explicit and confirmed** — never implicitly inherited from ambient UI state — consistent across UI, API, and CLI.
- Make a real-money account **machine-distinguishable and visually unmistakable**, with a server-side intent check that blocks a wrong-account real-money deploy.

### Success Metrics

| Metric | Target | How Measured |
| ------ | ------ | ------------ |
| Wrong-account deploys | 0 | No deploy reaches an account other than the operator's explicitly-confirmed target (server-side `confirm_account_id` gate; divergence audit) |
| Account context persistence | 100% | Selected account survives navigation + reload (re-request through the UI) |
| Surface consistency | UI = API = CLI | The same explicit-target + real-money-confirm contract holds on all three surfaces |

### Non-Goals (Explicitly Out of Scope)

- ❌ Per-account **real balances** for accounts the IB gateway is not connected to — `/account/summary` + `/account/portfolio` are gateway-bound; balance cards stay labeled "connected account only". (Would need gateway-per-account or a snapshot cache — future.)
- ❌ Multi-account simultaneous deploy / fan-out — "All" is never a deploy target.
- ❌ Account scoping for **backtesting** — backtests are account-agnostic.
- ❌ Broker-account CRUD changes (registration/rotation/archive already exist in `/settings/broker-accounts`).

## 3. User Personas

### Fleet Operator (primary)

- **Role:** Operates MSAI across multiple IB accounts (LVP test, HVP test, real FUND) from the web dashboard.
- **Permissions:** Full — can view all accounts, deploy/stop strategies, trigger kill-all.
- **Goals:** Focus views on one account; deploy a portfolio to a chosen account without ever hitting the fund by accident.

### API / CLI Operator

- **Role:** Drives deploys programmatically via `POST /api/v1/live/start-portfolio` or `msai live start-portfolio`.
- **Permissions:** Same capability as the UI operator.
- **Goals:** The same explicit-target + real-money-confirmation safety contract must hold identically on API and CLI (no surface is a weaker door).

## 4. User Stories

### US-001: Global account selector re-scopes account views

**As a** fleet operator
**I want** to pick an account in a global top-bar selector and have all account-related views re-scope to it
**So that** I can focus on one account's fleet without manually filtering each view.

**Scenario:**

```gherkin
Given I am signed in with multiple registered broker accounts
And the selector defaults to "All accounts"
When I choose "HVP (U4715997)" in the top-bar selector
Then the live-trading fleet view shows only HVP deployments
And the dashboard deployment cards reflect only HVP
And backtesting views are unaffected
```

**Acceptance Criteria:**

- [ ] A global selector is visible at the top of the dashboard on all account-scoped pages.
- [ ] Default is "All accounts" (current flat behavior preserved).
- [ ] Selecting an account filters the live-trading fleet view + dashboard deployment cards to that account.
- [ ] Selection persists across navigation and a full page reload.
- [ ] Backtesting pages are not affected by the selector.

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| No registered accounts | Selector shows "All" only; views behave as today |
| Selected account later archived | Selector falls back to "All"; no crash |

**Priority:** Must Have

### US-002: Deploy target is a separate, explicit, confirmed field

**As a** fleet operator
**I want** to choose the deploy target account in a separate explicit field in the deploy dialog
**So that** I never deploy to an account just because the view happened to be scoped to it.

**Scenario:**

```gherkin
Given the global selector is set to a single account
When I open the deploy-portfolio dialog
Then the target-account field is pre-filled with that account but editable
And I must confirm the target before the Deploy action is enabled
When the global selector is set to "All accounts" or "Unassigned"
Then the Deploy action is disabled until I explicitly pick a single account
```

**Acceptance Criteria:**

- [ ] The deploy dialog has an explicit target-account field, visually distinct from the global selector.
- [ ] The field pre-fills from the global selector ONLY when it is a single concrete account.
- [ ] "All accounts" / "Unassigned" disable the Deploy action and prompt for an explicit pick.
- [ ] The field shows a human label + account id + live/real-money badge — never a bare UUID.

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Operator clears the field | Deploy disabled |
| Target is a real-money account | Extra confirmation required (US-003) |

**Priority:** Must Have

### US-003: Real-money deploy requires server-side identity confirmation (UI/API/CLI)

**As any** operator (UI, API, or CLI)
**I want** deploying to a real-money account to require confirming the account identity, enforced server-side
**So that** a wrong-account real-money deploy is impossible regardless of which surface I use.

**Scenario:**

```gherkin
Given a portfolio revision and a real-money target account
When I deploy with a confirmation token that does NOT match the resolved account identity
Then the server rejects it with a 422 and an explanatory error
When I deploy with a confirmation that matches the resolved ib_account_id
Then the deploy proceeds
And deploying with an absent / "all" / "unassigned" / ambiguous target is rejected with 422
```

**Acceptance Criteria:**

- [ ] `POST /api/v1/live/start-portfolio` requires a `confirm_account_id` (or equivalent) that must equal the resolved `ib_account_id` when the target is real-money; mismatch → 422.
- [ ] Absent / "all" / "unassigned" / ambiguous deploy target → 422 with an actionable error.
- [ ] The CLI surfaces the same confirmation requirement (it already prompts a typed real-money confirm naming the resolved target — extend to echo the identity check).
- [ ] The UI deploy dialog satisfies the same gate (UI-only confirmation is NOT sufficient — the server enforces it).

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Target is a test (non-real-money) account | Standard confirm, no identity echo required (but explicit target still required) |
| `confirm_account_id` present but target is "all" | 422 (no single resolved identity) |

**Priority:** Must Have

### US-004: Account is machine-distinguishable as real-money; selector population is the honest union

**As a** fleet operator
**I want** the selector to list every account I might act on, with the fund unmistakably marked
**So that** I always know which account I'm viewing/targeting and the fund is never mistaken for a test account.

**Scenario:**

```gherkin
Given registered accounts and accounts present in deployments
When I open the global selector
Then it lists the union: "All" + registered accounts + accounts seen in deployments + "Unassigned"
And a real-money account is labeled unmistakably (e.g. "REAL FUND - Uxxxx - LIVE MONEY")
And an account id seen only in a deployment but not registered shows as "Unknown/retired account <id>"
```

**Acceptance Criteria:**

- [ ] `BrokerAccount` gains an explicit account-class / `is_real_money` attribute (NOT inferred from `ib_account_id` string prefix in the frontend), surfaced in `BrokerAccountResponse`.
- [ ] Selector population = union of registered accounts + accounts in `/live/status` + "All" + "Unassigned".
- [ ] Real-money accounts are visually + textually unmistakable across UI/API/CLI.
- [ ] Unknown/retired deployment-only account ids are shown explicitly, not folded into "All".
- [ ] "All" and "Unassigned" are filter-only (never deployable).

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Legacy deployment with `account_id = null` | Grouped under "Unassigned" (filter-only) |
| Account in deployment not in registry | Shown as "Unknown/retired account <id>" |

**Priority:** Must Have

### US-005: Balances stay honestly gateway-bound

**As a** fleet operator
**I want** balance cards to clearly state they reflect only the gateway-connected account
**So that** I'm never misled into thinking I'm seeing a non-connected account's real balances.

**Scenario:**

```gherkin
Given the IB gateway is connected to one account
When I select a different account in the global selector
Then deployment/fleet data re-scopes to the selected account
But balance cards remain labeled "connected account only — <id>"
And no per-account balances are fabricated for non-connected accounts
```

**Acceptance Criteria:**

- [ ] Balance cards (net liq, positions) are labeled with the connected gateway account id.
- [ ] Selecting a non-connected account does NOT change/fabricate balance numbers.
- [ ] The label makes the gateway-bound nature obvious to the operator.

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Gateway disconnected | Balance cards show unavailable, clearly (not stale-as-current) |

**Priority:** Should Have

## 5. Constraints & Policies

### Business / Compliance Constraints

- The real FUND account must never be traded by accident (it is untouched until post-PR-3); a wrong-account real-money deploy is the highest-severity failure this feature must prevent.

### Platform / Operational Constraints

- `/account/summary` + `/account/portfolio` are **gateway-bound** (reflect only the single IB account the gateway is logged into). Per-account balances for non-connected accounts are not available.
- IB allows **one live session per login** — the platform does not run multiple gateways simultaneously for the same login.
- DB migration must be **additive-only** (new nullable/defaulted column) per `rules/database.md` (deploy rolls back image SHAs but not schema).

### Dependencies & Required Integrations

- **Requires:** the multi-account broker fleet (`BrokerAccount` registry, `broker_account_id` on `/start-portfolio`) — already shipped.
- **Named integrations:** Interactive Brokers account identity (`ib_account_id`) is the canonical account key.

## 6. Security Outcomes Required

- **Who can access what:** operators act on accounts they can see; this PR does not add per-account RBAC (single-operator platform) but must not weaken existing auth.
- **What must never happen:** a strategy deployed to an account the operator did not explicitly confirm — enforced **server-side**, identically for UI/API/CLI.
- **What must be auditable:** every deploy records the resolved target account; a divergence between the active global selector and the actual deploy target is audited/metriced.
- **What must never be inferred unsafely:** real-money status must come from an explicit data field, never from frontend string-parsing of the account id.

## 7. Open Questions

- [ ] Real-money confirmation UX in the dialog: typed account-id echo vs explicit checkbox + unmistakable label (design phase).
- [ ] `account_class` as a boolean `is_real_money` vs an enum (`test` / `real` / `paper`) — enum is more future-proof (design phase).
- [ ] Whether any `account_id = null` legacy deployment rows actually exist on dev/prod (verify at implementation — council "missing evidence").
- [ ] Whether the global selector belongs in a shared app shell/header component or per-page (design phase; favor shared for consistency).

## 8. References

- **Discussion Log:** `docs/prds/dashboard-account-selector-discussion.md`
- **Council verdict:** recorded in the discussion log (Round 2, 5 advisors + Codex chairman, 2026-06-08)
- **Related:** PR5 (per-account hard caps + fleet ledger); broker-fleet PR 1/1b; the 2026-06-08 HVP drill (wrong-account failure class this feature prevents)

---

## Appendix A: Revision History

| Version | Date       | Author        | Changes     |
| ------- | ---------- | ------------- | ----------- |
| 1.0     | 2026-06-08 | Claude + Pablo | Initial PRD |

## Appendix B: Approval

- [ ] Product Owner approval
- [ ] Technical Lead approval
- [ ] Ready for technical design
