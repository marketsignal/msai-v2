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
