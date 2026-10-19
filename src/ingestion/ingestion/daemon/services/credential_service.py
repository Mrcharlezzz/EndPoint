"""Pause / unpause / mark-expired actions on credential rows.

Each function:

* Reads the *before* state via :class:`CredentialPort`.
* Performs the mutation via the same port (single SQL UPDATE for bulk
  user-level changes; per-row for credential-level ones).
* Emits exactly one audit row through the injected :class:`AuditWriter`.
* Returns a typed :mod:`.results` model the router can hand back
  unchanged.

Routers translate :class:`NotFoundError` to HTTP 404; nothing else is
raised by these functions.

Bulk variants (slice 7):

* :func:`bulk_pause_credentials` and :func:`bulk_mark_expired` accept an
  XOR selector — either a platform OR a list of user emails — and pause /
  expire every matching credential. Per-item failures are captured into
  the result's ``errors`` list rather than aborting the whole batch, so
  an operator running an incident-response pause sees the partial state.
  Each call emits ONE bulk audit row carrying ``requested_count`` and
  ``affected_count`` so the dashboard's diff view shows the success
  ratio without N rows of noise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingestion.daemon.services.exceptions import (
    CredentialNotFoundError,
    UserNotFoundError,
)
from ingestion.daemon.services.results import (
    BulkOperationError,
    BulkOperationResult,
    MarkExpiredResult,
    PauseResult,
)

if TYPE_CHECKING:
    from ingestion.daemon.ports.credential_port import CredentialPort
    from ingestion.daemon.services.audit_writer import AuditWriter


async def pause_user(
    port: CredentialPort,
    email: str,
    *,
    audit_writer: AuditWriter,
    reason: str | None,
) -> PauseResult:
    """Pause every credential for ``email``. One audit row per call."""
    return await _set_user_pause(
        port,
        email,
        paused=True,
        action="user.pause",
        audit_writer=audit_writer,
        reason=reason,
    )


async def unpause_user(
    port: CredentialPort,
    email: str,
    *,
    audit_writer: AuditWriter,
    reason: str | None,
) -> PauseResult:
    """Inverse of :func:`pause_user`. One audit row per call."""
    return await _set_user_pause(
        port,
        email,
        paused=False,
        action="user.unpause",
        audit_writer=audit_writer,
        reason=reason,
    )


async def pause_credential(
    port: CredentialPort,
    email: str,
    platform: str,
    *,
    audit_writer: AuditWriter,
    reason: str | None,
) -> PauseResult:
    """Pause one ``(email, platform)`` credential."""
    return await _set_credential_pause(
        port,
        email,
        platform,
        paused=True,
        action="credential.pause",
        audit_writer=audit_writer,
        reason=reason,
    )


async def unpause_credential(
    port: CredentialPort,
    email: str,
    platform: str,
    *,
    audit_writer: AuditWriter,
    reason: str | None,
) -> PauseResult:
    """Inverse of :func:`pause_credential`."""
    return await _set_credential_pause(
        port,
        email,
        platform,
        paused=False,
        action="credential.unpause",
        audit_writer=audit_writer,
        reason=reason,
    )


async def mark_credential_expired(
    port: CredentialPort,
    email: str,
    platform: str,
    *,
    audit_writer: AuditWriter,
    reason: str | None,
) -> MarkExpiredResult:
    """Mark a single credential expired so the scheduler skips it."""
    row = await port.get(email, platform)
    if row is None:
        raise CredentialNotFoundError(f"{email}/{platform}")
    before = {"is_expired": row.is_expired}
    updated = await port.set_expired(email, platform, expired=True)
    await audit_writer.write(
        action="credential.mark_expired",
        target=f"{email}/{platform}",
        before=before,
        after={"is_expired": True, "rows_updated": updated},
        reason=reason,
    )
    return MarkExpiredResult(rows_affected=updated)


async def _set_user_pause(
    port: CredentialPort,
    email: str,
    *,
    paused: bool,
    action: str,
    audit_writer: AuditWriter,
    reason: str | None,
) -> PauseResult:
    """Shared body for pause_user / unpause_user.

    Snapshots the pre-mutation state, runs one bulk UPDATE, and emits
    a single audit row carrying the platforms list as the ``before``
    payload (matches the legacy shape so dashboard diff views keep
    rendering).
    """
    rows = await port.list_for_user(email)
    if not rows:
        raise UserNotFoundError(email)
    before_rows = [{"platform": r.platform, "is_paused": r.is_paused} for r in rows]
    updated = await port.set_paused_bulk_for_user(email, paused=paused)
    after_rows = [{**r, "is_paused": paused} for r in before_rows]
    await audit_writer.write(
        action=action,
        target=email,
        before={"platforms": before_rows},
        after={"platforms": after_rows, "rows_updated": updated},
        reason=reason,
    )
    return PauseResult(rows_affected=updated, rows_total=len(before_rows))


async def _set_credential_pause(
    port: CredentialPort,
    email: str,
    platform: str,
    *,
    paused: bool,
    action: str,
    audit_writer: AuditWriter,
    reason: str | None,
) -> PauseResult:
    """Shared body for pause_credential / unpause_credential."""
    row = await port.get(email, platform)
    if row is None:
        raise CredentialNotFoundError(f"{email}/{platform}")
    before = {"is_paused": row.is_paused}
    updated = await port.set_paused(email, platform, paused=paused)
    await audit_writer.write(
        action=action,
        target=f"{email}/{platform}",
        before=before,
        after={"is_paused": paused},
        reason=reason,
    )
    return PauseResult(rows_affected=updated, rows_total=1)


# --- Bulk operations (slice 7) -------------------------------------------


async def bulk_pause_credentials(
    port: CredentialPort,
    *,
    platform: str | None,
    user_emails: list[str] | None,
    audit_writer: AuditWriter,
    reason: str | None,
) -> BulkOperationResult:
    """Pause every credential matching the selector. Idempotent.

    Selector contract — exactly ONE of ``platform`` / ``user_emails``
    must be set. The router's request-body validator rejects the
    other shapes with 422 before calling here; this function asserts
    again because callers from internal code paths could otherwise
    reach an undefined branch silently.

    Per-item failures (a row deleted mid-batch, a write that hit a
    locked DB) are captured into ``errors`` rather than aborting the
    batch — operators running this in an incident need to see partial
    success so they can decide whether to retry the failures or stop.

    Idempotency: calling this a second time for the same selector
    returns ``paused_count=0`` (every row already paused). The audit
    row is still written so the action trail shows the second attempt.
    """
    return await _bulk_set_paused(
        port,
        platform=platform,
        user_emails=user_emails,
        paused=True,
        action="user.bulk_pause",
        audit_writer=audit_writer,
        reason=reason,
    )


async def bulk_mark_expired(
    port: CredentialPort,
    *,
    platform: str | None,
    user_emails: list[str] | None,
    audit_writer: AuditWriter,
    reason: str | None,
) -> BulkOperationResult:
    """Mark every matching credential as expired. Idempotent.

    Same selector + per-item failure semantics as :func:`bulk_pause_credentials`.
    The result's ``paused_count`` field is repurposed as "rows_flipped"
    (the contract uses one shape for both bulk operations); the action
    string in the audit row distinguishes the intent.
    """
    return await _bulk_set_expired(
        port,
        platform=platform,
        user_emails=user_emails,
        action="user.bulk_mark_expired",
        audit_writer=audit_writer,
        reason=reason,
    )


def _validate_bulk_selector(
    platform: str | None,
    user_emails: list[str] | None,
) -> None:
    """Reject empty / both-set selectors. Raises ValueError.

    The router maps the ValueError to 422 before reaching here in
    practice — this is the defense-in-depth check for callers from
    internal code paths.
    """
    if platform is None and not user_emails:
        raise ValueError("either platform or user_emails must be provided")
    if platform is not None and user_emails:
        raise ValueError("platform and user_emails are mutually exclusive")


async def _collect_bulk_targets(
    port: CredentialPort,
    *,
    platform: str | None,
    user_emails: list[str] | None,
) -> list[tuple[str, str, bool, bool]]:
    """Resolve the selector to a list of (email, platform, is_paused, is_expired).

    Reads ``CredentialPort.list_for_platform`` for the platform branch
    and ``list_for_user`` per email for the user-emails branch. Order
    is stable: rows come out grouped by user_email and platform so the
    audit row is reproducible.
    """
    rows: list[tuple[str, str, bool, bool]] = []
    if platform is not None:
        for cred in await port.list_for_platform(platform):
            rows.append((cred.user_email, cred.platform, cred.is_paused, cred.is_expired))
        return rows
    # user_emails branch — _validate_bulk_selector already established
    # this is non-empty.
    assert user_emails is not None
    for email in user_emails:
        for cred in await port.list_for_user(email):
            rows.append((cred.user_email, cred.platform, cred.is_paused, cred.is_expired))
    return rows


async def _bulk_set_paused(
    port: CredentialPort,
    *,
    platform: str | None,
    user_emails: list[str] | None,
    paused: bool,
    action: str,
    audit_writer: AuditWriter,
    reason: str | None,
) -> BulkOperationResult:
    """Shared body for the bulk-pause flow. ``paused=False`` would unpause
    if a future caller wants the inverse — kept symmetric to the single-row
    helpers even though only the True path is exposed today."""
    _validate_bulk_selector(platform, user_emails)
    targets = await _collect_bulk_targets(
        port,
        platform=platform,
        user_emails=user_emails,
    )
    affected = 0
    skipped = 0
    errors: list[BulkOperationError] = []
    for email, plat, is_paused, _is_expired in targets:
        # Skip rows already in the target state; record as skipped so
        # the dashboard can show "5 paused, 3 already paused" cleanly.
        if is_paused == paused:
            skipped += 1
            continue
        try:
            updated = await port.set_paused(email, plat, paused=paused)
        except Exception as exc:  # noqa: BLE001 — capture per-item, continue batch
            # Swallow on a per-item basis so one wedged row doesn't
            # abort the whole batch. The exception type is recorded
            # for operator triage.
            errors.append(
                BulkOperationError(
                    user_email=email,
                    platform=plat,
                    error_code=type(exc).__name__,
                )
            )
            continue
        if updated:
            affected += 1
        else:
            # Race: someone else flipped state between our read and
            # write — count as skipped so the totals reconcile.
            skipped += 1
    await audit_writer.write(
        action=action,
        target=_bulk_audit_target(platform=platform, user_emails=user_emails),
        before={
            "selector": _selector_payload(platform, user_emails),
            "requested_count": len(targets),
        },
        after={
            "affected_count": affected,
            "skipped_count": skipped,
            "error_count": len(errors),
        },
        reason=reason,
    )
    return BulkOperationResult(
        paused_count=affected,
        skipped_count=skipped,
        errors=errors,
        requested_count=len(targets),
    )


async def _bulk_set_expired(
    port: CredentialPort,
    *,
    platform: str | None,
    user_emails: list[str] | None,
    action: str,
    audit_writer: AuditWriter,
    reason: str | None,
) -> BulkOperationResult:
    """Shared body for the bulk-mark-expired flow. Mirrors _bulk_set_paused
    but flips ``is_expired`` instead. Same idempotency + per-item error
    capture."""
    _validate_bulk_selector(platform, user_emails)
    targets = await _collect_bulk_targets(
        port,
        platform=platform,
        user_emails=user_emails,
    )
    affected = 0
    skipped = 0
    errors: list[BulkOperationError] = []
    for email, plat, _is_paused, is_expired in targets:
        if is_expired:
            skipped += 1
            continue
        try:
            updated = await port.set_expired(email, plat, expired=True)
        except Exception as exc:  # noqa: BLE001 — capture per-item, continue batch
            errors.append(
                BulkOperationError(
                    user_email=email,
                    platform=plat,
                    error_code=type(exc).__name__,
                )
            )
            continue
        if updated:
            affected += 1
        else:
            skipped += 1
    await audit_writer.write(
        action=action,
        target=_bulk_audit_target(platform=platform, user_emails=user_emails),
        before={
            "selector": _selector_payload(platform, user_emails),
            "requested_count": len(targets),
        },
        after={
            "affected_count": affected,
            "skipped_count": skipped,
            "error_count": len(errors),
        },
        reason=reason,
    )
    return BulkOperationResult(
        paused_count=affected,
        skipped_count=skipped,
        errors=errors,
        requested_count=len(targets),
    )


def _bulk_audit_target(
    *,
    platform: str | None,
    user_emails: list[str] | None,
) -> str:
    """Compose the ``audit_log.target`` string for a bulk operation.

    Platform branch -> ``platform:<name>``; user-emails branch ->
    ``users:<count>``. Keeps the audit-target column queryable without
    blowing up the value with a long email list (those land in the
    ``before`` JSON snapshot instead).
    """
    if platform is not None:
        return f"platform:{platform}"
    count = len(user_emails) if user_emails else 0
    return f"users:{count}"


def _selector_payload(
    platform: str | None,
    user_emails: list[str] | None,
) -> dict[str, object]:
    """Build a JSON-friendly snapshot of the selector for the audit row."""
    if platform is not None:
        return {"platform": platform}
    return {"user_emails": list(user_emails or [])}
