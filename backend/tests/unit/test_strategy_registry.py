"""Unit tests for the strategy registry service."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from msai.services.strategy_registry import (
    DiscoveredStrategy,
    compute_file_hash,
    discover_strategies,
    load_strategy_class,
    validate_strategy_file,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STRATEGIES_DIR = Path(__file__).resolve().parents[3] / "strategies" / "example"


@pytest.fixture()
def example_strategies_dir() -> Path:
    """Return the path to the example strategies directory."""
    return STRATEGIES_DIR


@pytest.fixture()
def empty_strategies_dir(tmp_path: Path) -> Path:
    """Return a temporary empty directory."""
    d = tmp_path / "empty_strategies"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Tests: discover_strategies
# ---------------------------------------------------------------------------


class TestDiscoverStrategies:
    """Tests for :func:`discover_strategies`."""

    def test_discover_strategies_finds_example(self, example_strategies_dir: Path) -> None:
        """discover_strategies finds EMACrossStrategy in the example directory."""
        # Act
        results = discover_strategies(example_strategies_dir)

        # Assert
        assert len(results) >= 1
        class_names = [r.strategy_class_name for r in results]
        assert "EMACrossStrategy" in class_names

        ema = next(r for r in results if r.strategy_class_name == "EMACrossStrategy")
        assert isinstance(ema, DiscoveredStrategy)
        assert ema.module_path.name == "ema_cross.py"
        assert ema.config_class_name == "EMACrossConfig"

    def test_discover_strategies_returns_code_hash(self, example_strategies_dir: Path) -> None:
        """Discovered strategies include a 64-character hex SHA256 hash."""
        # Act
        results = discover_strategies(example_strategies_dir)

        # Assert
        assert len(results) >= 1
        for info in results:
            assert len(info.code_hash) == 64
            int(info.code_hash, 16)  # must be valid hex

    def test_discover_strategies_empty_dir(self, empty_strategies_dir: Path) -> None:
        """An empty directory returns an empty list."""
        results = discover_strategies(empty_strategies_dir)

        assert results == []

    def test_discover_strategies_nonexistent_dir(self, tmp_path: Path) -> None:
        """A nonexistent directory returns an empty list without raising."""
        results = discover_strategies(tmp_path / "nonexistent")

        assert results == []


# ---------------------------------------------------------------------------
# Tests: compute_file_hash
# ---------------------------------------------------------------------------


class TestComputeFileHash:
    """Tests for :func:`compute_file_hash`."""

    def test_compute_file_hash_deterministic(self, example_strategies_dir: Path) -> None:
        """Hashing the same file twice produces the same result."""
        path = example_strategies_dir / "ema_cross.py"
        hash1 = compute_file_hash(path)
        hash2 = compute_file_hash(path)

        assert hash1 == hash2
        assert len(hash1) == 64

    def test_compute_file_hash_different_content(self, tmp_path: Path) -> None:
        """Different file content produces different hashes."""
        file_a = tmp_path / "a.py"
        file_b = tmp_path / "b.py"
        file_a.write_text("class AStrategy: pass\n")
        file_b.write_text("class BStrategy: pass\n")

        assert compute_file_hash(file_a) != compute_file_hash(file_b)


# ---------------------------------------------------------------------------
# Tests: validate_strategy_file
# ---------------------------------------------------------------------------


class TestValidateStrategyFile:
    """Tests for :func:`validate_strategy_file`."""

    def test_validate_example_strategy_passes(self, example_strategies_dir: Path) -> None:
        """The shipped example strategy validates successfully."""
        ok, message = validate_strategy_file(example_strategies_dir / "ema_cross.py")

        assert ok is True
        assert message == "EMACrossStrategy"

    def test_validate_missing_file_returns_error(self, tmp_path: Path) -> None:
        """A missing file returns ok=False with a clear message."""
        ok, message = validate_strategy_file(tmp_path / "nope.py")

        assert ok is False
        assert "not found" in message.lower()


# ---------------------------------------------------------------------------
# Tests: load_strategy_class (legacy helper retained for tests)
# ---------------------------------------------------------------------------


class TestLoadStrategyClass:
    """Tests for :func:`load_strategy_class`."""

    def test_load_strategy_class_success(self, example_strategies_dir: Path) -> None:
        """load_strategy_class returns the EMACrossStrategy class."""
        module_path = example_strategies_dir / "ema_cross.py"
        cls = load_strategy_class(module_path, "EMACrossStrategy")

        assert inspect.isclass(cls)
        assert cls.__name__ == "EMACrossStrategy"

    def test_load_strategy_class_missing_class(self, example_strategies_dir: Path) -> None:
        """Requesting a nonexistent class raises ImportError."""
        module_path = example_strategies_dir / "ema_cross.py"

        with pytest.raises(ImportError, match="NonExistent"):
            load_strategy_class(module_path, "NonExistent")

    def test_load_strategy_class_bad_path(self, tmp_path: Path) -> None:
        """A path that does not exist raises ImportError."""
        bad_path = tmp_path / "does_not_exist.py"

        with pytest.raises(ImportError):
            load_strategy_class(bad_path, "SomeStrategy")


# ---------------------------------------------------------------------------
# Council pre-gate spike — msgspec JSON Schema fidelity for Nautilus types
# (2026-04-20 council verdict; see
#  docs/prds/strategy-config-schema-extraction-discussion.md)
#
# The Contrarian flagged that `msgspec.json.schema()` behavior on Nautilus-
# native types (`InstrumentId`, `BarType`, `Decimal`) is unverified. These
# tests ARE the verification. They pin the three properties any later
# extraction must rely on:
#
#   (a) msgspec.json.schema(..., schema_hook=...) produces usable schema
#       for an EMACrossConfig — integers/decimals/defaults/nullable fields
#       render as expected; Nautilus ID types map to typed strings.
#   (b) StrategyConfig.parse(json_str) is the canonical round-trip:
#       string-shaped payloads decode into typed instances, unknown-format
#       values raise msgspec.ValidationError with field-level paths
#       suitable for 422 surfaces.
#   (c) User-defined fields can be distinguished from 17 inherited
#       StrategyConfig base-class fields via `__annotations__` so the
#       renderer doesn't expose `manage_stop`, `order_id_tag`, etc.
# ---------------------------------------------------------------------------


# Mirror strategies/example/config.py at module scope so msgspec can
# resolve the lazy `from __future__ import annotations` forward refs
# against real class objects in the module's globals. Re-declaring
# (rather than importing from the strategies package) avoids sys.path
# coupling to the runtime registry's _ensure_strategies_importable hack.
from decimal import Decimal as _SpikeDecimal  # noqa: E402

from nautilus_trader.model.data import BarType as _SpikeBarType  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId as _SpikeInstrumentId  # noqa: E402
from nautilus_trader.trading.config import StrategyConfig as _SpikeStrategyConfig  # noqa: E402

InstrumentId = _SpikeInstrumentId
BarType = _SpikeBarType
Decimal = _SpikeDecimal


class _SpikeEMACrossConfig(_SpikeStrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast_ema_period: int = 10
    slow_ema_period: int = 30
    trade_size: Decimal = Decimal("1")


class TestMsgspecSchemaFidelitySpike:
    """Council pre-gate — pin msgspec schema/parse behavior before building the renderer."""

    @staticmethod
    def _ema_cross_config() -> type:
        return _SpikeEMACrossConfig

    @staticmethod
    def _nautilus_schema_hook(t: type) -> dict:
        """Map Nautilus ID types to typed strings with format hints.

        `msgspec.json.schema()` raises TypeError on any custom class unless
        a schema_hook covers it. We map all Nautilus identifier classes to
        `type: string` so the renderer can pick a text input; `InstrumentId`
        and `BarType` get ``x-format`` + examples for nicer widgets later.
        """
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import (
            AccountId,
            ClientId,
            ComponentId,
            InstrumentId,
            OrderListId,
            PositionId,
            StrategyId,
            Symbol,
            TraderId,
            Venue,
        )

        if t is InstrumentId:
            return {
                "type": "string",
                "title": "Instrument ID",
                "x-format": "instrument-id",
                "description": "SYMBOL.VENUE",
                "examples": ["AAPL.NASDAQ", "EUR/USD.IDEALPRO"],
            }
        if t is BarType:
            return {
                "type": "string",
                "title": "Bar Type",
                "x-format": "bar-type",
                "description": "INSTRUMENT_ID-STEP-AGGREGATION-PRICE_TYPE-SOURCE",
                "examples": ["AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"],
            }
        nautilus_id_types = (
            StrategyId,
            ComponentId,
            Venue,
            Symbol,
            AccountId,
            ClientId,
            OrderListId,
            PositionId,
            TraderId,
        )
        if t in nautilus_id_types:
            return {"type": "string", "title": t.__name__}
        raise NotImplementedError(f"no schema hook for {t!r}")

    def test_json_schema_extracts_with_nautilus_hook(self) -> None:
        """schema_hook maps Nautilus IDs → typed strings; primitives render natively."""
        import msgspec

        config_cls = self._ema_cross_config()
        schema = msgspec.json.schema(config_cls, schema_hook=self._nautilus_schema_hook)

        ema_def = schema["$defs"][config_cls.__name__]
        props = ema_def["properties"]

        # Nautilus ID types → typed string with format hint
        assert props["instrument_id"]["type"] == "string"
        assert props["instrument_id"]["x-format"] == "instrument-id"
        assert "AAPL.NASDAQ" in props["instrument_id"]["examples"]

        assert props["bar_type"]["type"] == "string"
        assert props["bar_type"]["x-format"] == "bar-type"

        # Primitives with defaults
        assert props["fast_ema_period"] == {"type": "integer", "default": 10}
        assert props["slow_ema_period"] == {"type": "integer", "default": 30}
        assert props["trade_size"] == {"type": "string", "format": "decimal", "default": "1"}

    def test_json_schema_includes_inherited_base_fields(self) -> None:
        """msgspec emits the whole struct including StrategyConfig base plumbing.

        Pinning this behavior so the extractor knows to trim via
        ``__annotations__`` — we do NOT want the form to expose
        ``manage_stop``, ``order_id_tag``, etc. by default.
        """
        import msgspec

        config_cls = self._ema_cross_config()
        schema = msgspec.json.schema(config_cls, schema_hook=self._nautilus_schema_hook)
        props = schema["$defs"][config_cls.__name__]["properties"]

        inherited = {
            "strategy_id",
            "order_id_tag",
            "use_uuid_client_order_ids",
            "manage_stop",
            "log_events",
        }
        assert inherited.issubset(props.keys()), (
            "msgspec.json.schema emits all fields incl. inherited — trim via __annotations__"
        )

    def test_user_defined_fields_via_annotations(self) -> None:
        """``EMACrossConfig.__annotations__`` lists only the 5 user-defined fields."""
        config_cls = self._ema_cross_config()
        own_fields = set(config_cls.__annotations__.keys())
        assert own_fields == {
            "instrument_id",
            "bar_type",
            "fast_ema_period",
            "slow_ema_period",
            "trade_size",
        }

    def test_strategy_config_parse_round_trip(self) -> None:
        """StrategyConfig.parse(json_string) accepts string payloads → typed instances."""
        from decimal import Decimal

        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        config_cls = self._ema_cross_config()
        payload = (
            '{"instrument_id":"AAPL.NASDAQ",'
            '"bar_type":"AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",'
            '"fast_ema_period":5,'
            '"trade_size":"2.5"}'
        )

        instance = config_cls.parse(payload)

        assert isinstance(instance.instrument_id, InstrumentId)
        assert str(instance.instrument_id) == "AAPL.NASDAQ"
        assert isinstance(instance.bar_type, BarType)
        assert instance.fast_ema_period == 5
        assert instance.slow_ema_period == 30  # default honored
        assert isinstance(instance.trade_size, Decimal)
        assert instance.trade_size == Decimal("2.5")

    def test_strategy_config_parse_malformed_raises_field_level_error(self) -> None:
        """Bad InstrumentId → msgspec.ValidationError with field path — usable for 422."""
        import msgspec

        config_cls = self._ema_cross_config()
        bad_payload = '{"instrument_id":"garbage","bar_type":"AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"}'

        with pytest.raises(msgspec.ValidationError) as excinfo:
            config_cls.parse(bad_payload)

        # Field-level path is present in the message — renderable inline
        assert "$.instrument_id" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Tests: sync_strategies_to_db (prune_missing branch)
# ---------------------------------------------------------------------------


class _FakeAsyncSession:
    """Minimal async session covering ``sync_strategies_to_db`` call surface.

    Supports ``execute(select(Strategy)).scalars().all()``, ``add()``,
    ``delete()``. Commit is a no-op — caller commits.
    """

    def __init__(self, existing: list[Strategy] | None = None) -> None:
        from msai.models.strategy import Strategy

        self._rows: list[Strategy] = list(existing or [])
        self.deleted: list[Strategy] = []

    async def execute(self, _stmt: object) -> _FakeAsyncSession:
        return self

    def scalars(self) -> _FakeAsyncSession:
        return self

    def all(self) -> list[Strategy]:
        from msai.models.strategy import Strategy  # noqa: F401  (used in annotation)

        return [r for r in self._rows if r not in self.deleted]

    def add(self, row: Strategy) -> None:
        self._rows.append(row)

    async def delete(self, row: Strategy) -> None:
        self.deleted.append(row)


class TestSyncStrategiesToDb:
    """Tests for :func:`sync_strategies_to_db` — orphan-prune branch."""

    async def test_sync_prunes_row_whose_file_no_longer_exists(self, tmp_path: Path) -> None:
        """Soft-prune contract (plan R2 / T3): a row whose ``file_path``
        has vanished from disk is marked archived via ``deleted_at`` —
        NOT hard-deleted. Hard delete would orphan historical backtest
        and deployment foreign keys.
        """
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_dir = tmp_path / "strategies"
        strategies_dir.mkdir()

        orphan_file = tmp_path / "deleted_strategy.py"
        orphan_row = Strategy(
            name="deleted.strategy",
            file_path=str(orphan_file),
            strategy_class="DeletedStrategy",
            config_class=None,
            config_schema=None,
            default_config=None,
            config_schema_status="no_config_class",
            code_hash="deadbeef",
        )
        session = _FakeAsyncSession(existing=[orphan_row])

        # File does NOT exist; empty strategies_dir → no discovered rows
        assert not orphan_file.exists()

        result = await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_dir,
            prune_missing=True,
        )

        assert result == []
        # Soft-prune: ``deleted_at`` is stamped, row is NOT hard-deleted.
        assert orphan_row not in session.deleted, "Soft-prune must not hard-delete the row"
        assert orphan_row.deleted_at is not None, "Soft-prune must stamp deleted_at"

    async def test_sync_keeps_orphan_row_when_prune_missing_false(self, tmp_path: Path) -> None:
        """Opt-out: ``prune_missing=False`` leaves orphan rows untouched
        (no ``deleted_at`` stamp, no hard delete)."""
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_dir = tmp_path / "strategies"
        strategies_dir.mkdir()

        orphan_row = Strategy(
            name="kept.strategy",
            file_path=str(tmp_path / "gone.py"),
            strategy_class="KeptStrategy",
            config_class=None,
            config_schema=None,
            default_config=None,
            config_schema_status="no_config_class",
            code_hash="cafebabe",
        )
        session = _FakeAsyncSession(existing=[orphan_row])

        await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_dir,
            prune_missing=False,
        )

        assert orphan_row not in session.deleted
        assert orphan_row.deleted_at is None

    async def test_sync_preserves_user_patched_description(
        self, example_strategies_dir: Path
    ) -> None:
        """PATCH /api/v1/strategies/{id} sets ``description``; the next GET
        calls ``sync_strategies_to_db`` first. Before this regression test,
        the sync unconditionally overwrote ``row.description = info.description``
        on every call, so the on-disk docstring would clobber the
        PATCH-saved value — silent edit no-op caught by 2026-05-15 CLI
        completeness E2E.
        """
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import discover_strategies, sync_strategies_to_db

        # Arrange: an existing row whose description has been user-PATCHed
        # to something different from the on-disk docstring.
        discovered = discover_strategies(example_strategies_dir)
        assert discovered, "example strategies dir should yield at least one strategy"
        info = discovered[0]

        existing_row = Strategy(
            name=info.name,
            description="USER PATCHED DESCRIPTION — must survive sync",
            file_path=str(info.module_path),
            strategy_class=info.strategy_class_name,
            config_class=info.config_class_name,
            config_schema=info.config_schema,
            default_config=info.default_config,
            config_schema_status=info.config_schema_status,
            code_hash=info.code_hash,
        )
        session = _FakeAsyncSession(existing=[existing_row])

        # Act: trigger the same sync the GET endpoint runs.
        await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            example_strategies_dir,
        )

        # Assert: description was NOT clobbered by the on-disk docstring.
        assert existing_row.description == "USER PATCHED DESCRIPTION — must survive sync"
