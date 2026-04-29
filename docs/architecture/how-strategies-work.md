<!-- forge:doc how-strategies-work -->

# How Strategies Work

This is doc 02 of the [Developer Journey](00-developer-journey.md). The previous doc was [How Symbols Work](how-symbols-work.md) — you have instruments resolving correctly. This doc covers the next step: **authoring a strategy and getting it into the registry so a backtest worker (or live supervisor) can find it.**

If you've ever wondered "why can't I `POST /api/v1/strategies` to create a new strategy?" — read on. The answer is _by design_, and the design has consequences that ripple into every other surface.

---

## The Component Diagram

Read top-to-bottom: where strategy code enters, what scans it, where its metadata lands, what surfaces consume it.

```
                    ┌─ AUTHORING (git only — Phase 1) ──────────────────┐
                    │                                                   │
                    │   developer writes:                               │
                    │     strategies/example/ema_cross.py               │
                    │     strategies/example/config.py   (sibling)      │
                    │                                                   │
                    │   git add ; git commit ; git push                 │
                    │                                                   │
                    └────────────────────────┬──────────────────────────┘
                                             │ filesystem
                                             ▼
                    ┌─ GOVERNANCE CHECK (pre-import, AST) ──────────────┐
                    │   services/strategy_governance.py                 │
                    │   blocks os.system, subprocess, etc.              │
                    │   runs BEFORE importlib touches the file          │
                    └────────────────────────┬──────────────────────────┘
                                             │ approved
                                             ▼
                    ┌─ DISCOVER (filesystem walk + import) ─────────────┐
                    │   services/strategy_registry.py                   │
                    │     discover_strategies()      lines 137–236      │
                    │       _find_strategy_class()   lines 333–367      │
                    │       _find_config_class()     lines 370–421      │
                    │       build_user_schema()      schema_hooks.py    │
                    │       compute_file_hash()      lines 113–129      │
                    │       _combined_strategy_hash()lines 573–600      │
                    └────────────────────────┬──────────────────────────┘
                                             │ list[DiscoveredStrategy]
                                             ▼
                    ┌─ SYNC TO DB (memoized by code_hash) ──────────────┐
                    │   sync_strategies_to_db()      lines 485–570      │
                    │     · upsert by file_path                         │
                    │     · skip schema recompute when hash unchanged   │
                    │     · prune orphaned rows (default: yes)          │
                    └────────────────────────┬──────────────────────────┘
                                             │ INSERT / UPDATE / DELETE
                                             ▼
                    ┌─ POSTGRES — strategies table ─────────────────────┐
                    │   id · name · description · file_path             │
                    │   strategy_class · config_class                   │
                    │   config_schema (JSONB) · default_config (JSONB)  │
                    │   config_schema_status · code_hash                │
                    │   governance_status · created_by                  │
                    └────────┬────────────────┬────────────────┬────────┘
                             │                │                │
                             ▼                ▼                ▼
                    ┌─ API ─────────┐ ┌─ CLI ─────────┐ ┌─ UI ──────────┐
                    │ GET    /      │ │ msai strategy │ │ /strategies   │
                    │ GET    /{id}  │ │   list        │ │ /strategies   │
                    │ PATCH  /{id}  │ │   show        │ │   /[id]       │
                    │ POST   /{id}/ │ │   validate    │ │ Validate btn  │
                    │   validate    │ │               │ │ Edit / Delete │
                    │ DELETE /{id}  │ │               │ │               │
                    └───────────────┘ └───────────────┘ └───────────────┘

                    ╔═══════════════════════════════════════════════════╗
                    ║  NOTE: there is no POST / endpoint.               ║
                    ║  Authoring goes through git, not the API.         ║
                    ╚═══════════════════════════════════════════════════╝
```

The arrows are unidirectional from filesystem → registry → DB → surfaces. There is no write path back from surfaces to filesystem. **Strategies are authored in git and discovered by scan.**

---

## TL;DR

A strategy is a Python file under `strategies/` that subclasses NautilusTrader's `Strategy` and (optionally) ships a sibling `*Config` class. The system never instantiates your strategy in the API process — it only reads its source, hashes it, imports it for class-discovery, and extracts a JSON Schema from its config class. The registry is **derived state**: every list/detail call re-syncs from disk. The list of operations:

| Surface | Where                                                        | What you can do                                    |
| ------- | ------------------------------------------------------------ | -------------------------------------------------- |
| **API** | `/api/v1/strategies/` (`backend/src/msai/api/strategies.py`) | List, show, patch default config, validate, delete |
| **CLI** | `msai strategy …` (`backend/src/msai/cli.py:88,102`)         | `list`, `show`, `validate`                         |
| **UI**  | `/strategies` (`frontend/src/app/strategies/page.tsx`)       | List card grid, detail page, validate button       |

