"""Smoke-test the GlitchTip / Sentry forwarding pipeline.

Loads ``DaemonConfig`` (env vars + overlay), initialises ``sentry_sdk``,
emits one structured log-error and one explicit ``capture_exception``,
flushes, and exits. If the round-trip works the two events appear in the
configured GlitchTip project within a few seconds.

Usage::

    uv run python scripts/sentry_ping.py

Skips with a non-zero exit if ``ENDPOINT_DAEMON__SENTRY__DSN`` is empty —
otherwise we'd silently pass without proving anything.
"""

from __future__ import annotations

import logging
import sys

from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.logging import bind_sync_context, configure_logging, get_logger
from ingestion.daemon.sentry import init_sentry


def main() -> int:
    configure_logging("INFO")
    cfg = DaemonConfig()
    if not cfg.sentry.dsn:
        print(
            "ENDPOINT_DAEMON__SENTRY__DSN is empty — cannot verify forwarding.",
            file=sys.stderr,
        )
        return 2

    ok = init_sentry(cfg.sentry)
    if not ok:
        print("init_sentry returned False despite a DSN being set.", file=sys.stderr)
        return 3

    log = get_logger("ingestion.scripts.sentry_ping")

    # (1) structured log-error: flows via sentry_sdk.LoggingIntegration.
    with bind_sync_context(
        user_email="smoke-test@example.com",
        platform="teams",
        sync_id="smoke-ping-001",
    ):
        log.error(
            "ingestion.smoke.log_error",
            code="SMOKE_LOG_ERROR",
            detail="structured logger.error from sentry_ping.py",
        )

    # (2) explicit exception capture via sentry_sdk. Raising and letting the
    # except block call capture_exception keeps parity with the pipeline's
    # actual error-path shape.
    import sentry_sdk

    try:
        raise RuntimeError("smoke exception from sentry_ping.py")
    except RuntimeError as exc:
        sentry_sdk.capture_exception(exc)

    # Flush synchronously so a ~1s script doesn't race the background sender.
    sentry_sdk.flush(timeout=10.0)

    print("Sent two events. Check the GlitchTip project within a few seconds.")
    print(f"  log level   : {logging.getLevelName(logging.ERROR)}")
    print(f"  environment : {cfg.sentry.environment}")
    print(f"  public URL  : {cfg.sentry.public_url or '(unset)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
