"""Pluggable :class:`AlertNotifier` implementations.

The default :class:`NullAlertNotifier` ships in
:mod:`ingestion.daemon.alerts.engine`. Slice 13 adds
:class:`WebhookAlertNotifier` for ops integrations. Pick by setting
``alerts.webhook_url`` (and optionally ``alerts.webhook_token`` /
``alerts.notifier_format``) in the daemon config; runtime swaps the
notifier in :func:`runtime._start_observability_loops`.
"""

from __future__ import annotations

from ingestion.daemon.alerts.notifiers.webhook import WebhookAlertNotifier

__all__ = ["WebhookAlertNotifier"]
