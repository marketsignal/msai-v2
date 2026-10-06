# Isolated Nautilus V2 research evaluation

This procedure evaluates one research journey on labeled synthetic input. It does not authorize
production installation, vendor downloads, broker connections or live trading. Preserve the immutable
`quick-fix/october-release-status` baseline at `9d51a633ebeeff9b85ee06b6085df056dbe68458` and its
Nautilus 1.223 image before changing dependencies. API, worker, PostgreSQL, Redis and raw data must be
the separately named local research environment; existing primary and Azure stores remain untouched.

## Supported synthetic setup command

`scripts/seed_market_data.py` is the sanctioned fixture bootstrap. Use its read-only script mount
with the baseline image's interpreter, application sources and locked dependencies. The script uses
the existing SecurityMaster registry writer inside one application transaction. It does not create
provider clients, contact Databento/IB, or qualify an instrument for live execution.

The literal isolated database is `msai_v2_research`. Set `ENVIRONMENT=development` and an explicit
`DATABASE_URL` using the asyncpg driver, that database, and host `postgres` on the isolated Compose
network (or localhost/127.0.0.1 for an explicitly mapped isolated host port). The command also requires
`--isolated-research`; a production environment, different database, remote host or omitted flag
refuses before setup writes. Never reuse the primary database under this name.

Inside that preserved baseline application container, with this script mounted at
`/app/setup/seed_market_data.py`, execute:

```bash
PYTHONPATH=/app/src /app/.venv/bin/python /app/setup/seed_market_data.py /app/data \
  --symbols AAPL --bootstrap-registry --isolated-research
```

The container's nonsecret isolated development environment supplies the configured database URL;
do not source the checkout's root `.env`. `/app/data` must be that environment's fresh dedicated data
mount, writable by the application user. This command works with the unchanged baseline 1.223
application and writer, independently of candidate V2 dependencies. The same command is used for
candidate setup; keep the same preserved fixture bytes and registry binding when switching engines.

Data-only legacy usage remains available without registry registration:

```bash
python scripts/seed_market_data.py /path/to/fresh/synthetic-data
```

Its default symbols are AAPL, MSFT and SPY. Research acceptance uses only `--symbols AAPL` and the
single registry carrier `AAPL.NASDAQ`; other symbols are not registered by this command.

## Fixture meaning and repeat safety

The fixture identity is `msai-nautilus-v2-research-january-2025-v1`. Prices and volumes come from a
fixed SHA256-derived per-symbol generator seed. January 2025 has 23 synthetic weekdays with 390
minute bars at 09:30–15:59 **UTC**, including weekday holidays. These times are deliberately not an
exchange-local calendar or evidence of complete market sessions. Input is synthetic and cannot
validate alpha, market completeness, realistic fees or execution costs.

`research-fixture-manifest.json` records synthetic origin, parameters, UTC bounds, bar counts and
Parquet SHA256 hashes. `research-fixture-registry.json` binds its manifest SHA256 to the actual
definition/alias UUIDs and exact metadata. The registry provider remains the resolver's existing
Databento carrier; this is not evidence of downloaded Databento data. `trading_hours` remains absent,
and the binding explicitly records `live_qualified=false`.

Repeat setup is idempotent only when both fixture hashes and registry identity/metadata match.
Before any registry write the command pre-acquires the owning AAPL equity advisory locks for
Databento and Interactive Brokers, then checks definitions and aliases across providers. Unmarked
data, changed hashes/manifest, foreign or conflicting definitions, additional/historical aliases,
IB aliases, or a missing registry ownership binding refuse; existing rows/files are not replaced to
make setup pass. The existing application writer creates only the research alias, inside the
transaction. An already matching registration is read-only.

Backtests also refuse changed Parquet bytes while the fixture manifest remains present. This
preserves the meaning of the isolated synthetic evaluation: re-ingesting the same file does not
silently turn a marked fixture into unmarked data. Use a fresh evaluation environment for different
input; do not edit or delete the ownership manifest to make this acceptance fixture pass.

If the process stops after the registry commit but before writing its binding sidecar, a retry
deliberately refuses those unmarked rows. Preserve that isolated environment and diagnose the failed
setup; do not invent a sidecar, inject SQL, or overwrite ownership evidence. For a new evaluation,
use a freshly named isolated environment through the documented lifecycle. This small setup-window
limitation does not alter existing primary/production stores.

## Verification boundary

The seed command arranges inputs only. Register strategies using the public strategy listing, then
submit and verify actual experiments through API, installed CLI, and real browser in that order.
Saved-result checks compare those public payloads, fills, reports and reloads. Do not query PostgreSQL,
DuckDB or Parquet as a substitute for product acceptance. Capture baseline public observations and
compatible database/catalog recovery before the candidate upgrade. The complete runtime switch and
recovery record is added after those journeys are executed; this setup procedure alone is not a PASS.

