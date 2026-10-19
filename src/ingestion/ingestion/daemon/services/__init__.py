"""Service layer between API routers and the daemon's data ports.

Each service module owns one bounded action surface:

    * :mod:`.credential_service` — pause / unpause / mark-expired,
      operating through the :class:`CredentialPort` protocol.
    * :mod:`.sync_service` — force-sync, dry-run, cancel, scheduler
      drain / emergency-stop. Owns the queue + sync_history mutations.
    * :mod:`.maintenance_service` — reset-cursor, purge-item, vacuum,
      clear dead-letter, mute-rule, force-refresh.

Routers translate HTTP envelopes into service calls plus typed-result
translation. Services emit audit-log rows themselves so a missed write
is visible at the boundary that owns the action.
"""

from __future__ import annotations

__all__: list[str] = []
