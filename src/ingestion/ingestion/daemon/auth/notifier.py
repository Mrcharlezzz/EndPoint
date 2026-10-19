"""Pluggable notifier for credential-expired transitions.

A :class:`Notifier` is invoked the instant a credential row flips from
``is_expired=False`` to ``is_expired=True``. Phase 3 only ships the log-only
implementation; Slack / email / webhook notifiers are scaffolding-only until
Phase 4+ delivery surfaces land.
"""

from __future__ import annotations

from typing import Protocol

from ingestion.daemon.logging import get_logger


class Notifier(Protocol):
    async def user_expired(self, user_email: str, platform: str) -> None: ...


class LogOnlyNotifier:
    """Default notifier: emits a structured warning, does nothing else."""

    async def user_expired(self, user_email: str, platform: str) -> None:
        get_logger(__name__).warning(
            "credential.expired",
            user_email=user_email,
            platform=platform,
        )


class NullNotifier:
    """No-op notifier. Use in tests that don't care about transitions."""

    async def user_expired(self, user_email: str, platform: str) -> None:
        return None
