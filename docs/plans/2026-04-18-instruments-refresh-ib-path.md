# `msai instruments refresh --provider interactive_brokers` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete the deferred `--provider interactive_brokers` branch of `msai instruments refresh` so operators can pre-warm the instrument registry for closed-universe symbols before deploying live strategies.

**Architecture:** Three-phase CLI flow — preflight (validator + closed-universe check) → connect (short-lived Nautilus IB factory chain with caller-side timeout fence) → qualify (existing `IBQualifier` + `SecurityMaster.resolve_for_live`) → teardown (awaited async stop). Extracts the paper/live port-account validator from `live_node_config.py` to a shared module that also dedupes an inlined copy in the supervisor. Three new `Settings` fields wire timeouts + out-of-band `client_id=999`.

**Tech Stack:** Python 3.12 · Typer · pydantic-settings v2 · NautilusTrader 1.223.0 (pinned) · SQLAlchemy 2.0 async · pytest + pytest-asyncio · `uv` for packaging

**Context docs (read these first):**

- Design: `docs/plans/2026-04-18-instruments-refresh-ib-path-design.md`
- PRD: `docs/prds/instruments-refresh-ib-path.md`
- Research brief: `docs/research/2026-04-18-instruments-refresh-ib-path.md` ← **critical**, contains 4 design-changing Nautilus findings
- Gotchas reference: `.claude/rules/nautilus.md` (gotchas #3, #5, #6, #20)

---

## Working directory

All commands in this plan assume you are in the worktree backend:

```bash
cd /Users/pablomarin/Code/msai-v2/.worktrees/instruments-refresh-ib-path/claude-version/backend
```

Paths below are relative to that directory unless explicitly prefixed with `.worktrees/...`.

## Commit discipline

- Commit after each task passes its tests
- Commit-message prefix: `feat(ib-refresh)` for new functionality, `refactor(ib-refresh)` for the validator extraction, `test(ib-refresh)` for test-only additions
- Never squash across task boundaries — one task = one commit (bots will merge-squash at PR time)

---

## Phase A: Foundation — Settings + validator extraction

These tasks deduplicate existing code and add the three new Settings fields. No CLI behavior changes yet.

### Task A1: Add three IB Settings fields

**Files:**

- Modify: `src/msai/core/config.py` (after line 79, in the `Settings` class body — after existing `ib_port`)
- Test: `tests/unit/test_config_ib_env.py` (file exists — add 3 tests)

**Step 1: Write failing tests**

Append to `tests/unit/test_config_ib_env.py`:

```python
def test_ib_connect_timeout_seconds_default_and_env(monkeypatch):
    """Fresh instance reads IB_CONNECT_TIMEOUT_SECONDS alias; defaults to 5."""
    from msai.core.config import Settings
    # Default
    monkeypatch.delenv("IB_CONNECT_TIMEOUT_SECONDS", raising=False)
    assert Settings().ib_connect_timeout_seconds == 5
    # Env override
    monkeypatch.setenv("IB_CONNECT_TIMEOUT_SECONDS", "12")
    assert Settings().ib_connect_timeout_seconds == 12


def test_ib_request_timeout_seconds_default_and_env(monkeypatch):
    """Fresh instance reads IB_REQUEST_TIMEOUT_SECONDS alias; defaults to 30."""
    from msai.core.config import Settings
    monkeypatch.delenv("IB_REQUEST_TIMEOUT_SECONDS", raising=False)
    assert Settings().ib_request_timeout_seconds == 30
    monkeypatch.setenv("IB_REQUEST_TIMEOUT_SECONDS", "60")
    assert Settings().ib_request_timeout_seconds == 60


def test_ib_instrument_client_id_default_and_env(monkeypatch):
    """Fresh instance reads IB_INSTRUMENT_CLIENT_ID alias; defaults to 999."""
    from msai.core.config import Settings
    monkeypatch.delenv("IB_INSTRUMENT_CLIENT_ID", raising=False)
    assert Settings().ib_instrument_client_id == 999
    monkeypatch.setenv("IB_INSTRUMENT_CLIENT_ID", "900")
    assert Settings().ib_instrument_client_id == 900
```

**Step 2: Run tests — expect FAIL**

```bash
uv run pytest tests/unit/test_config_ib_env.py::test_ib_connect_timeout_seconds_default_and_env tests/unit/test_config_ib_env.py::test_ib_request_timeout_seconds_default_and_env tests/unit/test_config_ib_env.py::test_ib_instrument_client_id_default_and_env -v
```

Expected: 3 failures with `AttributeError: 'Settings' object has no attribute 'ib_connect_timeout_seconds'` etc.

**Step 3: Add Settings fields**

In `src/msai/core/config.py`, after the existing `ib_port` field (around line 79), add:

```python
    # ------------------------------------------------------------------
    # IB short-lived-client tunables (used by `msai instruments refresh`
    # and any other one-shot IB connection that isn't a live subprocess).
    # ------------------------------------------------------------------

    # Wall-clock budget for the IB Gateway TCP connection + client-ready
    # probe. Intentionally separate from ``ib_request_timeout_seconds``
    # so a dead gateway fails fast (~5s) while slow individual
    # qualifications still honor the longer per-request timeout.
    ib_connect_timeout_seconds: int = Field(
        default=5,
        validation_alias=AliasChoices("IB_CONNECT_TIMEOUT_SECONDS"),
    )

    # Post-connect per-request timeout for IB contract qualification
    # (``reqContractDetails`` round-trip). ``int`` matches Nautilus
    # ``get_cached_ib_client(request_timeout_secs=...)`` signature.
    ib_request_timeout_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices("IB_REQUEST_TIMEOUT_SECONDS"),
    )

    # Pragmatic default IB ``client_id`` for ``msai instruments
    # refresh`` — arbitrary integer outside the values an operator is
    # likely to set manually. NOTE: live subprocesses derive their
    # ``client_id`` from a 31-bit hash of the deployment slug
    # (``live_node_config.py:_derive_client_id``), so collision with
    # 999 is mathematically possible but extremely unlikely. Surfaced
    # in CLI help + every preflight log so the operator sees which
    # id the CLI is using. See nautilus.md gotcha #3 — two clients on
    # the same ``client_id`` silently disconnect each other.
    ib_instrument_client_id: int = Field(
        default=999,
        validation_alias=AliasChoices("IB_INSTRUMENT_CLIENT_ID"),
    )
```

**Step 4: Run tests — expect PASS**

```bash
uv run pytest tests/unit/test_config_ib_env.py -v
```

Expected: all tests pass (existing + 3 new).

**Step 5: Lint + typecheck**

```bash
uv run ruff check src/msai/core/config.py tests/unit/test_config_ib_env.py
uv run mypy src/msai/core/config.py --strict
```

Expected: clean.

**Step 6: Commit**

```bash
git add src/msai/core/config.py tests/unit/test_config_ib_env.py
git commit -m "feat(ib-refresh): add ib_connect_timeout_seconds + ib_request_timeout_seconds + ib_instrument_client_id Settings fields"
```

---

### Task A2: Extract `validate_port_account_consistency` to shared module

**Files:**

- Create: `src/msai/services/nautilus/ib_port_validator.py`
- Create: `tests/unit/test_ib_port_validator.py`
- Read only (for extraction): `src/msai/services/nautilus/live_node_config.py:66-215` (current location of validator + 3 constants)

**Step 1: Write failing tests**

Create `tests/unit/test_ib_port_validator.py`:

```python
"""Tests for the extracted IB port/account consistency validator.

Covers the gotcha #6 guard (paper port + live account prefix silently
misroutes orders) as a free-standing pure function. Combinatorial coverage
across paper/live ports (raw + socat) and account-prefix families (DU,
DF, U*).
"""
from __future__ import annotations

import pytest

from msai.services.nautilus.ib_port_validator import (
    IB_LIVE_PORTS,
    IB_PAPER_PORTS,
    IB_PAPER_PREFIXES,
    validate_port_account_consistency,
    validate_port_vs_paper_trading,
)


# ---------------------------------------------------------------------------
# validate_port_account_consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", [4002, 4004])
@pytest.mark.parametrize("account_id", ["DU1234567", "DF1234567", "DFP1234567"])
def test_paper_port_with_paper_account_passes(port: int, account_id: str) -> None:
    """Paper ports (4002 raw, 4004 socat) accept any DU/DF prefix."""
    validate_port_account_consistency(port, account_id)  # must not raise


@pytest.mark.parametrize("port", [4001, 4003])
@pytest.mark.parametrize("account_id", ["U1234567", "U9876543"])
def test_live_port_with_live_account_passes(port: int, account_id: str) -> None:
    """Live ports (4001 raw, 4003 socat) accept non-paper prefixes."""
    validate_port_account_consistency(port, account_id)  # must not raise


@pytest.mark.parametrize("port", [4001, 4003])
@pytest.mark.parametrize("account_id", ["DU1234567", "DF1234567"])
def test_live_port_with_paper_account_raises(port: int, account_id: str) -> None:
    """Gotcha #6: live port + paper prefix would silently misroute."""
    with pytest.raises(ValueError, match="paper"):
        validate_port_account_consistency(port, account_id)


@pytest.mark.parametrize("port", [4002, 4004])
@pytest.mark.parametrize("account_id", ["U1234567"])
def test_paper_port_with_live_account_raises(port: int, account_id: str) -> None:
    """Gotcha #6 inverse: paper port + live prefix is equally dangerous."""
    with pytest.raises(ValueError, match="live"):
        validate_port_account_consistency(port, account_id)


def test_unknown_port_raises() -> None:
    """Ports outside the known paper/live sets are rejected explicitly."""
    with pytest.raises(ValueError, match="unknown"):
        validate_port_account_consistency(4005, "DU1234567")


def test_whitespace_padded_account_is_normalized() -> None:
    """Account IDs with surrounding whitespace are stripped before validation."""
    validate_port_account_consistency(4002, "  DU1234567  ")  # must not raise


def test_empty_account_raises() -> None:
    with pytest.raises(ValueError, match="account"):
        validate_port_account_consistency(4002, "")


# ---------------------------------------------------------------------------
# validate_port_vs_paper_trading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", [4002, 4004])
def test_paper_port_with_paper_trading_true_passes(port: int) -> None:
    validate_port_vs_paper_trading(port, paper_trading=True)


@pytest.mark.parametrize("port", [4001, 4003])
def test_live_port_with_paper_trading_false_passes(port: int) -> None:
    validate_port_vs_paper_trading(port, paper_trading=False)


def test_paper_port_with_paper_trading_false_raises() -> None:
    with pytest.raises(ValueError, match="paper_trading=False"):
        validate_port_vs_paper_trading(4002, paper_trading=False)


def test_live_port_with_paper_trading_true_raises() -> None:
    with pytest.raises(ValueError, match="paper_trading=True"):
        validate_port_vs_paper_trading(4001, paper_trading=True)


# ---------------------------------------------------------------------------
# Constant shape
# ---------------------------------------------------------------------------


def test_paper_ports_include_raw_and_socat() -> None:
    assert 4002 in IB_PAPER_PORTS
    assert 4004 in IB_PAPER_PORTS


def test_live_ports_include_raw_and_socat() -> None:
    assert 4001 in IB_LIVE_PORTS
    assert 4003 in IB_LIVE_PORTS


def test_paper_prefixes_cover_du_and_df() -> None:
    assert "DU" in IB_PAPER_PREFIXES
    assert "DF" in IB_PAPER_PREFIXES
```

**Step 2: Run tests — expect FAIL**

```bash
uv run pytest tests/unit/test_ib_port_validator.py -v
```

Expected: ~14 failures — module doesn't exist.

**Step 3: Create the validator module**

Read the current validator at `src/msai/services/nautilus/live_node_config.py:66-215` to understand the exact rules. Then create `src/msai/services/nautilus/ib_port_validator.py`:

```python
"""IB Gateway port + account consistency validator.

Extracted from ``live_node_config.py`` so both the live subprocess
builder AND ``msai instruments refresh`` can enforce the gotcha #6 guard
without one importing subprocess-only deps from the other.

IB Gateway listens on:

- ``4001`` — live trading (raw)
- ``4002`` — paper trading (raw)
- ``4003`` — live trading (socat proxy, for cross-container access)
- ``4004`` — paper trading (socat proxy, for cross-container access)

IB account IDs start with:

- ``DU`` — standard paper account
- ``DF`` / ``DFP`` — Financial Advisor (FA) paper sub-accounts
- Anything else (typically ``U`` followed by digits) — live account

Pairing a live port with a paper account (or vice-versa) silently
misroutes orders — gotcha #6 is "no error, just wrong venue." This module
catches the misconfiguration BEFORE any IB connection is attempted.
"""
from __future__ import annotations

# Accept both raw IB ports and the socat proxy ports shipped in
# docker-compose.dev.yml / docker-compose.prod.yml.
IB_PAPER_PORTS: tuple[int, ...] = (4002, 4004)
IB_LIVE_PORTS: tuple[int, ...] = (4001, 4003)

# Paper account prefix families. ``DU`` is the standard personal paper
# prefix; ``DF``/``DFP`` are the FA sub-account prefixes used on
# combined advisor/sub-account setups.
IB_PAPER_PREFIXES: tuple[str, ...] = ("DU", "DF")


def validate_port_account_consistency(port: int, account_id: str) -> None:
    """Raise ``ValueError`` if ``port`` and ``account_id`` disagree on
    paper vs live.

    The account id is ``.strip()``-ed before prefix matching so that
    stray whitespace from a misformatted ``.env`` file can't sneak a
    silent mismatch past the guard.

    Args:
        port: One of ``IB_PAPER_PORTS`` or ``IB_LIVE_PORTS``.
        account_id: IB account identifier (e.g. ``DU1234567``,
            ``U9876543``).

    Raises:
        ValueError: If ``port`` is unknown, ``account_id`` is empty, or
            the port's paper/live nature doesn't match the account's
            prefix.
    """
    normalized = account_id.strip()
    if not normalized:
        raise ValueError("IB account_id is empty; set IB_ACCOUNT_ID")

    if port in IB_PAPER_PORTS:
        port_is_paper = True
    elif port in IB_LIVE_PORTS:
        port_is_paper = False
    else:
        raise ValueError(
            f"unknown IB port {port}; expected one of "
            f"{IB_PAPER_PORTS + IB_LIVE_PORTS}"
        )

    account_is_paper = normalized.startswith(IB_PAPER_PREFIXES)

    if port_is_paper and not account_is_paper:
        raise ValueError(
            f"paper port {port} paired with live-prefix account "
            f"{normalized!r}; set IB_PORT to a live port "
            f"{IB_LIVE_PORTS} or change IB_ACCOUNT_ID to a "
            f"paper-prefix account ({'/'.join(IB_PAPER_PREFIXES)}*)"
        )

    if not port_is_paper and account_is_paper:
        raise ValueError(
            f"live port {port} paired with paper-prefix account "
            f"{normalized!r}; set IB_PORT to a paper port "
            f"{IB_PAPER_PORTS} or change IB_ACCOUNT_ID to a non-paper "
            f"account"
        )


def validate_port_vs_paper_trading(port: int, paper_trading: bool) -> None:
    """Raise ``ValueError`` if ``port`` disagrees with an explicit
    ``paper_trading`` flag.

    Used by the live supervisor where the deployment row carries the
    operator's intent as a boolean, independent of the account id
    string.

    Args:
        port: One of ``IB_PAPER_PORTS`` or ``IB_LIVE_PORTS``.
        paper_trading: Operator intent from the deployment row.

    Raises:
        ValueError: If ``port`` is unknown or contradicts
            ``paper_trading``.
    """
    if port in IB_PAPER_PORTS:
        port_is_paper = True
    elif port in IB_LIVE_PORTS:
        port_is_paper = False
    else:
        raise ValueError(
            f"unknown IB port {port}; expected one of "
            f"{IB_PAPER_PORTS + IB_LIVE_PORTS}"
        )

    if paper_trading and not port_is_paper:
        raise ValueError(
            f"deployment has paper_trading=True but IB_PORT={port} is a "
            f"live port {IB_LIVE_PORTS}; flip IB_PORT to a paper port "
            f"{IB_PAPER_PORTS} or unset paper_trading"
        )

    if not paper_trading and port_is_paper:
        raise ValueError(
            f"deployment has paper_trading=False but IB_PORT={port} is "
            f"a paper port {IB_PAPER_PORTS}; flip IB_PORT to a live "
            f"port {IB_LIVE_PORTS} or set paper_trading=True"
        )
```

**Step 4: Run tests — expect PASS**

```bash
uv run pytest tests/unit/test_ib_port_validator.py -v
```

Expected: all tests pass.

**Step 5: Lint + typecheck**

```bash
uv run ruff check src/msai/services/nautilus/ib_port_validator.py tests/unit/test_ib_port_validator.py
uv run mypy src/msai/services/nautilus/ib_port_validator.py --strict
```

**Step 6: Commit**

```bash
git add src/msai/services/nautilus/ib_port_validator.py tests/unit/test_ib_port_validator.py
git commit -m "feat(ib-refresh): extract validate_port_account_consistency to shared module (nautilus.md gotcha #6)"
```

---

### Task A3: Rewire `live_node_config.py` to import from the new module

**Files:**

- Modify: `src/msai/services/nautilus/live_node_config.py` (remove lines 66-78 constants + 177-215 validator; update imports + call sites)
- Modify: `tests/unit/test_live_node_config.py` (ensure existing tests still pass; remove any that duplicated validator coverage now in test_ib_port_validator)

**Step 1: Audit existing tests to avoid accidental coverage loss**

```bash
grep -n "_validate_port_account_consistency\|_IB_PAPER_PORTS\|_IB_LIVE_PORTS\|_IB_PAPER_PREFIXES" tests/unit/test_live_node_config.py
```

Note which test names reference the private helper. These tests are redundant with `test_ib_port_validator.py` — mark them for deletion in Step 4.

**Step 2: Update `live_node_config.py`**

In `src/msai/services/nautilus/live_node_config.py`:

Delete lines 66-78 (the `_IB_PAPER_PORTS` / `_IB_LIVE_PORTS` / `_IB_PAPER_PREFIXES` module-level tuples).

Delete lines 177-215 (the local `_validate_port_account_consistency` function).

At the top of the file (alongside existing imports), add:

```python
from msai.services.nautilus.ib_port_validator import validate_port_account_consistency
```

At the call site (currently around line 308 where `_validate_port_account_consistency(ib_settings.port, normalized_account_id)` is called), replace with:

```python
validate_port_account_consistency(ib_settings.port, normalized_account_id)
```

**Step 3: Run the full live_node_config test suite**

```bash
uv run pytest tests/unit/test_live_node_config.py tests/unit/test_live_node_config_cache.py tests/unit/test_live_node_config_recovery.py tests/unit/test_live_node_config_risk.py -v
```

Expected: all pass (the behavior is identical; only the import path changed).

**Step 4: Remove redundant tests from `test_live_node_config.py`**

For each test whose name + body exclusively exercised the private helper's behavior (prefix matching, port classification), delete it. Any test that exercised `build_live_trading_node_config` end-to-end MUST stay.

**Step 5: Re-run the affected test suite**

```bash
uv run pytest tests/unit/test_live_node_config.py tests/unit/test_live_node_config_cache.py tests/unit/test_live_node_config_recovery.py tests/unit/test_live_node_config_risk.py -v
```

Expected: all pass; test count may drop by however many redundant tests you removed.

**Step 6: Lint + typecheck**

```bash
uv run ruff check src/msai/services/nautilus/live_node_config.py tests/unit/test_live_node_config.py
uv run mypy src/msai/services/nautilus/live_node_config.py --strict
```

**Step 7: Commit**

```bash
git add src/msai/services/nautilus/live_node_config.py tests/unit/test_live_node_config.py
git commit -m "refactor(ib-refresh): live_node_config uses shared validator (dedup #1 of 2)"
```

---

### Task A4: Rewire `live_supervisor/__main__.py` inline policy to validator calls

**Critical:** The supervisor validates the DEPLOYMENT ROW's `account_id` (per-deployment, from DB), NOT `settings.ib_account_id` (process-level env). This is intentional — each deployment carries its own account as source of truth. The new validator call MUST preserve this.

**Files:**

- Modify: `src/msai/live_supervisor/__main__.py:162-199` (the two-block inline policy — port-vs-paper_trading AND port-vs-deployment_account)
- Create: `tests/unit/test_live_supervisor_payload_validation.py` (new — the existing `test_live_supervisor_main.py` only covers dispatcher/ACK semantics, never imports `_build_production_payload_factory`)

**Step 1: Re-read the exact lines to replace**

```bash
sed -n '160,205p' src/msai/live_supervisor/__main__.py
```

Confirm the block you're replacing spans both inline checks (port-vs-paper_trading at ~162-175 AND port-vs-deployment_account at ~178-199, using `deployment_account = (deployment.account_id or "").strip()`).

**Step 2: Write failing test first (new file) — tests ACTUAL supervisor call site**

Iter-2 plan review finding P1: a test that only calls the shared validator directly doesn't prove the supervisor wires it correctly. This test exercises `_build_production_payload_factory` and asserts that (a) validators are actually invoked and (b) the account argument comes from `deployment.account_id`, NOT `settings.ib_account_id`.

Create `tests/unit/test_live_supervisor_payload_validation.py`:

```python
"""Proves the supervisor's payload factory calls the shared IB port
validators with the DEPLOYMENT ROW's account_id — NOT the process-wide
settings.ib_account_id.

Iter-2 review caught a too-weak test that only exercised the validators
themselves; this version asserts the call site in
`_build_production_payload_factory` binds the right argument."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_payload_factory_validates_with_deployment_account_not_settings():
    """When IB_PORT=4001 (live) and the deployment row's account_id is
    a paper 'DU*' account, the factory must RAISE — regardless of what
    settings.ib_account_id says. This catches the iter-1 regression
    where the factory would have validated settings.ib_account_id
    instead of deployment.account_id."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    # Mock deployment row: paper account on LIVE port → must raise
    mock_deployment = MagicMock()
    mock_deployment.paper_trading = False  # matches IB_PORT=4001
    mock_deployment.account_id = "DU1234567"  # paper account — MISMATCH

    # Session returns the mock deployment
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
        execute=AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=mock_deployment),
        )),
    ))
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    session_factory = MagicMock(return_value=mock_session_ctx)

    # Settings with DIFFERENT ib_account_id from deployment's — proves
    # the factory is using the deployment row, not settings.
    with patch("msai.live_supervisor.__main__.settings") as mock_settings:
        mock_settings.ib_port = 4001  # live port
        mock_settings.ib_account_id = "U9999999"  # live account (matches port)
        # ^ If the factory validated settings.ib_account_id instead of
        #   deployment.account_id, this would PASS (4001 + U9999999 is valid).
        #   The mismatch must surface from deployment.account_id (DU1234567).

        factory = _build_production_payload_factory(session_factory)

        with pytest.raises(ValueError, match=r"DU1234567|paper"):
            await factory(
                row_id=uuid4(),
                deployment_id=uuid4(),
                deployment_slug="test-slug",
                payload_dict={},
            )


@pytest.mark.asyncio
async def test_payload_factory_validates_paper_trading_vs_port():
    """Second half of gotcha #6: deployment.paper_trading must match
    port even if account_id is consistent."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    mock_deployment = MagicMock()
    mock_deployment.paper_trading = True  # operator said 'paper'
    mock_deployment.account_id = "DU1234567"  # paper account — consistent

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
        execute=AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=mock_deployment),
        )),
    ))
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=mock_session_ctx)

    with patch("msai.live_supervisor.__main__.settings") as mock_settings:
        mock_settings.ib_port = 4001  # LIVE port — conflicts with paper_trading=True
        mock_settings.ib_account_id = "DU1234567"

        factory = _build_production_payload_factory(session_factory)

        with pytest.raises(ValueError, match="paper_trading=True"):
            await factory(
                row_id=uuid4(),
                deployment_id=uuid4(),
                deployment_slug="test-slug",
                payload_dict={},
            )
```

**Step 3: Run — expect FAIL** (the supervisor hasn't been edited yet; it still uses `settings.ib_account_id` via the old inline logic). If the current supervisor already raises for BOTH cases (because gotcha #6 is also inline for deployment_account), this test may incidentally pass on current main; that's a true-positive either way — we still want the assertion to survive the extraction:

```bash
uv run pytest tests/unit/test_live_supervisor_payload_validation.py -v
```

If the test passes on current main (before Step 4), that only means the CURRENT inline code already uses deployment.account_id — good. The test now GUARDS against regression when Step 4 swaps in the new validator calls.

**Step 4: Replace inlined supervisor policy**

In `src/msai/live_supervisor/__main__.py`, replace the inline check block (approximately lines 162-199 — both the port-vs-paper_trading block AND the port-vs-deployment_account block) with the SAME two validator calls, but feeding them the existing variables — NOT `settings.ib_account_id`:

```python
            # Gotcha #6 guard: validate port vs (a) the deployment row's
            # paper_trading flag, and (b) the deployment row's account_id
            # (NOT settings.ib_account_id — account is per-deployment).
            #
            # Keep `deployment_account` local so the downstream payload
            # assembly (line ~373) still receives the stripped value
            # the subprocess expects.
            from msai.services.nautilus.ib_port_validator import (
                validate_port_account_consistency,
                validate_port_vs_paper_trading,
            )

            deployment_account = (deployment.account_id or "").strip()
            try:
                validate_port_vs_paper_trading(
                    settings.ib_port,
                    paper_trading=deployment.paper_trading,
                )
                validate_port_account_consistency(
                    settings.ib_port,
                    deployment_account,
                )
            except ValueError as exc:
                raise ValueError(
                    f"deployment {deployment_id}: {exc}"
                ) from exc
```

Keep the local-import (not module-top) — avoids import-cycle risk since the supervisor loads many modules at startup.

**Step 5: Verify the `deployment_account` variable is still consumed downstream**

```bash
grep -n "deployment_account" src/msai/live_supervisor/__main__.py
```

Expected: references at the original lines (185-190 area) and at the payload-assembly call (~373, ~389). The replacement block reintroduces the `deployment_account` local on the same line it used to appear, so downstream code is unaffected.

**Step 6: Run tests (unit + existing supervisor tests)**

```bash
uv run pytest tests/unit/test_live_supervisor_payload_validation.py tests/unit/test_live_supervisor_main.py -v
```

Expected: all pass. If `test_live_supervisor_main.py` asserts any substring from the OLD inline error messages, update to the new (validator-produced) messages.

**Step 7: Lint + typecheck**

```bash
uv run ruff check src/msai/live_supervisor/__main__.py tests/unit/test_live_supervisor_payload_validation.py
uv run mypy src/msai/live_supervisor/__main__.py --strict
```

**Step 8: Commit**

```bash
git add src/msai/live_supervisor/__main__.py tests/unit/test_live_supervisor_payload_validation.py tests/unit/test_live_supervisor_main.py
git commit -m "refactor(ib-refresh): supervisor uses shared validator, preserving deployment.account_id as source of truth"
```

---

### Task A5: Add autouse pytest fixture to clear Nautilus factory globals

**Files:**

- Modify: `tests/unit/conftest.py` (append fixture; create file if it doesn't exist at that exact path — check first)

**Step 1: Locate the right `conftest.py`**

```bash
ls tests/unit/conftest.py 2>/dev/null && echo "exists" || echo "missing"
ls tests/conftest.py 2>/dev/null && echo "exists" || echo "missing"
```

If `tests/unit/conftest.py` doesn't exist, create it. If it does, append to it.

**Step 2: Add the fixture**

Append to `tests/unit/conftest.py`:

```python
"""Shared unit-test fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_ib_factory_globals():
    """Clear Nautilus IB adapter factory globals between tests.

    Rationale (research brief finding #3): Nautilus 1.223.0 caches
    clients/providers in module-level dicts that have no ``.clear()``
    helper. Between unit tests that touch ``get_cached_ib_client`` or
    ``get_cached_interactive_brokers_instrument_provider``, a stale
    cached client from an earlier test can leak into a later one.
    Production is unaffected because each ``msai instruments refresh``
    invocation is a fresh process.

    We don't clear ``GATEWAYS`` because that dict is only populated
    when ``dockerized_gateway=...`` is passed to ``get_cached_ib_client``
    — the CLI never does, so the dict stays empty in our test paths.

    Runs on every unit test (autouse) but the clear is cheap: the
    dicts are empty when untouched.
    """
    yield
    try:
        from nautilus_trader.adapters.interactive_brokers import factories
    except ImportError:
        # Running without Nautilus installed (some CI jobs skip heavy
        # deps). No globals to clear.
        return
    factories.IB_CLIENTS.clear()
    factories.IB_INSTRUMENT_PROVIDERS.clear()
```

**Step 3: Run full unit test suite to confirm no regressions**

```bash
uv run pytest tests/unit -q
```

Expected: same pass count as before the fixture (no test changed behavior; just ensures hygiene).

**Step 4: Commit**

```bash
git add tests/unit/conftest.py
git commit -m "test(ib-refresh): autouse fixture clears Nautilus IB factory globals between unit tests"
```

---

### Task A6: Fix `_spec_from_canonical` to set expiry for fixed-month futures

**Motivation (Codex plan-review iteration 1, P1):** `resolve_for_live(["ES"])` currently flows through
`canonical_instrument_id("ES", today)` → `ESM6.CME` → `_spec_from_canonical` returns
`InstrumentSpec(asset_class="future", symbol="ESM6", venue="CME")` with **no expiry**.
`IBQualifier.spec_to_ib_contract` maps future-without-expiry to `CONTFUT`, but ES on CME needs
`FUT` + `lastTradeDateOrContractMonth`. Without this fix, the IB branch will fail on ES despite
the rest of the CLI wiring being correct. This path has been latent (`--provider interactive_brokers`
was deferred) — the CLI is the first caller to exercise it.

**Files:**

- Modify: `src/msai/services/nautilus/security_master/service.py:249` (change `today = datetime.now(UTC).date()` → `today = exchange_local_today()` — the bootstrap helpers require exchange-local date to avoid roll-boundary drift, per existing invariant at `tests/unit/test_live_instrument_bootstrap.py:297-310`)
- Modify: `src/msai/services/nautilus/security_master/service.py:206-280` (`resolve_for_live` — pass the shared `today` through `_spec_from_canonical`)
- Modify: `src/msai/services/nautilus/security_master/service.py:522-560` (`_spec_from_canonical` — accept optional `today` kwarg, set expiry for futures)
- Modify: `tests/integration/test_security_master_resolve_live.py` (add a regression asserting `ES` resolves with a FUT spec + specific non-`ESM6` root symbol — the stronger check iter-2 review flagged)

**Step 1: Write failing test**

Add to `tests/integration/test_security_master_resolve_live.py` (the fixture is `session_factory: async_sessionmaker[AsyncSession]` — see lines 10, 46-63 of the existing file for usage; the test below mirrors an existing fixture consumer pattern):

Uses the same mock-instrument shape as the existing cold-miss test at `test_security_master_resolve_live.py:98-160` (`raw_symbol.value`, `__class__.__name__`, `to_dict`, `_provider.contract_details`). The test captures the spec the qualifier receives and asserts its fields.

```python
@pytest.mark.asyncio
async def test_resolve_for_live_es_routes_through_fixed_month_future(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression (plan-review iter 1 P1 / iter 2 & 3 refined):
    resolving ES through resolve_for_live's cold path must build a
    FUT spec with expiry — NOT a CONTFUT spec (expiry=None), and NOT
    an ESM6-rooted spec that would double-encode the month via
    InstrumentSpec.canonical_id → "ESM6M6.CME".

    Asserts:
      (a) expiry set on the spec (rules out CONTFUT)
      (b) root symbol 'ES' (not 'ESM6' — rules out duplicate-month bug)
      (c) asset_class + venue are future/CME (full roundtrip shape).

    Uses the same mock shape as the existing cold-miss test
    (`test_resolve_for_live_cold_miss_calls_ib_and_upserts`).
    """
    async with session_factory() as session:
        # Capture the spec the qualifier receives — the whole point of
        # this test — and return a fake Instrument of the right shape.
        captured_specs: list = []

        fake_instrument = MagicMock()
        fake_instrument.id = MagicMock()
        fake_instrument.id.__str__ = MagicMock(return_value="ESM6.CME")
        fake_instrument.id.venue.value = "CME"
        fake_instrument.raw_symbol.value = "ES"
        # Matches what Nautilus's FUT class name is — falls through
        # `_asset_class_for_instrument` to 'future'.
        fake_instrument.__class__.__name__ = "FuturesContract"
        fake_instrument.to_dict = MagicMock(
            return_value={
                "type": "FuturesContract",
                "instrument_id": "ESM6.CME",
                "raw_symbol": "ES",
            }
        )

        async def _capture_and_return(spec):
            captured_specs.append(spec)
            return fake_instrument

        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock(side_effect=_capture_and_return)

        mock_provider = MagicMock()
        fake_details = MagicMock()
        fake_details.contract.primaryExchange = "CME"
        mock_provider.contract_details = {fake_instrument.id: fake_details}
        mock_qualifier._provider = mock_provider

        sm = SecurityMaster(qualifier=mock_qualifier, db=session)

        # Act
        ids = await sm.resolve_for_live(["ES"])

        # Assert — return value shape
        assert len(ids) == 1

        # Assert — the spec the qualifier got
        assert len(captured_specs) == 1
        spec = captured_specs[0]
        assert spec.asset_class == "future"
        assert spec.venue == "CME"
        assert spec.expiry is not None, "gotcha: no expiry → maps to CONTFUT"
        assert spec.symbol == "ES", (
            f"must be root 'ES', not local-symbol 'ESM6' — otherwise "
            f"InstrumentSpec.canonical_id produces 'ESM6M6.CME'. "
            f"Got: {spec.symbol!r}"
        )
```

**Step 2: Run — expect FAIL** (current `_spec_from_canonical` returns `expiry=None`)

**Step 3: Patch `_spec_from_canonical`**

Thread `today` through:

```python
    def _spec_from_canonical(
        self, canonical: str, *, today: date | None = None,
    ) -> InstrumentSpec:
        """Parse canonical alias → InstrumentSpec.

        `today` is used to compute the fixed-month future expiry for
        the ES path (canonical like ``ESM6.CME``). Without it, futures
        become CONTFUT at the IB qualifier, which IB resolves to the
        continuous-future placeholder rather than the concrete
        front-month IB Gateway actually fills orders on.
        """
        symbol, _, venue = canonical.rpartition(".")
        if not venue:
            raise ValueError(f"Canonical alias {canonical!r} has no venue suffix")
        if venue == "NASDAQ":
            return InstrumentSpec(asset_class="equity", symbol=symbol, venue="NASDAQ")
        if venue == "ARCA":
            return InstrumentSpec(asset_class="equity", symbol=symbol, venue="ARCA")
        if venue == "IDEALPRO":
            base, _, quote = symbol.partition("/")
            return InstrumentSpec(
                asset_class="forex",
                symbol=base,
                venue="IDEALPRO",
                currency=quote or "USD",
            )
        if venue == "CME":
            # Import locally to avoid a security_master → live_instrument_bootstrap
            # cycle at module import time.
            from datetime import timedelta
            from msai.services.nautilus.live_instrument_bootstrap import (
                _current_quarterly_expiry,
                exchange_local_today,
            )
            if today is None:
                # Use CME-local date (America/Chicago), same as the
                # supervisor's spawn_today — otherwise on late-UTC-night
                # runs the UTC date disagrees with the exchange date.
                today = exchange_local_today()
            # The incoming `symbol` is the canonical-alias local-symbol
            # form (e.g. "ESM6") — root + 1-char month code + 1-digit
            # year. InstrumentSpec.canonical_id RECOMPUTES that suffix
            # from the expiry, so if we pass "ESM6" AND an expiry we'd
            # get "ESM6M6.CME" (duplicated). Strip to the root.
            # Closed-universe assumption: every CME symbol produced by
            # canonical_instrument_id follows the 2-char-suffix pattern.
            root = symbol[:-2]
            # _current_quarterly_expiry returns YYYYMM (6 chars — month
            # precision, not day precision — e.g. "202606"). Compute
            # the actual third-Friday expiry so spec_to_ib_contract
            # emits the yyyyMMdd IB expects.
            expiry_str = _current_quarterly_expiry(today)  # YYYYMM
            year = int(expiry_str[0:4])
            month = int(expiry_str[4:6])
            # Third Friday of (year, month) — matches
            # _current_quarterly_expiry's own internal rule at
            # live_instrument_bootstrap.py:102-104.
            first_of_month = date(year, month, 1)
            first_friday_offset = (4 - first_of_month.weekday()) % 7
            third_friday = first_of_month + timedelta(days=first_friday_offset + 14)
            return InstrumentSpec(
                asset_class="future",
                symbol=root,  # "ES" — InstrumentSpec re-derives "ESM6" from expiry
                venue="CME",
                expiry=third_friday,
            )
        raise ValueError(
            f"Unknown venue {venue!r} in canonical {canonical!r} — extend "
            "SecurityMaster._spec_from_canonical for new venues."
        )
```

Then update `resolve_for_live` in two places:

1. At line 249, switch the caller's `today` from UTC to exchange-local so it agrees with `canonical_instrument_id` and `build_ib_instrument_provider_config` (which use America/Chicago — see invariant test at `tests/unit/test_live_instrument_bootstrap.py:297-310`):

```python
        # Replace:
        #   today = datetime.now(UTC).date()
        # With:
        from msai.services.nautilus.live_instrument_bootstrap import (
            exchange_local_today,
        )
        today = exchange_local_today()
```

2. At the cold-path call site (around line 279), pass `today=today` through to `_spec_from_canonical`:

```python
            canonical = canonical_instrument_id(sym, today=today)
            spec = self._spec_from_canonical(canonical, today=today)
            # ... rest unchanged
```

**Step 4: Run — expect PASS**

```bash
uv run pytest tests/integration/test_security_master_resolve_live.py::test_resolve_for_live_es_routes_through_fixed_month_future -v
```

**Step 5: Run full resolve_for_live suite to catch regressions**

```bash
uv run pytest tests/integration/test_security_master_resolve_live.py tests/unit/test_security_master* -v
```

**Step 6: Lint + typecheck**

```bash
uv run ruff check src/msai/services/nautilus/security_master/service.py
uv run mypy src/msai/services/nautilus/security_master/service.py --strict
```

**Step 7: Commit**

```bash
git add src/msai/services/nautilus/security_master/service.py tests/integration/test_security_master_resolve_live.py
git commit -m "fix(security-master): _spec_from_canonical sets FUT expiry for CME futures (closes CONTFUT misroute)"
```

---

## Phase B: CLI implementation — `--provider interactive_brokers` branch

Now the real work. Each task is 2-5 steps of red-green-refactor.

### Task B1: Unknown-symbol preflight + closed-universe allow-list (and remove stale deferral test)

**Files:**

- Modify: `src/msai/cli.py:694-803` (the `instruments_refresh` function)
- Modify: `tests/unit/test_cli_instruments_refresh.py` — delete the obsolete stub test AND add a new failing test

**Step 1: Audit the stale deferral-path test**

```bash
grep -n "interactive_brokers.*deferred\|follow-up PR\|Task B1.*not yet" tests/unit/test_cli_instruments_refresh.py
```

There is an existing test (around `tests/unit/test_cli_instruments_refresh.py:172`) that asserts the IB branch errors with the "deferred / use databento" stub message. It will become a false FAIL the moment the stub is replaced. **Delete that test now** before touching the CLI — it has no future, and leaving it breaks the red-green TDD rhythm for the real tests.

**Step 2: Delete the stale test**

Remove the single `def test_*` block that asserts the deferral behaviour. If unclear from grep which block, search for these substrings inside the test body: `"deferred to a follow-up"`, `"use databento"`, or `"follow-up PR"` — whichever appears is the marker. Do NOT remove any test that covers the current Databento branch.

**Step 3: Write NEW failing test (appends to the same file)**

```python
def test_ib_provider_rejects_unknown_symbol():
    """Symbols outside PHASE_1_PAPER_SYMBOLS are rejected in preflight,
    before any IB connection is attempted."""
    from typer.testing import CliRunner
    from msai.cli import app

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        app,
        [
            "instruments", "refresh",
            "--symbols", "NVDA",
            "--provider", "interactive_brokers",
        ],
    )
    assert result.exit_code != 0
    combined = (result.stderr or "") + (result.stdout or "")
    # Error names the symbol AND the closed universe
    assert "NVDA" in combined
    assert "AAPL" in combined  # a symbol from PHASE_1_PAPER_SYMBOLS
```

**Note on the settings monkeypatch pattern.** `msai/cli.py:64` does `from msai.core.config import settings` at import time, which binds `settings` as a local module attribute in `msai.cli`. Replacing `msai.core.config.settings` after import has **no effect** on the CLI. Any test that needs to change Settings for the CLI MUST patch `msai.cli.settings` directly OR construct a new Settings instance and replace it on the CLI module. Pattern used below and in Task B2/B3:

```python
import msai.cli as cli_mod
from msai.core.config import Settings
monkeypatch.setattr(cli_mod, "settings", Settings())
```

This test doesn't need the pattern because the CLI's own `settings` object is still imported once per test process — the closed-universe check reads it via `msai.services.nautilus.live_instrument_bootstrap` not `msai.cli.settings`, so no env-var control is needed here.

**Step 4: Run test — expect FAIL**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py::test_ib_provider_rejects_unknown_symbol -v
```

Expected: FAIL — the branch still hits the deferral `_fail(...)`.

**Step 5: Replace the deferral branch with a preflight skeleton**

In `src/msai/cli.py`, replace the `if provider == "interactive_brokers":` block starting at line 747 with:

```python
    if provider == "interactive_brokers":
        from msai.services.nautilus.live_instrument_bootstrap import (
            phase_1_paper_symbols,
        )

        known = phase_1_paper_symbols()
        unknown = [s for s in symbol_list if s not in known]
        if unknown:
            _fail(
                f"symbol(s) {unknown} not in the closed universe for "
                f"--provider interactive_brokers. Supported symbols: "
                f"{sorted(known)}. Options outside this list require the "
                f"live-path wiring PR (follow-up)."
            )

        # TODO(Task B2): port/account validator preflight
        # TODO(Task B3): connect + qualify + teardown
        _fail(
            "Task B1 complete; connect+qualify not yet implemented — "
            "continue with Task B2."
        )
```

**Step 6: Run test — expect PASS**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py::test_ib_provider_rejects_unknown_symbol -v
```

**Step 7: Run FULL CLI test file to confirm no regressions**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py -v
```

Expected: existing Databento tests still pass; stale deferral test is gone; new unknown-symbol test passes.

**Step 8: Commit**

```bash
git add src/msai/cli.py tests/unit/test_cli_instruments_refresh.py
git commit -m "feat(ib-refresh): CLI rejects unknown symbols outside PHASE_1_PAPER_SYMBOLS; drop stale deferral test"
```

---

### Task B2: Port/account preflight validator

**Files:**

- Modify: `src/msai/cli.py` (the IB branch in `instruments_refresh`)
- Modify: `tests/unit/test_cli_instruments_refresh.py`

**Step 1: Write failing test**

Append to `tests/unit/test_cli_instruments_refresh.py`:

```python
def test_ib_provider_rejects_port_account_mismatch(monkeypatch):
    """Preflight validator fires BEFORE any IB connection attempt when
    IB_PORT and IB_ACCOUNT_ID disagree on paper vs live."""
    from typer.testing import CliRunner
    import msai.cli as cli_mod
    from msai.cli import app
    from msai.core.config import Settings

    # Live port + paper account → gotcha #6 silent misroute trap.
    # Patch `msai.cli.settings` directly — NOT `msai.core.config.settings`
    # — because `cli.py:64` does `from msai.core.config import settings`
    # at import time, binding the reference locally in the CLI module.
    monkeypatch.setenv("IB_PORT", "4001")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        app,
        [
            "instruments", "refresh",
            "--symbols", "AAPL",
            "--provider", "interactive_brokers",
        ],
    )
    assert result.exit_code != 0
    combined = (result.stderr or "") + (result.stdout or "")
    assert "4001" in combined
    assert "DU1234567" in combined
```

**Step 2: Run test — expect FAIL**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py::test_ib_provider_rejects_port_account_mismatch -v
```

Expected: FAIL — validator not wired in yet.

**Step 3: Wire validator into the IB branch**

Replace the `TODO(Task B2)` line in `cli.py` with:

```python
        from msai.services.nautilus.ib_port_validator import (
            validate_port_account_consistency,
        )

        try:
            validate_port_account_consistency(
                settings.ib_port, settings.ib_account_id,
            )
        except ValueError as exc:
            _fail(str(exc))
```

Keep the `TODO(Task B3)` `_fail` in place.

**Step 4: Run test — expect PASS**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py::test_ib_provider_rejects_port_account_mismatch -v
```

**Step 5: Run full CLI test file**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py -v
```

**Step 6: Commit**

```bash
git add src/msai/cli.py tests/unit/test_cli_instruments_refresh.py
git commit -m "feat(ib-refresh): CLI preflight validates IB_PORT vs IB_ACCOUNT_ID (gotcha #6)"
```

---

### Task B3: Short-lived IB factory chain + connect fence + qualify + teardown

**This is the biggest task.** Split into substeps but one commit at the end.

**Files:**

- Modify: `src/msai/cli.py` (replace the final `TODO(Task B3)` `_fail`)
- Modify: `tests/unit/test_cli_instruments_refresh.py` (add happy-path + dead-gateway tests)

**Step 1: Write failing tests — happy path + dead gateway + factory-kwargs**

Append to `tests/unit/test_cli_instruments_refresh.py`. **Monkeypatch pattern:** always target `msai.cli.settings`, never `msai.core.config.settings`, because the CLI imports `settings` at module load and the local reference binds eagerly.

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def _ib_env(monkeypatch):
    """Env setup for IB branch tests.

    Patches `msai.cli.settings` directly so the CLI picks up the env
    values. Patching `msai.core.config.settings` DOES NOT WORK because
    `cli.py:64` does `from msai.core.config import settings` at import
    time — the local reference is already bound.
    """
    import msai.cli as cli_mod
    from msai.core.config import Settings

    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setenv("IB_HOST", "127.0.0.1")
    monkeypatch.setenv("IB_CONNECT_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("IB_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("IB_INSTRUMENT_CLIENT_ID", "999")
    monkeypatch.setattr(cli_mod, "settings", Settings())


def test_ib_provider_happy_path_calls_factory_and_resolve(_ib_env, monkeypatch):
    """AAPL qualifies via the mocked factory chain + IBQualifier, then
    SecurityMaster.resolve_for_live commits. Exit 0. Asserts the
    CORRECT factory kwargs and verifies `client.start()` is NOT called
    by the CLI (the factory already starts the client on construction)
    and that teardown awaits `_stop_async`."""
    from typer.testing import CliRunner
    import msai.cli as cli_mod
    from msai.cli import app

    # Mock the factory chain
    mock_client = MagicMock()
    mock_client._is_client_ready = MagicMock()
    mock_client._is_client_ready.wait = AsyncMock(return_value=None)
    mock_client.start = MagicMock()  # must NOT be called
    mock_client.stop = MagicMock()  # must NOT be called
    mock_client._stop_async = AsyncMock(return_value=None)  # must be awaited

    mock_provider = MagicMock()

    mock_get_client = MagicMock(return_value=mock_client)
    mock_get_provider = MagicMock(return_value=mock_provider)

    # Mock SecurityMaster.resolve_for_live at the class level so it doesn't
    # try to hit a real DB; this is the ONLY piece we stub below
    # `_run_ib_resolve_for_live` — the factory + lifecycle is exercised.
    async def _fake_resolve(self, symbols):
        return [f"{s}.NASDAQ" for s in symbols]

    with patch(
        "nautilus_trader.adapters.interactive_brokers.factories.get_cached_ib_client",
        mock_get_client,
    ), patch(
        "nautilus_trader.adapters.interactive_brokers.factories."
        "get_cached_interactive_brokers_instrument_provider",
        mock_get_provider,
    ), patch(
        "msai.services.nautilus.security_master.service.SecurityMaster.resolve_for_live",
        _fake_resolve,
    ), patch(
        "msai.core.database.async_session_factory",
        MagicMock(return_value=MagicMock(
            __aenter__=AsyncMock(return_value=MagicMock(commit=AsyncMock())),
            __aexit__=AsyncMock(return_value=None),
        )),
    ):
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(
            app,
            [
                "instruments", "refresh",
                "--symbols", "AAPL",
                "--provider", "interactive_brokers",
            ],
        )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "AAPL" in (result.stdout or "")

    # --- Factory kwargs correctness (Codex plan-review iter 1 P2) ---
    assert mock_get_client.called
    kwargs = mock_get_client.call_args.kwargs
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 4002
    assert kwargs["client_id"] == 999
    assert kwargs["request_timeout_secs"] == 30

    # --- Lifecycle correctness (Codex plan-review iter 1 P1 #2, P1 #6) ---
    # Factory already calls client.start() internally; CLI must NOT call it.
    mock_client.start.assert_not_called()
    # Public stop() only schedules the async stop; CLI must NOT call it
    # to avoid double-running _stop_async.
    mock_client.stop.assert_not_called()
    # _stop_async MUST have been awaited directly.
    mock_client._stop_async.assert_awaited_once()


def test_ib_provider_dead_gateway_times_out_with_operator_hint(monkeypatch):
    """When the IB client never reaches ready state, CLI times out in
    the short connect-timeout window (not the long request-timeout),
    prints an operator hint naming all relevant env vars, and still
    runs teardown."""
    import msai.cli as cli_mod
    from msai.core.config import Settings
    from msai.cli import app
    from typer.testing import CliRunner

    # 1s connect timeout for fast test; paper env so preflight passes.
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setenv("IB_HOST", "127.0.0.1")
    monkeypatch.setenv("IB_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("IB_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("IB_INSTRUMENT_CLIENT_ID", "999")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    mock_client = MagicMock()

    # Block forever — _is_client_ready.wait() never returns.
    async def _never() -> None:
        await asyncio.sleep(3600)

    mock_client._is_client_ready = MagicMock()
    mock_client._is_client_ready.wait = _never
    mock_client.start = MagicMock()
    mock_client.stop = MagicMock()
    mock_client._stop_async = AsyncMock(return_value=None)

    with patch(
        "nautilus_trader.adapters.interactive_brokers.factories.get_cached_ib_client",
        return_value=mock_client,
    ), patch(
        "nautilus_trader.adapters.interactive_brokers.factories."
        "get_cached_interactive_brokers_instrument_provider",
        return_value=MagicMock(),
    ):
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(
            app,
            [
                "instruments", "refresh",
                "--symbols", "AAPL",
                "--provider", "interactive_brokers",
            ],
        )

    assert result.exit_code != 0
    combined = (result.stderr or "") + (result.stdout or "")
    # Operator hint names all 4 env vars
    assert "IB_HOST" in combined or "127.0.0.1" in combined
    assert "IB_PORT" in combined or "4002" in combined
    assert "IB_ACCOUNT_ID" in combined or "DU1234567" in combined
    assert "IB_INSTRUMENT_CLIENT_ID" in combined or "999" in combined
    # Teardown still ran — `_stop_async` awaited even on timeout
    mock_client._stop_async.assert_awaited_once()
    # `client.stop()` must NOT have been called (avoids double _stop_async)
    mock_client.stop.assert_not_called()
```

**Step 2: Run tests — expect FAIL**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py::test_ib_provider_happy_path_calls_factory_and_resolve tests/unit/test_cli_instruments_refresh.py::test_ib_provider_dead_gateway_times_out_with_operator_hint -v
```

Expected: FAIL — `_run_ib_resolve_for_live` doesn't exist; the deferral `_fail` still fires.

**Step 3: Implement the full IB branch**

Replace the `TODO(Task B3)` `_fail` in `src/msai/cli.py` with a full implementation. First, add imports at the top of `cli.py` (alongside existing imports):

```python
import asyncio
# ... existing imports ...
```

Replace the IB branch in `instruments_refresh` with:

```python
    if provider == "interactive_brokers":
        from msai.services.nautilus.live_instrument_bootstrap import (
            phase_1_paper_symbols,
        )
        from msai.services.nautilus.ib_port_validator import (
            validate_port_account_consistency,
        )

        # ---- Preflight: closed-universe check ----
        known = phase_1_paper_symbols()
        unknown = [s for s in symbol_list if s not in known]
        if unknown:
            _fail(
                f"symbol(s) {unknown} not in the closed universe for "
                f"--provider interactive_brokers. Supported symbols: "
                f"{sorted(known)}. Options outside this list require the "
                f"live-path wiring PR (follow-up)."
            )

        # ---- Preflight: port/account mode consistency (gotcha #6) ----
        try:
            validate_port_account_consistency(
                settings.ib_port, settings.ib_account_id,
            )
        except ValueError as exc:
            _fail(str(exc))

        # ---- Preflight: log the resolved tuple ----
        typer.echo(
            f"Pre-warming IB registry: host={settings.ib_host} "
            f"port={settings.ib_port} "
            f"account={settings.ib_account_id.strip()} "
            f"client_id={settings.ib_instrument_client_id} "
            f"connect_timeout={settings.ib_connect_timeout_seconds}s "
            f"request_timeout={settings.ib_request_timeout_seconds}s",
        )

        resolved = asyncio.run(_run_ib_resolve_for_live(symbol_list))
        _emit_json({"provider": provider, "resolved": resolved})
        return


async def _run_ib_resolve_for_live(symbol_list: list[str]) -> list[str]:
    """Short-lived Nautilus IB client lifecycle wrapping
    ``SecurityMaster.resolve_for_live``.

    Lifecycle (matches design doc Section 2, corrected after Codex
    plan-review iteration 1):

    1. Cap the IB client's internal reconnect loop to one attempt
       (``IB_MAX_CONNECTION_ATTEMPTS=1``) BEFORE constructing the
       client. Without this, a dead gateway → ``_connect`` swallows
       the exception, ``_start_async``'s ``while not _is_ib_connected:``
       loop retries forever as a background task, and our
       caller-side timeout fence only stops us waiting, not the task
       (research brief finding #4).
    2. Build MessageBus + Cache + LiveClock.
    3. ``get_cached_ib_client(...)`` — this ALREADY calls
       ``client.start()`` internally at construction
       (``factories.py:122,134``). Do NOT call ``client.start()`` again.
    4. Connect fence: ``asyncio.wait_for`` on
       ``client._is_client_ready.wait()`` — we OWN the timeout.
       Nautilus's ``wait_until_ready`` (``client.py:362-376``) silently
       swallows the ``TimeoutError`` and only logs — research brief
       finding #1.
    5. ``get_cached_interactive_brokers_instrument_provider`` → wrap in
       ``IBQualifier``.
    6. ``SecurityMaster.resolve_for_live(symbols)`` + commit.
    7. ``try/finally`` teardown: await ``client._stop_async()``
       DIRECTLY. The public ``client.stop()`` would only schedule
       ``_stop_async`` as a new task (``client.py:275,279``) — if we
       then also awaited ``_stop_async`` we'd run the coroutine
       twice. Going direct sidesteps the race and the FSM state
       doesn't matter because the process exits immediately after.
    """
    import os

    # Finding #4 fix: cap the reconnect loop BEFORE client construction.
    # `get_cached_ib_client` reads this env var on first call to
    # `_start_async`; setting it AFTER construction is too late.
    os.environ.setdefault("IB_MAX_CONNECTION_ATTEMPTS", "1")

    # Import Nautilus only inside the function so the CLI module stays
    # importable on machines without the IB extras (e.g. during ruff
    # checks in CI).
    from nautilus_trader.adapters.interactive_brokers.factories import (
        get_cached_ib_client,
        get_cached_interactive_brokers_instrument_provider,
    )
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.model.identifiers import TraderId

    from msai.core.database import async_session_factory
    from msai.core.logging import get_logger
    from msai.services.nautilus.live_instrument_bootstrap import (
        build_ib_instrument_provider_config,
    )
    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier
    from msai.services.nautilus.security_master.service import SecurityMaster

    log = get_logger(__name__)

    clock = LiveClock()
    trader_id = TraderId("MSAI-INSTRUMENTS-REFRESH")
    msgbus = MessageBus(trader_id=trader_id, clock=clock)
    cache = Cache()

    client = get_cached_ib_client(
        loop=asyncio.get_running_loop(),
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        host=settings.ib_host,
        port=settings.ib_port,
        client_id=settings.ib_instrument_client_id,
        request_timeout_secs=settings.ib_request_timeout_seconds,
    )
    # NOTE: get_cached_ib_client ALREADY calls client.start() internally
    # (nautilus_trader/adapters/interactive_brokers/factories.py:122,134).
    # DO NOT call client.start() here — it would schedule a second
    # _start_async task, racing the first.

    try:
        # Caller-side timeout fence — bypasses `wait_until_ready` which
        # swallows TimeoutError (research brief finding #1).
        try:
            await asyncio.wait_for(
                client._is_client_ready.wait(),
                timeout=settings.ib_connect_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise _IBGatewayUnreachable(
                f"IB Gateway not reachable at {settings.ib_host}:"
                f"{settings.ib_port} within "
                f"{settings.ib_connect_timeout_seconds}s. Check: "
                f"(a) gateway container running, "
                f"(b) IB_PORT matches IB_ACCOUNT_ID prefix "
                f"(DU/DF* → paper 4002/4004, U* → live 4001/4003), "
                f"(c) IB_INSTRUMENT_CLIENT_ID={settings.ib_instrument_client_id} "
                f"not colliding with an active subprocess."
            ) from exc

        provider_cfg = build_ib_instrument_provider_config(symbol_list)
        provider = get_cached_interactive_brokers_instrument_provider(
            client=client,
            clock=clock,
            config=provider_cfg,
        )
        qualifier = IBQualifier(provider)

        async with async_session_factory() as session:
            sm = SecurityMaster(qualifier=qualifier, db=session)
            try:
                resolved = await sm.resolve_for_live(symbol_list)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        return resolved
    finally:
        # Research brief finding #2 (refined by plan-review iter 1):
        # `client.stop()` is cpdef void; it schedules `_stop_async` as
        # a new task. We bypass the public `stop()` and await
        # `_stop_async()` directly — one invocation, guaranteed to
        # complete before the process exits (required for US-005:
        # re-run within 60s without leaving a zombie client_id slot).
        # FSM state doesn't matter because we're exiting immediately.
        try:
            await client._stop_async()
        except Exception:  # pragma: no cover — best-effort teardown
            log.warning("ib_refresh_teardown_error", exc_info=True)


class _IBGatewayUnreachable(RuntimeError):
    """Raised by _run_ib_resolve_for_live when the caller-side timeout
    fence fires. Caught at the CLI boundary and converted to
    ``_fail(str(exc))``."""
```

Then update the CLI entry to catch `_IBGatewayUnreachable` and convert to `_fail`:

```python
    if provider == "interactive_brokers":
        # ... preflight as above ...

        try:
            resolved = asyncio.run(_run_ib_resolve_for_live(symbol_list))
        except _IBGatewayUnreachable as exc:
            _fail(str(exc))
        _emit_json({"provider": provider, "resolved": resolved})
        return
```

**Step 4: Run both new tests — expect PASS**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py::test_ib_provider_happy_path_calls_factory_and_resolve tests/unit/test_cli_instruments_refresh.py::test_ib_provider_dead_gateway_times_out_with_operator_hint -v
```

Expected: both pass.

**Step 5: Run full CLI test file**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py -v
```

**Step 6: Lint + typecheck**

```bash
uv run ruff check src/msai/cli.py tests/unit/test_cli_instruments_refresh.py
uv run mypy src/msai/cli.py --strict
```

**Step 7: Commit**

```bash
git add src/msai/cli.py tests/unit/test_cli_instruments_refresh.py
git commit -m "feat(ib-refresh): implement --provider interactive_brokers CLI branch with connect fence + awaited teardown"
```

---

### Task B4: Update CLI docstring + `--help` text (remove deferral language)

**Files:**

- Modify: `src/msai/cli.py` (the docstring for `instruments_refresh`, currently line 725-742, and the `--provider` option help at line 701-708)

**Step 1: Update the `--provider` help text**

Replace:

```python
    provider: str = typer.Option(
        "databento",
        "--provider",
        help=(
            "Provider to pre-warm: ``databento`` (supported) or "
            "``interactive_brokers`` (deferred to follow-up PR)."
        ),
    ),
```

With:

```python
    provider: str = typer.Option(
        "databento",
        "--provider",
        help=(
            "Provider to pre-warm: ``databento`` (Parquet `.Z.N` "
            "continuous futures via DatabentoClient) or "
            "``interactive_brokers`` (short-lived IB Gateway client; "
            "uses IB_INSTRUMENT_CLIENT_ID=999 out-of-band so it never "
            "collides with live subprocesses — see nautilus.md "
            "gotcha #3)."
        ),
    ),
```

**Step 2: Update the main docstring**

Replace the existing docstring (lines 725-742) that describes IB as deferred with:

```python
    """Pre-warm the instrument registry so later deployments never hit
    a cold-miss at bar-event time.

    This is the PRD §47-48 pre-warm tool. Operators run it before
    deploying a new strategy so:

    * Backtest resolve (:meth:`SecurityMaster.resolve_for_backtest`)
      succeeds on the ``.Z.N`` continuous-futures path by downloading
      the Databento ``definition`` payload and upserting the registry
      row.
    * Live resolve (:meth:`SecurityMaster.resolve_for_live`) — for
      ``--provider interactive_brokers`` — connects a short-lived
      Nautilus IB client (``IB_INSTRUMENT_CLIENT_ID``, default 999),
      qualifies each requested symbol against IB Gateway, upserts
      registry rows, then disconnects. Day-1 scope is the closed
      universe ``resolve_for_live`` supports today: ``AAPL``, ``MSFT``,
      ``SPY``, ``EUR/USD``, ``ES``.

    Settings read:

    * ``IB_HOST`` / ``IB_PORT`` / ``IB_ACCOUNT_ID`` — gateway target
      (paper port 4002 + ``DU*`` account, or live port 4001 + non-``D``
      account; gotcha #6 mismatch guard fires at preflight).
    * ``IB_CONNECT_TIMEOUT_SECONDS`` (default 5) — gateway-reachability
      probe.
    * ``IB_REQUEST_TIMEOUT_SECONDS`` (default 30) — per-symbol
      qualification round-trip.
    * ``IB_INSTRUMENT_CLIENT_ID`` (default 999) — out-of-band client
      id, never collides with live subprocesses.
    """
```

**Step 3: Confirm CLI `--help` renders correctly**

```bash
uv run msai instruments refresh --help
```

Expected: help text shows the updated description; no "deferred" language.

**Step 4: Run full CLI test file to confirm no regressions**

```bash
uv run pytest tests/unit/test_cli_instruments_refresh.py -v
```

**Step 5: Commit**

```bash
git add src/msai/cli.py
git commit -m "docs(ib-refresh): update CLI docstring + --help to reflect shipped IB path"
```

---

### Task B5: Update `claude-version/CLAUDE.md` to remove "deferred" language

**Files:**

- Modify: `../../claude-version/CLAUDE.md` (the "Instrument Registry (2026-04-17)" section)

**Step 1: Find the deferral blurb**

```bash
grep -n "interactive_brokers.*deferred\|follow-up PR.*IBQualifier" claude-version/CLAUDE.md
```

**Step 2: Update the blurb**

In `claude-version/CLAUDE.md`, find the paragraph:

> The `msai instruments refresh --provider interactive_brokers` path is currently deferred — follow-up PR will add the required `Settings` fields (`ib_request_timeout_seconds`, `ib_instrument_client_id`, etc.) plus the full IBQualifier factory.

Replace with:

> The `msai instruments refresh --provider interactive_brokers` path is live as of 2026-04-18. Requires `IB_HOST`, `IB_PORT`, `IB_ACCOUNT_ID` to be set; optional tunables are `IB_CONNECT_TIMEOUT_SECONDS` (default 5), `IB_REQUEST_TIMEOUT_SECONDS` (default 30), `IB_INSTRUMENT_CLIENT_ID` (default 999, out-of-band from the live subprocess range). Day-1 scope: closed universe (`AAPL`, `MSFT`, `SPY`, `EUR/USD`, `ES`).

**Step 3: Commit**

```bash
git add claude-version/CLAUDE.md
git commit -m "docs(ib-refresh): update CLAUDE.md — IB refresh path shipped"
```

---

## Phase C: Live-paper smoke test (opt-in)

### Task C1: Opt-in `pytest.mark.ib_paper` smoke test

**Files:**

- Create: `tests/e2e/test_instruments_refresh_ib_smoke.py`
- Modify: `pyproject.toml` (add `ib_paper` marker to `[tool.pytest.ini_options]` markers list — if the section exists)

**Step 1: Register the marker**

Check `pyproject.toml`:

```bash
grep -A 10 "\[tool.pytest.ini_options\]" pyproject.toml
```

If there's a `markers = [...]` list, append `"ib_paper: opt-in live paper IB Gateway test"`. If not, add the section:

```toml
[tool.pytest.ini_options]
markers = [
    "ib_paper: opt-in test that requires a running paper IB Gateway (set RUN_PAPER_E2E=1)",
]
```

**Step 2: Write the smoke test**

Create `tests/e2e/test_instruments_refresh_ib_smoke.py`:

```python
"""Opt-in live-paper smoke test for `msai instruments refresh
--provider interactive_brokers`.

Gated on ``RUN_PAPER_E2E=1`` (mirrors the existing
``tests/e2e/test_ib_paper_smoke.py`` pattern in codex-version).
Requires the paper IB Gateway container to be up on port 4002 with a
DU* account.

Verifies the three things mocks can't:

1. The Nautilus factory signatures are actually correct in 1.223.0
   (research brief finding: `wait_until_ready` is bypassed; `_stop_async`
   is awaited; factory globals are cleared between runs).
2. Idempotent re-run: two back-to-back CLI invocations produce the same
   row count in ``instrument_definitions`` + ``instrument_aliases``.
3. Warm resolve: after refresh, ``SecurityMaster.resolve_for_live``
   returns without touching IB.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

RUN_PAPER_E2E = os.getenv("RUN_PAPER_E2E") == "1"

pytestmark = [
    pytest.mark.ib_paper,
    pytest.mark.skipif(
        not RUN_PAPER_E2E,
        reason="set RUN_PAPER_E2E=1 to run paper IB Gateway smoke tests",
    ),
]


def _invoke_refresh(*symbols: str) -> subprocess.CompletedProcess:
    """Run `msai instruments refresh` as a subprocess (fresh process —
    so Nautilus factory globals are naturally fresh per invocation)."""
    return subprocess.run(
        [
            sys.executable, "-m", "msai.cli",
            "instruments", "refresh",
            "--symbols", ",".join(symbols),
            "--provider", "interactive_brokers",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


async def _count_rows():
    """Return current (definition_count, alias_count) for the
    interactive_brokers provider. Small helper so each test's
    assertions are unambiguous."""
    from sqlalchemy import select, func
    from msai.core.database import async_session_factory
    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition

    async with async_session_factory() as session:
        def_count = await session.scalar(
            select(func.count()).select_from(InstrumentDefinition).where(
                InstrumentDefinition.provider == "interactive_brokers",
            ),
        )
        alias_count = await session.scalar(
            select(func.count()).select_from(InstrumentAlias).where(
                InstrumentAlias.provider == "interactive_brokers",
            ),
        )
    return def_count or 0, alias_count or 0


@pytest.mark.asyncio
async def test_refresh_writes_rows_for_aapl_and_es():
    """First invocation qualifies AAPL + ES and writes registry rows.

    Asserts actual row appearance in `instrument_definitions` and
    `instrument_aliases`, not just CLI exit code — PRD US-001
    acceptance criteria.
    """
    before_defs, before_aliases = await _count_rows()
    result = _invoke_refresh("AAPL", "ES")
    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    after_defs, after_aliases = await _count_rows()
    # Both symbols must have produced at least one new row each.
    assert after_defs >= before_defs + 2, (
        f"expected ≥2 new InstrumentDefinition rows; "
        f"before={before_defs}, after={after_defs}"
    )
    assert after_aliases >= before_aliases + 2


@pytest.mark.asyncio
async def test_refresh_is_idempotent_on_second_run():
    """Second invocation within 60s is a no-op upsert — no NEW rows
    added to instrument_definitions OR instrument_aliases.

    PRD US-002 acceptance: 'running the CLI twice with the same
    symbols produces NO duplicate alias-window rows'.
    """
    first = _invoke_refresh("AAPL")
    assert first.returncode == 0, first.stderr
    mid_defs, mid_aliases = await _count_rows()

    second = _invoke_refresh("AAPL")
    assert second.returncode == 0, (
        f"second run failed (client_id=999 collision?): {second.stderr}"
    )
    after_defs, after_aliases = await _count_rows()

    # Exact equality: no new rows on the idempotent re-run.
    assert after_defs == mid_defs, (
        f"idempotency broken: definition rows grew "
        f"{mid_defs} → {after_defs} on a no-op re-run"
    )
    assert after_aliases == mid_aliases, (
        f"idempotency broken: alias rows grew "
        f"{mid_aliases} → {after_aliases} on a no-op re-run"
    )


@pytest.mark.asyncio
async def test_warm_resolve_does_not_touch_ib():
    """After a successful refresh, resolve_for_live returns from the
    registry without spawning a new IB client (warm-path proof — PRD
    US-001 post-condition, US-002 persistence check).
    """
    # Pre-warm
    prewarm = _invoke_refresh("AAPL")
    assert prewarm.returncode == 0

    # Now call resolve_for_live with NO qualifier — SecurityMaster
    # must resolve from the DB only. If it tries to touch IB, it'll
    # raise because qualifier is None.
    from msai.core.database import async_session_factory
    from msai.services.nautilus.security_master.service import SecurityMaster

    async with async_session_factory() as session:
        sm = SecurityMaster(qualifier=None, db=session)
        resolved = await sm.resolve_for_live(["AAPL"])
        assert len(resolved) == 1
        assert "AAPL" in resolved[0]
```

**Step 3: Verify the tests are skipped by default**

```bash
uv run pytest tests/e2e/test_instruments_refresh_ib_smoke.py -v
```

Expected: 3 tests, all marked `SKIPPED` with "set RUN_PAPER_E2E=1 to run paper IB Gateway smoke tests".

**Step 4: Run full unit suite to confirm the marker doesn't break anything**

```bash
uv run pytest tests/unit -q
```

Expected: no unexpected new failures from marker collision.

**Step 5: Commit**

```bash
git add tests/e2e/test_instruments_refresh_ib_smoke.py pyproject.toml
git commit -m "test(ib-refresh): opt-in ib_paper smoke test — idempotent re-run + warm-resolve proof"
```

---

## Phase D: Manual verification (human-in-the-loop)

**These tasks MUST be run by the human operator against a real paper IB Gateway before declaring the PR ready. They cannot be automated because `RUN_PAPER_E2E=1` is off by default.**

### Task D1: Start the stack and confirm gateway health

```bash
cd /Users/pablomarin/Code/msai-v2/.worktrees/instruments-refresh-ib-path/claude-version
docker compose -f docker-compose.dev.yml up -d
curl -sf http://localhost:8800/health    # backend alive
# IB Gateway health: logs show "DU..." paper account logged in
docker compose -f docker-compose.dev.yml logs ib-gateway | tail -20
```

### Task D2: Run the smoke test suite

```bash
cd claude-version/backend
RUN_PAPER_E2E=1 uv run pytest tests/e2e/test_instruments_refresh_ib_smoke.py -v
```

Expected: 3 passes. If any test fails, it's a real bug — stop, diagnose, fix, re-run.

### Task D3: Manual CLI drill

```bash
cd claude-version/backend
uv run msai instruments refresh --symbols AAPL,ES --provider interactive_brokers
# Expected stdout: "Pre-warming IB registry: host=... port=4002 account=DU... client_id=999 ..."
# Expected stdout: "{"provider": "interactive_brokers", "resolved": ["AAPL.NASDAQ", "ESM6.CME"]}"
# Expected exit code: 0
```

Re-run immediately:

```bash
uv run msai instruments refresh --symbols AAPL,ES --provider interactive_brokers
# Expected: same output, exit 0, no client_id=999 collision
```

Verify registry rows directly (Postgres on `:5433`):

```bash
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U msai -d msai -c \
  "SELECT raw_symbol, provider, asset_class FROM instrument_definitions
     WHERE provider = 'interactive_brokers' ORDER BY raw_symbol;"
```

Expected: rows for AAPL + ES (the exact columns shipped in PR #32's schema).

### Task D4: Negative drill — stop gateway, confirm fast-fail

```bash
cd claude-version
docker compose -f docker-compose.dev.yml stop ib-gateway

cd backend
time uv run msai instruments refresh --symbols AAPL --provider interactive_brokers
```

Expected: exit non-zero within ~6 seconds (5s connect timeout + ~1s overhead), stderr contains the operator hint naming `IB_HOST`, `IB_PORT`, `IB_ACCOUNT_ID`, `IB_INSTRUMENT_CLIENT_ID`.

Restart the gateway:

```bash
cd ..
docker compose -f docker-compose.dev.yml start ib-gateway
# wait ~30s for re-login
```

### Task D5: Port/account mismatch drill

```bash
cd claude-version/backend
# Temporarily flip to live port with paper account (misconfigured)
IB_PORT=4001 uv run msai instruments refresh --symbols AAPL --provider interactive_brokers
```

Expected: exit non-zero immediately (no IB connection attempt) with error naming `4001` + the paper account prefix.

---

## Phase E: Quality gates

Run all of these in sequence from `claude-version/backend`:

### Task E1: Full unit test suite

```bash
uv run pytest tests/unit -v
```

Expected: the same pass count as `main` plus the new tests (~20 new: 3 config + ~14 validator + 5 CLI IB branch).

### Task E2: Integration tests

```bash
uv run pytest tests/integration -v
```

Expected: no regressions. (This PR touches no integration-test surface.)

### Task E3: Lint

```bash
uv run ruff check src/ tests/
```

### Task E4: Typecheck

```bash
uv run mypy src/ --strict
```

Expected: clean on all files touched by this plan.

### Task E5: Worker restart (for dev stack)

If the dev stack is running, restart workers so they pick up the new Settings fields:

```bash
cd /Users/pablomarin/Code/msai-v2/.worktrees/instruments-refresh-ib-path/claude-version
./scripts/restart-workers.sh
```

Then `curl -sf http://localhost:8800/health` to confirm still healthy.

---

## Phase F: State sync + commits

### Task F1: Update `CONTINUITY.md` Done/Now/Next

Edit `CONTINUITY.md` (in the worktree root, not backend):

- Move the "PR #32 deferred item #2" bullet from "Now" → "Done"
- Add a paragraph under "Done (cont'd X)" summarizing the shipped functionality (~10 lines following the format of existing "Done (cont'd N)" sections)
- Update "Now" to whatever is next (probably PR review / finish-branch)

### Task F2: Update `docs/CHANGELOG.md`

Move the "In progress" bullet (added in commit following `087690b`) to an "Added" entry with the PR #, test count, and summary once the PR lands.

### Task F3: Final commit

Only the state-sync files:

```bash
git add CONTINUITY.md docs/CHANGELOG.md
git commit -m "docs(ib-refresh): update CONTINUITY + CHANGELOG — IB refresh path shipped"
```

---

## Phase G: PR handoff

When all Phase A-F tasks pass: `/finish-branch` per project convention. The finish-branch flow handles:

- Plan-review loop (if re-entered)
- Code-review loop (Codex + PR-toolkit)
- Push + PR creation
- PR review comments
- Merge + cleanup

---

## Open items for the implementer (carried from design §8)

1. **Casing normalization** — test D3 uses `AAPL` (upper); if `aapl` input is expected, decide during B1 whether to `.upper()` or reject.
2. **Bare root handling for `AAPL.NASDAQ`-style input** — if the CLI accepts dotted input, strip suffix in preflight before the closed-universe check. Default assumption: operators pass bare roots; decide during B1 if edge-case input shows up.
3. **Commit cadence** — this plan commits once per batch after `resolve_for_live`. If mid-batch failures are frequent in practice, add per-symbol commit in a follow-up.
4. **`IB_MAX_CONNECTION_ATTEMPTS=1`** — RESOLVED in iter-1 plan review. `_run_ib_resolve_for_live` now sets the env var before `get_cached_ib_client` so the factory's internal retry loop caps at 1 attempt. Plan-review iter 1 P1 fix.
