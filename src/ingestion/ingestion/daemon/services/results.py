"""Pydantic response models shared between services and routers.

Each shape owned here was previously inlined in
``ingestion.daemon.api.routers.actions``. They are lifted into the
service layer so the routers (which now exist as four split files) and
the services agree on a single source of truth for the response shape.

Only models that surface in the OpenAPI document live here. Anything
that's an internal-only result type stays inside the service module that
returns it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PauseResult(BaseModel):
    """Outcome of a (un)pause action.

    ``rows_affected`` counts rows that flipped state; ``rows_total`` is
    the total rows in scope (one for credential-level actions, the
    user's full credential count for user-level actions).
    """

    model_config = ConfigDict(extra="forbid")

    rows_affected: int
    rows_total: int


class MarkExpiredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows_affected: int


class DrainModeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drain_mode: bool
    previous: bool


class ForceRefreshResult(BaseModel):
    """Outcome of one on-demand Teams refresh.

    Returned only when the refresh succeeded or the credential was
    detected as expired; transient failures surface as 502 and never
    reach the response model."""

    model_config = ConfigDict(extra="forbid")

    outcome: str


class ForceSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    user_email: str
    platform: str


class ForceSyncAllResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_email: str
    enqueued: list[str]
    skipped: list[str]


class DryRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items_fetched: int
    items_would_write: int
    items_would_skip: int
    errors: list[str]


class ResetCursorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows_deleted: int


class BulkRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enqueued: int


class PurgeItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    item_id: str


class CompactStorageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_bytes: int
    after_bytes: int


class MuteRuleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: int
    enabled: bool


class CancelSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sync_id: str
    previous_state: str
    new_state: str


class BulkOperationError(BaseModel):
    """Per-item failure carried inside :class:`BulkOperationResult`.

    Bulk operations capture per-row failures rather than aborting the
    whole batch — operators running an incident-response action need to
    see partial state. ``error_code`` is the exception class name (not
    the message) so the wire format stays small and predictable; the
    full traceback lands in the daemon's structlog feed.
    """

    model_config = ConfigDict(extra="forbid")

    user_email: str
    platform: str
    error_code: str


class BulkOperationResult(BaseModel):
    """Outcome envelope for bulk pause / mark-expired calls.

    ``paused_count`` is the number of rows that flipped state — for
    bulk-mark-expired this represents rows whose ``is_expired`` flipped
    True, named ``paused_count`` for shape parity with bulk-pause so the
    dashboard can render both with one component.

    ``skipped_count`` covers rows already in the requested state (the
    common idempotent re-call) AND rows where the ``set_*`` call
    returned 0 due to a race with another writer. The two cases are
    indistinguishable on the wire by design — the operator only cares
    about the totals.

    ``errors`` carries per-item failure rows; an empty list is the
    happy path. ``requested_count`` is ``affected + skipped + len(errors)``,
    redundant on the wire but useful so operators don't have to add
    three numbers in their head.
    """

    model_config = ConfigDict(extra="forbid")

    paused_count: int
    skipped_count: int
    errors: list[BulkOperationError]
    requested_count: int


class OverlayResetResult(BaseModel):
    """Outcome of POST /api/config/overlay/reset-all.

    ``cleared_keys`` is the list of overlay keys that existed pre-reset
    and were deleted by this call. Empty list when the overlay was
    already empty (idempotent re-call).
    """

    model_config = ConfigDict(extra="forbid")

    cleared_keys: list[str]


class ReplayDeadLetterResult(BaseModel):
    """Outcome of POST /api/actions/dead_letter/{email}/{platform}/replay.

    ``counter_preserved`` is True by design — the contrast with the
    ``clear`` action is the one piece of operator-facing information
    the wire shape needs to surface, so callers can branch on it
    without reading docs. (See ``maintenance_service.replay_dead_letter``
    for why preservation matters.)
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    user_email: str
    platform: str
    counter_preserved: bool


class ProcessorHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processed_count: int
    failed_count: int
    pending_queue: int
    last_processed_at: str | None


class ProcessorLLMSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: int
    input_tokens: int
    output_tokens: int
    failed_count: int
    unclassified_count: int


class ProcessorSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_start: str
    window_end: str
    baseline_at: str | None
    health: ProcessorHealthSummary
    llm: ProcessorLLMSummary


class ProcessorErrorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failed_at: str
    ingested_item_id: str
    platform: str
    fetched_by_user_email: str | None
    course_external_id: str | None
    content_type: str | None
    error_message: str


class ProcessorErrorsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_start: str
    window_end: str
    baseline_at: str | None
    items: list[ProcessorErrorItem]


class ProcessorWindowResetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_at: str
    previous_baseline_at: str | None
