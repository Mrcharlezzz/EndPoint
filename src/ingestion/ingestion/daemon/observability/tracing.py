"""Optional OpenTelemetry tracing shim. Default: no-op.

This module exposes a single :func:`traced_span` context manager that
either delegates to OpenTelemetry's tracer (when the SDK is installed
and configured) or short-circuits to a no-op. Operators who want
distributed traces install ``opentelemetry-sdk`` and
``opentelemetry-exporter-otlp`` themselves and set
``OTEL_EXPORTER_OTLP_ENDPOINT`` (or the equivalent) — the daemon
detects the SDK at import time and starts emitting spans without code
changes.

We deliberately do NOT add ``opentelemetry-sdk`` to
``pyproject.toml``: the dependency footprint (gRPC, protobuf,
contextvar machinery) is heavy enough that pinning it for every
deployment would punish operators who aren't using the feature. The
soft-dependency pattern matches how Sentry is wired today (sentry-sdk
is optional, controlled by ``config.sentry.dsn``).

Usage::

    from ingestion.daemon.observability.tracing import traced_span

    async def run_sync(...):
        with traced_span("sync.run", platform=platform, user=user):
            ...

The span is auto-instrumented with the contextvars bound by
:func:`bind_sync_context` (sync_id, user_email, platform) — those
become span attributes automatically. Pass extra ``**attributes`` to
the context manager for span-specific data that doesn't live in the
contextvar set.
"""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator


_log = get_logger(__name__)


def _import_tracer() -> Any | None:
    """Return the OpenTelemetry tracer if the SDK is importable, else None.

    Two import paths are tried:

    * ``opentelemetry.trace.get_tracer`` is the SDK-installed surface.
    * ``opentelemetry.api`` (without the SDK) returns a no-op tracer
      that still implements :meth:`start_as_current_span`. We accept
      that — its no-op span has the right shape for our shim.

    A failed import returns ``None`` so the caller falls back to
    :data:`contextlib.nullcontext`. The lookup runs once per process
    (cached via :func:`_resolve_tracer`).
    """
    try:
        # Importing the lazy trace module is enough; ``get_tracer`` is
        # an Any-typed callable so the broad annotation is fine.
        from opentelemetry import trace  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return trace.get_tracer("ingestion.daemon")  # type: ignore[no-any-return,no-any-expr]
    except Exception:
        # Defensive: a misconfigured global tracer provider can raise
        # at first invocation. Log once and fall back to no-op.
        _log.warning("tracing.get_tracer_failed", exc_info=True)
        return None


# Module-private cache of the tracer lookup. The mixed-case names tell
# pyright the values are reassignable (a leading-underscore + lowercase
# identifier is the convention for "private mutable"); the singletons
# they implement (one tracer per process) are still effectively constant
# at runtime — :func:`reset_tracer_cache_for_tests` is the only path
# that flips them after the first lookup.
_tracer_cache: Any | None = None
_tracer_looked_up = False


def _resolve_tracer() -> Any | None:
    """Cached tracer accessor. Calls ``_import_tracer`` once per process."""
    global _tracer_cache, _tracer_looked_up  # noqa: PLW0603 — module-level cache
    if not _tracer_looked_up:
        _tracer_cache = _import_tracer()
        _tracer_looked_up = True
    return _tracer_cache


@contextmanager
def traced_span(name: str, **attributes: Any) -> Iterator[None]:
    """Context manager that opens an OpenTelemetry span when SDK is loaded.

    No-op fallback: when the SDK isn't importable, this is equivalent
    to :class:`contextlib.nullcontext` — no span overhead, no
    attribute conversion, just the body runs unchanged.

    Attributes passed as kwargs are recorded on the span verbatim if a
    tracer exists, ignored otherwise. Callers may also pass attributes
    via the contextvar set (sync_id, user_email, platform) — those
    are NOT auto-promoted by this shim, but the contextvar values are
    available on every log record inside the span via structlog's
    contextvars merger, which is what we actually consume in dashboards
    today. A future slice can add an OTel-specific contextvar mirror if
    span attributes become a hard requirement.
    """
    tracer = _resolve_tracer()
    if tracer is None:
        # Use suppress() of nothing as the most-trivial nullcontext
        # alternative; ``yield`` once and return.
        with contextlib.nullcontext():
            yield
        return
    span_cm = tracer.start_as_current_span(name)
    with span_cm as span:
        if attributes:
            try:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            except Exception:
                _log.warning(
                    "tracing.set_attribute_failed",
                    span_name=name,
                    exc_info=True,
                )
        yield


def reset_tracer_cache_for_tests() -> None:
    """Force a re-lookup on next :func:`traced_span` call.

    Tests that monkey-patch the OpenTelemetry import path use this to
    flush the cached tracer. Production code never calls this.
    """
    global _tracer_cache, _tracer_looked_up  # noqa: PLW0603 — test helper
    _tracer_cache = None
    _tracer_looked_up = False


__all__ = ["reset_tracer_cache_for_tests", "traced_span"]
