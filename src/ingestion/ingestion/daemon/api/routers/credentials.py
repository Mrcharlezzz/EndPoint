"""/api/credentials read surface.

Lists PlatformCredential rows for the dashboard's Credentials page
(handoff §7.5). Plaintext tokens never leave the daemon: the detail
endpoint can peek inside the encrypted payload to enumerate which keys
are present (e.g. ``["graph_token", "csa_token"]``) so the UI can show a
health breakdown, but the values are never surfaced.

The Credentials surface is read-only from the API layer. Mutations
(force refresh, mark expired, pause) live under ``/api/actions/*``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.system_users import SYSTEM_USER_EMAILS

router = APIRouter(prefix="/credentials", tags=["credentials"])

_MAX_LIMIT = 500


class CredentialSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_email: str
    display_name: str | None
    platform: str
    is_paused: bool
    is_expired: bool
    last_refreshed_at: str | None
    expired_at: str | None
    refresh_url: str | None


class CredentialDetail(CredentialSummary):
    """Summary plus the set of keys inside the encrypted payload.

    ``payload_readable=False`` means the Fernet decrypt failed; the UI
    should surface that as a credential health signal rather than
    treating it as a missing row."""

    payload_keys: list[str]
    payload_readable: bool


def _row_to_summary(row: Any) -> CredentialSummary:
    return CredentialSummary(
        user_email=row.user_email,
        display_name=row.display_name,
        platform=row.platform,
        is_paused=bool(row.is_paused),
        is_expired=bool(row.is_expired),
        last_refreshed_at=(
            row.last_refreshed_at.isoformat() if row.last_refreshed_at is not None else None
        ),
        expired_at=(row.expired_at.isoformat() if row.expired_at is not None else None),
        refresh_url=row.refresh_url or None,
    )


@router.get("", response_model=list[CredentialSummary])
async def list_credentials(
    ctx: ContextDep,
    platform: str | None = None,
    user_email: str | None = None,
    expired_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CredentialSummary]:
    """List credential rows with metadata but no encrypted material.

    Filterable by platform, user email, and ``is_expired``. Sorted by
    ``(user_email, platform)`` so the response is stable for the UI to
    paginate clientside.
    """
    _ = ctx
    from core.models import PlatformCredential

    qs = PlatformCredential.objects.exclude(user_email__in=SYSTEM_USER_EMAILS).order_by(
        "user_email", "platform"
    )
    if platform:
        qs = qs.filter(platform=platform)
    if user_email:
        qs = qs.filter(user_email=user_email)
    if expired_only:
        qs = qs.filter(is_expired=True)
    rows: list[CredentialSummary] = []
    fetched = 0
    async for row in qs.aiterator():
        if fetched < offset:
            fetched += 1
            continue
        rows.append(_row_to_summary(row))
        fetched += 1
        if len(rows) >= limit:
            break
    return rows


@router.get("/{email}/{platform}", response_model=CredentialDetail)
async def credential_detail(email: str, platform: str, ctx: ContextDep) -> CredentialDetail:
    """Return one credential's metadata plus the list of keys present in
    the encrypted payload (NOT the values). Useful for the health view:
    missing ``graph_token`` or ``csa_token`` means the Teams bundle is
    incomplete even if ``is_expired`` hasn't flipped yet.
    """
    from core.models import PlatformCredential

    if email in SYSTEM_USER_EMAILS:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "credential not found",
        )
    try:
        row = await PlatformCredential.objects.aget(user_email=email, platform=platform)
    except PlatformCredential.DoesNotExist:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "credential not found",
        )
    # Decrypt only to enumerate keys. A decrypt error becomes a credential
    # health signal (``payload_readable=False``) rather than a 500 so the
    # UI can render the row without the whole page crashing.
    payload_keys: list[str] = []
    payload_readable = False
    try:
        decoded = ctx.cipher.decrypt_json(bytes(row.encrypted_payload))
    except Exception:  # noqa: BLE001 (health signal; narrow-typed cipher errors are unavailable here)
        decoded = None
    if decoded is not None:
        payload_keys = sorted(decoded.keys())
        payload_readable = True

    summary = _row_to_summary(row)
    return CredentialDetail(
        **summary.model_dump(),
        payload_keys=payload_keys,
        payload_readable=payload_readable,
    )
