"""Admin-only operations: Fernet key rotation, etc.

Routers translate HTTP envelope; this module owns the orchestration
between :class:`CredentialCipher`, the lifecycle rotation pass, and
the audit trail. Kept separate from the credential / sync services
because the operations here mutate the encryption key itself, not
the credentials they protect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingestion.daemon.lifecycle.fernet_rotation import (
    RotationResult,
    build_new_cipher,
    rotate_fernet_in_place,
)
from ingestion.daemon.logging import get_logger
from ingestion.daemon.services.exceptions import FeatureDisabledError

if TYPE_CHECKING:
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.services.audit_writer import AuditWriter


async def trigger_fernet_rotation(
    *,
    current_cipher: CredentialCipher,
    audit_writer: AuditWriter,
    reason: str | None,
) -> RotationResult:
    """Rotate every PlatformCredential row to the new primary Fernet key.

    The new key is read from the daemon's environment via
    :func:`build_new_cipher`. Returns the per-row counts.

    Raises :class:`FeatureDisabledError` (-> HTTP 503) when the env
    var ``ENDPOINT_FERNET_KEY_NEW`` is missing — the operator must
    export the new key before calling this endpoint.

    The rotation is idempotent: a re-run after a partial rotation
    only touches rows still on the old key. If a row is unreadable
    by either cipher (e.g. corrupted), the result's ``failed`` count
    surfaces it for triage.
    """
    log = get_logger(__name__)
    try:
        new_cipher = build_new_cipher()
    except Exception as exc:
        # build_new_cipher raises CryptoConfigError on missing env
        # vars or malformed key material. Surface as feature-disabled
        # so the router maps to 503 with a useful detail.
        log.warning("admin.fernet_rotation_setup_failed", error=str(exc))
        raise FeatureDisabledError(str(exc)) from exc

    log.info("admin.fernet_rotation_started")
    # The audit row carries one bookkeeping entry per row inside the
    # rotation pass; this is the umbrella entry the operator
    # initiated. Audit-first so a partial rotation is still traceable.
    await audit_writer.write(
        action="admin.fernet_rotation_requested",
        target="all_credentials",
        reason=reason,
    )
    result = await rotate_fernet_in_place(
        current_cipher=current_cipher,
        new_cipher=new_cipher,
        audit_write=audit_writer.write,
    )
    log.info(
        "admin.fernet_rotation_finished",
        total=result.total_rows,
        rotated=result.rotated,
        already_rotated=result.already_rotated,
        raced=result.raced,
        failed=result.failed,
    )
    await audit_writer.write(
        action="admin.fernet_rotation_completed",
        target="all_credentials",
        after={
            "total": result.total_rows,
            "rotated": result.rotated,
            "already_rotated": result.already_rotated,
            "raced": result.raced,
            "failed": result.failed,
        },
        reason=reason,
    )
    return result
