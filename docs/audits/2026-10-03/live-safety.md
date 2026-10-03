# Live execution, account portfolios, monitoring, and safety assessment

Audit date: 2026-10-03. Source revision: `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. Assessment only; no production-code changes. Installed Nautilus version used for the contract probe: **1.223.0**. This report assesses that installed implementation, not the newest upstream documentation.

The system has substantial account isolation, revision binding, halt enforcement, command recovery, and subprocess lifecycle machinery. The highest-priority work is correctness at the boundaries between that machinery and Nautilus: actual engine events do not match the projection translator, position projections lose essential identity and direction, portfolio weights do not reach live sizing, and several named risk controls remain unwired. Successful component tests therefore do not establish a trustworthy deploy-and-monitor workflow yet.

## Scope and method

Read `docs/agent-context.md` and `.forge/instructions.md` completely before project work. Inspected current live API, portfolio/account services and models, supervisor/command/heartbeat/recovery code, Nautilus node configuration and subprocess integration, halt/risk strategies, projection and order/trade persistence, relevant tests, and installed Nautilus source. Parent audit owns authentication/product analysis and external dependency comparison.

All execution was offline, using the existing `backend/.venv`. No services were started; no broker, vendor, cloud, credential store, real Redis, or real PostgreSQL was contacted; no migrations or trading actions were performed. Tests were run from an empty temporary working directory to avoid the application's automatic `.env` loading. Python network connections were blocked in the validation process. The test scope was selected after inspecting fixtures.

Evidence labels:

- **Reproduced offline:** observed against pure Python/in-memory objects or the installed engine without broker/service access.
- **Confirmed source behavior:** directly supported by the cited current control/data flow, without exercising the deployed environment.
- **Conditional operational risk:** the implementation supports the stated failure sequence, but this audit did not reproduce the infrastructure/broker event or establish that it has ever happened.

File references below are repository-relative unless they explicitly begin with the installed dependency directory. Line numbers identify the inspected source revision. Severity reflects the intended research → backtest → deploy → monitor product; no actual trading loss or production incident is asserted.

## Capability and authority map

| Capability | Current implementation and authoritative state | Assessment |
| --- | --- | --- |
| Broker account lifecycle | Account creation/credential rotation coordinate database state with credential storage and cleanup: `backend/src/msai/services/live/broker_account_service.py:233`, `:483`. Archive rejects active deployments: `:574`. | Keep. Account-specific process isolation is useful even for a few partners. Credential operations were inspected, never invoked. |
| Gateway routing | `backend/src/msai/services/live/gateway_router.py:39` validates explicit account routes and rejects duplicate host/port paths through `:165`. | Keep explicit routing and validation. |
| Immutable portfolio revisions | Draft lock, composition hash, deduplication, and freeze in `backend/src/msai/services/live/revision_service.py:87`, `:114`, `:127`, `:142`; strategy mutation locks in `backend/src/msai/services/live/portfolio_service.py:137`. | Keep. This is a useful deployment identity boundary. A frozen allocation is not yet proof of effective live sizing. |
| Start/bind | `backend/src/msai/api/live.py:1531` reads credentials before the final transaction; `:1630` locks/rechecks revision and account; `:1991` binds candidate under locks; `:2234` polls committed deployment state. | Good race-conscious orchestration. Large procedural implementation needs clearer ownership boundaries. |
| Durable command transport | `backend/src/msai/services/live_command_bus.py:394` creates consumer groups before publishing; `:539` consumes; `:626` reclaims pending deliveries with retry/DLQ handling. `backend/src/msai/live_supervisor/main.py:238` isolates dispatch failures and acknowledges successful handling at `:285`. | Keep at-least-once delivery plus idempotent handlers. Durability still depends on Redis configuration. |
| Per-account runtime | Supervisor constructs account payloads in `backend/src/msai/live_supervisor/__main__.py:425`; subprocess integrates real Nautilus in `backend/src/msai/services/nautilus/trading_node_subprocess.py:1290`, `:1363`, `:2892`. | Keep one account runtime with member strategies. Do not replace Nautilus with another custom execution engine. |
| Native execution/reconciliation | Redis-backed Nautilus cache/message bus at `backend/src/msai/services/nautilus/live_node_config.py:1019`; load/save and execution reconciliation at `:1109`. | Appropriate reuse of the engine. Empty-cache and broker-reconnect behavior still requires integration validation. |
| Opening-order halt | `backend/src/msai/services/nautilus/risk/risk_aware_strategy.py:432`, `:463`, `:496` enforce armed/unset/stale halt state and permit reduce/flatten orders; subprocess enforces mixin order and wires refresh at `trading_node_subprocess.py:2333`. | Strong implemented control, covered by focused tests. Distinguish this from the unwired quantitative risk suite. |
| Kill and stop | Kill writes halt atomically before stops in `backend/src/msai/api/live.py:2838`; per-account stop/flatness requests at `:2880`. Stop locks deployment before node at `backend/src/msai/live_supervisor/fleet_router.py:4612` and persists intent before signaling at `:4839`. | Keep halt-first semantics and durable stop intent. |
| Recovery | Persisted restart policy, bounded retries, reaper/rescan, heartbeat monitoring, and stop-intent checks in supervisor modules; rescan contract at `backend/src/msai/live_supervisor/fleet_router.py:3685`. | Useful recovery coverage, but substantial complexity. Current single-supervisor assumptions should be explicit. |
| Resume | `backend/src/msai/api/live.py:3382`, `:3423`, `:3482` require feed manifest, reconciliation evidence, and warm feed checks before clearing halt. | Keep fail-closed recovery gates, and validate them with real integration fault tests before relying on recovery claims. |
| Monitoring | Redis event consumer → translator → projection state/reader → live API; PostgreSQL order/trade audit is a separate persistence path. | Repair contract/identity defects below before treating this as reliable monitoring. |

The principal flow is:

```text
portfolio draft + strategies/config + allocation metadata
  → frozen revision / composition hash
  → account + candidate binding + committed deployment
  → Redis command stream
  → supervisor account router
  → per-account subprocess / Nautilus TradingNode
  → broker adapter, native cache, execution reconciliation
  → (a) Redis projection events → API read model
    (b) asynchronous SQL order/trade audit

