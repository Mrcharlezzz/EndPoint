"""Plain factory functions for e2e seed data.

Each factory returns a dict or ORM instance ready to hand to Django's
``.objects.create(**kwargs)`` or to insert via raw SQL. Factories stay flat
— no inheritance, no builder pattern — so a test that needs a specific
shape overrides whichever kwargs it cares about.

Populated as PR-3+ needs land.
"""

from __future__ import annotations


def make_credential(
    email: str,
    platform: str,
    **overrides: object,
) -> dict[str, object]:
    """Placeholder. Full implementation lands in PR-2."""
    return {"email": email, "platform": platform, **overrides}
