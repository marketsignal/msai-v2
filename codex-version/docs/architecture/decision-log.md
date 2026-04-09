# Architecture Decision Log

## ADR-001: Use NautilusTrader as the trading runtime, not a custom engine

Decision:

- MSAI uses NautilusTrader for catalog usage, backtests, live nodes, and
  execution/runtime state.

Why:

- the original prototype was drifting toward rebuilding trading infrastructure
  beside Nautilus instead of leaning on it
- Nautilus already provides the concepts we need for backtests and live control

Consequence:

- the app layer should stay focused on orchestration, audit, UI, and operator
  workflow

## ADR-002: Use Databento as the primary historical provider for US equities and CME futures

Decision:

- Databento is the research backbone for initial US equities and CME futures
  work

Why:

- one provider covers the first two target asset classes cleanly
- it maps well to NautilusTrader’s data integration model
- it is better aligned with serious repeatable research than using IB as the
  primary historical source

Consequence:

- Databento definitions must be ingested and persisted before research catalogs
  are trusted

## ADR-003: Keep Interactive Brokers as the live execution venue

Decision:

- Interactive Brokers remains the live execution venue and live account source

Why:

- that is the broker stack already available for paper/live trading
- it supports the target asset classes we are starting with

Consequence:

- historical research and live execution are intentionally separated:
  Databento for research, IB for live trading

## ADR-004: Remove synthetic research bootstrap instruments

Decision:

- research backtests now require persisted Databento/Nautilus definitions
  instead of generating synthetic instruments on the fly

Why:

- synthetic instruments break research/live parity
- they hide real symbology and venue mismatches

Consequence:

- backtests fail honestly if definitions are missing
- ingest quality matters earlier, which is the correct tradeoff

## ADR-005: Promotion is draft-based, not auto-deploy

Decision:

- a winning research result becomes a paper-trading draft first

Why:

- this creates an operator review checkpoint
- it avoids hidden side effects from a single click turning into live trading

Consequence:

- the Research UI creates promotion drafts
- the Live UI consumes those drafts with explicit operator confirmation

## ADR-006: Keep research artifacts file-backed for now

Decision:

- parameter sweep and walk-forward reports remain JSON artifacts under
  `data/research/`

Why:

- this is fast to implement and inspect
- it keeps the Phase 2/3 research loop simple while the experiment model is
  still evolving

Consequence:

- listing/comparison APIs read from the filesystem today
- a future phase can move toward richer experiment persistence if needed

## ADR-007: Favor safe restrictions over ambiguous multi-strategy live overlap

Decision:

- same-account overlapping live deployments are intentionally restricted rather
  than guessed-at

Why:

- attribution ambiguity is dangerous with real broker exposure
- safe refusal is better than unsafe inference

Consequence:

- production multi-strategy coexistence still needs stronger account/model-code
  isolation before it should be expanded

## ADR-008: Paper-first certification remains mandatory

Decision:

- no real-money confidence claim should be made without broker-connected paper
  validation and failure drills

Why:

- passing unit/integration/UI tests is necessary but not sufficient for a
  trading platform

Consequence:

- the remaining deployment work should focus on:
  - broker-connected paper E2E
  - Azure hardening
  - observability
  - staged rollout controls

## ADR-009: Broker-facing services must be explicit and fail fast on missing credentials

Decision:

- broker-facing dev services (`ib-gateway`, `live-runtime`) are opt-in through
  the `broker` Compose profile
- `ib-gateway` must not start with placeholder or missing `TWS_USERID` /
  `TWS_PASSWORD`

Why:

- silent fallback to fake credentials can trigger IBKR login lockouts
- research/dev startup should be safe by default
- broker-connected paper validation should always be an intentional act with an
  explicit env file

Consequence:

- normal local development uses plain `docker compose -f docker-compose.dev.yml up -d`
- paper trading uses
  `docker compose --profile broker --env-file .env.paper-e2e.local -f docker-compose.dev.yml up -d ...`
- if broker credentials are missing, the correct behavior is an immediate,
  visible startup failure rather than an automated login attempt
