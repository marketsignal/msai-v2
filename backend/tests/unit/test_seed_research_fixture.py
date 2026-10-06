"""Owning tests for the documented, isolated synthetic setup command."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "seed_market_data.py"
spec = importlib.util.spec_from_file_location("seed_market_data", SCRIPT)
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


def test_data_is_stable_utc_synthetic_weekdays(tmp_path):
    first = seed.seed_fixture(tmp_path)
    second = seed.seed_fixture(tmp_path)
    assert first == second
    assert first["origin"] == "synthetic"
    assert first["calendar"] == "UTC weekdays, not exchange sessions"
    assert first["fixture_id"] == "msai-nautilus-v2-research-january-2025-v1"
    assert first["files"][0]["bars"] == 23 * 390
    frame = seed.generate_bars("AAPL", seed.SYMBOLS["AAPL"])
    assert str(frame.timestamp.dt.tz) == "UTC"
    assert frame.timestamp.iloc[0].hour == 9
    assert frame.timestamp.iloc[0].minute == 30
    assert frame.equals(seed.generate_bars("AAPL", seed.SYMBOLS["AAPL"]))


def test_documented_command_is_deterministic_across_process_hash_seeds(tmp_path):
    manifests = []
    for hash_seed in ("1", "2"):
        target = tmp_path / hash_seed
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(target), "--symbols", "AAPL"],
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            capture_output=True, text=True, check=True,
        )
        manifests.append(json.loads(result.stdout))
    assert manifests[0] == manifests[1]


def test_command_unsafe_target_refuses_before_files_or_secret_diagnostics(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), str(tmp_path), "--bootstrap-registry", "--isolated-research",
    ])
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:secret-value@postgres/msai")
    with pytest.raises(SystemExit) as exc:
        seed.main()
    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []
    stderr = capsys.readouterr().err
    assert "isolated development" in stderr
    assert "secret-value" not in stderr


def test_refuses_unmarked_data_and_changed_manifest(tmp_path):
    target = tmp_path / "parquet/stocks/AAPL/2025/01.parquet"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing market data")
    with pytest.raises(ValueError, match="unmarked"):
        seed.seed_fixture(tmp_path)
    target.unlink()
    seed.seed_fixture(tmp_path)
    target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        seed.seed_fixture(tmp_path)


@pytest.mark.parametrize("environment,url,isolated", [
    ("production", "postgresql+asyncpg://u:p@postgres/msai_v2_research", True),
    ("development", "postgresql+asyncpg://u:p@remote.example/msai_v2_research", True),
    ("development", "postgresql+asyncpg://u:p@postgres/msai", True),
    ("development", "postgresql+asyncpg://u:p@postgres/msai_v2_research", False),
])
def test_bootstrap_refuses_unsafe_targets(environment, url, isolated):
    with pytest.raises(ValueError, match="isolated development"):
        seed.validate_registry_target(environment, url, isolated)


def test_bootstrap_accepts_literal_isolated_target():
    seed.validate_registry_target(
        "development", "postgresql+asyncpg://u:p@postgres/msai_v2_research", True,
    )


def registry_db(definitions=(), aliases=(), new_rows=None):
    db = AsyncMock()
    definition_result = MagicMock()
    definition_result.scalars.return_value.all.return_value = list(definitions)
    alias_result = MagicMock()
    alias_result.scalars.return_value.all.return_value = list(aliases)
    db.execute.side_effect = [MagicMock(), MagicMock(), definition_result, alias_result]
    if new_rows is not None:
        new_definitions, new_aliases = new_rows
        new_def_result, new_alias_result = MagicMock(), MagicMock()
        new_def_result.scalars.return_value.all.return_value = new_definitions
        new_alias_result.scalars.return_value.all.return_value = new_aliases
        db.execute.side_effect = [
            MagicMock(), MagicMock(), definition_result, alias_result,
            new_def_result, new_alias_result,
        ]
    return db


def fixture_rows():
    definition = SimpleNamespace(
        instrument_uid="one", raw_symbol="AAPL", listing_venue="NASDAQ",
        routing_venue="NASDAQ", asset_class="equity", provider="databento",
        lifecycle_state="active", trading_hours=None, roll_policy=None,
        continuous_pattern=None, hidden_from_inventory=False,
    )
    alias = SimpleNamespace(
        id="alias-one", instrument_uid="one", alias_string="AAPL.NASDAQ", provider="databento",
        venue_format="exchange_name", source_venue_raw=None,
        effective_from=date(1900, 1, 1), effective_to=None,
    )
    return definition, alias


def fixture_binding(manifest, definition, alias):
    return {
        "fixture_id": seed.FIXTURE_ID, "manifest_sha256": seed.manifest_hash(manifest),
        "instrument_uid": definition.instrument_uid, "alias_id": alias.id,
        "definition": seed.definition_metadata(definition), "alias": seed.alias_metadata(alias),
        "origin": "synthetic", "live_qualified": False,
    }


@pytest.mark.asyncio
async def test_bootstrap_uses_owned_writer_and_preacquires_locks(monkeypatch, tmp_path):
    manifest = seed.seed_fixture(tmp_path)
    definition, alias = fixture_rows()
    db = registry_db(new_rows=([definition], [alias]))
    writer = AsyncMock()
    monkeypatch.setattr(seed, "make_security_master", lambda db: SimpleNamespace(
        _upsert_definition_and_alias=writer,
    ))
    binding = await seed.bootstrap_registry(db, manifest)
    assert binding == fixture_binding(manifest, definition, alias)
    assert "pg_advisory_xact_lock" in str(db.execute.call_args_list[0].args[0])
    assert "pg_advisory_xact_lock" in str(db.execute.call_args_list[1].args[0])
    writer.assert_awaited_once()
    kwargs = writer.call_args.kwargs
    assert kwargs["alias_string"] == "AAPL.NASDAQ"
    assert kwargs["provider"] == "databento"
    assert kwargs["asset_class"] == "equity"
    assert "trading_hours" not in kwargs
    assert "live" not in kwargs
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unmarked", "ib", "wrong_manifest", "wrong_alias"])
async def test_bootstrap_refuses_conflicts_before_owned_writer(monkeypatch, tmp_path, kind):
    manifest = seed.seed_fixture(tmp_path)
    definition, alias = fixture_rows()
    binding = fixture_binding(manifest, definition, alias)
    if kind == "unmarked":
        binding = None
    elif kind == "ib":
        alias.provider = "interactive_brokers"
    elif kind == "wrong_manifest":
        binding["manifest_sha256"] = "wrong"
    else:
        alias.alias_string = "AAPL.ARCA"
    db = registry_db([definition], [alias])
    writer = AsyncMock()
    monkeypatch.setattr(seed, "make_security_master", lambda db: SimpleNamespace(
        _upsert_definition_and_alias=writer,
    ))
    with pytest.raises(ValueError, match="conflicting|unmarked"):
        await seed.bootstrap_registry(db, manifest, binding)
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_matching_fixture_is_read_only(monkeypatch, tmp_path):
    manifest = seed.seed_fixture(tmp_path)
    definition, alias = fixture_rows()
    db = registry_db([definition], [alias])
    writer = AsyncMock()
    monkeypatch.setattr(seed, "make_security_master", lambda db: SimpleNamespace(
        _upsert_definition_and_alias=writer,
    ))
    await seed.bootstrap_registry(db, manifest, fixture_binding(manifest, definition, alias))
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_registry_transaction_leaves_files_untouched(monkeypatch, tmp_path):
    from msai.core import database

    db = registry_db()
    events = []

    @asynccontextmanager
    async def begin():
        events.append("begin")
        try:
            yield
        except RuntimeError:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    @asynccontextmanager
    async def factory():
        db.begin = begin
        yield db

    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(database, "engine", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(seed, "make_security_master", lambda db: SimpleNamespace(
        _upsert_definition_and_alias=AsyncMock(side_effect=RuntimeError("writer failed")),
    ))
    with pytest.raises(RuntimeError, match="writer failed"):
        await seed.run_bootstrap(tmp_path, ("AAPL",))
    assert events == ["begin", "rollback"]
    assert list(tmp_path.iterdir()) == []