kill/stop → halt latch first → durable stop intent → subprocess stop/flatten
recovery → persisted lifecycle/restart state + halt/feed/reconciliation gates
```

## Findings

### L1 — P1: projection event contract differs from installed Nautilus

**Confidence: high. Reproduced offline against Nautilus 1.223.0. Repair before relying on live monitoring.**

The translator expects status-specific order topics such as `events.order.submitted`, the exact fill topic `events.order.filled`, and `events.account.state`. Nautilus publishes order events by strategy and account events by account identity. The payload's event type, rather than those invented topic suffixes, distinguishes order states.

Evidence:

- Installed `backend/.venv/lib/python3.12/site-packages/nautilus_trader/execution/engine.pyx:851` publishes `events.order.{strategy_id}`; position topic at `:859`; instrument fill topic at `:867`.
- Installed `backend/.venv/lib/python3.12/site-packages/nautilus_trader/portfolio/portfolio.pyx:487` publishes `events.account.{account_id}`.
- Application translator routes at `backend/src/msai/services/nautilus/projection/translator.py:154` and its order status topic map at `:204` return `None` for the real order/account topics.
- `backend/src/msai/services/nautilus/projection/consumer.py:242` acknowledges events that translate to `None`. These are not retried as malformed events.
- Application subprocess comments/subscriptions already recognize the real strategy-specific order topic at `backend/src/msai/services/nautilus/trading_node_subprocess.py:2450` and `:2654`. The mismatch is between two integration paths, not uncertainty about upstream documentation.

Position events pass the topic check but fail on the installed engine's money format: translator `:70` uses `Decimal(str(value))`, while position translation at `:177` feeds it `realized_pnl`. Installed position serialization emits currency-bearing strings such as `"0.00 USD"`. The real position event probe raised `decimal.InvalidOperation`. Consumer exception handling at `consumer.py:250` leaves such messages unacknowledged; retry/DLQ logic at `:340` eventually diverts them.

The focused translator tests pass because they build simplified topic names and bare decimal money strings (`backend/tests/unit/test_projection_translator.py:22`, `:87`, `:149`). They validate internal assumptions rather than the engine boundary.

**Result:** order/account projection events can be silently discarded and real position events can fail translation. This affects the projection path; it does not prove Nautilus execution or the separate SQL audit receives no events.

**Repair:** normalize actual installed engine serialization, dispatch by payload event type with the real topic families, and add contract tests using actual Nautilus events and an in-memory execution engine. Include an observable counter/error for unexpected event contracts rather than silently treating every unsupported event as harmless.

### L2 — P1: position read models lose direction and member identity

**Confidence: high. Direction and key collision reproduced offline; cold-read scope confirmed in source.**

The projection cannot represent the portfolio it deploys faithfully:

- `backend/src/msai/services/nautilus/projection/events.py:62` defines position snapshots without `strategy_id` or `position_id`.
- `projection/translator.py:180` reads `quantity`, which is unsigned, and ignores `signed_qty`/side. `projection/position_reader.py:325` does the same in the cold reader. An actual short position with signed quantity `-3.0` becomes reported quantity `3`.
- `projection/projection_state.py:130` keys snapshots only by deployment and instrument. Two member strategies holding the same instrument overwrite each other rather than remain distinct or aggregate explicitly. The isolated collision probe ended with just `AAPL.XNAS: 7` after applying `-3` and `7` member positions.
- `backend/src/msai/api/live.py:3963` passes only the deployment's legacy `strategy_id_full` to the reader. `projection/position_reader.py:261` filters cold-cache positions to that one strategy. Multi-member positions can therefore be incomplete after backend restart.
- `projection/projection_state.py:150` treats any existing position snapshot as a hydrated deployment, and `:198` does not cold-hydrate an already present deployment. A partial stream view can prevent discovery of untouched members.

The collision probe deliberately replaced the currency-bearing PnL field with a bare numeric value to isolate L2 from L1. No order execution was involved.

**Repair:** preserve engine position identity, member strategy identity, and signed quantity; decide whether the API returns individual positions, explicit account aggregates, or both. Hydrate the full deployed member set and track hydration completeness/freshness separately from the existence of one position. Verify short/long members on the same instrument and cold-start recovery. These are monitoring/accounting defects, not evidence that the broker or engine itself holds the wrong signed position.

### L3 — P1: live quantitative risk controls are scaffolded but unwired

**Confidence: high. Confirmed source behavior; named implementation gap.**

The implemented halt mechanism must not be confused with broader risk limits:

- `backend/src/msai/services/nautilus/risk/risk_aware_strategy.py:267` initializes `_risk_limits = None`.
- The check suite at `:508` skips position size, exposure, daily loss, and configured market-hours checks when limits remain unset.
- Production wiring at `backend/src/msai/services/nautilus/trading_node_subprocess.py:2333` enforces the mixin, arms the halt gate, and wires halt/audit/market-hours support, but does not assign `_risk_limits`. The source comments describe future wiring; the audit found no runtime assignment in production source/strategies.
- Production node construction at `trading_node_subprocess.py:2892` does not supply an order-notional cap. The native risk engine remains enabled and rate limits are configured, but `backend/src/msai/services/nautilus/live_node_config.py:1119` defaults the notional-cap mapping to `{}`.
- The separate `RiskEngine.validate_deployment` integration is in the older placeholder `TradingNodeManager` (`backend/src/msai/services/nautilus/trading_node.py:90`). The legacy `/start` route now returns 410 (`backend/src/msai/api/live.py:434`); this does not supply the missing production wiring.

**Result:** halt/staleness protections and native engine protections exist, but this implementation does not establish active portfolio exposure, per-strategy position size, or daily loss limits merely because the corresponding Python classes and tests exist.

**Repair:** define the small live risk contract needed for the initial partners, require validated effective values in the deployment snapshot, and wire it at the actual production node boundary. Before enabling the existing suite, review market-order notional (currently returns zero without a limit price at `risk_aware_strategy.py:649`), batch reservations, order modifications, and daily-loss reset semantics. These are limitations of unused scaffolding, not claims that active limits have already malfunctioned.

### L4 — P1: portfolio weights are frozen but do not affect live strategy sizing

**Confidence: high. Confirmed source data flow.**

- `backend/src/msai/services/live/portfolio_service.py:403` stores validated allocation weights from a run into revision members.
- `backend/src/msai/services/live/revision_service.py:114` includes weight in the immutable composition hash.
- `backend/src/msai/live_supervisor/__main__.py:425` copies member configuration and instrument information into `StrategyMemberPayload` but does not read the weight.
- `backend/src/msai/services/nautilus/trading_node_subprocess.py:419` defines that payload without weight/capital allocation.
- `backend/src/msai/services/nautilus/live_node_config.py:256` constructs strategy configuration without applying portfolio allocation sizing.
- For example, `strategies/example/ema_cross.py:76` uses configured trade size and `:191` submits it; `strategies/example/config.py:15` defaults it to one unit.

**Result:** changing only the member's allocation weight changes revision identity but does not change its live order size. This is a deployment parity gap if the product presents allocation promotion as executable weighting. It does not imply every strategy should be automatically capital-weighted; some Python strategies may deliberately own explicit quantity sizing.

**Repair:** define one explicit deployment sizing contract: strategy-owned quantities or platform-owned capital/weight allocation, with effective settings persisted and exposed. Reject or clearly label unsupported weighted promotion until it is implemented. A revision hash by itself cannot establish backtest/live sizing parity.

### L5 — P1: empty Redis recovery can erase halt state while appearing healthy

**Confidence: high in the code/config chain. Conditional operational risk; no loss incident observed or reproduced.**

The production Redis service (`docker-compose.prod.yml:80`) has a health check and restart policy, but no declared persistent volume or explicit AOF configuration. A replacement container/empty Redis is therefore a materially different failure from a connection outage.

- `backend/src/msai/services/nautilus/trading_node_subprocess.py:293` interprets a successful read with both fleet and account halt keys absent as `False`.
- Refresh at `:380` passes that successful result to strategies; `backend/src/msai/services/nautilus/risk/risk_aware_strategy.py:496` replaces the cached state.
- The same strategy correctly fails closed for unset/stale state at `:463`. A fresh, successful read from empty Redis bypasses that stale-read protection by reporting no halt.
- Halt TTL is also finite (`backend/src/msai/core/halt_keys.py:23`, 86,400 seconds).
- Native strategy/order/cache state also uses Redis (`backend/src/msai/services/nautilus/live_node_config.py:1019`) with state loading/reconciliation enabled at `:1109`. Supervisor rescan can recover eligible deployments from PostgreSQL (`backend/src/msai/live_supervisor/fleet_router.py:3685`), but PostgreSQL lifecycle recovery is not a substitute for the lost engine/strategy state.

**Supported failure sequence:** if a subprocess is still running and held by a halt latch, Redis is recreated empty, and the subprocess successfully refreshes before stop finishes, the missing latch is interpreted as permission to open orders. A successfully stopped subprocess remains stopped; this finding does not claim all deployments automatically resume. Broker reconciliation may recover execution facts, but it does not automatically restore every strategy's indicator or arbitrary saved state.

**Repair:** specify and test Redis durability, and distinguish uninitialized/lost safety state from an explicitly cleared halt. Require conservative reconciliation/rearm after state loss. Test this failure mode separately from the existing network-outage/stale-halt tests. Verify deployment storage configuration operationally before asserting persistence is present or absent in a real environment.

### L6 — P2: order/trade audit can misattribute multi-member code and fail on partial-close PnL

**Confidence: high in source; not exercised against PostgreSQL in this audit.**

Two independent record-correctness issues exist in the separate SQL audit path:

1. **Wrong code hash for later members.** The top-level process payload's hash comes from the first member (`backend/src/msai/live_supervisor/__main__.py:617`). The event hook resolves each order's member strategy identity (`backend/src/msai/services/nautilus/trading_node_subprocess.py:2470`) but submitted-order and fill records use the top-level `p.strategy_code_hash` (`:2509`, `:2582`). With two different strategy code files, later members' audit records carry the first member's code hash. Per-member denial context correctly uses the member-specific hash at `:2386`, showing inconsistent attribution across paths.
2. **PnL update assumes one trade row per client order.** `backend/src/msai/services/nautilus/audit_hook.py:354` documents updating the latest closing fill, but its update filters only deployment/client order ID, with no latest-row selection, and then uses `scalar_one_or_none()`. The `Trade` model is one row per fill (`backend/src/msai/models/trade.py:30`) and deduplicates by deployment/broker trade ID (`:54`), not client order ID. Multiple fills for one closing order can update multiple rows and make the scalar read raise, rolling the transaction back. A PostgreSQL test is needed to validate the intended PnL distribution, not merely suppress the exception.

Related source concern: audit writes are scheduled asynchronously (`trading_node_subprocess.py:2483`) and missing-row status updates only log (`backend/src/msai/services/nautilus/audit_hook.py:549`). Broker-fill idempotency using `ON CONFLICT DO NOTHING` at `audit_hook.py:280` is valuable, but ordering/recovery of missing audit rows deserves one focused integration test. No incidence rate is established here.

**Repair:** carry the per-member code hash through every audit fact, and define fill-level versus order-level PnL semantics explicitly. Preserve idempotent broker-fill ingestion and reconcile missing audit facts rather than relying on task scheduling order.

### L7 — P2: active deployment count reads the retired placeholder manager

**Confidence: high. Confirmed source behavior.**

`backend/src/msai/api/live.py:118` creates a `TradingNodeManager` for the older path. The real supervisor is separate, and the legacy start endpoint returns 410 at `:434`. `live_status` reads actual deployment/process rows at `:3700`, but the response's `active_count` at `:3812` still reads the old manager. No production route populates that manager; its own spawn implementation remains a placeholder (`backend/src/msai/services/nautilus/trading_node.py:105`). The resulting count can remain zero while the returned database deployment list contains live deployments.

**Repair:** derive status/count from the same canonical lifecycle data and retire unused manager/risk objects. The separate deployment-gate consequence is now confirmed in [product audit PO-08](product-operations.md#po-08--high-deploy-refusal-misses-deployments-that-are-still-stopping): `.github/workflows/deploy.yml:444` calls `/live/status?active_only=true`, whose filter at `backend/src/msai/api/live.py:3718` excludes `stopping`; the workflow repeats that exclusion at `deploy.yml:468`. Source inspection plus an offline AST/copied-`jq` predicate check showed that a synthetic stopping-only fleet clears the gate while teardown may still be in progress. This resolves the caller cross-check; it is not evidence of an actual production deployment incident.

## Additional operational boundaries

- **Flatness is narrower than its label may suggest.** The drain check at `backend/src/msai/services/nautilus/trading_node_subprocess.py:1967` inspects local Nautilus cache positions for deployed member strategy IDs after stop and reports `broker_flat`; it is not an independent fresh account-wide broker query. Cache-read errors fail closed. Coalesced request mechanics at `backend/src/msai/services/live/flatness_service.py:84` are sensible, but API/operator language and acceptance tests should distinguish deployment-cache flatness from verified account-wide broker flatness.
- **Single supervisor is an operating constraint.** Recovery documentation at `backend/src/msai/live_supervisor/fleet_router.py:3740` relies on a single-supervisor/deploy-exclusion contract; rescan is not a complete distributed leader/fencing design. For a few partners, keep this explicit topology instead of adding multi-host HA before the simpler correctness gaps are fixed.
- **Callback isolation is bounded.** `backend/src/msai/services/nautilus/failure_isolated_strategy.py:34` wraps selected callback hooks and marks a strategy degraded after exceptions. This does not establish universal isolation for every Nautilus callback or automatic liquidation on arbitrary strategy failure.
- **Projection freshness needs a recovery contract.** `backend/src/msai/services/nautilus/projection/position_reader.py:158` reuses hydrated state; pubsub receive in `projection/state_applier.py:87` is outside the inner application-error guard. A reader/applier outage can require rehydration rather than indefinite reuse. This is a source-derived follow-up, not a reproduced outage.
- **Failed/stopping deployments may retain exposure.** The positions endpoint scopes its deployment selection to ready/running (`backend/src/msai/api/live.py:3934`, `:3956`). Operations need a way to see residual positions while a stop or recovery is incomplete. The assessment did not establish the actual broker state of any stopped/failed deployment.

## What to keep, simplify, repair, and defer

**Keep:** Nautilus for native order/execution/reconciliation, per-account runtime isolation, frozen deployment revisions, account/gateway validation, database locking/uniqueness, halt-first control, fail-closed stale halt checks, durable stop intent, idempotent command processing, restart ceilings, and idempotent broker-fill persistence.

**Repair first:** actual engine event contract and monetary serialization; signed/member-aware positions and complete cold hydration; explicit effective live sizing/risk contract; durable safety state and empty-state recovery; audit code attribution/partial-fill PnL; canonical status counts. These directly affect the ability to understand and control a live deployment.

**Simplify after repair:** retire the old `TradingNodeManager` path and duplicate risk objects; converge configuration on the supported account-portfolio topology; break large live handlers along existing authority boundaries (deployment binding, commands, health reads, audit persistence). Reduce historical phase/reviewer commentary after preserving rationale in ADRs. Do not introduce a new generic orchestration framework or rewrite the engine.

The inspected files illustrate the maintenance burden: `api/live.py` 4,087 lines, `fleet_router.py` 4,906, `trading_node_subprocess.py` 2,943, `live_node_config.py` 1,149, and the two supervisor entry/run modules 1,906 combined. These are physical line counts including extensive comments, not measured complexity or executable-line counts. At 5–10 minute/daily cadence, boundary correctness and clear state ownership are higher priorities than throughput optimization.

**Defer:** multi-host supervisor HA, elastic fleet scaling, more broker/exchange abstraction, and advanced risk-suite expansion. Start with a narrow, explicit IBKR/account/strategy contract and prove its recovery behavior. Event-driven plumbing itself is not unnecessary merely because trading is slow; broker callbacks and safety actions still need reliable handling.

## Validation performed

### Focused component tests

Inspected shared and unit fixtures before choosing tests. Testcontainers exist in the repository but were not requested by the selected tests. The selected flatness tests use fake Redis, and the other modules use mocks/in-memory objects. No real external services were contacted.

Executed from the repository using the existing interpreter, with clean temporary cwd, bytecode/cache writes disabled, dummy service addresses, and network blocking:

```bash
backend/.venv/bin/python - <<'PY'
import os, socket, sys, tempfile
sys.dont_write_bytecode = True
os.environ['ENVIRONMENT'] = 'test'
os.environ['DATABASE_URL'] = 'postgresql+asyncpg://offline:offline@127.0.0.1:1/offline'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:1/0'
os.environ['DATABENTO_API_KEY'] = ''
def no_network(*args, **kwargs):
    raise RuntimeError('Network disabled for offline audit')
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
import pytest
root = '/Users/pablomarin/Code/msai-v2/backend'
files = [
    'tests/unit/services/nautilus/risk/test_node_side_live_halt.py',
    'tests/unit/services/nautilus/test_live_strategy_halt_enforcement.py',
    'tests/unit/live_supervisor/test_restart_policy.py',
    'tests/unit/test_flatness_drain.py',
    'tests/unit/test_projection_translator.py',
]
with tempfile.TemporaryDirectory(prefix='msai-live-audit-') as clean:
    os.chdir(clean)
    result = pytest.main(['-q', '-p', 'no:cacheprovider', '-c',
                         root + '/pyproject.toml',
                         *[root + '/' + f for f in files]])
