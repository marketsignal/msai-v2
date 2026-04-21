"""Smoke test for the failed-backtest fixtures added in B0.

NOTE: These tests are intentionally "red" until Tasks B4+B5 land. Those tasks
add the new ``error_code``/``error_public_message``/``error_suggested_action``/
``error_remediation`` columns to ``Backtest``. Until then, the fixtures
themselves are valid Python but the ORM row construction inside them will
raise ``TypeError: 'error_code' is an invalid keyword argument for Backtest``
(or similar). That is the expected TDD "red" state for B0.
"""

from __future__ import annotations


async def test_seed_failed_backtest_fixture_returns_row(seed_failed_backtest):
    bt_id, raw_msg = seed_failed_backtest
    assert bt_id
    assert raw_msg


async def test_seed_historical_failed_row_fixture_returns_id(seed_historical_failed_row):
    assert seed_historical_failed_row


async def test_seed_pending_backtest_fixture_returns_id(seed_pending_backtest):
    assert seed_pending_backtest
