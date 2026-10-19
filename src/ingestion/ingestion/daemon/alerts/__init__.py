"""Rule-based alert engine for the ingestor daemon.

Rules live in the ``alert_rules`` SQLite table. Every 30 seconds (default)
the engine evaluates each enabled rule against a :class:`MetricProvider`,
writes any firings into ``alert_firings``, and invokes the configured
:class:`Notifier`. Phase 4 ships the rule engine and a built-in metric
provider; control-panel CRUD for rules lands in phase 5.
"""

from __future__ import annotations

__all__: list[str] = []