raise SystemExit(result)
PY
```

**Observed result: `79 passed in 0.84s`.** This supports the implemented halt/restart/drain mechanics under their fixtures. The translator tests passing does not contradict L1; their fixtures omit the real engine contract.

### Installed-engine contract probe

Executed with the same environment/network guard and temporary cwd as above, adding `backend/src` to `sys.path`. Core probe below uses the installed engine's in-memory test helpers; it does not create a live TradingNode or broker client.

```python
from uuid import uuid4
from nautilus_trader import __version__
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import PositionId
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from msai.services.nautilus.projection.translator import translate
from msai.services.nautilus.projection.projection_state import ProjectionState
from msai.services.nautilus.projection.position_reader import PositionReader

instrument = TestInstrumentProvider.equity()
order = TestExecStubs.market_order(
    instrument=instrument, order_side=OrderSide.SELL,
    quantity=instrument.make_qty(3),
)
submitted = TestEventStubs.order_submitted(order)
fill = TestEventStubs.order_filled(order, instrument,
                                  position_id=PositionId('P-1'))
position = Position(instrument=instrument, fill=fill)
opened = TestEventStubs.position_opened(position)
account = TestEventStubs.cash_account_state()
bus = TestComponentStubs.msgbus()
cache = TestComponentStubs.cache()
clock = TestComponentStubs.clock()
engine = ExecutionEngine(msgbus=bus, cache=cache, clock=clock)
cache.add_instrument(instrument)
cache.add_order(order)
observed = []
for topic in [f'events.order.{order.strategy_id}', 'events.order.submitted']:
    bus.subscribe(topic=topic, handler=lambda event, topic=topic:
                  observed.append((topic, type(event).__name__)))
