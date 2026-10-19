"""Fetch assignments from Microsoft Education Assignments API.

The Teams web client uses assignments.edu.cloud.microsoft — a separate
service from Graph API. The Graph token (aud: graph.microsoft.com) works
against this service despite the Education Graph API requiring EduAssignments
scopes that the Teams web client token doesn't have.

Endpoints:
  /api/v1.0/edu/classes/{classId}/assignments            — per-class assignments
  /api/v1.0/edu/classes/{classId}/assignments/{id}/resources — attached files
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ingestion.adapters.teams.endpoints import (
    edu_class_assignment_resources,
    edu_class_assignments,
)
from ingestion.exceptions import InsufficientScopeError, PlatformUnavailableError

if TYPE_CHECKING:
    from ingestion.clients.http import AsyncHTTPClient

logger = logging.getLogger(__name__)

_DEFAULT_ASSIGNMENTS_BASE = "https://assignments.edu.cloud.microsoft"


async def fetch_class_assignments(
    client: AsyncHTTPClient,
    class_id: str,
    *,
    base_url: str = _DEFAULT_ASSIGNMENTS_BASE,
) -> list[dict]:
    """Fetch all assignments for a class (team).

    Returns raw assignment objects with displayName, dueDateTime, instructions, etc.
    Returns [] on 403 (class may not have assignments enabled).
    Raises on auth errors and unexpected failures so callers can record them.
    """
    url = edu_class_assignments(class_id, base_url=base_url)
    try:
        return await client.get_paginated(url)
    except InsufficientScopeError:
        logger.debug("Assignments not accessible for class %s (403).", class_id)
        return []
    except PlatformUnavailableError:
        logger.warning("Assignments API unavailable for class %s.", class_id)
        return []


async def fetch_assignment_resources(
    client: AsyncHTTPClient,
    class_id: str,
    assignment_id: str,
    *,
    base_url: str = _DEFAULT_ASSIGNMENTS_BASE,
) -> list[dict]:
    """Fetch resources (attached files) for an assignment.

    Returns raw resource objects with resource.displayName, resource.fileUrl, etc.
    Returns [] on 403/5xx. Raises on auth errors and unexpected failures.
    """
    url = edu_class_assignment_resources(class_id, assignment_id, base_url=base_url)
    try:
        return await client.get_paginated(url)
    except InsufficientScopeError:
        logger.debug(
            "Resources not accessible for assignment %s/%s (403).",
            class_id,
            assignment_id,
        )
        return []
    except PlatformUnavailableError:
        logger.warning(
            "Resources API unavailable for assignment %s/%s.",
            class_id,
            assignment_id,
        )
        return []
