"""StreamRegistry — slug → deployment_id resolver
(Phase 3 task 3.4 helper).

The :class:`ProjectionConsumer` reads from one Nautilus
message bus stream per active deployment. Nautilus events
carry the ``trader_id`` (= ``MSAI-{deployment_slug}`` per
Task 1.5), but the projection layer + WebSocket fan-out key
on the ``deployment_id`` UUID. This module owns the slug
→ ``deployment_id`` resolution.

Why a separate module:

- The translator stays free of DB / Redis dependencies. It
  takes ``deployment_id`` as a kwarg and trusts the caller
  to resolve.
- The consumer maintains a small in-memory cache of
  ``slug → UUID`` so per-message DB lookups don't bottleneck
  the hot path. The cache is rebuilt on cold start by
  scanning the ``live_deployments`` table for active rows.
- Tests can pass a hand-rolled registry with synthetic
  mappings without standing up a real database.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID


class StreamRegistry:
    """Thread-safe in-memory cache of
    ``deployment_slug → deployment_id``.

    The registry is intentionally simple — a dict + a lock.
    The consumer populates it on startup by scanning
    ``live_deployments`` for active rows, and adds entries
    incrementally as new deployments are observed.

    Lookup is O(1). The lock is uncontended in practice
    because the only writers are the consumer's startup-scan
    coroutine + the deployment-start hook.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._slug_to_id: dict[str, UUID] = {}
        self._id_to_slug: dict[UUID, str] = {}
        self._streams: dict[UUID, str] = {}
        """deployment_id → message_bus_stream name (the
        ``trader-MSAI-{slug}-stream`` value Task 1.14 wrote
        to ``live_deployments.message_bus_stream`` at deploy
        time)."""

    def register(
        self,
        *,
        deployment_id: UUID,
        deployment_slug: str,
        stream_name: str,
    ) -> None:
        """Add a new deployment to the registry. Idempotent —
        re-registering the same slug overwrites the existing
        entry, which is the right behavior for a deployment
        being warm-restarted under the same slug."""
        with self._lock:
            self._slug_to_id[deployment_slug] = deployment_id
            self._id_to_slug[deployment_id] = deployment_slug
            self._streams[deployment_id] = stream_name

    def unregister(self, deployment_id: UUID) -> None:
        """Remove a deployment from the registry. Called when
        the supervisor reports a terminal status. The consumer
        stops reading the stream after the next loop iteration."""
        with self._lock:
            slug = self._id_to_slug.pop(deployment_id, None)
            if slug is not None:
                self._slug_to_id.pop(slug, None)
            self._streams.pop(deployment_id, None)

    def deployment_id_for_slug(self, slug: str) -> UUID | None:
        """Translate a Nautilus ``trader_id`` slug into the
        ``deployment_id`` the projection layer keys on. Returns
        ``None`` for an unknown slug — the consumer logs and
        ACKs the message rather than crashing on a slug it
        hasn't seen yet (the next startup scan will pick it
        up)."""
        with self._lock:
            return self._slug_to_id.get(slug)

    def deployment_id_for_trader_id(self, trader_id: str) -> UUID | None:
        """Convenience: extract the slug from a full
        ``MSAI-{slug}`` trader_id and look up the
        deployment_id. Returns ``None`` if the trader_id
        doesn't have the expected prefix or the slug is
        unknown."""
        if not trader_id.startswith("MSAI-"):
            return None
        slug = trader_id[len("MSAI-") :]
        return self.deployment_id_for_slug(slug)

    def stream_name_for(self, deployment_id: UUID) -> str | None:
        with self._lock:
            return self._streams.get(deployment_id)

    def active_streams(self) -> dict[UUID, str]:
        """Return a snapshot copy of every registered
        ``deployment_id → stream_name`` mapping. The
        consumer iterates this on every loop tick to know
        which streams to ``XREADGROUP`` from."""
        with self._lock:
            return dict(self._streams)

    def has_deployment(self, deployment_id: UUID) -> bool:
        with self._lock:
            return deployment_id in self._streams

    def __len__(self) -> int:
        with self._lock:
            return len(self._streams)

    def known_slugs(self) -> Iterable[str]:
        with self._lock:
            return list(self._slug_to_id.keys())