engine.process(submitted)
print('installed_nautilus', __version__)
print('actual_engine_publish', observed)
dep = uuid4()
for label, topic, event in [
    ('submitted', f'events.order.{order.strategy_id}', submitted),
    ('filled', f'events.order.{order.strategy_id}', fill),
    ('account', f'events.account.{account.account_id}', account),
    ('position', f'events.position.{order.strategy_id}', opened),
]:
    try:
        result = translate(topic=topic, event_dict=type(event).to_dict(event),
                           deployment_id=dep)
        print('translate_real_event', label, repr(result))
    except Exception as exc:
        print('translate_real_event', label, type(exc).__name__, str(exc))
print('short_position_source', {'quantity': str(position.quantity),
      'signed_qty': position.signed_qty, 'side': str(position.side)})
print('short_cold_reader_qty', str(PositionReader._to_snapshot(position, dep).qty))
raw = type(opened).to_dict(opened)
raw['realized_pnl'] = '0'  # Isolate identity/direction from money parsing failure.
first = translate(topic=f'events.position.{order.strategy_id}',
                  event_dict=raw, deployment_id=dep)
raw2 = {**raw, 'strategy_id': 'Second-001', 'position_id': 'P-2',
        'quantity': '7', 'signed_qty': 7.0}
second = translate(topic='events.position.Second-001', event_dict=raw2,
                   deployment_id=dep)
state = ProjectionState()
state.apply(first)
state.apply(second)
print('two_same_symbol_positions_projected',
      [(x.instrument_id, str(x.qty)) for x in state.positions(dep)])
```

Observed output:

```text
installed_nautilus 1.223.0
actual_engine_publish [('events.order.S-001', 'OrderSubmitted')]
translate_real_event submitted None
translate_real_event filled None
translate_real_event account None
translate_real_event position InvalidOperation [<class 'decimal.ConversionSyntax'>]
short_position_source {'quantity': '3', 'signed_qty': -3.0, 'side': '3'}
short_cold_reader_qty 3
two_same_symbol_positions_projected [('AAPL.XNAS', '7')]
```

### Limits of this assessment

No real PostgreSQL transaction/lock/race tests, Redis consumer-group durability/recreation tests, IBKR order/position reconciliation, broker flatten verification, market-data permissions/warmup checks, or deployed performance/load tests were performed. This audit does not certify live readiness. The useful next validation is a small fault-oriented integration matrix covering the repaired boundaries, followed by broker acceptance on a freshly verified, explicitly authorized test account; another broad mocked-unit pass would not answer those questions.
