# Fix: three live-deploy bugs surfaced by the real-money drill

## Goal

Three discrete bugs surfaced during the 2026-05-13 paper+live drill (post PR #62 + #63). All three sit on the real-money trading path; "no bugs left behind" + single-operator personal-fund design require they ship together. Designs were independently verified by Codex against Nautilus source under `.venv/lib/python3.12/site-packages/nautilus_trader/`.

## Architecture

Touches three different layers:

1. **API contract layer** (Bug #1): `schemas/live.py` ↔ `models/live_deployment.py` disagree on `ib_login_key` nullability.
2. **Stop/teardown integrity** (Bug #2): Nautilus `Strategy.stop()` invokes `market_exit()` when `manage_stop=True` (already set everywhere in MSAI). The gap is at the API: `POST /stop` returns success before verifying the broker is actually flat. Plus a TIF default that conflicts with IB account presets.
3. **Live-deploy snapshot binding** (Bug #3): the PR #63 graduation gate verifies `strategy_id` but not `config`/`instruments`. The frozen revision member can hold parameters that diverge from the approved candidate — currently blocked by a temporary 503 guard. This PR replaces the guard with real binding.

## Tech Stack

- `backend/src/msai/schemas/live.py` — `PortfolioStartRequest`
- `backend/src/msai/api/live.py` — `/start-portfolio` + `/stop` + `/kill-all` + idempotency layer
- `backend/src/msai/services/live/portfolio_service.py` — `_is_graduated` (already in PR #63)
- `backend/src/msai/services/live/portfolio_composition.py` — reference only (its `_canonicalize_member` canonicalizes the FULL member; binding uses an inline `_canonicalize_config` in `snapshot_binding.py` for the config-only shape; see §"Bug #3 step 2")
- `backend/src/msai/services/nautilus/live_node_config.py` — strategy config injection (verify `manage_stop=True` + add `market_exit_time_in_force=DAY` for US equities)
- `backend/src/msai/live_supervisor/process_manager.py` — kill-all path (verify flatness)
- `backend/src/msai/services/nautilus/trading_node_subprocess.py` — child shutdown-finally hook (drains `flatness_pending:{deployment_id}`, reads `node.kernel.cache.positions_open()`, writes `stop_report:{nonce}`)
- `backend/src/msai/models/graduation_candidate.py` — instruments source (TBD: `candidate.config["instruments"]`)
- `backend/src/msai/models/live_portfolio_revision_strategy.py` — member config + instruments source
- Tests for all three layers

## Approach Comparison

| Bug                     | Codex-recommended                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Why over alternatives                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **#1 ib_login_key**     | Make API require it (`str`, not `Optional`). Add to idempotency hash.                                                                                                                                                                                                                                                                                                                                                                                                     | Multi-login routing key is a deliberate operator choice; `settings.tws_userid` doesn't exist so auto-fill is impossible without adding settings; DB-nullable destroys the supervisor's grouping contract; `account_id → login_key` registry is right for the future but out-of-scope today (single-operator personal fund)                                                                                                                                                                      |
| **#2 stop+flatten**     | Keep `manage_stop=True` (Nautilus auto-flattens). Add post-stop deployment-cache flatness verification (Nautilus cache, filtered by member_strategy_id_fulls, reported via Redis from the child shutdown-finally hook). Set `market_exit_time_in_force=DAY` for US equities to avoid IB-preset cancel-fill on GTC.                                                                                                                                                        | Adding `on_stop()` to every strategy is duplicative — Nautilus already does cancel+close when `manage_stop=True` (verified at `trading/strategy.pyx:404-416`). But Nautilus's market_exit can hit `max_attempts` while positions remain (`strategy.pyx:1835-1843`), so the API must verify the deployment-scoped Nautilus cache (account-level broker view is deferred — see §"Bug #2 step 3 No account-level secondary check"). TIF=DAY matches the IB account preset rather than fighting it. |
| **#3 snapshot binding** | Replace the 503 guard with: (1) inline config-only canonical-JSON helper in `snapshot_binding.py` (NOT reusing `portfolio_composition.py`'s `_canonicalize_member` — wrong shape per §"Bug #3 step 2"), (2) strict-set instruments match via `lookup_for_live`, (3) bind to linked candidate or unique `live_candidate` (no orphan `live_running`), (4) 422 with diff on mismatch, (5) check fires on EVERY start (idempotency body_hash includes `binding_fingerprint`). | Council from PR #63 already approved this shape. Codex iter-2 confirmed: warm-restart cached path must re-verify (the guard's whole point). Multiple-eligible-candidates = 422 ambiguity, not pick-newest.                                                                                                                                                                                                                                                                                      |

## Contrarian Verdict

**PRE-DONE** by Codex's deep design review (transcript above this plan in the conversation). Three explicit recommendations cite Nautilus source paths, contradict alternatives, and flag follow-ups. Treating Codex's analysis as the Contrarian Gate verdict per memory's `feedback_skip_phase3_brainstorm_when_council_predone` (council verdict for the gate itself was in PR #63; this PR implements the deferred follow-up identified there).

## Fix Design

### Bug #1: `ib_login_key` required at API

1. **Schema:** `schemas/live.py:31` — change `ib_login_key: str | None = None` to `ib_login_key: str = Field(min_length=1, max_length=64)`.
2. **Idempotency body hash:** `api/live.py:305-310` — add `"ib_login_key": request.ib_login_key`.
3. **Deployment identity (Codex iter-1 P1 #4):** `services/live/deployment_identity.py:236-280` — add `ib_login_key` to the `PortfolioDeploymentIdentity` tuple. Currently a retry with a different login_key reuses the old deployment/gateway (silent footgun on multi-login setups).
4. **UNIQUE(revision_id, account_id) collision (Codex iter-2 P1 #1):** `live_deployments` has `UniqueConstraint("portfolio_revision_id", "account_id", name="uq_live_deployments_revision_account")` (`models/live_deployment.py:53-59`). Changing `ib_login_key` produces a NEW `identity_signature` (now that step 3 adds it to the tuple), but the INSERT collides on `(revision_id, account_id)` because that constraint is independent of `identity_signature`. The `on_conflict_do_update` path at `api/live.py:468-480` keys on `identity_signature`, so a new identity hits the OTHER unique key as an unresolved conflict → IntegrityError.
   - **Decision: reject with 422 `LIVE_DEPLOY_CONFLICT`, do NOT migrate the unique constraint** (Codex iter-3 P2 #1 fix — the previous paragraph mixed 409 and 422; harmonized to 422 here and below). A future migration could relax to `UNIQUE(revision_id, account_id, ib_login_key)` but that's a semantic shift (now two deployments of the same revision into the same account via different logins are allowed — debatable; defer). For this PR: detect the collision BEFORE insert. Logic:
     1. After computing the new `identity_signature` (with login_key), look up existing rows by `(revision_id, account_id)`.
     2. If a row exists with `identity_signature == new_signature` → warm restart, use existing row (covered by current logic).
     3. If a row exists with `identity_signature != new_signature` — **regardless of status** (Codex iter-3 P1 #2: `failed` and `stopped` rows both still hold the UNIQUE slot; a `failed` row can come from `trading_node_subprocess.py:331` terminal-sync writes or supervisor failure at `process_manager.py:675`) → **422 LIVE_DEPLOY_CONFLICT** with `{existing_deployment_id, existing_status, existing_login_key, hint: "an existing deployment of this revision+account exists with a different identity (different ib_login_key, paper_trading, or other identity-bearing field). Archive/delete the existing row OR re-submit with the same identity."}`.
   - **Test:** start D1 with login=k1, send SIGKILL to put it into `failed`; try to start same revision+account with login=k2 → 422 citing the failed row.
5. **API handler:** `api/live.py:451-466` — pass `request.ib_login_key` directly (Pydantic guarantees non-None after schema change).
6. Tests:
   - `POST /api/v1/live/start-portfolio` without `ib_login_key` → 422 (not 500).
   - Idempotency-replay with different `ib_login_key` produces different identity (no cache hit).
   - Identity-signature test: changing `ib_login_key` in the request body changes the signature.

**Operator follow-up note** (out of scope this PR): a future `account_id → login_key` registry would make this derivable, but for now the operator passes it explicitly. Documented in the solution doc.

### Bug #2: stop+flatten verification + TIF fix

1. **TIF fix in `services/nautilus/live_node_config.py`:**
   - When injecting `manage_stop=True` for any strategy targeting a US equity venue (`NASDAQ`, `NYSE`, `ARCA`, `BATS`, etc. — match the existing `IB_PAPER_PORTS`/`IB_LIVE_PORTS` venue inference), also inject `market_exit_time_in_force=TimeInForce.DAY`.
   - For non-US-equity venues (FX, futures), keep default GTC.
   - Test: integration test asserts a freshly-built `TradingNodeConfig` for an AAPL strategy has `manage_stop=True` AND `market_exit_time_in_force=DAY`.

2. **TIF injection in BOTH config builders (Codex iter-1 P2 #4):**
   - Single-strategy: `build_real_node` (`live_node_config.py:393-416`).
   - Multi-strategy: `build_portfolio_trading_node_config` (`live_node_config.py:570-582`) — the path `/start-portfolio` uses.
   - Test asserts both paths get `market_exit_time_in_force=DAY` for US equity venues.

3. **Flatness via per-nonce Redis keys + child shutdown-finally hook (revised per Codex iter-4 P1 #1+#2+#3 + P2 #1+#2):**
   - **Constraints:**
     - Parent ProcessManager has no in-process reference to `trading_node.cache` (Codex iter-2 P1 #2; `live_supervisor/process_manager.py:1004`).
     - Child has no command-bus consumer — only the SIGTERM signal handler that schedules `node.stop_async()` (`trading_node_subprocess.py:580-594`).
     - `_on_sigterm` is a sync `loop.add_signal_handler` callback — it CAN schedule async work but cannot `await` it; reporting must happen elsewhere, namely in the child's `finally` block AFTER `node_run_task` completes (`trading_node_subprocess.py:735-758, 801+`).
     - Child does not have a top-level Redis client; the only existing client is local to the disconnect-handler factory at `:1125`. We must add one (the child's `TradingNodePayload.redis_url` at `:182` is already passed in).
     - Redis Stream fields must be flat-encodable. The bus pattern (`services/live/command_bus.py:277-280`) JSON-encodes each field. Stream + shared consumer group has a load-balancing risk for correlation: caller A's XREADGROUP can receive caller B's entry and ACK it, leaving B to time out.
   - **Design (per-nonce Redis key, no streams, no signal-handler awaits):**
     1. **New supervisor-handled command:** `LiveCommandType.STOP_AND_REPORT_FLATNESS` with `{deployment_id, stop_nonce: str, member_strategy_id_fulls: list[str], requested_at: ISO8601}`. Supervisor `handle_command` (extend `live_supervisor/main.py:63-95`) handles it as follows:
        - `RPUSH flatness_pending:{deployment_id} <json-of-{stop_nonce,member_strategy_id_fulls}>` then `EXPIRE flatness_pending:{deployment_id} 120` (Codex iter-4 P2 #1 fix — per-nonce LIST avoids the singleton-key race where two concurrent stop requests for the same deployment overwrite each other; the list preserves both nonces and the child drains it).
        - Invoke the existing STOP path (ProcessManager → SIGTERM the child).
        - Supervisor ACKs the command after signaling — the API is responsible for awaiting the report (Codex iter-4 P2 #2 fix — `ProcessManager.stop()` at `process_manager.py:918` returns immediately after SIGTERM, so supervisor cannot meaningfully "wait" before ACKing).
     2. **Child-owned Redis client (Codex iter-4 P1 #2 — fixes "no accessible Redis client"):** at the top of `run_trading_node`, construct an aioredis client from `payload.redis_url` and bind it to a local variable. Used by the shutdown-finally hook below. Closed in the finally block AFTER the report is written (or skipped on timeout).
     3. **Child shutdown-finally hook (Codex iter-4 P1 #2 — moves reporting OUT of signal handler):** the existing `finally` block in `run_trading_node` (`trading_node_subprocess.py:801+`) already awaits `node.stop_async()` + `node.dispose()`. Insert a new step AFTER stop_async resolves and BEFORE dispose:
        ```python
        # Drain pending flatness requests (concurrent stops handled).
        # Iterates because two STOP_AND_REPORT_FLATNESS commands could
        # have been published before SIGTERM landed.
        while True:
            entry = await redis.lpop(f"flatness_pending:{deployment_id}")
            if entry is None:
                break
            req = json.loads(entry)
            positions = node.kernel.cache.positions_open()
            my_positions = [
                p for p in positions
                if p.strategy_id.value in req["member_strategy_id_fulls"]
            ]
            report = {
                "stop_nonce": req["stop_nonce"],
                "deployment_id": str(deployment_id),
                "broker_flat": not my_positions,
                "remaining_positions": [
                    {
                        "strategy_id": p.strategy_id.value,
                        "instrument_id": p.instrument_id.value,
                        "quantity": str(p.quantity),
                        "side": str(p.side),
                    }
                    for p in my_positions
                ],
                "reason": "ok" if not my_positions else "max_attempts_exhausted",
                "reported_at": datetime.now(UTC).isoformat(),
            }
            # Per-nonce key (Codex iter-4 P1 #1 — eliminates consumer-group
            # load-balancing risk; no shared stream, no ACK semantics).
            # JSON-encoded value, NOT stream fields (Codex iter-4 P1 #3).
            await redis.set(
                f"stop_report:{req['stop_nonce']}",
                json.dumps(report),
                ex=120,  # 2-min TTL — API only waits 30s, generous slack
            )
        ```
        **The entire drain block is wrapped in `asyncio.wait_for(..., timeout=5.0)` (Codex iter-5 P1 #3)** so an unresponsive Redis (network partition, Redis crash) cannot block shutdown indefinitely. The aioredis client itself is constructed with `socket_connect_timeout=2.0` + `socket_timeout=2.0`. Any `TimeoutError` / `RedisError` / `CancelledError` is logged at `error` level and execution proceeds straight to `node.dispose()` + the terminal status-row write. The API caller sees a 504 timeout on `GET stop_report:{nonce}` (existing behavior) — the operator must check IB portal directly per the timeout hint.
     4. **API publishes the command with a fresh `stop_nonce = uuid4().hex`** (Codex iter-2 P1 #4 fix — `deployment_id` is stable across warm restarts so cannot serve as correlation; the nonce can). After publishing, API polls `GET stop_report:{stop_nonce}` with exponential backoff (50ms → 100ms → 200ms → 400ms → 800ms → 1600ms → 1600ms…) up to 30s. On hit: parse JSON and return. **Do NOT `DEL` the report (Codex iter-6 P2 #2)** — coalesced callers (see step 7) may be polling the same nonce; deleting it would race them into timeouts. The 120s TTL handles cleanup.
     5. **All-member filter (Codex iter-2 P1 #3):** the command payload (and therefore the list-entry payload) carries `member_strategy_id_fulls: list[str]` — child filters `kernel.cache.positions_open()` by membership in that list. A 2-member portfolio is correctly reported as non-flat if EITHER member has positions.
     6. **API timeout 30s on stop, 15s on kill-all.** On timeout return 504 with `{stop_initiated: true, broker_flat: unknown, stop_nonce, hint: "child did not write stop_report within timeout — operator must verify via IB portal and re-issue stop if needed. The Nautilus subprocess may still complete the flatness check; check stop_report:{stop_nonce} manually in Redis if needed."}`. Halt flag remains set if this is the kill-all path.
     7. **Concurrent-stop / already-stopped coalescing (Codex iter-5 P2 + iter-6 P2 corrections):**
        - **Source of truth for "stop in progress" (iter-6 P2 #1):** `LiveDeployment.status` lags reality (stays `running` until terminal sync writes `stopped` at `trading_node_subprocess.py:331` or the API writes it at `api/live.py:764`). The "stopping" flag actually lives on `LiveNodeProcess.status` (`process_manager.py:999`). Don't rely on `LiveDeployment.status` for the in-progress check.
        - **Atomic coalescing primitive:** use Redis `SET inflight_stop:{deployment_id} <stop_nonce> NX EX 60` BEFORE publishing the command. NX gives us atomic test-and-set:
          - If `SET NX` succeeds → this caller is the originator; publish `STOP_AND_REPORT_FLATNESS{stop_nonce}`, then poll `stop_report:{stop_nonce}`.
          - If `SET NX` fails → another caller is in flight; read the existing key value (`GET inflight_stop:{deployment_id}`) → that's the in-flight nonce → poll on that nonce instead.
        - **Report TTL — DO NOT DEL after first read (iter-6 P2 #2):** coalesced callers race on `DEL stop_report:{nonce}`. The first caller would invalidate the report before the second reads it. Solution: rely on the 120s TTL for cleanup; the API NEVER DELs `stop_report:{nonce}`. Slightly higher Redis usage; negligible.
        - **Active states for "publish allowed" (iter-6 P2 #3 + iter-9 P2 source-of-truth fix):** `process_manager.py:940` treats `starting`, `building`, `ready`, `running` as stoppable. **Source of truth is the LATEST `LiveNodeProcess.status` for the deployment**, NOT `LiveDeployment.status` (which lags reality per iter-6). The API must accept all four LiveNodeProcess statuses. So:
          - If latest `LiveNodeProcess.status in {"starting", "building", "ready", "running"}` → proceed (after SET-NX coalescing check above).
          - Else (no active process row, or process is terminal like `stopped`/`failed`/`exited`) → return immediately with `{broker_flat: unknown, stop_nonce: null, hint: "deployment has no active subprocess — operator must verify flatness via IB portal. The child has exited; no live cache available."}`. Do not publish.
        - **`/kill-all` operates on active deployments** (anything matching the four-status active set above), not just `running`.
   - **No account-level secondary check in this PR (Codex iter-3 P2 #2):** see Deferred follow-ups.

4. **`/kill-all` extension (Codex iter-1 P1 #1+#2 + Codex iter-4 P2 #2 wording fix):**
   - API `/kill-all` iterates over **active deployments** (those whose latest `LiveNodeProcess.status` is in `{"starting","building","ready","running"}` — same set as step 7, matching `process_manager.py:940`) and publishes one `STOP_AND_REPORT_FLATNESS` per deployment (each with its own `stop_nonce`). The supervisor handler RPUSHes the per-deployment list and SIGTERMs the child (single SIGTERM per child, even if multiple commands stack — child drains the list).
   - **The supervisor does NOT wait for the report; it ACKs after signaling** (`ProcessManager.stop()` at `process_manager.py:918` returns post-SIGTERM). The API is the only collector — it polls `stop_report:{stop_nonce}` per dispatched command, in parallel, with a 15s total deadline.
   - Any deployment whose key never materializes within 15s → return `{deployment_id, broker_flat: unknown, stop_nonce}` in the aggregated `/kill-all` response. No supervisor-side "fallback SIGTERM" — SIGTERM was already sent at publish time.
   - Aggregated response includes per-deployment flatness; if ANY non-flat OR `unknown` → operator MUST manually flatten via IB portal before `/resume`. Risk halt flag stays set.

5. **Strategy classes left alone (verified via Nautilus source):**
   - `Strategy.stop()` in Nautilus 1.225+ calls `market_exit()` when `StrategyConfig.manage_stop=True` (`trading/strategy.pyx:404-416`); `market_exit()` calls `cancel_all_orders()` + `close_all_positions()` (`trading/strategy.pyx:1773-1799`).
   - MSAI already injects `manage_stop=True` for every strategy (`live_node_config.py:393-416` single, `:570-582` portfolio).
   - Adding `on_stop()` to individual strategies would duplicate Nautilus's built-in behavior. Per Codex: leave them alone.
   - The example `ema_cross.py` has its own `on_stop()` which is fine (acceptable but partly duplicative — not regression-worthy).

### Bug #3: snapshot binding

1. **Remove the 503 guard** at `api/live.py:270-299` (the temporary `LIVE_DEPLOY_BLOCKED` block).

2. **Add the binding check** in a new helper `services/live/snapshot_binding.py`:

   ```python
   _DEPLOY_INJECTED_FIELDS: frozenset[str] = frozenset({
       "manage_stop", "order_id_tag", "market_exit_time_in_force"
   })
   _COMPARISON_STRIPPED_FIELDS: frozenset[str] = _DEPLOY_INJECTED_FIELDS | frozenset({"instruments"})

   def verify_member_matches_candidate(
       member: LivePortfolioRevisionStrategy,
       candidate: GraduationCandidate,
   ) -> None:
       """Raises BindingMismatchError(field, member_value, candidate_value) if
       config (minus deploy-injected fields) OR instruments diverge."""
   ```

   - **Canonical-JSON comparison (Codex iter-2 P2 #1 correction):** the actual helper in `services/live/portfolio_composition.py:53` is `_canonicalize_member` — it canonicalizes the FULL member (strategy_id, order_index, config, instruments, weight), not just config. Wrong shape for binding comparison. **Decision: inline a small config-only canonicalizer in `services/live/snapshot_binding.py`:**
     ```python
     def _canonicalize_config(config: dict[str, Any]) -> str:
         """sort_keys + separators yields a deterministic round-trip
         string. Decimal/UUID/datetime not expected in strategy config —
         if encountered, json.dumps raises TypeError and we surface
         that as a 500 (config has an unserializable value)."""
         return json.dumps(config, sort_keys=True, separators=(",", ":"))
     ```
     One unit test pins: `{"a":1,"b":{"c":2,"d":3}}` and `{"b":{"d":3,"c":2},"a":1}` produce identical strings. No reuse of `_canonicalize_member` — the shapes don't match.
   - **Strip deploy-injected fields before config comparison:** `manage_stop`, `order_id_tag`, `market_exit_time_in_force` (added in Bug #2 fix). MSAI injects these at deploy time, not strategy-design time. Strip from BOTH sides (member.config and candidate.config) before canonicalization.
   - **Strip `instruments` from config comparison (Codex iter-1 P2 #2):** member has BOTH `member.config` AND `member.instruments` (separate column). If we leave `instruments` in config comparison we either (a) double-count (config and column both checked) or (b) get spurious diffs when config has different `instruments` key than the column. Compare `member.instruments` to candidate's instruments separately (see below).
   - **Instruments source from candidate (Codex iter-5 P1 #2 fix):** `GraduationCandidate` has NO `instruments` column (`models/graduation_candidate.py:43` — only `config: JSONB`). Promotion at `api/research.py:280` / `:296` copies `job.best_config` / `trial.config`, but research best configs are built as `{**base_config, **params}` at `services/research_engine.py:636` and **instruments are a separate top-level request field, not in `base_config`**. So `candidate.config["instruments"]` is empirically absent for research-graduated candidates.
     - **Fix in this PR (Codex iter-6 P1 correction):** `GraduationService` does NOT expose a `promote()` method — only `create_candidate()` and `update_stage()` (`services/graduation.py:79`). Stamp instruments **in the API caller** (`api/research.py:280` and `:296`) BEFORE the `create_candidate()` call: read `job.config["instruments"]` (or the trial's resolved instruments) and inject into the `config` dict that gets handed to the service. This keeps `GraduationService` decoupled from instrument semantics. Update the existing integration test (`tests/integration/test_research_flow.py:279` — asserts candidate config exact-match) to expect the stamped `instruments` key; add explicit tests for both best-result and trial-index promotion paths.
     - **For existing candidates without `instruments` in config:** reject with 422 `BINDING_INSTRUMENTS_MISSING` + guidance ("candidate predates the snapshot-binding contract; re-graduate or run the one-shot backfill script"). Provide a small backfill script at `scripts/backfill_candidate_instruments.py` that reads the promotion source (research_job + trial_id) and rewrites `candidate.config["instruments"]`. Documented; operator runs once at deploy time.
     - **Not adding a dedicated `instruments` column on `GraduationCandidate` in this PR** — would be cleaner but requires migration + dual-write + deprecation. Deferred (already in Deferred follow-ups).
   - **Instruments canonicalization — use the live path (Codex iter-2 P2 #3 correction):** previous draft said `SecurityMaster.resolve_for_backtest`. For LIVE deploy that's wrong: the supervisor's live spawn uses `lookup_for_live` (`live_supervisor/__main__.py:304-310`, signature in `services/nautilus/security_master/live_resolver.py:447`). For futures rolls / alias drift, `resolve_for_backtest` (historical-window aware) and `lookup_for_live` (current-as-of-today) can return different alias strings — binding must use the LIVE resolution because that's what the subprocess will actually trade. Pass `as_of_date=exchange_local_today()` (memory `feedback_alias_windowing_must_use_exchange_local_today` — UTC vs Chicago boundary). Caveat: `lookup_for_live` is async + IB-aware; for binding we want a pure local lookup (no IB qualification). If `lookup_for_live` mandates IB qualification, extract the local-registry-only path (`security_master.SecurityMaster.lookup_for_live` likely has this — verify at implementation; if not, add a `lookup_for_live_local(symbols, as_of_date)` helper).
   - **Instruments comparison:** strict sorted-set equality after canonicalization. Set semantics, not list order.

3. **Replace the eligibility check** in `api/live.py` (called for each member):
   - For each member in the frozen revision:
     - Query candidates: `WHERE strategy_id = member.strategy_id AND stage IN ELIGIBLE_FOR_LIVE_PORTFOLIO`.
     - **Linked candidate (warm restart):** if any candidate's `deployment_id == this_deployment.id`, use it. (Re-verify binding even on warm restart — guards against operator editing the member config + redeploying with the same revision id, although the frozen-revision invariant should prevent that... still cheap to re-check.)
     - **First deploy:** require EXACTLY one candidate at `live_candidate` stage with `deployment_id IS NULL`. If multiple → 422 ambiguity (explicit error naming the candidate ids; operator must archive the duplicates or open a follow-up to define a tie-breaker).
     - **Not eligible:** zero candidates → 422 "strategy not graduated."
   - Call `verify_member_matches_candidate(member, candidate)`. On mismatch → 422 with `{error.code: "BINDING_MISMATCH", error.details: [{field, member_value, candidate_value}, ...]}`.
   - All match → proceed to deploy.

4. **Fix the stage-transition logic at `api/live.py:572-581` (Codex iter-1 P2 #3):**
   - Currently queries `stage == "live_running" AND deployment_id IS NULL` — looking for an orphan.
   - Replace with: for each member, **in the SAME DB session** as the deployment row create, link `candidate.deployment_id = deployment.id` AND transition the stage via `GraduationService.update_stage()` (NOT a re-entry into the API):
     - `live_candidate → live_running` for first deploy.
     - `paused → live_running` for resume.
     - `live_running → live_running` no-op for restart-of-already-running (don't transition, just verify linked).
   - **Do the link+transition BEFORE publishing START to the command bus** — otherwise a published START with no candidate linked is a stale audit row.
   - **Do NOT swallow link failures** — currently the link block has `except Exception: log.warning("graduation_candidate_link_failed", exc_info=True)`. Replace with hard failure: roll back the deployment row, raise 500, log details.

5. **Idempotency interaction (Codex iter-1 P1 #3 + Codex iter-3 P1 #3) — CRITICAL:**
   - **Problem A (cache bypass):** the binding check happens AFTER `idem.reserve(...)`. If a CachedOutcome exists from a previous successful start, the API returns the cached outcome without re-checking the binding. Frozen revisions are immutable on the MEMBER side — but the CANDIDATE side can drift (someone re-graduates with different config; archives a candidate; promotes another).
   - **Problem B (warm-restart fingerprint stability — Codex iter-3 P1 #3):** the candidate-lookup query `WHERE strategy_id = ? AND stage IN ELIGIBLE_FOR_LIVE_PORTFOLIO` returns multiple candidates after first deploy (the `live_running` row from D1 plus any new `live_candidate` rows for the same strategy). Without identity-context, a replay can pick a DIFFERENT candidate than the first call and compute a different fingerprint → idempotency bypassed every time.
   - **Pre-reserve sequence (extends `api/live.py` BEFORE `idem.reserve` at :301):**
     1. Compute `identity_signature` from request body (revision_id, account_id, ib_login_key, paper_trading).
     2. Query existing deployment by `identity_signature` (warm-restart lookup). If exists, capture `existing_deployment.id` for downstream filtering.
     3. Load the frozen revision + its `LivePortfolioRevisionStrategy` members.
     4. For each member, resolve the bound candidate:
        - If `existing_deployment` is set: pick `candidate WHERE strategy_id=member.strategy_id AND deployment_id=existing_deployment.id` (deterministic warm restart). If none found → **hard-fail with 422 `LIVE_DEPLOY_REPAIR_REQUIRED`** (Codex iter-4 P1 #4 fix — falling back to the first-deploy eligibility query would let a replay rebind the existing deployment to a DIFFERENT candidate, reintroducing the unstable-fingerprint problem this whole sequence is supposed to prevent). Hint to operator: "deployment row exists but no linked candidate found — likely the candidate was archived after first deploy; operator must restore the candidate or archive the deployment row before re-deploying."
        - Else (first deploy): query `candidate WHERE strategy_id=member.strategy_id AND stage IN {"live_candidate"} AND deployment_id IS NULL`. Reject 422 if zero or >1.
     5. **Eligibility guard (Codex iter-4 P1 #5):** verify each resolved candidate's `stage IN ELIGIBLE_FOR_LIVE_PORTFOLIO` (`{"live_candidate", "live_running", "paused"}`). If a linked candidate has drifted to `archived` or any other non-eligible stage → reject 422 `BINDING_INELIGIBLE` BEFORE computing the fingerprint. (Why before: the fingerprint excludes stage by design — `live_candidate → live_running` must produce the same hash so warm-restart replays cache-hit. But `archived` invalidates the binding entirely and must surface immediately, not get cached.)
     6. Compute `binding_fingerprint` (below).
     7. Append fingerprint to body_hash → `idem.reserve(...)`.
   - **Solution to Problem A:** include `binding_fingerprint` in idempotency body_hash, computed from **stable content** that does NOT mutate on successful binding (Codex iter-2 P2 #2 fix — the previous draft used `candidate.updated_at`, which step 4 mutates when transitioning stage + setting `deployment_id`; that mutation would change body_hash on the very next replay and trigger a spurious body-mismatch 409):
     ```python
     # Per-member contribution (stable across binding state transitions).
     # Both strip + canonicalize use the SAME helper as the binding
     # verifier (Codex iter-6 P2 #4) — strip_for_comparison removes
     # _COMPARISON_STRIPPED_FIELDS (deploy-injected fields + "instruments"
     # — instruments are checked separately as a sorted set).
     member_part = "|".join([
         str(member.id),
         _canonicalize_config(strip_for_comparison(member.config)),
         "|".join(sorted(member.instruments_canonical)),  # via lookup_for_live
         str(candidate.id),
         # Hash of the candidate's binding-relevant content rather than
         # its updated_at. Stable through stage transitions; changes only
         # when an operator re-graduates / edits the candidate config.
         sha256(
             _canonicalize_config(strip_for_comparison(candidate.config)).encode()
             + b"|"
             + "|".join(sorted(candidate_instruments_canonical)).encode()
         ).hexdigest(),
     ])
     binding_fingerprint = sha256(
         "||".join(member_parts).encode()
     ).hexdigest()
     ```
   - Compute this BEFORE `idem.reserve(...)`. If the candidate is re-graduated (config diff) or its instruments change, the candidate-content-hash changes → body_hash changes → idempotency treats it as a new request → binding is re-checked. If the candidate is merely advanced from `live_candidate` to `live_running` by THIS request, the hash is unchanged → replay returns the cached outcome correctly.
   - **Tradeoff:** computing the fingerprint requires reading members + candidates BEFORE idempotency reservation. That's a DB read on every replay — acceptable cost (<10ms for the typical 1-3 member portfolio).
   - **Alternative considered:** re-verify the binding INSIDE the cached-outcome path. Rejected because it requires duplicating the binding logic in two code paths (cached and non-cached). Adding to body_hash is the cleaner factoring.

## E2E Use Cases

Project type: **fullstack** (API + UI). All UCs are API-first.

### UC1 — Bug #1: missing `ib_login_key` returns 422 not 500

**Intent:** A client omitting `ib_login_key` gets a validation error, not an internal server error.

**Interface:** API.

**Setup:** Existing graduated portfolio revision (any).

**Steps:** `POST /api/v1/live/start-portfolio` with body missing `ib_login_key`.

**Verification:** Status 422; response body contains `"ib_login_key"` field error + clear message.

**Persistence:** Negative — no `live_deployments` row created.

### UC2 — Bug #2: stop returns broker-flat status

**Intent:** After deploying a strategy that takes a position, `POST /stop` waits for the broker to actually be flat (Nautilus's `market_exit` to complete) before returning success.

**Interface:** API + operational (requires broker).

**Setup:** Real-money 1-share smoke deploy on test-lvp (the same drill scenario from this session).

**Steps:**

1. Deploy smoke strategy + observe fill (position=1).
2. `POST /api/v1/live/stop`.

**Verification:**

- Response includes `broker_flat: true` and `remaining_positions: []`.
- IB position for the strategy's instrument is 0 (verified via `/api/v1/account/portfolio`).
- Elapsed time between stop request and response is in {1–30s range}.

**Persistence:** Deployment row `status=stopped`; no remaining open orders or positions.

### UC3 — Bug #3: live deploy rejects member with divergent config

**Intent:** A portfolio member whose `config` differs from the approved candidate's `config` is rejected with a clear diff, not silently deployed.

**Interface:** API.

**Setup:**

1. Graduate smoke_market_order with `config={instrument_id, bar_type, manage_stop=False, instruments=["AAPL.NASDAQ"]}` to `live_candidate`.
2. Add the strategy to a live portfolio with `config={instrument_id, bar_type, fast_ema_period=99, instruments=["AAPL.NASDAQ"]}` (extra field `fast_ema_period`).
3. Snapshot the revision.

**Steps:** `POST /api/v1/live/start-portfolio` with the frozen revision.

**Verification:**

- Status 422.
- Body contains `"BINDING_MISMATCH"` error code.
- Body includes a diff naming `fast_ema_period` as the divergent field.

**Persistence:** No deployment row created.

### UC4 — Bug #3 happy path: matching config deploys successfully

**Intent:** When member config matches candidate config (modulo MSAI-injected fields), live deploy succeeds and the 503 guard is gone.

**Interface:** API.

**Setup:** Graduate smoke with config X; add member with config X (no divergence); snapshot.

**Steps:** `POST /api/v1/live/start-portfolio` with `paper_trading=false` + valid `ib_login_key`.

**Verification:**

- Status 200/201 with deployment id (NOT 503).
- Candidate's `deployment_id` is set, stage transitioned `live_candidate → live_running`.

**Persistence:** Deployment row visible at `/status`; candidate row linked.

## Implementation Order

1. **Layer A: Bug #1 (RED → GREEN).** Smallest scope. Update schema + idempotency + tests.
2. **Layer B: Bug #2 — TIF fix.** Inject `market_exit_time_in_force=DAY` for US equities in live_node_config. Unit tests.
3. **Layer C: Bug #2 — flat verification (per-nonce Redis key + child shutdown-finally hook).** Extend `LiveCommandType` with `STOP_AND_REPORT_FLATNESS`. API gates publish on `LiveNodeProcess.status IN {"starting","building","ready","running"}` (NOT `LiveDeployment.status`, which lags reality) and uses atomic `SET inflight_stop:{deployment_id} <nonce> NX EX 60` to coalesce concurrent stops. Supervisor handler `RPUSH flatness_pending:{deployment_id} <json>` (60-120s EXPIRE), then triggers existing STOP-via-SIGTERM, ACKs after signaling. Child constructs its own aioredis client at run_trading_node start (using existing `payload.redis_url`, with `socket_connect_timeout=2.0`/`socket_timeout=2.0`); in the existing `finally` block at `trading_node_subprocess.py:801+`, AFTER `node.stop_async()` resolves and BEFORE `node.dispose()`, drains the list (`LPOP` loop) and writes each report as JSON-encoded `SET stop_report:{nonce} ... EX=120` — entire block wrapped in `asyncio.wait_for(..., timeout=5s)` to never block shutdown. API publishes the command + polls `GET stop_report:{stop_nonce}` with exponential backoff up to 30s; on hit parses JSON and returns (no DEL — 120s TTL handles cleanup; coalesced readers may be polling the same nonce). `/kill-all` iterates over **active deployments** (same four-status set above; not just `running`), publishes one command per deployment, polls in parallel with 15s total deadline. Tests: unit (supervisor handler with fake Redis + ProcessManager), unit (child finally-block drain with stubbed node.kernel.cache + fakeredis), unit (API SET-NX coalescing with two simultaneous /stop callers — both return same outcome), integration (real Redis round-trip API → supervisor → child via subprocess fixture), drill-equivalent E2E against a live paper session.
4. **Layer D: Bug #3 — snapshot binding.** New `services/live/snapshot_binding.py`. Wire into `/start-portfolio`. Replace the 503 guard. Fix the stage-link query.
5. **Layer E:** Solution doc + CHANGELOG + UC graduation + commit + push + PR.

## Plan Review

To be filled by Codex iter-1 review of THIS document.

## Deferred follow-ups (post-merge)

- **`ib_login_key` derivation** via `account_id → login_key` registry — when MSAI supports multi-login per-account routing.
- **Nautilus multi-account exec_clients** — currently MSAI builds `{IB: one_exec_client}`; future scope is `{IB: {acct_a: client_a, acct_b: client_b}}` per Nautilus PR #3194.
- **Account-preset detection** — query IB for the account's order TIF preset at deploy time, instead of hardcoding `market_exit_time_in_force=DAY`. Defer until a non-DAY-preset account appears.
- **Candidate instruments dedicated column** — currently sourced from `candidate.config["instruments"]`. A dedicated `instruments` column with foreign keys to `instrument_definitions` would be cleaner. Defer to a later migration.
- **`GraduationCandidate.config_schema_version`** — version field on the snapshot for forward-compat. Defer until first schema change.
- **Operator-visible account drift detector** — secondary "account-level" cross-check that queries `IBAccountService.get_portfolio(login_key=..., account=...)` (requires extending the service constructor/method signature to accept login routing AND return `account`/`primaryExchange`/`exchange`/`currency` on each row) and surfaces "account holds positions in this instrument from OUTSIDE the deployment" as a warning on `/stop` response. Defer to a dedicated PR — the Nautilus-cache view (this PR) is authoritative for deployment ownership.
