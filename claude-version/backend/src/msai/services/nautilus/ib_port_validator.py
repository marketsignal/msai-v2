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
            f"unknown IB port {port}; expected one of {IB_PAPER_PORTS + IB_LIVE_PORTS}"
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
            f"unknown IB port {port}; expected one of {IB_PAPER_PORTS + IB_LIVE_PORTS}"
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
