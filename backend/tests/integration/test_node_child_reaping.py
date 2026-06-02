"""T7 — node-crash child reaping: ``os.setsid`` + ``init: true``.

**Honest scope (documented in the plan T7 + the decision doc):** this hardens
the NODE-crash + child-reaping case. It does NOT make a supervisor-PROCESS
crash survivable — the supervisor IS the container PID 1, so its crash recreates
the container; that survivability is the deferred per-account-container
capability.

What PR 2 T7 actually delivers, and what these tests pin:

1. ``os.setsid()`` is called as the **first** executable statement of the spawned
   child entrypoint (``_trading_node_subprocess``). ``multiprocessing.Process``
   has NO ``preexec_fn`` (that's a ``subprocess.Popen`` feature), so the call
   lives inside the target function rather than as a spawn kwarg. Putting the
   child in its own session/process group means a SIGTERM delivered to the
   supervisor's process group does NOT cascade into the trading nodes, and the
   child reparents cleanly to the container init when the supervisor exits.

2. ``init: true`` on the ``live-supervisor`` service (both dev + prod compose)
   makes the container PID 1 a real init (tini) that reaps exited node children
   — no zombie accumulation. ``os.setsid`` only detaches the session; tini does
   the actual reaping. We can't run tini inside a pytest, so the compose
   assertions below pin the declarative half, and the live ``mp.Process`` test
   pins the ``os.setsid`` half by spawning a real child and confirming it became
   its own session leader and was reaped (no zombie) via ``join()``.
"""

from __future__ import annotations

import ast
import inspect
import multiprocessing as mp
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from msai.services.nautilus import trading_node_subprocess as tns

if TYPE_CHECKING:
    from multiprocessing.queues import Queue as MPQueue

pytestmark = pytest.mark.skipif(
    not hasattr(os, "setsid"),
    reason="os.setsid is POSIX-only; the reaping hardening is Linux/macOS-only",
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _child_reports_session(queue: MPQueue[int]) -> None:
    """Spawned-child target: exercise the SAME detach helper the production
    entrypoint calls as its first line, then report this child's own session id
    back to the parent and exit cleanly.

    Running the real ``tns._detach_session()`` (not a re-implementation) is what
    makes this an integration test of the actual code path rather than a test of
    ``os.setsid`` itself.
    """
    tns._detach_session()
    queue.put(os.getsid(0))  # 0 → "this process"


def test_child_detaches_into_own_session_and_is_reaped() -> None:
    # Arrange: a spawn-context process (matches production
    # ``mp.get_context('spawn')`` in FleetRouter) running the real detach helper.
    ctx = mp.get_context("spawn")
    queue: MPQueue[int] = ctx.Queue()
    proc = ctx.Process(target=_child_reports_session, args=(queue,))

    # Act
    proc.start()
    child_pid = proc.pid
    assert child_pid is not None
    reported_sid = queue.get(timeout=30)
    proc.join(timeout=30)

    # Assert (1): the child became its own session leader — setsid took effect.
    # A fresh ``spawn`` child would otherwise share the parent's session.
    assert reported_sid == child_pid, (
        "child should have created its own session via os.setsid "
        f"(reported sid {reported_sid} != child pid {child_pid})"
    )

    # Assert (2): the child exited cleanly and was reaped — ``join()`` returning
    # with a non-None exitcode means no zombie is left behind from the parent's
    # perspective. (Container-PID-1 reaping under tini/``init:true`` is the
    # complementary half, pinned by the compose assertions below.)
    assert proc.exitcode == 0, f"child should exit cleanly, got exitcode {proc.exitcode}"
    assert proc.pid is not None  # still resolvable post-join; not a zombie GC race


def test_detach_session_is_first_statement_of_child_entrypoint() -> None:
    """The detach MUST be the first executable statement of the spawned target.

    If a later refactor moves it below an import that pulls in
    ``nautilus_trader`` (which installs the uvloop policy and opens sockets), the
    child could already be sharing the supervisor's session when a signal
    arrives. Pin ordering structurally so the guarantee can't silently regress.
    """
    # ``_trading_node_subprocess`` is a module-level function, so
    # ``inspect.getsource`` returns it with no leading indentation and
    # ``ast.parse`` accepts it as-is (no dedent needed).
    src = inspect.getsource(tns._trading_node_subprocess)
    tree = ast.parse(src)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)

    body = func_def.body
    # Skip a leading docstring expression if present.
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        first = body[1]

    assert isinstance(first, ast.Expr), "first statement should be the detach call"
    call = first.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "_detach_session", (
        "os.setsid (via _detach_session) must be the FIRST executable statement "
        "of _trading_node_subprocess, before any nautilus_trader import"
    )


def test_detach_session_calls_os_setsid() -> None:
    """``_detach_session`` must actually call ``os.setsid`` (the real syscall),
    not just exist as a stub."""
    src = inspect.getsource(tns._detach_session)
    assert "os.setsid()" in src, "_detach_session must call os.setsid()"


@pytest.mark.parametrize("compose_file", ["docker-compose.dev.yml", "docker-compose.prod.yml"])
def test_live_supervisor_has_init_true(compose_file: str) -> None:
    """``init: true`` on the ``live-supervisor`` service in BOTH compose files so
    the container PID-1 init (tini) reaps exited node children."""
    path = _REPO_ROOT / compose_file
    config = yaml.safe_load(path.read_text())
    service = config["services"]["live-supervisor"]
    assert service.get("init") is True, (
        f"{compose_file}: live-supervisor must set 'init: true' so tini (PID 1) "
        "reaps exited node children (no zombie accumulation)"
    )


if __name__ == "__main__":  # pragma: no cover — manual smoke
    sys.exit(pytest.main([__file__, "-q"]))
