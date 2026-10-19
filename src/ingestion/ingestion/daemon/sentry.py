"""Optional Sentry / GlitchTip integration.

Initialise ``sentry_sdk`` once at daemon startup. The SDK's
:class:`LoggingIntegration` auto-captures every ``logger.error`` and
``logger.exception`` call as a Sentry event; the Starlette/FastAPI
integration catches unhandled request exceptions and attaches request
context; the asyncio integration reports unhandled task failures.

Sync-context correlation is handled via a ``before_send`` processor that
promotes structlog's contextvars (``user_email``, ``platform``,
``sync_id``) onto the outgoing event as tags so per-user filtering in
the GlitchTip UI works without any manual tagging in call sites.

Module is a no-op when ``SentryConfig.dsn`` is empty, so CI, tests, and
environments without a GlitchTip instance all boot cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ingestion.daemon.config import SentryConfig

_PROMOTED_CONTEXT_KEYS = ("user_email", "platform", "sync_id", "scope")


def _contextvar_tag_processor(event: Any, hint: Any) -> Any:
    """Copy the daemon's bound contextvars onto the outgoing Sentry event.

    structlog stores the current correlation fields in ``contextvars``;
    the vanilla Sentry SDK doesn't look there. Pulling them onto the
    event's ``tags`` makes user/platform/sync filtering work in GlitchTip
    with zero per-call-site tagging.
    """
    from structlog.contextvars import get_contextvars

    _ = hint
    bound = get_contextvars()
    if not bound:
        return event
    event_dict = cast("dict[str, Any]", event)
    tags = event_dict.setdefault("tags", {})
    for key in _PROMOTED_CONTEXT_KEYS:
        value = bound.get(key)
        if value is None:
            continue
        # Sentry tag values must be strings; coerce defensively.
        tags.setdefault(key, str(value))
    return event


def init_sentry(cfg: SentryConfig) -> bool:
    """Initialise sentry_sdk when a DSN is configured.

    Returns ``True`` if Sentry was initialised, ``False`` if the DSN was
    empty (no-op, daemon proceeds without external error forwarding).

    Import is lazy so environments that don't install the extras (tests,
    CI) don't explode on module import.
    """
    if not cfg.dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=cfg.dsn,
        environment=cfg.environment,
        traces_sample_rate=cfg.traces_sample_rate,
        integrations=[
            # level=INFO keeps INFO-level logs as breadcrumbs (not events);
            # event_level=ERROR means ERROR and CRITICAL records become
            # Sentry events. Matches how the daemon already uses logging.
            LoggingIntegration(level=None, event_level=40),  # logging.ERROR
            AsyncioIntegration(),
            StarletteIntegration(),
        ],
        before_send=_contextvar_tag_processor,
    )
    return True
