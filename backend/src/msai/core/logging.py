"""Structured logging foundation for MSAI v2.

Provides centralized logging configuration using structlog with environment-aware
rendering (pretty console for dev, JSON for prod) and FastAPI middleware for
automatic request_id injection via contextvars.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import structlog
from starlette.requests import Request
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    get_contextvars,
    unbind_contextvars,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.responses import Response
    from starlette.types import ASGIApp, Receive, Scope, Send


def setup_logging(environment: str) -> None:
    """Configure structlog globally based on the target environment.

    Args:
        environment: Runtime environment name. Use ``"development"`` for
            human-readable console output or ``"production"`` for JSON lines.
            Any value other than ``"development"`` is treated as production.

    The function sets up a processor chain that:
    - Merges contextvars (e.g. ``request_id`` bound by middleware) into every log
    - Adds the log level string
    - Stamps an ISO-8601 timestamp
    - Renders stack info and exception tracebacks
    - Decodes bytes to strings

    In **development** the final renderer is ``ConsoleRenderer`` (coloured, padded).
    In **production** the final renderer is ``JSONRenderer`` for machine parsing.

    Log level is ``DEBUG`` (10) in development and ``INFO`` (20) in production.
    """
    is_dev: bool = environment.lower() == "development"
    is_test: bool = environment.lower() == "test"
    log_level: int = logging.DEBUG if is_dev else logging.INFO

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.types.Processor
    if is_dev:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        # Test envs disable the first-use cache so structlog.testing.capture_logs()
        # can replace the processor chain on already-bound loggers. Production +
        # dev keep caching enabled for the perf win.
        cache_logger_on_first_use=not is_test,
        context_class=dict,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a named structlog bound logger.

    The returned logger inherits the global configuration set by
    :func:`setup_logging` and automatically includes any context variables
    (e.g. ``request_id``) that are active in the current execution context.

    The ``logger_name`` key is bound into the event dict so it appears in
    both console and JSON output regardless of the underlying logger factory.

    Args:
        name: Logical name for the logger, typically ``__name__`` of the
            calling module.

    Returns:
        A structlog ``BoundLogger`` instance with the given name bound.
    """
    logger: structlog.BoundLogger = structlog.get_logger(logger_name=name)
    return logger


@contextmanager
def bind_deployment(deployment_id: UUID | str) -> Iterator[None]:
    """Bind ``deployment_id`` to structlog contextvars for the duration of a block.

    Every log call emitted inside the ``with`` block — including from
    helper functions and async tasks scheduled within the block — will
    automatically carry the ``deployment_id`` field. On exit (normal or
    exceptional), the prior contextvars value is restored so concurrent
    deployments don't leak ids into each other's log streams.

    Nested usage is supported: an inner ``bind_deployment`` overrides
    the outer id while it's in scope, and the outer id is restored when
    the inner block exits.

    Args:
        deployment_id: Either a ``UUID`` (canonical DB form, hex-stringified
            into the log record) or a string (slug-form id used by the
            live trading subprocesses).

    Example::

        from msai.core.logging import bind_deployment, get_logger

        log = get_logger(__name__)

        with bind_deployment(deployment.id):
            log.info("starting_node")  # → {... "deployment_id": "abc..."}
            await trading_node.start_async()
    """
    serialized: str = deployment_id.hex if isinstance(deployment_id, UUID) else deployment_id
    # Capture the prior value (if any) so we can restore it on exit.
    prior: dict[str, Any] = get_contextvars()
    prior_value: Any = prior.get("deployment_id")

    bind_contextvars(deployment_id=serialized)
    try:
        yield
    finally:
        if prior_value is None:
            unbind_contextvars("deployment_id")
        else:
            bind_contextvars(deployment_id=prior_value)


class LoggingMiddleware:
    """ASGI middleware that injects a unique ``request_id`` into structlog context.

    For every incoming HTTP request the middleware:

    1. Clears any stale contextvars from a previous request.
    2. Generates a new UUID4 ``request_id``.
    3. Binds it (plus ``method`` and ``path``) to structlog contextvars so that
       **all** log statements emitted during the request automatically include
       the ``request_id``.

    Usage with FastAPI::

        from fastapi import FastAPI
        from msai.core.logging import LoggingMiddleware, setup_logging

        app = FastAPI()
        setup_logging("development")
        app.add_middleware(LoggingMiddleware)
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process an ASGI connection.

        Only HTTP requests get a ``request_id``; other protocols (websocket,
        lifespan) are passed through unchanged.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        clear_contextvars()

        request_id: str = str(uuid4())
        request = Request(scope)
        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        await self.app(scope, receive, send)


async def logging_middleware(request: Request, call_next: Any) -> Response:
    """FastAPI-style HTTP middleware that injects ``request_id`` into structlog context.

    Designed for use with ``app.middleware("http")``::

        @app.middleware("http")
        async def add_logging(request: Request, call_next):
            return await logging_middleware(request, call_next)

    Or equivalently::

        app.middleware("http")(logging_middleware)

    Args:
        request: The incoming Starlette/FastAPI request.
        call_next: Callable that forwards the request to the next middleware
            or route handler and returns the response.

    Returns:
        The ``Response`` produced by the downstream handler, with a
        ``X-Request-ID`` header appended.
    """
    clear_contextvars()

    request_id: str = str(uuid4())
    bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )

    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
