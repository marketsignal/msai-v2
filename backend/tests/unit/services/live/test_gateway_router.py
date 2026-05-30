"""Tests for GatewayRouter parser (PR 1 T4).

Covers the accounts= segment + fail-closed duplicate ib_login_key
(council 2026-05-29 blocking objection #13).
"""

from __future__ import annotations

import pytest

from msai.services.live.gateway_router import GatewayRouter


def test_legacy_3tuple_format_still_parses() -> None:
    r = GatewayRouter("marin1016test:ib-gateway-paper:4004")
    ep = r.resolve("marin1016test")
    assert ep.host == "ib-gateway-paper"
    assert ep.port == 4004
    assert r.accounts_for("marin1016test") == []  # no accounts segment


def test_accounts_segment_parses_pipe_separated() -> None:
    # Grammar fix (Codex iter 1 P1-2): accounts within an entry are
    # pipe-separated to avoid colliding with the comma used between
    # gateway entries. Format: login:host:port:accounts=A1|A2|A3
    r = GatewayRouter("marin1016test:ib-gateway-paper:4004:accounts=DUP733214|DUP733215")
    assert r.accounts_for("marin1016test") == ["DUP733214", "DUP733215"]
    assert r.resolve("marin1016test").host == "ib-gateway-paper"


def test_fail_closed_on_duplicate_login() -> None:
    with pytest.raises(ValueError, match="duplicate ib_login_key 'marin1016test'"):
        GatewayRouter("marin1016test:host-a:4004,marin1016test:host-b:4005")


def test_fail_closed_on_duplicate_host_port() -> None:
    """Codex iter 4 P2-3: two different ib_login_keys pointing at the
    same (host, port) would bypass the per-session startup serialization
    (which is keyed by login). The parser MUST refuse this config."""
    with pytest.raises(ValueError, match="duplicate gateway endpoint"):
        GatewayRouter(
            "marin1016test:ib-gateway:4004:accounts=DUP1,mslvp000:ib-gateway:4004:accounts=U1"
        )


def test_distinct_hosts_with_same_port_allowed() -> None:
    """Same port on different hosts is fine — they're physically distinct
    gateway containers despite the matching internal port number."""
    r = GatewayRouter(
        "marin1016test:ib-gateway-paper:4004:accounts=DUP1,mslvp000:ib-gateway-lvp:4004:accounts=U1"
    )
    assert sorted(r.login_keys) == ["marin1016test", "mslvp000"]


def test_multi_login_still_works() -> None:
    r = GatewayRouter(
        "marin1016test:ib-gateway-paper:4004:accounts=DUP733214|DUP733215,"
        "mslvp000:ib-gateway-lvp:4003:accounts=U1234567"
    )
    assert r.is_multi_login
    assert r.accounts_for("mslvp000") == ["U1234567"]
    assert sorted(r.login_keys) == ["marin1016test", "mslvp000"]


def test_empty_config_str_yields_empty_router() -> None:
    r = GatewayRouter(None)
    assert r.login_keys == []
    assert not r.is_multi_login
    with pytest.raises(ValueError, match="No gateway configured"):
        r.resolve("marin1016test")


def test_accounts_for_unknown_login_returns_empty_list() -> None:
    r = GatewayRouter("marin1016test:ib-gateway:4002:accounts=DUP733214")
    assert r.accounts_for("nonexistent") == []


def test_malformed_entry_raises_value_error() -> None:
    """F6 fix (Codex iter 2 P1 / silent-failure-hunter F8): the parser
    must fail-closed on malformed entries instead of silently dropping
    them. A typo like ``marin1016test_typo:ib-gateway:4002:accounts=...``
    (missing colon, only 3 fields when accounts segment is intended) or
    a bare token like ``bogus`` would otherwise be ignored at boot and
    the deployment would silently fall through to ``settings.ib_host/port``."""
    with pytest.raises(ValueError, match="malformed GATEWAY_CONFIG entry"):
        GatewayRouter("bogus,marin1016test:ib-gateway:4002")


def test_malformed_first_entry_in_multi_entry_config_raises() -> None:
    """The parser walks entries left-to-right; a malformed entry anywhere
    in the comma-separated list fails the whole parse — there is no
    "best-effort" tolerance."""
    with pytest.raises(ValueError, match="malformed GATEWAY_CONFIG entry"):
        GatewayRouter("marin1016test:ib-gateway:4002,malformed")


def test_typo_account_segment_fails_closed() -> None:
    """Codex iter 16 P2: a typo like ``account=`` (missing ``s``) used
    to silently fall through to ``accounts_for() == []``, opting OUT of
    binding enforcement. Now the parser raises so the operator sees the
    typo at supervisor boot instead of discovering it via a misrouted
    order."""
    with pytest.raises(ValueError, match="malformed GATEWAY_CONFIG segment"):
        GatewayRouter("marin1016test:ib-gateway:4002:account=DUP733214")


def test_garbage_4th_segment_fails_closed() -> None:
    """Codex iter 16 P2: any 4th+ segment that isn't ``accounts=...``
    raises. Prevents partial-typo edge cases (e.g. ``acocunts=``,
    ``account_ids=``) from opting out of binding enforcement."""
    with pytest.raises(ValueError, match="malformed GATEWAY_CONFIG segment"):
        GatewayRouter("marin1016test:ib-gateway:4002:garbage_token")


def test_empty_accounts_segment_fails_closed() -> None:
    """Codex iter 16 P2: ``accounts=`` with no values after the equals
    is a typo — the operator clearly INTENDED a binding but provided
    none. Refuse to interpret it as 'no binding' (which would silently
    bypass enforcement); the operator should drop the segment entirely
    if they want an unbound login."""
    with pytest.raises(ValueError, match="must list at least one account_id"):
        GatewayRouter("marin1016test:ib-gateway:4002:accounts=")


def test_trailing_colon_after_port_fails_closed() -> None:
    """Codex iter 17 P2: a trailing colon like
    ``marin1016test:host:4002:`` used to silently no-op (empty 4th
    token skipped), leaving the login unbound. Now empty extra tokens
    raise so the operator catches the typo at boot."""
    with pytest.raises(ValueError, match="empty token after"):
        GatewayRouter("marin1016test:ib-gateway:4002:")


def test_multiple_trailing_colons_fail_closed() -> None:
    """Codex iter 17 P2: defensive — multiple empty segments still raise."""
    with pytest.raises(ValueError, match="empty token after"):
        GatewayRouter("marin1016test:ib-gateway:4002::")
