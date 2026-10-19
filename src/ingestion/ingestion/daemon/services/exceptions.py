"""Typed service-layer exceptions.

Routers translate each subclass to an HTTP error so callers don't see
arbitrary Python exception text on the wire. Each exception carries
just enough context for the router to compose a useful ``detail``
string; structured payloads (e.g., the rejected confirm value) are
passed through as keyword fields rather than baked into the message.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base for service-layer errors that translate to HTTP responses.

    Subclasses pin the failure category — routers branch on type, not
    on message text.
    """


class NotFoundError(ServiceError):
    """A targeted resource (user, credential, item, sync, rule) does
    not exist. Routers map this to 404."""


class CredentialNotFoundError(NotFoundError):
    """No row matched ``(user_email, platform)``."""


class UserNotFoundError(NotFoundError):
    """The user has zero credential rows; the email is unknown."""


class ItemNotFoundError(NotFoundError):
    """No ``IngestedItem`` matched the supplied id."""


class SyncNotFoundError(NotFoundError):
    """No ``sync_history`` row matched the supplied sync_id."""


class AlertRuleNotFoundError(NotFoundError):
    """No ``alert_rules`` row matched the supplied id."""


class StateConflictError(ServiceError):
    """Action is incompatible with the resource's current state.

    Used for cancel-on-non-running and similar transitions. Routers map
    this to 409.
    """


class FeatureDisabledError(ServiceError):
    """The action's feature gate is off (e.g., teams refresh disabled
    by config). Routers map this to 503."""


class TransientFailureError(ServiceError):
    """The action attempted but failed transiently (downstream HTTP
    error, network blip). Routers map this to 502 and surface ``detail``
    as a structured payload."""

    def __init__(self, detail: object) -> None:
        super().__init__(str(detail))
        self.detail = detail


class BackpressureError(ServiceError):
    """The action's queue is at capacity; refuse the new request.

    Slice 10 introduces this for the force_sync_requests queue: once
    the queue depth hits ``api.max_force_sync_queue_depth`` further
    inserts raise so an operator script cannot flood SQLite into a
    multi-second blocked-writer state. Routers map this to 503 with
    ``code: rate_limited`` so clients can retry later.

    Note: ``rate_limited`` is the operative code rather than
    ``transient_failure`` because a queue cap is an admission-control
    decision, not a downstream failure. ``Retry-After`` could be
    surfaced in a future iteration; the current call returns the
    current depth so clients can decide whether to back off.
    """

    def __init__(self, *, queue_depth: int, max_depth: int) -> None:
        super().__init__(f"force-sync queue at capacity ({queue_depth} >= {max_depth})")
        self.queue_depth = queue_depth
        self.max_depth = max_depth


class AuthenticationFailedError(ServiceError):
    """Pipeline detected a 401/403 from the platform mid-sync.

    Slice 8: raised by :func:`ingestion.pipeline.run_sync` after the
    pipeline catches an :class:`ingestion.exceptions.TokenExpiredError`
    (HTTP 401) or :class:`ingestion.exceptions.InsufficientScopeError`
    (HTTP 403) from the adapter. The pipeline marks the credential
    expired via the token store *before* raising so the dead-letter
    pivot point sees a consistent state.

    Carries the platform name so the worker pool's ``on_finish``
    callback can pivot on it for log-search drill-downs without
    parsing the message text.

    The pipeline does NOT retry the sync within the same call —
    a 401 is terminal, retrying just storms the platform with a
    revoked token. The bulk-refresh loop's next cycle will try to
    refresh; if that also returns ``EXPIRED`` the operator is
    notified via the credential.expired event. Routers map this to
    401 (consistent with the underlying HTTP cause) but production
    sees it through the worker pool, not directly on the wire.
    """

    def __init__(
        self,
        *,
        platform: str,
        user_email: str,
        http_status: int,
        cause: str,
    ) -> None:
        super().__init__(
            f"authentication failed for {user_email}/{platform} (http {http_status}): {cause}"
        )
        self.platform = platform
        self.user_email = user_email
        self.http_status = http_status
        self.cause = cause