## Isolated application runtime

The evaluated local environment uses Compose project `msai-v2-research`, with configuration in
`.forge/local/runtime/research.compose.yml` and candidate image overrides in
`.forge/local/runtime/research-candidate.override.yml`. These are retained local evaluation files,
not production deployment configuration. Run commands from the feature worktree, always passing
`--env-file /dev/null` so Compose cannot load the primary checkout's environment.

The API binds `127.0.0.1:18800`; the host frontend uses `localhost:13300`. PostgreSQL 16 owns the
dedicated `msai_v2_research` database and `research_fixture_database` volume. Redis 7 and
`research_data` belong to this project. The five application services are `backend`,
`backtest-worker`, `research-worker`, `portfolio-worker` and `ingest-worker`.

The local configuration supplies the nonsecret API key `msai-v2-research-local-only` and
`ENVIRONMENT=development`. Vendor credentials are empty, daily ingestion is disabled, auto-heal is
zero, and every broker host/port is loopback/65534 with an empty account and disabled gateway slot.
No broker service is started. Keep these guards when rebuilding images; do not source root `.env`
or enable a broker profile to satisfy research checks.

Start the candidate application services with the existing dedicated database/data volumes:

```bash
docker compose --env-file /dev/null -f .forge/local/runtime/research.compose.yml \
  -f .forge/local/runtime/research-candidate.override.yml up -d \
  backend backtest-worker research-worker portfolio-worker ingest-worker
```

The preserved image is `msai-v2-research-baseline:9d51a63` (Nautilus 1.223.0); the local candidate
tag is `msai-v2-research-candidate:rc6-wip` (exact Nautilus 2.0.0rc6). Record the actual image digest
after each rebuild: a mutable local tag alone cannot identify accepted application bytes. The V2
catalog lives under a separate `nautilus-v2` namespace; retain the original V1 catalog.

The host UI was prepared with Node 24 and pnpm 9.15.0, using locked Next.js 15.5.12. Its supported
local development settings are `NEXT_PUBLIC_API_URL=http://localhost:18800` and
`NEXT_PUBLIC_MSAI_API_KEY=msai-v2-research-local-only`; the API-key auth path bypasses the Entra
redirect. Run the dev server on port 13300. This local journey does not certify Entra sign-in.

Healthy `/health` and `/ready` responses establish startup only. Authenticated live release
readiness and unsupported cold live reads explicitly refuse on RC6. Do not interpret research
startup or a portfolio worker process as accepted shared-capital portfolio/live operation.

## Compatible code and state recovery

Recovery uses the same schema-compatible isolated database, preserved fixture bytes and original
V1 catalog. The saved baseline job is `81766b6e-9da8-4a67-814b-ef7a73e8fb37`, for registered EMA
strategy `3464b896-7c63-4934-ba14-e6bb9b641da8`, AAPL.NASDAQ, January 2–2, 2025. Its public results
record 390 bars, 16 fills and a $1,000,002.10 realized balance from $1,000,000 USD. Record hashes of
the public results, trades and report before switching. The original report bytes are preserved,
including its historical undefined `save()` onload error; corrected new reports do not rewrite it.

Before switching, inspect every page of public backtest history, research jobs, portfolio runs
and live status. If queued/running/cancelling work or an active deployment exists, stop the recovery
exercise and complete or cancel it through its supported product command. Do not edit queues or
database rows to manufacture an idle state.

With the public preflight clear, switch only the five application services to the preserved image:

```bash
docker compose --env-file /dev/null -f .forge/local/runtime/research.compose.yml \
  up -d backend backtest-worker research-worker portfolio-worker ingest-worker
```

Verify readiness and the actual installed engine version, then fetch the saved baseline result,
trades and report through public interfaces and compare the original hashes. Submit a new EMA
January 2–2 experiment through the API or installed CLI; its completion, fills and recorded
economics must match the V1 reference. Reopen the saved result/report in the browser. Restore the
candidate with the two-file Compose command above, recheck its actual image/engine and fetch both
saved jobs again. A successful image swap alone is not recovery acceptance.

The retained `/private/tmp/msai-v2-research-v1-baseline.dump` and
`/private/tmp/msai-v2-research-v1-data.tgz` are separate read-only recovery archives. Their hashes
and the baseline public observations are recorded in local `baseline-capture.md` evidence. This
compatible-switch exercise does not restore the dump, replace volumes or manipulate saved jobs.
Keep the archives and baseline image until accepted recovery and separately authorized cleanup.

## Current acceptance record

### Backend CI scope

