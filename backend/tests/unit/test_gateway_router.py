"""Tests for GatewayRouter -- IB login key to gateway endpoint resolution."""

from __future__ import annotations

import pytest

from msai.services.live.gateway_router import GatewayEndpoint, GatewayRouter


def test_parse_single_entry():
    r = GatewayRouter("marin1016test:ib-gateway-paper:4004")
    ep = r.resolve("marin1016test")
    assert ep.host == "ib-gateway-paper"
    assert ep.port == 4004


def test_parse_multiple_entries():
    r = GatewayRouter("login1:host1:4004,login2:host2:4005")
    assert r.resolve("login1").port == 4004
    assert r.resolve("login2").port == 4005


def test_missing_login_raises():
    r = GatewayRouter("login1:host1:4004")
    with pytest.raises(ValueError, match="No gateway configured"):
        r.resolve("nonexistent")


def test_empty_config():
    r = GatewayRouter(None)
    assert r.login_keys == []
    assert r.is_multi_login is False


def test_is_multi_login():
    r = GatewayRouter("a:h:1,b:h:2")
    assert r.is_multi_login is True


def test_single_login_not_multi():
    r = GatewayRouter("a:h:1")
    assert r.is_multi_login is False


def test_whitespace_handling():
    r = GatewayRouter(" login1 : host1 : 4004 , login2 : host2 : 4005 ")
    assert r.resolve("login1").host == "host1"
    assert r.resolve("login2").port == 4005


def test_gateway_endpoint_frozen():
    ep = GatewayEndpoint(host="h", port=1)
    with pytest.raises(AttributeError):
        ep.host = "other"  # type: ignore[misc]
