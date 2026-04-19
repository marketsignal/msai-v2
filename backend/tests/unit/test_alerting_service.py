"""Unit tests for the alerting service + /api/v1/alerts/ router.

Covers the new Codex-ported :class:`AlertingService` file-backed history
store, the module-level singleton, and the existing :class:`AlertService`
wrapper's new history side-effect.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest

from msai.services.alerting import (
    _MAX_HISTORY,
    AlertingService,
    AlertService,
    alerting_service,
)

if TYPE_CHECKING:
    from pathlib import Path


def _emit_from_subprocess(path_str: str, idx: int) -> None:
    """Module-level target so ``multiprocessing`` (spawn) can import it."""
    from pathlib import Path as _Path

    from msai.services.alerting import AlertingService as _Svc

    _Svc(path=_Path(path_str)).send_alert(level="info", title=f"subproc-{idx}", message=".")


# ---------------------------------------------------------------------------
# AlertingService (file-backed history)
# ---------------------------------------------------------------------------


class TestAlertingServiceHistory:
    def test_send_alert_persists_to_json_file(self, tmp_path: Path) -> None:
        svc = AlertingService(path=tmp_path / "alerts.json")

        svc.send_alert(level="warning", title="Disk full", message="95% used")

        payload = json.loads((tmp_path / "alerts.json").read_text())
        assert len(payload["alerts"]) == 1
        entry = payload["alerts"][0]
        assert entry["type"] == "alert"
        assert entry["level"] == "warning"
        assert entry["title"] == "Disk full"
        assert entry["message"] == "95% used"
        assert "created_at" in entry

    @pytest.mark.parametrize(
        ("alert_level", "expected_method"),
        [
            ("info", "info"),
            ("warning", "warning"),
            ("error", "error"),
            ("critical", "critical"),
            ("unknown-severity", "warning"),
        ],
    )
    def test_send_alert_routes_to_matching_log_level(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alert_level: str,
        expected_method: str,
    ) -> None:
        # Codex iter 4 P2: the backend log line's level must match the
        # alert severity. Without this, structlog's add_log_level would
        # emit every alert as "warning" and log-based monitoring would
        # misclassify critical alerts.
        from msai.services import alerting as alerting_module

        svc = AlertingService(path=tmp_path / "alerts.json")
        calls: list[str] = []

        class _SpyLog:
            def __getattr__(self, name: str) -> Any:
                def _spy(*_args: object, **_kwargs: object) -> None:
                    calls.append(name)

                return _spy

        monkeypatch.setattr(alerting_module, "log", _SpyLog())
        svc.send_alert(level=alert_level, title="t", message="m")

        assert calls == [expected_method]

    def test_send_recovery_uses_recovery_type_and_info_level(self, tmp_path: Path) -> None:
        svc = AlertingService(path=tmp_path / "alerts.json")

        svc.send_recovery(title="IB back online", message="Reconnected")

        entry = json.loads((tmp_path / "alerts.json").read_text())["alerts"][0]
        assert entry["type"] == "recovery"
        assert entry["level"] == "info"

    def test_list_alerts_returns_newest_first(self, tmp_path: Path) -> None:
        svc = AlertingService(path=tmp_path / "alerts.json")

        svc.send_alert(level="warning", title="A", message="first")
        svc.send_alert(level="error", title="B", message="second")
        svc.send_alert(level="critical", title="C", message="third")

        alerts = svc.list_alerts()
        assert [a["title"] for a in alerts] == ["C", "B", "A"]

    def test_list_alerts_respects_limit(self, tmp_path: Path) -> None:
        svc = AlertingService(path=tmp_path / "alerts.json")

        for i in range(5):
            svc.send_alert(level="warning", title=f"alert-{i}", message=".")

        alerts = svc.list_alerts(limit=2)
        assert len(alerts) == 2
        # Newest first: alert-4, alert-3.
        assert alerts[0]["title"] == "alert-4"
        assert alerts[1]["title"] == "alert-3"

    def test_list_alerts_missing_file_returns_empty(self, tmp_path: Path) -> None:
        svc = AlertingService(path=tmp_path / "missing.json")
        assert svc.list_alerts() == []

    def test_list_alerts_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        # Operators occasionally eyeball/edit the alert log. A broken JSON
        # file must not crash the API — return empty and let the next alert
        # overwrite with a fresh structure.
        path = tmp_path / "alerts.json"
        path.write_text("{ not valid json ")

        svc = AlertingService(path=path)
        assert svc.list_alerts() == []

    @pytest.mark.parametrize(
        "content",
        ["[]", '"oops"', "null", "42", "true"],
    )
    def test_list_alerts_wrong_shape_returns_empty(self, tmp_path: Path, content: str) -> None:
        # Valid JSON of the wrong shape must not surface as AttributeError
        # on payload.get(). Fall back to empty so list_alerts() is total.
        path = tmp_path / "alerts.json"
        path.write_text(content)

        svc = AlertingService(path=path)
        assert svc.list_alerts() == []

    @pytest.mark.parametrize(
        "content",
        ['{"alerts": "oops"}', '{"alerts": 42}', '{"alerts": null}', '{"alerts": {"nested": 1}}'],
    )
    def test_non_list_alerts_field_returns_empty(self, tmp_path: Path, content: str) -> None:
        # Even more malformed: top-level is a dict but 'alerts' value is
        # not a list. Without coercion, list() on a str iterates chars and
        # list() on an int raises TypeError. Coercion drops it to [].
        path = tmp_path / "alerts.json"
        path.write_text(content)

        svc = AlertingService(path=path)
        assert svc.list_alerts() == []

    def test_write_drops_malformed_rows_before_applying_cap(self, tmp_path: Path) -> None:
        # Codex iter 5 P2: a hand-edited bad row must not permanently
        # consume one of the _MAX_HISTORY slots. The write path filters
        # invalid rows BEFORE capping so every retained slot is a real alert.
        path = tmp_path / "alerts.json"
        seed: list[dict[str, str]] = [
            {"type": "alert", "level": "warning"}  # missing fields → invalid
        ]
        for i in range(_MAX_HISTORY - 1):
            seed.append(
                {
                    "type": "alert",
                    "level": "info",
                    "title": f"seed-{i}",
                    "message": ".",
                    "created_at": "2024-01-01T00:00:00Z",
                }
            )
        path.write_text(json.dumps({"alerts": seed}))

        svc = AlertingService(path=path)
        svc.send_alert(level="warning", title="new", message=".")

        retained = json.loads(path.read_text())["alerts"]
        titles = [entry.get("title") for entry in retained]
        assert titles[0] == "new"
        assert "seed-0" in titles  # oldest real alert still present
        from msai.services.alerting import _is_valid_alert_entry

        assert all(_is_valid_alert_entry(entry) for entry in retained)

    def test_malformed_entry_does_not_consume_limit_slot(self, tmp_path: Path) -> None:
        # Codex iter 3 P3: the limit slice used to happen BEFORE malformed
        # rows were filtered, so a single bad top entry + limit=1 made the
        # endpoint return []. Filtering must happen first so the limit
        # always fills up to `limit` VALID rows when enough exist.
        path = tmp_path / "alerts.json"
        path.write_text(
            json.dumps(
                {
                    "alerts": [
                        {"type": "alert", "level": "warning"},  # missing title/message/created_at
                        {
                            "type": "alert",
                            "level": "warning",
                            "title": "older-valid",
                            "message": "m",
                            "created_at": "2024-01-01T00:00:00Z",
                        },
                    ]
                }
            )
        )

        svc = AlertingService(path=path)
        alerts = svc.list_alerts(limit=1)

        assert len(alerts) == 1
        assert alerts[0]["title"] == "older-valid"

    def test_list_alerts_falls_back_to_unlocked_read_on_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 3 P2: a read-only volume breaks open(lockfile, "a")
        # inside _locked(). The read path must not 500 in that state —
        # fall back to an unlocked read so operators can still inspect
        # history during degraded storage.
        path = tmp_path / "alerts.json"
        svc = AlertingService(path=path)
        svc.send_alert(level="warning", title="before-ro", message=".")

        # Simulate the read-only filesystem failure inside _locked() only.
        import contextlib as _ctx

        @_ctx.contextmanager
        def _fail_lock() -> Any:
            raise PermissionError("read-only filesystem")
            yield  # pragma: no cover

        monkeypatch.setattr(svc, "_locked", _fail_lock)
        alerts = svc.list_alerts()

        assert len(alerts) == 1
        assert alerts[0]["title"] == "before-ro"

    def test_non_dict_entries_in_alerts_list_are_dropped(self, tmp_path: Path) -> None:
        # Mixed payload: some valid dict entries, some garbage. Valid
        # entries survive, garbage is dropped.
        path = tmp_path / "alerts.json"
        path.write_text(
            json.dumps(
                {
                    "alerts": [
                        {
                            "type": "alert",
                            "level": "warning",
                            "title": "real",
                            "message": "m",
                            "created_at": "2024-01-01T00:00:00Z",
                        },
                        "stray-string",
                        42,
                        None,
                    ]
                }
            )
        )

        svc = AlertingService(path=path)
        alerts = svc.list_alerts()
        assert len(alerts) == 1
        assert alerts[0]["title"] == "real"

    @pytest.mark.parametrize("content", ["[]", '"oops"', "null"])
    def test_write_recovers_from_wrong_shape(self, tmp_path: Path, content: str) -> None:
        # Codex iter 2 P2: _write_event must also self-heal from valid-
        # JSON-wrong-shape, not just JSONDecodeError. Otherwise a stray
        # operator edit permanently breaks future alerts.
        path = tmp_path / "alerts.json"
        path.write_text(content)

        svc = AlertingService(path=path)
        svc.send_alert(level="warning", title="recovered", message=".")

        alerts = svc.list_alerts()
        assert len(alerts) == 1
        assert alerts[0]["title"] == "recovered"

    def test_history_cap_enforced_at_max(self, tmp_path: Path) -> None:
        # Writing MAX+5 records must leave exactly MAX on disk.
        svc = AlertingService(path=tmp_path / "alerts.json")

        for i in range(_MAX_HISTORY + 5):
            svc.send_alert(level="info", title=f"n-{i}", message=".")

        payload = json.loads((tmp_path / "alerts.json").read_text())
        assert len(payload["alerts"]) == _MAX_HISTORY
        # Newest (n-204) must survive; oldest kept is n-5 (first 5 dropped).
        assert payload["alerts"][0]["title"] == f"n-{_MAX_HISTORY + 4}"
        assert payload["alerts"][-1]["title"] == "n-5"

    def test_creates_parent_directory_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "alerts.json"
        svc = AlertingService(path=nested)

        svc.send_alert(level="warning", title="x", message="y")

        assert nested.exists()
        assert nested.parent.is_dir()

    def test_concurrent_writes_do_not_lose_records(self, tmp_path: Path) -> None:
        # Regression guard for the Codex P1: two threads (standing in for
        # two worker processes) emitting alerts at the same time against a
        # shared AlertingService must not clobber each other. Without the
        # fcntl.flock serialization, the later write used to overwrite the
        # earlier reader's snapshot and drop records.
        import threading

        path = tmp_path / "alerts.json"
        svc = AlertingService(path=path)
        barrier = threading.Barrier(8)

        def _emit(i: int) -> None:
            barrier.wait()
            svc.send_alert(level="info", title=f"t-{i}", message=".")

        threads = [threading.Thread(target=_emit, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        titles = {entry["title"] for entry in svc.list_alerts(limit=50)}
        assert titles == {f"t-{i}" for i in range(8)}

    def test_concurrent_writes_across_processes_do_not_lose_records(self, tmp_path: Path) -> None:
        # Stronger regression guard — the real target of fcntl.flock. Same
        # expectation as the thread test but fires OS-level processes that
        # each acquire their own file descriptor against the sidecar lock.
        # Without cross-process locking, interleaved read-modify-write
        # cycles drop records; threading-only tests miss this because the
        # GIL happens to serialize enough of the path.
        import multiprocessing

        path = tmp_path / "alerts.json"
        # Ensure the file and its parent dir exist before spawning
        # children so they all race on write, not on mkdir.
        AlertingService(path=path).send_alert(level="info", title="seed", message=".")

        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_emit_from_subprocess, args=(str(path), i)) for i in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=15)
            assert p.exitcode == 0, f"worker {p.pid} exited {p.exitcode}"

        titles = {entry["title"] for entry in AlertingService(path=path).list_alerts(limit=50)}
        # All 6 subprocess-emitted alerts + the seed must survive.
        expected = {"seed"} | {f"subproc-{i}" for i in range(6)}
        assert titles == expected


# ---------------------------------------------------------------------------
# AlertService (SMTP wrapper) — history side-effect
# ---------------------------------------------------------------------------


class TestAlertServiceWritesHistory:
    """``AlertService.send_alert`` must always write history, even when
    SMTP is unconfigured or fails. This is the regression guard against
    "alert fired but operator can't see it" scenarios."""

    @pytest.mark.asyncio
    async def test_send_alert_without_smtp_still_records_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService()  # smtp_host="" → graceful degrade

        ok = await service.send_alert("No SMTP", "body", level="warning")
        # History is fire-and-forget; wait for the executor task before
        # asserting on the file contents.
        assert service.last_history_task is not None
        await service.last_history_task

        assert ok is False  # email not sent
        entry = json.loads((tmp_path / "alerts.json").read_text())["alerts"][0]
        assert entry["title"] == "No SMTP"
        assert entry["level"] == "warning"

    @pytest.mark.asyncio
    async def test_alert_strategy_error_records_history_with_error_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService()

        await service.alert_strategy_error("ema_cross", "Division by zero")
        assert service.last_history_task is not None
        await service.last_history_task

        entry = json.loads((tmp_path / "alerts.json").read_text())["alerts"][0]
        assert entry["level"] == "error"
        assert "ema_cross" in entry["title"]
        assert "Division by zero" in entry["message"]

    @pytest.mark.asyncio
    async def test_smtp_transport_failure_still_records_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService(
            smtp_host="smtp.example.com",
            sender="a@b",
            password="pw",
            default_recipients=["ops@example.com"],
        )

        with patch.object(service, "_send_smtp", side_effect=OSError("tls broke")):
            ok = await service.send_alert("Transport fail", "body", level="critical")
        assert service.last_history_task is not None
        await service.last_history_task

        assert ok is False
        entry = json.loads((tmp_path / "alerts.json").read_text())["alerts"][0]
        assert entry["title"] == "Transport fail"
        assert entry["level"] == "critical"

    @pytest.mark.asyncio
    async def test_history_write_runs_in_executor_not_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 5 P2: the sync file-IO history write must NOT run on
        # the event loop. Verifies the call goes through
        # loop.run_in_executor so a slow disk or contended flock can't
        # stall unrelated coroutines.
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService()  # no SMTP

        observed_threads: list[int] = []
        original = alerting_service.send_alert

        def _recording_send_alert(level: str, title: str, message: str) -> None:
            import threading

            observed_threads.append(threading.get_ident())
            original(level, title, message)

        monkeypatch.setattr(alerting_service, "send_alert", _recording_send_alert)
        main_thread = __import__("threading").get_ident()

        await service.send_alert("Async", "body", level="warning")
        # Fire-and-forget: await the executor task so the assertion below
        # is deterministic.
        assert service.last_history_task is not None
        await service.last_history_task

        assert len(observed_threads) == 1
        assert observed_threads[0] != main_thread, (
            "history write must run in the executor, not the event-loop thread"
        )

    @pytest.mark.asyncio
    async def test_history_uses_dedicated_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 9 P1: history writes must run on a dedicated
        # executor so a wedged alerts volume can't saturate the default
        # pool that SMTP (and other asyncio coroutines) share.
        from msai.services.alerting import _HISTORY_EXECUTOR

        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService()

        observed_prefix: list[str] = []
        original = alerting_service.send_alert

        def _recording(level: str, title: str, message: str) -> None:
            import threading

            observed_prefix.append(threading.current_thread().name)
            original(level, title, message)

        monkeypatch.setattr(alerting_service, "send_alert", _recording)
        await service.send_alert("dedicated", "body", level="info")

        assert len(observed_prefix) == 1
        # Default executor names start with "ThreadPoolExecutor-" or
        # "asyncio_"; our dedicated executor uses "alert-history".
        assert "alert-history" in observed_prefix[0], observed_prefix
        # Sanity: executor is still alive for subsequent callers.
        assert not _HISTORY_EXECUTOR._shutdown

    @pytest.mark.asyncio
    async def test_send_alert_bounded_by_timeout_on_wedged_storage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 7 + iter 8 synthesis: history awaits the executor
        # task so short-lived callers (IB disconnect subprocess,
        # _mark_failed) get persistence, BUT a wedged alerts volume must
        # not hang the caller forever — a hard timeout bounds the worst
        # case so downstream work can still proceed.
        import threading
        import time

        from msai.services import alerting as alerting_module

        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        monkeypatch.setattr(alerting_module, "_HISTORY_WRITE_TIMEOUT_S", 0.3)
        service = AlertService()  # no SMTP

        release = threading.Event()

        def _wedged_history(level: str, title: str, message: str) -> None:
            release.wait(timeout=5.0)

        monkeypatch.setattr(alerting_service, "send_alert", _wedged_history)

        start = time.monotonic()
        ok = await service.send_alert("wedged", "body", level="critical")
        elapsed = time.monotonic() - start

        # Timeout was 0.3s — caller must return within ~1s (slack for
        # executor scheduling).
        assert elapsed < 1.0, f"timeout did not bound the wait: {elapsed}s"
        assert ok is False  # no SMTP configured, history didn't finish

        # Release the history task so it can be cleaned up.
        release.set()
        assert service.last_history_task is not None
        await service.last_history_task

    @pytest.mark.asyncio
    async def test_send_alert_awaits_history_in_normal_case(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 8 P1 regression: short-lived callers (IB disconnect
        # subprocess) must see the history persisted before send_alert
        # returns — otherwise the subprocess exits before the background
        # write reaches disk and the critical alert is lost.
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService()

        await service.send_alert("critical-alert", "body", level="critical")

        # Without an explicit await of last_history_task: history must
        # already be on disk because send_alert awaited it.
        payload = json.loads((tmp_path / "alerts.json").read_text())
        assert payload["alerts"][0]["title"] == "critical-alert"
        assert payload["alerts"][0]["level"] == "critical"

    @pytest.mark.asyncio
    async def test_history_write_failure_does_not_block_smtp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 2 P1: a read-only or full alerts volume must not
        # take down operational email. History write is best-effort.
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService(
            smtp_host="smtp.example.com",
            sender="a@b",
            password="pw",
            default_recipients=["ops@example.com"],
        )

        with (
            patch.object(
                alerting_service,
                "send_alert",
                side_effect=OSError("read-only filesystem"),
            ),
            patch.object(service, "_send_smtp") as mock_smtp,
        ):
            ok = await service.send_alert("Disk gone", "still need the email", level="critical")

        assert ok is True, "SMTP must still succeed when history write fails"
        assert mock_smtp.call_count == 1

    @pytest.mark.asyncio
    async def test_smtp_success_path_also_records_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression guard: history write must not be skipped on the happy
        # path. Without this test the "always auditable" claim would hold
        # only on failure paths.
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService(
            smtp_host="smtp.example.com",
            sender="a@b",
            password="pw",
            default_recipients=["ops@example.com"],
        )

        with patch.object(service, "_send_smtp") as mock_send:
            ok = await service.send_alert("Ship it", "body", level="info")
        assert service.last_history_task is not None
        await service.last_history_task

        assert ok is True
        assert mock_send.call_count == 1
        entry = json.loads((tmp_path / "alerts.json").read_text())["alerts"][0]
        assert entry["title"] == "Ship it"
        assert entry["level"] == "info"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "args", "expected_level", "expected_title_fragment"),
        [
            ("alert_strategy_error", ("ema", "boom"), "error", "Strategy Error: ema"),
            ("alert_daily_loss", (-1000.0, -500.0), "critical", "Daily Loss"),
            ("alert_system_down", ("redis",), "critical", "Service Down: redis"),
            ("alert_ib_disconnect", (), "critical", "IB Gateway Disconnected"),
        ],
    )
    async def test_convenience_methods_record_history_with_expected_level(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        method_name: str,
        args: tuple[object, ...],
        expected_level: str,
        expected_title_fragment: str,
    ) -> None:
        # Regression guard for existing callers (process_manager.py uses
        # alert_strategy_error; disconnect_handler.py uses alert_ib_disconnect).
        # The new kw-only level= parameter must not break these sites and
        # must carry severity through to the history record.
        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        service = AlertService()  # no SMTP

        await getattr(service, method_name)(*args)
        assert service.last_history_task is not None
        await service.last_history_task

        entry = json.loads((tmp_path / "alerts.json").read_text())["alerts"][0]
        assert entry["level"] == expected_level
        assert expected_title_fragment in entry["title"]


# ---------------------------------------------------------------------------
# GET /api/v1/alerts/ router
# ---------------------------------------------------------------------------


class TestAlertsRouter:
    async def test_list_alerts_returns_empty_when_no_history(
        self,
        client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(alerting_service, "path", tmp_path / "absent.json")

        async with client:
            response = await client.get("/api/v1/alerts/")

        assert response.status_code == 200
        assert response.json() == {"alerts": []}

    async def test_list_alerts_returns_persisted_records(
        self,
        client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "alerts.json"
        monkeypatch.setattr(alerting_service, "path", path)
        alerting_service.send_alert(level="warning", title="Hello", message="world")

        async with client:
            response = await client.get("/api/v1/alerts/")

        body = response.json()
        assert len(body["alerts"]) == 1
        assert body["alerts"][0]["title"] == "Hello"
        assert body["alerts"][0]["level"] == "warning"

    async def test_list_alerts_silently_clamps_limit(
        self,
        client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Codex contract: out-of-range limit is silently clamped to [1, 200]
        # rather than 422'd. Matches so a shared frontend hitting either
        # backend behaves identically.
        path = tmp_path / "alerts.json"
        monkeypatch.setattr(alerting_service, "path", path)
        for i in range(250):
            alerting_service.send_alert(level="info", title=f"t-{i}", message=".")

        async with client:
            too_low = await client.get("/api/v1/alerts/?limit=0")
            too_high = await client.get("/api/v1/alerts/?limit=500")

        assert too_low.status_code == 200
        assert too_high.status_code == 200
        # limit=0 clamps to 1; limit=500 clamps to 200 (also the history cap).
        assert len(too_low.json()["alerts"]) == 1
        assert len(too_high.json()["alerts"]) == 200

    async def test_list_alerts_skips_malformed_entries(
        self,
        client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operators occasionally hand-edit alerts.json. A malformed row
        # must not surface as a 500 — skip it, keep the valid rows.
        path = tmp_path / "alerts.json"
        monkeypatch.setattr(alerting_service, "path", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "alerts": [
                        {
                            "type": "alert",
                            "level": "warning",
                            "title": "valid",
                            "message": "ok",
                            "created_at": "2024-01-01T00:00:00Z",
                        },
                        {"type": "alert", "level": "warning"},  # missing title/message
                    ]
                }
            )
        )

        async with client:
            response = await client.get("/api/v1/alerts/")

        assert response.status_code == 200
        body = response.json()
        assert len(body["alerts"]) == 1
        assert body["alerts"][0]["title"] == "valid"

    async def test_list_alerts_returns_empty_when_history_lock_wedged(
        self,
        client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Codex iter 9 P2: the router must fail open when a wedged
        # writer holds the history lock — otherwise every GET /alerts
        # hangs indefinitely behind a stuck fsync/os.replace.
        import threading

        from msai.services import alerting as alerting_module

        path = tmp_path / "alerts.json"
        monkeypatch.setattr(alerting_service, "path", path)
        monkeypatch.setattr(alerting_module, "_HISTORY_WRITE_TIMEOUT_S", 0.3)
        alerting_service.send_alert(level="info", title="seed", message=".")

        release = threading.Event()

        def _wedged_list(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
            release.wait(timeout=5.0)
            return []

        monkeypatch.setattr(alerting_service, "list_alerts", _wedged_list)

        async with client:
            response = await client.get("/api/v1/alerts/")

        assert response.status_code == 200
        assert response.json() == {"alerts": []}

        # Cleanup
        release.set()

    async def test_list_alerts_runs_in_executor_not_event_loop(
        self,
        client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Codex iter 6 P2: alerting_service.list_alerts takes flock + reads
        # file I/O synchronously. The async router must offload it via
        # run_in_executor so slow storage or a contended write-lock can't
        # stall unrelated coroutines on the event loop.
        import threading

        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        alerting_service.send_alert(level="info", title="x", message=".")

        observed: list[int] = []
        main_thread = threading.get_ident()
        original = alerting_service.list_alerts

        def _record(*args: object, **kwargs: object) -> Any:
            observed.append(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(alerting_service, "list_alerts", _record)

        async with client:
            response = await client.get("/api/v1/alerts/?limit=10")

        assert response.status_code == 200
        assert len(observed) == 1
        assert observed[0] != main_thread, (
            "list_alerts must run in the executor, not the event-loop thread"
        )

    async def test_list_alerts_requires_authentication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Autouse fixture in conftest overrides get_current_user for every
        # test; lift that override here to verify the router enforces auth.
        from msai.core.auth import get_current_user
        from msai.main import app

        monkeypatch.setattr(alerting_service, "path", tmp_path / "alerts.json")
        app.dependency_overrides.pop(get_current_user, None)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/alerts/")

        assert response.status_code in (401, 403)

    async def test_list_alerts_honours_limit(
        self,
        client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "alerts.json"
        monkeypatch.setattr(alerting_service, "path", path)
        for i in range(10):
            alerting_service.send_alert(level="warning", title=f"t-{i}", message=".")

        async with client:
            response = await client.get("/api/v1/alerts/?limit=3")

        body = response.json()
        assert len(body["alerts"]) == 3
        # Newest first (t-9 latest).
        assert body["alerts"][0]["title"] == "t-9"