> **Authoring is via git/filesystem, not API/UI.** This is an explicit Phase 1 decision (see `CLAUDE.md` _Key Design Decisions_ — "Strategies are Python files in `strategies/` dir (no UI uploads in Phase 1 — git-only)"). The reason is concrete and stated below in [§ 2](#2-the-three-surfaces).

---

## Table of Contents

1. [Concepts & data model](#1-concepts--data-model)
2. [The three surfaces](#2-the-three-surfaces)
3. [Internal sequence diagram](#3-internal-sequence-diagram)
4. [See / Verify / Troubleshoot](#4-see--verify--troubleshoot)
5. [Common failures](#5-common-failures)
6. [Idempotency / retry behavior](#6-idempotency--retry-behavior)
7. [Rollback / repair](#7-rollback--repair)
8. [Key files](#8-key-files)

---

## 1. Concepts & data model

### 1.1 The `strategies` table

The single source of authoritative metadata is a row in the `strategies` table, defined in `backend/src/msai/models/strategy.py`. The columns:

| Column                 | Type        | Nullable | Notes                                                                               |
| ---------------------- | ----------- | -------- | ----------------------------------------------------------------------------------- |
| `id`                   | UUID        | No       | Primary key, default `uuid4` (line 29)                                              |
| `name`                 | String(255) | No       | Human-readable name (line 30)                                                       |
| `description`          | Text        | Yes      | Strategy docstring (line 31)                                                        |
| `file_path`            | String(500) | No       | Absolute path to `.py` file (line 32)                                               |
| `strategy_class`       | String(255) | No       | FQDN class name (e.g., `EMACrossStrategy`) (line 33)                                |
| `config_class`         | String(255) | Yes      | Matching `*Config` class name; **may differ from suffix swap** (lines 34–40)        |
| `config_schema`        | JSONB       | Yes      | JSON Schema for tunable config fields (line 41)                                     |
| `default_config`       | JSONB       | Yes      | Default values dict (line 42)                                                       |
| `config_schema_status` | String(32)  | No       | Enum: `no_config_class`, `ready`, `unsupported`, `extraction_failed` (lines 43–49)  |
| `code_hash`            | String(64)  | Yes      | SHA256 of file bytes; indexed for fast cache invalidation (line 52)                 |
| `governance_status`    | String(20)  | Yes      | Default `unchecked`; **never updated by sync today** — see § 1.7 (lines 53–55)      |
| `created_by`           | UUID FK     | Yes      | Foreign key to `users.id` (lines 56–58)                                             |
| `created_at`           | DateTime    | No       | Server default `now()` (`TimestampMixin`, `models/base.py:23`)                      |
| `updated_at`           | DateTime    | No       | Server default `now()`, refreshes on UPDATE (`TimestampMixin`, `models/base.py:24`) |

(Source: `backend/src/msai/models/strategy.py:18,29–58`. `Strategy` declares `class Strategy(TimestampMixin, Base)` so it inherits both `created_at` and `updated_at` from the mixin — there is no override in `models/strategy.py`.)

Most columns are **derived state**: the registry overwrites them whenever the source's `code_hash` changes. The two writable-by-API fields are `default_config` and `description`, mutated by `PATCH /api/v1/strategies/{id}` (lines 119–155 of the router) — `updated_at` ticks forward on every such write (and on every disk-driven sync that mutates the row).

### 1.2 The `code_hash` algorithm

`compute_file_hash(path)` (registry lines 113–129) is the canonical SHA256 of the strategy file. It reads the file in **8 KiB chunks** through `hashlib.sha256()` and returns a 64-character lowercase hex string. Same hash function is invoked from the backtest enqueue path so a backtest's `strategy_code_hash` matches the strategy's at the moment it ran (lineage anchor for reproducibility).

### 1.3 The combined hash (catches config-only edits)

A strategy file imports its config from a sibling `config.py`. If you only edit `config.py`, the strategy file's bytes don't change — so its SHA wouldn't either, and the registry's memoized `code_hash` would never invalidate.

`_combined_strategy_hash(info)` (registry lines 573–600) closes this hole:

- If no sibling `config.py` exists: returns `info.code_hash` unchanged.
- If sibling exists: returns `SHA256(info.code_hash.encode() + b"\x00" + sha256(config.py))`.

This is the value `sync_strategies_to_db()` compares against the DB row's stored hash to decide whether schema columns need recomputing (see [§ 6](#6-idempotency--retry-behavior)). The null byte separator avoids collision between `"AB" + "CD"` and `"A" + "BCD"`.

### 1.4 `config_class` — not derivable from the strategy class name

A naive implementation would map `EMACrossStrategy` → `EMACrossConfig` by suffix swap. That's wrong. A strategy class might pair with `EMACrossConfig`, `EMAParams`, `EMAStrategyConfig`, or anything else its author chose. So the registry runs `_find_config_class(module)` (lines 370–421). The actual algorithm — purely module-class scan, no annotation introspection:

1. Iterate `inspect.getmembers(module, inspect.isclass)`. For each class, keep it only if its lowercased name ends in `"config"` AND it has a `.parse` attribute (Nautilus's `StrategyConfig` API).
2. Among matches, **prefer a class defined in the strategy's own module** (`cls.__module__ == module.__name__`) over a class imported from elsewhere — so a re-export of someone else's config doesn't get picked up by accident.
3. If no same-module match exists, fall back to the first imported `*Config` class **with one explicit exclusion**: `nautilus_trader.trading.config.StrategyConfig` itself. Every strategy file imports this base; without the guard, the fallback would always pick it up and produce an empty config schema. The exclusion is at lines 413–419 of the registry.
4. Stores the **exact name discovered** in `config_class`.

Note what's _not_ implemented: there is no inspection of the strategy class's `__init__(config: ...)` annotation, no suffix swap, no name-distance heuristic. A class that doesn't end in "config" or doesn't expose `.parse` is invisible to the discovery path. Server-side validation at backtest enqueue then loads the config class **by the exact stored name** rather than rederiving it. Citation: registry lines 370–421.

### 1.5 `config_schema` extraction

`build_user_schema(config_cls)` lives in `backend/src/msai/services/nautilus/schema_hooks.py` lines 115–169. It:

- Calls `msgspec.json.schema(config_cls, schema_hook=nautilus_schema_hook)`.
- **Trims the result to only `config_cls.__annotations__` keys** — Nautilus base config classes carry plumbing fields (e.g., `strategy_id`, `order_id_tag`) that aren't user-tunable; trimming keeps the auto-form clean.
- Extracts defaults from `schema_def["properties"][field]["default"]` (line 167).
- Returns `(schema_dict, defaults_dict, ConfigSchemaStatus.READY)` on success; `(None, None, ConfigSchemaStatus.EXTRACTION_FAILED)` on any exception.

The `nautilus_schema_hook` (lines 63–113) handles Nautilus-native types (`Venue`, `InstrumentId`, etc.) by mapping them to JSON Schema string types with format hints; it defers Nautilus imports to the function body so the schema module loads even on machines without Nautilus installed.

### 1.6 `config_schema_status` — the four states

Defined in `schema_hooks.py:48–61`:

| Value               | Meaning                                                                                                                        |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `no_config_class`   | Strategy file has no matching `*Config` class. PATCH'ing `default_config` is allowed but no auto-form is rendered.             |
| `ready`             | Schema extracted; UI renders an auto-form; `default_config` validates against the schema.                                      |
| `unsupported`       | Config class type is something `msgspec` can't introspect (rare; e.g., dynamically constructed classes).                       |
| `extraction_failed` | `build_user_schema` raised — schema is `None`. The strategy is still registerable and runnable, but the UI can't auto-form it. |

The router and UI never crash on a non-`ready` status — they degrade gracefully (raw JSON editor, server-side config validation only).

### 1.7 `governance_status` — pre-import AST review (column currently inert)

`backend/src/msai/services/strategy_governance.py` runs an AST-based scan **before `importlib` touches the file** (registry lines 164, 172–182). This is the only window in which we can block a malicious strategy without already executing its module-scope code (`os.system(...)`, `subprocess.Popen(...)`, etc.). The runtime check:

- **Pass** (no violations): `discover_strategies` builds a `DiscoveredStrategy` whose dataclass field carries `governance_status="passed"` (registry line 229).
- **Fail** (violations): the file is skipped entirely — no row is registered, and the registry logs `strategy_governance_violations` with the offending nodes.

**Important caveat — the DB column is currently never updated by sync.** `sync_strategies_to_db()` (registry lines 525–559) does NOT copy `info.governance_status` onto the `Strategy` row, neither on INSERT (lines 533–545) nor on UPDATE (lines 546–558). So the row's `governance_status` column stays at its model default `"unchecked"` (`models/strategy.py:53–55`) **forever**, regardless of what the AST check decided. The pass/fail decision lives only in logs and in the in-memory `DiscoveredStrategy` returned by the sync — it is not persisted. Treat the column as a stub today; do not key UI logic off it. (Filing this as a "no bugs left behind" gap — the scaffolding is there but the wire-up is missing.)

The reason the AST check itself lives BEFORE import (rather than running on a sandboxed import) is that Python module imports execute top-level statements unconditionally. By the time you've imported `evil.py`, it has already done its damage. AST review is the static-analysis shield.

### 1.8 `FailureIsolatedStrategy` — available mixin (not yet adopted)

`backend/src/msai/services/nautilus/failure_isolated_strategy.py` defines `FailureIsolatedStrategy`. Its purpose: **prevent a single strategy's exception from crashing a multi-strategy TradingNode.** A live deployment may run 5–10 strategies in one node (per `TradingNodeConfig.strategies=[…]`); without isolation, one buggy `on_bar` would tear down the entire fleet and stop trading for every other strategy in the node.

> **Status:** This is _available infrastructure, not a current convention._ Neither of the two reference strategies in `strategies/example/` (`ema_cross.py:38`, `smoke_market_order.py:59`) uses the mixin — they both subclass `nautilus_trader.trading.strategy.Strategy` directly. The mixin has unit-test coverage (`backend/tests/unit/test_failure_isolated_strategy.py`) and is ready for adoption when multi-strategy live deployments land; today no production strategy mixes it in.

**The class is a bare-object mixin, not a `Strategy` subclass.** It is meant to be combined via multiple inheritance with the real Nautilus base:

```python
from nautilus_trader.trading.strategy import Strategy
from msai.services.nautilus.failure_isolated_strategy import FailureIsolatedStrategy

class MyStrategy(FailureIsolatedStrategy, Strategy):     # mixin first; Strategy second
    def on_bar(self, bar): ...
```

Putting `FailureIsolatedStrategy` _first_ in the MRO is what gets `__init_subclass__` to fire for `MyStrategy` and its subclasses — Cython's `Strategy` doesn't propagate the hook on its own.

**The wrap mechanism** (`failure_isolated_strategy.py:37–43`):

```python
class FailureIsolatedStrategy:                # bare-object mixin (no base)
    _is_degraded: bool = False
    _WRAPPED_HOOKS: tuple[str, ...] = ("on_bar", "on_quote_tick", "on_order_event")

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for hook_name in FailureIsolatedStrategy._WRAPPED_HOOKS:
            original = cls.__dict__.get(hook_name)                       # cls.__dict__, not getattr
            if original is not None and not getattr(original, "_fi_wrapped", False):  # idempotency guard
                wrapped = FailureIsolatedStrategy._make_safe_wrapper(hook_name, original)
                setattr(cls, hook_name, wrapped)
```

Two subtleties relative to the obvious implementation:

- It uses `cls.__dict__.get(hook_name)`, not `getattr(cls, hook_name)`. That means it only wraps a handler defined **on this exact class** — it does not re-wrap a handler inherited from a parent. (If a parent already had it wrapped, the child's call still resolves through the parent's already-wrapped method via normal attribute lookup.)
- It checks `not getattr(original, "_fi_wrapped", False)` before wrapping, and the wrapper sets `safe_wrapper._fi_wrapped = True` after construction (line 63). This guard makes it idempotent — repeated subclassing or class-definition-time mutation cannot double-wrap.

**The wrapper itself** (`_make_safe_wrapper`, lines 45–64):

```python
def safe_wrapper(self, *args, **kwargs):
    if self._is_degraded:                                     # short-circuit if already broken
        self.log.warning(f"Degraded — skipping {hook_name}")
        return None
    try:
        return original(self, *args, **kwargs)
    except Exception as exc:
        self._is_degraded = True
        self.log.error(
            f"{hook_name} raised {type(exc).__name__}: {exc} — strategy degraded"
        )
        return None
```

What it actually does on an exception:

1. Sets `self._is_degraded = True`.
2. Logs at ERROR via the strategy's own `self.log` — the message is the literal string `"<hook_name> raised <ExcType>: <msg> — strategy degraded"`. **No traceback is captured, and no strategy id is included** beyond whatever `self.log` prefixes by default. If you need a stack trace at runtime, switch the log call to `self.log.exception(...)` or pull `exc.__traceback__` explicitly — neither happens today.
3. Returns `None` so Nautilus's Cython dispatch sees a normal completion and continues to the next subscriber.

For subsequent events, the degraded-check at the top short-circuits: the wrapper returns `None` early and emits a `WARNING` per call (`"Degraded — skipping <hook>"`). It does not re-enter the try/except, so a broken strategy isn't repeatedly raising — but it _is_ logging one warning per tick, which can become noisy. (Treat persistent degraded-warning floods as a signal to redeploy without the bad strategy.)

**The invariant:** _a single strategy crash degrades that strategy only; co-located strategies continue trading._ This applies once the mixin is wired in — in both backtest (a multi-strategy backtest doesn't fail-stop on one bad component) and live (the live supervisor doesn't kill the node — it just notes a degraded strategy in the deployment status). Until the example strategies adopt it, today's single-strategy nodes do not benefit.

---

## 2. The three surfaces

The parity table:

| Intent                             | API                                          | CLI                           | UI                                 | Observe / Verify                                            |
| ---------------------------------- | -------------------------------------------- | ----------------------------- | ---------------------------------- | ----------------------------------------------------------- |
| **Author a strategy**              | _none — by design_                           | _none_                        | _none_                             | New row appears after `git push` + first sync               |
| List strategies                    | `GET /api/v1/strategies/`                    | `msai strategy list`          | `/strategies`                      | Card grid + JSON array; counts match disk after re-sync     |
| Show one                           | `GET /api/v1/strategies/{id}`                | `msai strategy show <id>`     | `/strategies/[id]`                 | Detail page renders; `config_schema` + `code_hash` non-null |
| Validate                           | `POST /api/v1/strategies/{id}/validate`      | `msai strategy validate <id>` | Validate button on detail          | Returns 200 with class name; 422 with error message         |
| Patch `default_config`/description | `PATCH /api/v1/strategies/{id}`              | _none_                        | Edit form on detail (when `ready`) | GET returns updated values                                  |
| Delete                             | `DELETE /api/v1/strategies/{id}`             | _none_                        | Delete button on detail            | Subsequent GET returns 404                                  |
| Re-sync with disk                  | (implicit — every list/detail call re-syncs) | (implicit)                    | (implicit on page load)            | Newly-pushed file appears without explicit refresh          |

### 2.1 Why "author a strategy" has no surface

This is not an oversight; it is an explicit Phase 1 decision documented in `CLAUDE.md` (_Key Design Decisions_). The reasons stack up:

1. **Strategy code is privileged.** A strategy file runs inside the Nautilus engine subprocess with full Python privileges. Allowing arbitrary file uploads through the API would be equivalent to giving authenticated users RCE on the worker host. The pre-import AST governance check (§ 1.7) is one layer of defense — it is _not_ sufficient on its own (see e.g. timing attacks, polyglot files, and the long history of AST-based filters being escapable).

2. **Auditability requires git.** Every backtest and live deployment stamps `strategy_code_hash` (and, soon, `strategy_git_sha`) for reproducibility. If strategies are uploaded through the API, you have a hash but no commit history — you can't `git blame` a regression, can't bisect, can't review. Git is the ledger.

3. **Reviewer flow.** A strategy is reviewed via PR like any other code change. The governance check would have to run somewhere _outside_ the upload endpoint to be useful for review, which means another surface, more drift, more duplication.

4. **Phase 1 scope.** The Phase 1 goal (per `CLAUDE.md`) is _"first real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data."_ A strategy-upload UI is not on that path. It can be added in Phase 2 once the deployment surface is settled and the governance model is mature. Today: it isn't.

The practical workflow:

```bash
# Author
edit strategies/myteam/awesome.py
edit strategies/myteam/config.py        # if you have one

# Commit & push
git add strategies/myteam/
git commit -m "Add awesome strategy"
git push origin <branch>

# Pull on the host running the backend
ssh azure-vm
cd /srv/msai
git pull
docker compose -f docker-compose.dev.yml restart backend  # picks up new files

# Verify
curl -H "Authorization: Bearer $TOKEN" http://localhost:8800/api/v1/strategies/ | jq '.[].name'
# → "AwesomeStrategy" appears
```

`docker-compose.dev.yml` mounts `strategies/` into the container; in dev a `git pull` is enough — the next API call re-syncs and the row appears.

### 2.2 Validation never instantiates

`POST /api/v1/strategies/{id}/validate` (router lines 158–194) calls `validate_strategy_file(module_path)` (registry lines 244–276), which:

- Verifies the file exists and is rooted under a `strategies/` directory (path-traversal guard, via `_infer_strategies_root`).
- Ensures the strategies parent is on `sys.path` (`_ensure_strategies_importable`).
- Imports the module via `_import_strategy_module()`.
- Locates the `Strategy` subclass via `_find_strategy_class()`.
- Returns `(True, class_name)` on success or `(False, error_message)` on any failure.

**Note: `validate_strategy_file` does NOT re-run governance.** `_import_strategy_module` (registry lines 299–330) is a pure `importlib` helper — `spec_from_file_location` + `module_from_spec` + `exec_module`. It does not call `StrategyGovernanceService`. Governance only runs inside `discover_strategies()` (registry line 172), before that helper. In practice this is fine because the file already passed governance during the discovery sync that populated the row; but be aware that hitting `/validate` directly will exec module-scope code without an AST shield if the row's underlying file has been swapped on disk after discovery.

Critically, **it never calls `StrategyClass(...)`**. Instantiating Nautilus strategies in the API process would pollute Nautilus's global engine state (clock, message bus, cache) and is the kind of cross-contamination that costs you a 4 a.m. debugging session. The validation contract is "this file exposes a class named X that subclasses Strategy" — nothing more.

### 2.3 PATCH semantics

The schema has only two writable fields:

```
{
  "default_config": {"fast_ema": 10, "slow_ema": 30},
  "description":    "EMA crossover, my custom defaults"
}
```

Other fields (`name`, `file_path`, `code_hash`, `config_schema`, `governance_status`) are derived from disk and are overwritten by the next sync. Trying to PATCH them silently no-ops (they're not in the `StrategyUpdate` schema).

### 2.4 DELETE — currently hard-delete with a soft-delete TODO

`DELETE /api/v1/strategies/{id}` (router lines 197–220) currently removes the row outright. The cluster-B citation report flags an open TODO to convert this to a soft-delete (`deleted_at` flag) so backtest history can still resolve `strategy_id → name` for displayed-but-orphaned rows. Today, the workaround is that backtests carry `strategy_code_hash` independent of `strategy_id`, so a deleted strategy doesn't break historical reports — but it does break the FK if you've configured it as `RESTRICT`. The schema treats it as nullable from history's perspective.

---

## 3. Internal sequence diagram

The dominant flow is `GET /api/v1/strategies/`. Show, validate, and patch are slight variants — they all share the discover-and-sync pre-step. Here is the sequence for the list endpoint:

```
caller            FastAPI                  registry                       Postgres
  │                 │                         │                                │
  │  GET /api/v1/   │                         │                                │
  │  strategies/    │                         │                                │
  │  Bearer JWT     │                         │                                │
  │ ──────────────▶ │                         │                                │
  │                 │                         │                                │
  │                 │  validate JWT           │                                │
  │                 │  (PyJWT, JWKS cache)    │                                │
  │                 │                         │                                │
  │                 │  sync_strategies_to_db( │                                │
  │                 │    session,             │                                │
  │                 │    strategies_dir,      │                                │
  │                 │    prune_missing=True)  │                                │
  │                 │ ──────────────────────▶ │                                │
  │                 │                         │                                │
  │                 │                         │ discover_strategies(           │
  │                 │                         │   strategies_dir)              │
  │                 │                         │ ─────────┐                     │
  │                 │                         │          │ filesystem.walk()   │
  │                 │                         │          │ skip __init__.py,   │
  │                 │                         │          │      config.py,     │
  │                 │                         │          │      _*.py          │
  │                 │                         │ ◀────────┘                     │
  │                 │                         │                                │
  │                 │                         │ FOR EACH file:                 │
  │                 │                         │   governance_check(file)       │
  │                 │                         │     [AST scan, no import yet]  │
  │                 │                         │   IF blocked: skip + log       │
  │                 │                         │                                │
  │                 │                         │   _import_strategy_module(file)│
  │                 │                         │     spec_from_file_location()  │
  │                 │                         │     module_from_spec()         │
  │                 │                         │     spec.loader.exec_module()  │
  │                 │                         │                                │
  │                 │                         │   _find_strategy_class(module) │
  │                 │                         │     → first Strategy subclass  │
  │                 │                         │                                │
  │                 │                         │   _find_config_class(module,   │
  │                 │                         │     strategy_cls)              │
  │                 │                         │     → exact name discovered    │
  │                 │                         │                                │
  │                 │                         │   compute_file_hash(file)      │
  │                 │                         │     → SHA256, 8KB chunks       │
  │                 │                         │                                │
  │                 │                         │   _combined_strategy_hash()    │
  │                 │                         │     incl. sibling config.py    │
  │                 │                         │                                │
  │                 │                         │   IF config_cls present:       │
  │                 │                         │     build_user_schema(cls)     │
  │                 │                         │     → (schema, defaults,       │
  │                 │                         │        ConfigSchemaStatus)     │
  │                 │                         │                                │
  │                 │                         │ → list[DiscoveredStrategy]     │
  │                 │                         │                                │
  │                 │                         │ FOR EACH discovered:           │
  │                 │                         │   SELECT row WHERE             │
  │                 │                         │     file_path = ?              │
  │                 │                         │ ─────────────────────────────▶ │
  │                 │                         │                                │
  │                 │                         │ ◀───────────────────────────── │
  │                 │                         │   row found / not found        │
  │                 │                         │                                │
  │                 │                         │   row.name, description,       │
  │                 │                         │     strategy_class always      │
  │                 │                         │     refreshed from disk        │
  │                 │                         │   IF row.code_hash == new:     │
  │                 │                         │     SKIP schema recompute      │
  │                 │                         │     (memoization, line 553–8)  │
  │                 │                         │   ELSE:                        │
  │                 │                         │     UPDATE config_class,       │
  │                 │                         │       config_schema,           │
  │                 │                         │       default_config,          │
  │                 │                         │       config_schema_status,    │
  │                 │                         │       code_hash                │
  │                 │                         │   (governance_status NOT       │
  │                 │                         │    written — see § 1.7)        │
  │                 │                         │ ─────────────────────────────▶ │
  │                 │                         │                                │
  │                 │                         │ IF prune_missing:              │
  │                 │                         │   DELETE rows WHERE            │
  │                 │                         │     file_path NOT IN scanned   │
  │                 │                         │ ─────────────────────────────▶ │
  │                 │                         │                                │
  │                 │                         │ → list[(Strategy,              │
  │                 │                         │         DiscoveredStrategy)]   │
  │                 │ ◀────────────────────── │                                │
  │                 │                         │                                │
  │                 │  await session.commit() │                                │
  │                 │ ───────────────────────────────────────────────────────▶ │
  │                 │                         │                                │
  │                 │  FOR EACH paired row:                                    │
  │                 │    await db.refresh(row) — fills server-defaulted       │
  │                 │    created_at / updated_at on freshly-INSERTed rows     │
  │                 │ ───────────────────────────────────────────────────────▶ │
  │                 │                         │                                │
  │                 │  build StrategyResponse[]                                │
  │                 │                                                          │
  │  200 OK         │                                                          │
  │ ◀────────────── │                                                          │
  │  {items: [...], total}                                                     │
```

The sequence is identical for `GET /api/v1/strategies/{id}` except the final SELECT is `WHERE id = ?` and a 404 is returned if no row matches after sync. **The detail endpoint re-syncs independently of list** so neither depends on the other's side effects (Maintainer blocking objection #2, council 2026-04-20; cited from `scratch/citations-cluster-b.md` PART I §1).

The post-commit `await db.refresh(row)` loop on the list endpoint (`api/strategies.py:52–53`) is what makes server-defaulted timestamps visible: a freshly-INSERTed row has `created_at` populated by the database, but the in-memory ORM object only sees that value after the explicit refresh. Without it, first-discovery list responses would have `created_at = None`. The detail endpoint achieves the same property by issuing a fresh `SELECT` after commit (`api/strategies.py:95`).

---

## 4. See / Verify / Troubleshoot

You've added `strategies/myteam/foo.py` with a `FooStrategy` class. How do you confirm it registered?

### 4.1 The four verification points

```bash
# 1. The file exists where the registry can see it
ls strategies/myteam/foo.py

# 2. The CLI lists it
uv run msai strategy list | grep FooStrategy

# 3. The API returns it
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8800/api/v1/strategies/ \
  | jq '.[] | select(.strategy_class == "FooStrategy")'

# 4. The validate endpoint passes
STRATEGY_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8800/api/v1/strategies/ \
  | jq -r '.[] | select(.strategy_class == "FooStrategy") | .id')
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8800/api/v1/strategies/$STRATEGY_ID/validate \
  | jq
# → {"valid": true, "class_name": "FooStrategy"}
```

### 4.2 What "registered" means

A strategy is **registered** when:

- A row exists in the `strategies` table with `file_path` pointing at your file.
- `code_hash` is non-empty (`compute_file_hash` ran successfully).
- The AST scan in `discover_strategies` didn't block — _but_ this is not visible on the row: `governance_status` always reads `"unchecked"` because sync does not write that column today (see § 1.7). The way you confirm the AST shield didn't block your file is by the row's existence: blocked files are not registered at all and produce a `strategy_governance_violations` log line.
- `config_schema_status` is one of `ready`, `no_config_class`, `unsupported`, `extraction_failed` (any non-error state means the registry processed the file; only `ready` enables the auto-form).

If you see `code_hash` is null, the registry never finished processing the file — check backend logs for the failure (`docker compose -f docker-compose.dev.yml logs backend | grep -i strategy`).

### 4.3 The UI

The `/strategies` page (`frontend/src/app/strategies/page.tsx:24–27`) calls `apiGet<StrategyListResponse>("/api/v1/strategies/", token)` and renders a card grid (lines 72–82). The detail page at `/strategies/[id]` (`frontend/src/app/strategies/[id]/page.tsx`) calls `GET /api/v1/strategies/{id}` and exposes the validate button. If the card grid is empty after a `git pull` + restart, hit the API directly first — the UI is the last surface to debug because it's the most layers removed from the registry.

---

## 5. Common failures

### 5.1 Invalid Strategy class — no Nautilus subclass found

**Symptom:** Strategy file is on disk but never appears in the API response, or appears with the wrong class name.

**Cause:** `_find_strategy_class()` (registry lines 333–367) walks the imported module looking for a class defined in that module whose lowercased name ends in `"strategy"` (and is not literally `"strategy"`). It **prefers** a class that subclasses `nautilus_trader.trading.strategy.Strategy`; if it finds one, that's returned immediately. Otherwise it tracks the first matching name as a `fallback` and returns it at the end. The fallback branch exists so tests or stubs without Nautilus installed still resolve a class — see the function's own docstring (lines 336–338).

The operational consequences:

- A file whose class name doesn't end in "strategy" — e.g. `class EMA(Strategy):` — is skipped silently. There is no error, just no row.
- A file with a Nautilus subclass _and_ a non-Nautilus `*Strategy` class: the Nautilus subclass wins (issubclass check fires first).
- A file with only a non-Nautilus `*Strategy` class _and_ Nautilus is importable in the API process: the function still returns the fallback, because its scan never enforces `issubclass(cls, nautilus_base)` as a hard requirement — it only prefers it. In production this is unlikely (Nautilus is always installed), but it explains why a misconfigured strategy can still appear with a row pointing at the wrong class.

**Fix:** Confirm your class signature:

```python
from nautilus_trader.trading.strategy import Strategy

class FooStrategy(Strategy):  # ← must be a Strategy subclass
    ...
```

The `validate` endpoint will return `{"valid": false, "error": "..."}` if you have a row but the class went missing — but if no row was created in the first place, you'll only see it in backend logs. `docker compose -f docker-compose.dev.yml logs backend | grep -i strategy_registry`.

### 5.2 Missing file — file deleted after the row was created

**Symptom:** `GET /api/v1/strategies/{id}` returns 404 _or_ the row vanishes between two list calls.

**Cause:** You deleted (or renamed) the file. With `prune_missing=True` (default — registry line 489; loop at lines 563–568), the next `sync_strategies_to_db()` removes orphaned rows.

**Fix:** This is intended behavior — the registry's contract is "DB reflects disk." If you wanted to keep historical metadata around, you should soft-delete instead (see § 2.4 TODO). For now: `git revert` the deletion if it was a mistake.

If a file is _moved_ (e.g., `strategies/old/foo.py` → `strategies/new/foo.py`), the row at the old path is pruned and a new row at the new path is upserted — they get **different UUIDs**. Backtests that referenced the old `strategy_id` will 404 on the strategy lookup but their `strategy_code_hash` lineage column still matches (so QuantStats reports still load).

### 5.3 `config_schema_status = unsupported`

**Symptom:** The strategy registers and runs in backtest, but the UI shows "Schema unavailable — use raw JSON" instead of an auto-form.

**Cause:** Your config class uses a type `msgspec` can't introspect. Common culprits:

- Dynamically constructed classes (e.g., `type("FooConfig", (), {...})`).
- Generic types parameterized at runtime.
- Forward references that don't resolve.

**Fix:** Use a plain `msgspec.Struct` or `pydantic.BaseModel` config class with concrete annotations. The strategy will run regardless — this only affects the auto-form.

### 5.4 `config_schema_status = extraction_failed`

**Symptom:** Same UI symptom as `unsupported`, but `code_hash` is set.

**Cause:** `build_user_schema()` raised an exception. Could be a custom type the `nautilus_schema_hook` doesn't handle, a malformed annotation, or msgspec hitting a bug.

**Fix:** Check backend logs for the traceback (`grep build_user_schema`). The strategy is still registered and runnable — only the form generation failed. Consider filing an issue if your config class is well-typed and shouldn't fail.

### 5.5 Governance check failures

**Symptom:** Strategy file on disk, no row created, backend log shows `strategy_governance: blocked /path/to/foo.py`.

**Cause:** AST scan found a forbidden construct (`os.system`, `subprocess.Popen`, dynamic `eval`/`exec`, or whatever else `strategy_governance.py` blocks).

**Fix:** Remove the construct or re-architect the strategy. The governance check is a pre-import shield (§ 1.7) — by definition, any strategy that needs it is suspect. If you have a legitimate use case (e.g., calling out to a process for a model), the right path is to add a sanctioned helper to `services/` and call _that_ from the strategy, after which it goes through the same code review as everything else.

### 5.6 Mismatched `config_class` discovery

**Symptom:** `default_config` validates fine in PATCH, but the backtest enqueue endpoint returns 422 _"unable to load config class FooConfig in module strategies.myteam.foo"_.

**Cause:** `_find_config_class()` picked a class with one name, but at backtest enqueue time the registry tries to re-import and find that exact name — and somebody renamed the class without re-syncing, or there's a name clash with an imported symbol.

**Fix:** Re-trigger sync with a no-op call:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8800/api/v1/strategies/ > /dev/null
```

Then re-enqueue. If the issue persists, inspect the row's `config_class` column directly (yes, this is one of the only times it's worth peeking at Postgres for debugging — but verify through the API afterwards):

```bash
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U msai -d msai -c \
  "SELECT name, strategy_class, config_class FROM strategies WHERE id = '<id>';"
```

Compare against what's actually defined in the module.

---

## 6. Idempotency / retry behavior

`sync_strategies_to_db()` is **idempotent**: same disk state → same DB rows. You can call it a thousand times in a row and the table will stabilize. This is a hot-path property — every API list/detail call goes through it, so it has to be cheap on the no-op case.

### 6.1 The memoization (registry lines 547–558)

The memoization is **a DB-write skip, not an import skip.** `discover_strategies()` runs unconditionally for every list/detail call (registry lines 166–236) — every `.py` file under `strategies/` is re-AST-scanned, re-imported via `_import_strategy_module`, re-walked by `_find_strategy_class` / `_find_config_class`, and re-fed to `build_user_schema`. There is no "skip the import" branch. The savings are downstream of that work.

When a row already exists for a given `file_path` (registry lines 546–558):

- `row.name`, `row.description`, and `row.strategy_class` are **always overwritten** from the freshly-discovered values.
- The combined hash (file + sibling `config.py`) is compared to `row.code_hash`.
- If they match: the schema columns (`config_class`, `config_schema`, `default_config`, `config_schema_status`, `code_hash`) are **not written**. The discovered values were computed but are simply not persisted.
- If they differ: those five columns refresh atomically with the hash bump.

The practical effect: a steady-state list call still imports every strategy module (Python's import cache makes the second import cheap — `sys.modules` already holds the result, and `_import_strategy_module` registers there at line 328) and still computes schemas; what it skips is touching the DB row's schema columns when nothing changed. That's what keeps the latency dominated by a filesystem walk + N hashes (each in 8 KiB chunks of small `.py` files — measured in microseconds), with the import cost amortized through `sys.modules`.

If you _need_ a path that avoids re-importing modules entirely, it doesn't exist today — you'd add a "discover-from-DB only" mode that trusts the persisted row when `code_hash` is unchanged. That's a deferred enhancement; today's contract prioritizes consistency-with-disk over import-cost.

### 6.2 The combined hash invalidates on config-only edits

Section 1.3 covered the algorithm. The operational consequence: editing `strategies/example/config.py` to change a default _will_ bump every strategy in `strategies/example/` whose file imports from `config.py`. Each one's `code_hash` changes; each one re-imports on the next sync; each one's `default_config` reflects the new sibling config defaults.

This is the property you want — it would be a lineage bug if a backtest ran with "the old defaults" because the strategy file's bytes happened not to change when its config did.

### 6.3 Pruning (loop at lines 563–568; default at line 489)

`prune_missing=True` is the default (function signature, registry line 489). The contract:

- After upserting all discovered rows, run `DELETE FROM strategies WHERE file_path NOT IN (...scanned paths)`.
- Cascading deletes don't apply here — the rows go directly. Backtests' `strategy_id` FK is configured `ON DELETE SET NULL` so historical records become orphan-safe (they still carry `strategy_code_hash` for lineage).

You can override with `prune_missing=False` if you want a one-shot sync that doesn't delete (e.g., during a migration where files are temporarily moved). The router never sets this — every API call prunes.

### 6.4 Retry: same input → same output

Both the `_combined_strategy_hash` and the `compute_file_hash` are deterministic functions of file bytes. There is no retry needed at the registry level — if a sync fails halfway through (e.g., DB connection drop), the next call replays from scratch and converges to the right state.

---

## 7. Rollback / repair

### 7.1 Rolling back a bad strategy

You pushed a broken `awesome.py` and want to back it out.

**Option A — revert the file:**

```bash
git revert <commit-that-added-awesome.py>
git push
# pull on host, restart backend, sync runs
```

The DB row for `awesome.py` is removed by pruning on the next sync. **Backtests that already ran against the old `code_hash` keep their results** — their `strategy_code_hash` lineage column is intact, the QuantStats HTML files on disk are intact. They will, however, fail to "rerun" because the file is gone.

**Option B — hard delete via API:**

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://localhost:8800/api/v1/strategies/<id>
```

This deletes the DB row immediately. The file on disk is _not_ touched — and the next sync will re-create the row. So `DELETE` is only useful as a transient cleanup if you want to force a re-import on the next call (e.g., after manual DB fiddling broke the row).

> The soft-delete TODO from § 2.4 will eventually let `DELETE` mark a `deleted_at` timestamp without removing the row, and sync will respect that flag instead of re-creating. Until then, `DELETE` is hard and re-syncs eagerly.

### 7.2 The audit consequence — old hashes live on

A backtest stamped `strategy_code_hash = abc123…` and the row `file_path = strategies/foo.py` was deleted, undeleted, and edited five times since. The backtest's hash _doesn't change_. The lineage is "this run executed against the source whose SHA was abc123…, regardless of what's at that path now." This is deliberate — backtests are immutable artifacts whose reproducibility depends on the hash, not on whether the file still exists.

If you need to rerun an old backtest:

1. `git checkout <sha>` of the strategy's source (commit hash you'd ideally have stored alongside `strategy_code_hash` — `strategy_git_sha` column exists but is opt-in).
2. Confirm the new `compute_file_hash` matches the stored `strategy_code_hash`.
3. If yes: rerun is faithful.
4. If no: the file content has drifted (whitespace? line endings?), and you have to dig into the diff.

### 7.3 Repair: the row is wrong but the file is right

Symptom: `code_hash` in the DB doesn't match `compute_file_hash(file)` for some reason — perhaps a partial write, a bad migration, or someone manually UPDATE'd the row.

Repair:

```bash
# Force re-import on next sync by clearing the cache invalidation key
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U msai -d msai -c \
  "UPDATE strategies SET code_hash = '' WHERE id = '<id>';"

# Trigger sync
curl -H "Authorization: Bearer $TOKEN" http://localhost:8800/api/v1/strategies/ > /dev/null

# Verify
curl -H "Authorization: Bearer $TOKEN" http://localhost:8800/api/v1/strategies/<id> | jq .code_hash
```

A blank `code_hash` won't match anything, so `sync_strategies_to_db()` re-extracts schema/defaults/status atomically and writes the correct hash.

### 7.4 Repair: file moved, want the old UUID back

You can't get the old UUID back. That's by design — UUIDs are stable identifiers for `(file_path)` tuples in the registry's contract. Move the file → new `file_path` → new row → new UUID. If your callers (CI scripts, dashboards) bookmark UUIDs, they break on a move. The fix is to bookmark the strategy by `(strategy_class, name)` — those are stable across path changes — or to never move strategy files (treat the path as part of the identity).

---

## 8. Key files

| Path                                                              | Lines     | Purpose                                                                                  |
| ----------------------------------------------------------------- | --------- | ---------------------------------------------------------------------------------------- |
| `backend/src/msai/api/strategies.py`                              | 39–220    | Router: list, show, patch, validate, delete                                              |
| `backend/src/msai/api/strategies.py`                              | 39–72     | `GET /api/v1/strategies/` — list + sync                                                  |
| `backend/src/msai/api/strategies.py`                              | 75–116    | `GET /api/v1/strategies/{id}` — detail + independent sync                                |
| `backend/src/msai/api/strategies.py`                              | 119–155   | `PATCH /api/v1/strategies/{id}` — update default_config / desc                           |
| `backend/src/msai/api/strategies.py`                              | 158–194   | `POST /api/v1/strategies/{id}/validate` — file validation                                |
| `backend/src/msai/api/strategies.py`                              | 197–220   | `DELETE /api/v1/strategies/{id}` — hard-delete (soft-delete TODO)                        |
| `backend/src/msai/models/strategy.py`                             | 18, 29–58 | `Strategy` SQLAlchemy model (inherits `created_at`/`updated_at` via `TimestampMixin`)    |
| `backend/src/msai/models/base.py`                                 | 15–24     | `TimestampMixin` — `created_at` + `updated_at` columns                                   |
| `backend/src/msai/schemas/strategy.py`                            | 10–32     | `StrategyResponse` Pydantic schema                                                       |
| `backend/src/msai/services/strategy_registry.py`                  | 113–129   | `compute_file_hash()` — SHA256, 8KB chunks                                               |
| `backend/src/msai/services/strategy_registry.py`                  | 137–236   | `discover_strategies()` — filesystem walk + import + class find                          |
| `backend/src/msai/services/strategy_registry.py`                  | 244–276   | `validate_strategy_file()` — used by validate endpoint                                   |
| `backend/src/msai/services/strategy_registry.py`                  | 299–330   | `_import_strategy_module()` — `importlib.util.spec_from_file_location`                   |
| `backend/src/msai/services/strategy_registry.py`                  | 333–367   | `_find_strategy_class()` — first Nautilus Strategy subclass                              |
| `backend/src/msai/services/strategy_registry.py`                  | 370–421   | `_find_config_class()` — prefer same-module over imported                                |
| `backend/src/msai/services/strategy_registry.py`                  | 485–570   | `sync_strategies_to_db()` — idempotent upsert + prune                                    |
| `backend/src/msai/services/strategy_registry.py`                  | 553–558   | Memoization: skip schema recompute when hash unchanged                                   |
| `backend/src/msai/services/strategy_registry.py`                  | 563–568   | Pruning: orphaned rows deleted when `prune_missing=True`                                 |
| `backend/src/msai/services/strategy_registry.py`                  | 573–600   | `_combined_strategy_hash()` — file + sibling config.py                                   |
| `backend/src/msai/services/strategy_governance.py`                | —         | AST-based pre-import security review                                                     |
| `backend/src/msai/services/nautilus/schema_hooks.py`              | 48–61     | `ConfigSchemaStatus` enum: ready / unsupported / failed / no                             |
| `backend/src/msai/services/nautilus/schema_hooks.py`              | 63–113    | `nautilus_schema_hook()` — Venue/InstrumentId → JSON Schema                              |
| `backend/src/msai/services/nautilus/schema_hooks.py`              | 115–169   | `build_user_schema()` — msgspec.json.schema + trim + defaults                            |
| `backend/src/msai/services/nautilus/failure_isolated_strategy.py` | 22–43     | `FailureIsolatedStrategy` mixin + `__init_subclass__` event-handler wrapping             |
| `backend/src/msai/services/nautilus/failure_isolated_strategy.py` | 45–64     | `_make_safe_wrapper()` — degrade-check, try/except, set `_is_degraded`, log, return None |
| `backend/tests/unit/test_failure_isolated_strategy.py`            | —         | Mixin behavior tests (no production strategy uses the mixin yet)                         |
| `backend/src/msai/cli.py`                                         | 88, 102   | Strategy sub-app declaration                                                             |
| `backend/src/msai/cli.py`                                         | 285       | `msai strategy list`                                                                     |
| `backend/src/msai/cli.py`                                         | 297       | `msai strategy show <id>`                                                                |
| `backend/src/msai/cli.py`                                         | 306       | `msai strategy validate <id>`                                                            |
| `frontend/src/app/strategies/page.tsx`                            | 1–87      | List view — card grid                                                                    |
| `frontend/src/app/strategies/page.tsx`                            | 24–27     | `apiGet<StrategyListResponse>("/api/v1/strategies/", token)`                             |
| `frontend/src/app/strategies/[id]/page.tsx`                       | —         | Detail view + validate button                                                            |
| `strategies/example/ema_cross.py`                                 | 1–50      | `EMACrossStrategy` reference implementation                                              |
| `strategies/example/config.py`                                    | —         | `EMACrossConfig` shared config class (sibling)                                           |
| `strategies/example/smoke_market_order.py`                        | —         | Minimal smoke test (one market order)                                                    |

---

## Cross-references

- **Previous:** [How Symbols Work →](how-symbols-work.md) — instruments must resolve before a strategy can subscribe to anything.
- **Next:** [How Backtesting Works →](how-backtesting-works.md) — once a strategy is registered, you run it against historical data via the backtest worker. The `code_hash` from this doc is stamped onto every backtest row for lineage.

---

**Date verified against codebase:** 2026-04-28
**Citation source:** `scratch/citations-cluster-b.md` PART I (strategies subsystem)
