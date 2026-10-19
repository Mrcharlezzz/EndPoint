"""Per-test Postgres state reset.

Truncates every table in the ``public`` schema except Django bookkeeping
tables. Protection is twofold:

1. An enumerated ``_PROTECTED_TABLES`` set lists today's known contrib
   tables for documentation and explicit opt-out.
2. A prefix filter also excludes any table named ``django_*`` or
   ``auth_*``, so future Django contrib apps (e.g. ``django.contrib.sites``,
   DRF's ``authtoken``) stay safe without an explicit update here.

This is cheaper than rebuilding the schema and avoids migration replay
on each test.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Enumerated defaults of Django bookkeeping tables. These MUST NOT be truncated
# between tests. The list is kept for documentation; the actual filter below
# also excludes any table matching django_* / auth_* so that future Django
# contrib apps (django.contrib.sites, DRF authtoken, etc.) stay safe without
# an explicit update here.
_PROTECTED_TABLES: frozenset[str] = frozenset(
    {
        "django_migrations",
        "django_content_type",
        "django_admin_log",
        "django_session",
        "django_site",
        "auth_permission",
        "auth_group",
        "auth_group_permissions",
        "auth_user",
        "auth_user_groups",
        "auth_user_user_permissions",
    }
)

_PROTECTED_PREFIXES: tuple[str, ...] = ("django_", "auth_")


def _is_protected(name: str, protected_set: frozenset[str]) -> bool:
    if name in protected_set:
        return True
    return name.startswith(_PROTECTED_PREFIXES)


def _list_tables(cursor: Any) -> list[str]:
    cursor.execute(
        """
        SELECT tablename
          FROM pg_tables
         WHERE schemaname = 'public'
        """
    )
    return [row[0] for row in cursor.fetchall()]


def truncate_all_except(connection: Any, protected: Iterable[str] = _PROTECTED_TABLES) -> None:
    """Truncate every public-schema table except Django bookkeeping tables.

    Protection covers:
    - The enumerated ``protected`` set (defaults to ``_PROTECTED_TABLES``).
    - Any table whose name starts with ``django_`` or ``auth_``.
    """
    protected_set = frozenset(protected)
    with connection.cursor() as cursor:
        tables = [t for t in _list_tables(cursor) if not _is_protected(t, protected_set)]
        if not tables:
            return
        quoted = ", ".join(f'"{t}"' for t in tables)
        cursor.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
    connection.commit()