Full-source Ruff and `mypy src/ --strict`, release-control behavior, frontend and image-data-path
gates remain required. Exact Nautilus `2.0.0rc6` uses the committed positive research manifest
[nautilus_v2_research_tests.txt](../../scripts/nautilus_v2_research_tests.txt): 22 research owning
files plus two CI/refusal regression files. Run the same bounded gate from `backend/`:

```bash
uv run python ../scripts/run_backend_ci.py -v --cov=msai
```

The launcher validates the actual installed version and nonempty, unique, existing test paths
before replacing itself with pytest; both selection refusal and pytest failures propagate to CI.
Unknown V2 releases fail closed. This replaces the previous full-backend pytest gate on RC6,
including after merge to main. Legacy live and broader backend suite acceptance remain unverified;
expand the measured positive manifest or complete live migration before claiming that coverage.
The retained `1.x` full-suite route is unverified for the current V2 application source. Isolated
actual 1.223 controls on repaired legacy modules certify only those module contracts, not a full
V1 application or live acceptance. Native-independent helpers remain importable; legacy actors
and config builders refuse V2 before unavailable V1 imports or construction.

The baseline API, installed CLI and real-browser result/report/reload/history journey passed.
RC6 isolated Linux startup and focused native configuration/catalog/runner checks have executed.
The first candidate EMA job exposed a removed `Portfolio.is_flat` callback and is retained as
`FAIL_BUG`; an actual native callback regression now passes using `Portfolio.is_net_flat`.
The repaired public API job `fa5e5e85-6309-49fd-8a33-8bb533634e48` and independently submitted CLI
job `5adc8843-7887-4e89-8de2-39b28c6b8359` each recorded RC6, 390 bars, 16 fills and +$2.10 on
$1,000,000 USD; independent public fill arithmetic reconciled the API result. CLI reversed-date
refusal, repeat show and report delivery were captured. Actual browser validation rejected a
negative trade size; correction and January 2–2 submission created
`0e1c46be-9c51-44fd-9505-97d872123b37` with the same 390 bars, 16 fills and +0.000210% display.
The native results, 16 fill rows, full report, reload/history and legacy **Not recorded** assumptions
were observed. New report rendering had no new console error. The first research sweep failed
missing engine-owned instrument/bar preparation and remains retained as `FAIL_BUG`; research
execution was repaired at the common runner boundary. Repaired sweep
`06d108cc-2488-4d21-84e5-ec9108906990` completed two successful trials and selected training index 0;
walk-forward `593f9ba6-2ba2-4492-b0b1-a069e5d50728` completed two chronological windows and selected
latest window index 1 despite the earlier window's larger objective. CLI repeat reads matched API.
Actual browser Discovery/refusal, exploratory creation, provenance and reload/reopen passed;
Discovery `b2884434-eec9-4183-9d46-469398a846de` retains training selection. Invalid failed-sweep
promotion returned 409 without creating a candidate. Compatible recovery passed: actual immutable
V1/1.223 rerun `cb551f41-43f8-4ef3-b6be-afb0321b8ffb` recorded 390 bars, 16 fills and +$2.10; original
saved payloads/fills/report stayed unchanged and actual V1 CLI/report reads matched. Restoring all
five RC6 application services preserved the accepted API and browser result/fill/report payloads
and rechecked startup/refusal. No dump restore or saved-record manipulation was used.

Preliminary acceptance passed all five journeys after fresh API/CLI/research/recovery verification
and separate fresh actual-browser closure. The first verifier's PARTIAL browser-infrastructure
report remains retained; it is not relabeled as an all-surface PASS. Fresh browser job
`c6fe273f-2478-43ad-a0fa-d91328876fc6` reproduced 390 bars, 16 fills and +0.000210%, useful negative-size
correction, actual assumptions/report and saved-result reload/history. Fresh research used
total-return objective and retained training/latest-window selection even where winners differ
from the earlier root Sharpe journey. The [graduated journeys](../../tests/e2e/use-cases/backtests/nautilus-v2-research.md)
record that scope. Final candidate review and installation remain separate gates. See the
[implementation explanation](../solutions/backtesting/nautilus-v2-research.md)
for financial and native execution limits.

The graduated browser regression runs only against this explicit isolated runtime:

```bash
PLAYWRIGHT_BASE_URL=http://localhost:13300 \
TEST_API_KEY=msai-v2-research-local-only MSAI_REAL_V2_RESEARCH_E2E=1 \
pnpm --dir frontend exec playwright test tests/e2e/specs/nautilus-v2-research-real.spec.ts \
  --project=chromium --workers=1
```

Use the locked Playwright browser installation. The evaluated host retained that browser cache at
`/private/tmp/msai-v2-verifier-browsers`; set `PLAYWRIGHT_BROWSERS_PATH` when using that cache.
The regression creates an actual new experiment through the form and observes public responses;
it neither intercepts API results nor creates the fixture.
