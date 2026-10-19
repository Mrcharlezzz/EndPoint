"""Postgres-backed :class:`ContentSink` against ``IngestedItem``.

The normalized in-memory item projects to v5 via :meth:`NormalizedItem.to_payload`;
platform, external_id, source_url, and the legacy course_external_id column stay
at the top level. Upserts key on ``external_id`` alone, which is globally unique
in the DB; ``platform`` is a defaults-only column. Adapters prefix external_id
with the platform name so cross-platform collisions are structurally impossible.

Processing-status policy: on a re-ingest (same external_id, fresh payload) the
existing ``processing_status`` is preserved. Upstream processors that need a
re-run after a payload change can either bump schema_version or explicitly
reset the row themselves — the ingestion path does not force re-processing.

Shadow mode: when ``is_shadow_mode()`` returns True, writes are logged and
suppressed — the sink behaves as if every upsert succeeded (returning a
sentinel id) but no row lands in Postgres. Scope is deliberately limited to
content (``upsert``/``upsert_batch``); other writers (audit, dead-letter,
sync-history failures) are not gated here. The overlay is polled on every
call so an operator can flip the switch mid-flight without restarting the
daemon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from core.models import IngestedItem

from ingestion import __version__ as _adapter_version
from ingestion.schemas import PAYLOAD_SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable

    from ingestion.schemas import NormalizedItem

_log = structlog.get_logger(__name__)

# Sentinel returned from ``upsert`` under shadow mode. The pipeline logs
# per-id at DEBUG; returning a fixed prefix lets a grep at ``shadow:`` find
# every suppressed write without polluting the real IngestedItem.id space.
_SHADOW_ID_PREFIX = "shadow:"


class PostgresContentSink:
    """Write normalized items to the shared ``ingested_items`` table.

    ``is_shadow_mode`` is polled before each write. A truthy return causes
    the sink to emit a ``shadow_mode_write_suppressed`` log line and skip
    the DB round-trip. Default is a no-op callable returning False so
    constructions that don't care about shadow mode keep the old shape.
    """

    def __init__(
        self,
        adapter_version: str | None = None,
        *,
        is_shadow_mode: Callable[[], bool] | None = None,
    ) -> None:
        self._adapter_version = adapter_version or _adapter_version
        # Default: no shadow-mode. Runtime wires in a ``lambda:
        # load_config(store).ops.shadow_mode`` so the overlay is read live.
        self._is_shadow_mode = is_shadow_mode or (lambda: False)

    async def exists(self, platform: str, external_id: str) -> bool:
        # external_id is unique across the table, so platform is redundant in
        # the filter; we keep it as a defensive correctness check (an
        # external_id that exists but belongs to a different platform would
        # flag a contract violation elsewhere).
        return await IngestedItem.objects.filter(
            platform=platform,
            external_id=external_id,
        ).aexists()

    async def exists_batch(self, platform: str, external_ids: list[str]) -> set[str]:
        """Return the subset of ``external_ids`` already present for ``platform``.

        Replaces an O(N) round-trip loop with a single ``WHERE external_id IN
        (...)`` query. The pipeline calls this once on the full fetch result,
        then iterates items locally to compute the new-vs-updated split.

        Implementation notes:

        * Empty input short-circuits — Postgres rejects ``IN ()`` and there is
          no useful query to run anyway.
        * Inputs are deduplicated before the query so the parameter list never
          carries unnecessary duplicates from a noisy adapter pass.
        * The list is chunked at 800 ids per query to stay well under SQLite's
          default 999-parameter limit (the same client code may be ported to
          drive a SQLite sink later) and Postgres's practical limits on a
          single ``IN`` list.
        """
        if not external_ids:
            return set()
        unique_ids = list(dict.fromkeys(external_ids))
        chunk_size = 800
        found: set[str] = set()
        for start in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[start : start + chunk_size]
            rows = IngestedItem.objects.filter(
                platform=platform,
                external_id__in=chunk,
            ).values_list("external_id", flat=True)
            async for existing in rows:
                found.add(str(existing))
        return found

    async def upsert(self, item: NormalizedItem) -> str:
        if self._is_shadow_mode():
            _log.info(
                "shadow_mode_write_suppressed",
                platform=item.platform,
                external_id=item.external_id,
                item_count=1,
            )
            return f"{_SHADOW_ID_PREFIX}{item.external_id}"
        defaults = {
            "platform": item.platform,
            "course_external_id": item.course_external_id,
            "source_url": item.source_url,
            "schema_version": PAYLOAD_SCHEMA_VERSION,
            "adapter_version": self._adapter_version,
            "ingestion_mode": item.ingestion_mode,
            "fetched_by_user_email": item.fetched_by_user_email,
            "payload": item.to_payload(),
            "raw_payload": item.raw_payload,
        }
        obj, _created = await IngestedItem.objects.aupdate_or_create(
            external_id=item.external_id,
            defaults=defaults,
        )
        return str(obj.id)

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        # Phase 1: per-item upsert. Django has no async bulk upsert; batching
        # via a single executemany loses update-or-create semantics. Revisit
        # if the per-user item volume forces it.
        #
        # Shadow mode is checked once per batch so a mid-batch overlay flip
        # doesn't produce a split-write (some rows persisted, some not).
        # The single log line carries item_count so one batch == one audit
        # trail entry rather than N identical lines.
        if self._is_shadow_mode():
            external_ids = [item.external_id for item in items]
            sync_id = None
            if items and items[0].fetched_by_user_email is not None:
                # Identifier the pipeline uses in its log context — binding
                # it here makes the suppressed write join cleanly with the
                # surrounding structlog contextvars (user_email / platform).
                sync_id = items[0].fetched_by_user_email
            _log.info(
                "shadow_mode_write_suppressed",
                item_count=len(items),
                sync_id=sync_id,
                external_ids=external_ids[:10],
            )
            return [f"{_SHADOW_ID_PREFIX}{xid}" for xid in external_ids]
        return [await self.upsert(item) for item in items]
