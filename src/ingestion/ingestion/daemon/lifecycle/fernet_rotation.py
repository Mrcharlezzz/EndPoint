"""In-place Fernet key rotation for ``PlatformCredential`` rows.

When the operator rotates the Fernet key, the active
:class:`CredentialCipher` is built with both the new and old keys (the
new one as primary) so reads keep working through the transition.
But the on-disk ciphertexts still carry the old key's identifier — a
subsequent rotation that drops the old key would leave them
unreadable.

This module's :func:`rotate_fernet_in_place` walks every credential
row, decrypts with the current cipher, encrypts with the new cipher's
primary key, and writes back. It is:

* Idempotent: when a row's payload is already encrypted with the new
  primary, the rotation is a no-op (we detect this by trying the new
  cipher first).
* Interruptible: a SIGTERM mid-pass leaves the already-rotated rows
  intact. A subsequent run picks up where the first left off because
  the same idempotent detection skips already-rotated rows.
* Audit-logged: every rotated row writes one ``credential.fernet_rotated``
  audit entry. The audit row never includes ciphertext.
* Race-safe vs concurrent writes: the database UPDATE is keyed on the
  unchanged ``encrypted_payload`` value at decrypt time. If a refresh
  loop wrote a new payload between our read and our update, the update
  matches zero rows and we count the row as ``raced`` — the next pass
  will pick it up because the new write is encrypted with the same
  cipher that just landed.

Designed to be called from the ``POST /v1/actions/admin/fernet-rotate``
endpoint or directly from a one-shot ops script. The endpoint reads
``ENDPOINT_FERNET_KEY_NEW`` from the daemon's environment for the
target key — operators export the new key, send the request, then
once the response confirms ``failed=0`` they restart the daemon with
``ENDPOINT_FERNET_KEY`` set to just the new key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken

from ingestion.daemon.crypto import CredentialCipher, CryptoConfigError
from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Env var holding the rotation target key. The active key (the one
# already in ``ENDPOINT_FERNET_KEY``) decrypts existing rows; the
# value here is the new key the operator wants every row to land on.
ENV_FERNET_KEY_NEW = "ENDPOINT_FERNET_KEY_NEW"


@dataclass(frozen=True)
class RotationResult:
    """Per-row counts from a rotation pass.

    ``rotated`` is the number of rows that successfully re-encrypted
    against ``new_cipher``'s primary key. ``already_rotated`` covers
    rows whose payload was already readable by ``new_cipher`` — the
    pass left them untouched. ``raced`` rows had a concurrent write
    between read and update; a follow-up pass will pick them up.
    ``failed`` rows raised an exception other than the idempotent
    detection (e.g., a ciphertext readable by NEITHER cipher); the
    operator must investigate manually.
    """

    total_rows: int
    rotated: int
    already_rotated: int
    raced: int
    failed: int


# The audit-writer protocol matches services/audit_writer.py. Bare
# Callable here keeps the lifecycle module free of FastAPI imports.
type AuditWrite = Callable[..., Awaitable[int]]


async def rotate_fernet_in_place(
    *,
    current_cipher: CredentialCipher,
    new_cipher: CredentialCipher,
    audit_write: AuditWrite | None = None,
) -> RotationResult:
    """Walk every PlatformCredential row, decrypt+re-encrypt under the new key.

    Returns a :class:`RotationResult` carrying per-row counts. Each
    rotated row writes a ``credential.fernet_rotated`` audit entry
    via ``audit_write`` when supplied. The caller's audit writer is
    responsible for binding the actor; the rotation function only
    knows the (user_email, platform) target identifier.

    Concurrency: the rotation runs serially per row (one decrypt+
    encrypt per credential). Parallelising would speed it up at the
    cost of adding a contention path between the rotation pass and
    the live refresh loop — keep it sequential for now; the typical
    deployment carries 100s-1000s of rows, well within a few-second
    budget.
    """
    log = get_logger(__name__)
    # Lazy import: PlatformCredential touches the Django ORM and must
    # only load after core.bootstrap.init has run.
    from core.models import PlatformCredential  # noqa: PLC0415

    total = 0
    rotated = 0
    already = 0
    raced = 0
    failed = 0
    async for row in PlatformCredential.objects.all().aiterator():
        total += 1
        old_payload = bytes(row.encrypted_payload)
        outcome = await _rotate_one(
            row=row,
            old_payload=old_payload,
            current_cipher=current_cipher,
            new_cipher=new_cipher,
            audit_write=audit_write,
        )
        if outcome == "rotated":
            rotated += 1
        elif outcome == "already_rotated":
            already += 1
        elif outcome == "raced":
            raced += 1
        else:
            failed += 1
    log.info(
        "lifecycle.fernet_rotation_complete",
        total=total,
        rotated=rotated,
        already_rotated=already,
        raced=raced,
        failed=failed,
    )
    return RotationResult(
        total_rows=total,
        rotated=rotated,
        already_rotated=already,
        raced=raced,
        failed=failed,
    )


async def _rotate_one(
    *,
    row: Any,
    old_payload: bytes,
    current_cipher: CredentialCipher,
    new_cipher: CredentialCipher,
    audit_write: AuditWrite | None,
) -> str:
    """Rotate one row. Returns one of ``rotated``,
    ``already_rotated``, ``raced``, ``failed``.

    Detection order:

    1. Try ``new_cipher.decrypt_json`` first. If it succeeds, the row
       is already on the new key — no work needed.
    2. Fall back to ``current_cipher.decrypt_json``. If neither works,
       the row is corrupt or encrypted by a key not in any active
       cipher — return ``failed`` and let the caller surface the count
       for operator triage.
    3. Re-encrypt with ``new_cipher`` (primary key) and update the
       database. The UPDATE is keyed on ``encrypted_payload =
       old_payload`` so a concurrent write between our read and our
       update lands as a no-op (returns ``raced``).
    """
    log = get_logger(__name__)
    # Step 1: idempotent detection — already on new key?
    try:
        new_cipher.decrypt_json(old_payload)
    except CryptoConfigError:
        # Row is not yet on new key; fall through to rotation.
        pass
    else:
        return "already_rotated"

    # Step 2: decrypt with current cipher.
    try:
        plaintext = current_cipher.decrypt_json(old_payload)
    except CryptoConfigError:
        log.error(
            "lifecycle.fernet_rotation_unreadable",
            user_email=row.user_email,
            platform=row.platform,
        )
        return "failed"

    # Step 3: re-encrypt with the new primary and write back. The
    # WHERE clause matches the original payload so a concurrent
    # write (refresh loop) doesn't get clobbered.
    new_payload = new_cipher.encrypt_json(plaintext)
    # Lazy import: see top-of-module comment.
    from core.models import PlatformCredential  # noqa: PLC0415

    updated = await PlatformCredential.objects.filter(
        pk=row.pk,
        encrypted_payload=old_payload,
    ).aupdate(encrypted_payload=new_payload)
    if updated == 0:
        # Concurrent writer landed between read and update. The next
        # pass will see the row already on whichever cipher won.
        return "raced"
    if audit_write is not None:
        try:
            await audit_write(
                action="credential.fernet_rotated",
                target=f"{row.user_email}/{row.platform}",
                after={
                    "rotated_at": datetime.now(tz=UTC).isoformat(),
                },
            )
        except Exception:
            # Audit failure must not poison the rotation pass — the
            # write already landed, and re-running the rotation will
            # just no-op via the idempotent path. Log and move on.
            log.exception("lifecycle.fernet_rotation_audit_failed")
    return "rotated"


def build_new_cipher(env: dict[str, str] | None = None) -> CredentialCipher:
    """Build a single-key cipher whose only key is ``ENDPOINT_FERNET_KEY_NEW``.

    The rotation pass uses two ciphers: ``current_cipher`` (the daemon's
    existing one — typically a MultiFernet that knows the active and
    any prior keys) and ``new_cipher`` (this single-key cipher).

    Keeping ``new_cipher`` single-key is what makes the idempotent
    detection accurate: ``new_cipher.decrypt_json`` succeeds ONLY when
    the row is already encrypted with the new key. A MultiFernet that
    also knew the old key would silently decrypt old rows on the first
    attempt and the rotation would mis-classify them as "already
    rotated" and skip the re-encryption.

    Raises :class:`CryptoConfigError` when ``ENDPOINT_FERNET_KEY_NEW``
    is missing or malformed.
    """
    import os  # noqa: PLC0415

    source = env if env is not None else os.environ
    new_key = source.get(ENV_FERNET_KEY_NEW)
    if not new_key:
        raise CryptoConfigError(
            f"{ENV_FERNET_KEY_NEW} is not set; export it before triggering a rotation"
        )
    # Validate the new key is well-formed before constructing the
    # cipher so the rotation never starts against a typo.
    try:
        Fernet(new_key.encode())
    except (ValueError, InvalidToken) as exc:
        raise CryptoConfigError("ENDPOINT_FERNET_KEY_NEW is not valid Fernet key material") from exc
    return CredentialCipher([new_key.strip()])
