"""Unit tests for broker-account observability metrics (Task 7)."""

import importlib


def test_broker_account_metrics_registered_and_increment() -> None:
    from msai.services.observability import broker_account_metrics, get_registry

    # Reload the module so its import-time ``_r.counter()/.gauge()`` registration
    # re-runs against the CURRENT registry. Without this the test is order-dependent
    # (Codex code-review iter-8 P2): a prior test that imports ``msai.main`` and then
    # calls ``get_registry().reset()`` would orphan the module-cached SPAWN_FAILED /
    # KV_SECRET_AGE globals from the registry, so incrementing them would not appear
    # in ``render()``. Reloading re-registers them on the live singleton.
    importlib.reload(broker_account_metrics)

    broker_account_metrics.SPAWN_FAILED.inc(account_id="DU1", reason="kv_unauthorized")
    broker_account_metrics.KV_SECRET_AGE.set(123.0, account_id="DU1")

    rendered = get_registry().render()
    rendered_text = "\n".join(rendered) if isinstance(rendered, list) else rendered
    assert "msai_broker_account_spawn_failed_total" in rendered_text
    assert "msai_kv_secret_age_seconds" in rendered_text
