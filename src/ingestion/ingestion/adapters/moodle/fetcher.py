"""Moodle REST API fetch helpers — pure async functions over MoodleRestClient.

Each function calls one or more Moodle Web Service functions and returns
raw dicts for the normalizer to process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ingestion.adapters.moodle.client import MoodleRestClient


async def fetch_user_courses(client: MoodleRestClient, user_id: int) -> list[dict]:
    """Fetch all courses the user is enrolled in."""
    data = await client.call("core_enrol_get_users_courses", userid=user_id)
    if not isinstance(data, list):
        return []
    return data


async def fetch_assignments(
    client: MoodleRestClient,
    course_ids: list[int],
) -> list[dict]:
    """Fetch assignments across multiple courses.

    Returns a flat list of (assignment_dict, course_fullname, course_id) tuples
    packed as dicts with _course_name and _course_id injected.
    """
    if not course_ids:
        return []

    params: dict[str, object] = {}
    for i, cid in enumerate(course_ids):
        params[f"courseids[{i}]"] = cid

    data = await client.call("mod_assign_get_assignments", **params)
    if not isinstance(data, dict):
        return []

    results: list[dict] = []
    for course in data.get("courses", []):
        course_name = course.get("fullname", "")
        course_id = course.get("id", 0)
        for assignment in course.get("assignments", []):
            results.append(
                {
                    **assignment,
                    "_course_name": course_name,
                    "_course_id": course_id,
                }
            )
    return results


async def fetch_forums(
    client: MoodleRestClient,
    course_ids: list[int],
    forum_scope: str = "announcements",
) -> list[dict]:
    """Fetch forums across courses.

    Args:
        forum_scope: 'announcements' returns only news-type forums,
                     'all' returns every forum.
    """
    if not course_ids:
        return []

    params: dict[str, object] = {}
    for i, cid in enumerate(course_ids):
        params[f"courseids[{i}]"] = cid

    data = await client.call("mod_forum_get_forums_by_courses", **params)
    if not isinstance(data, list):
        return []

    if forum_scope == "announcements":
        return [f for f in data if f.get("type") == "news"]
    return data


async def fetch_forum_discussions(
    client: MoodleRestClient,
    forum_id: int,
    per_page: int = 50,
) -> list[dict]:
    """Fetch discussions from a single forum."""
    data = await client.call(
        "mod_forum_get_forum_discussions",
        forumid=forum_id,
        perpage=per_page,
        sortorder=3,  # sort by time modified descending
    )
    if not isinstance(data, dict):
        return []
    return data.get("discussions", [])


async def fetch_course_contents(
    client: MoodleRestClient,
    course_id: int,
) -> list[dict]:
    """Fetch all sections/modules/files for a course."""
    data = await client.call("core_course_get_contents", courseid=course_id)
    if not isinstance(data, list):
        return []
    return data


async def fetch_calendar_events(
    client: MoodleRestClient,
    time_from: int,
    per_page: int = 50,
    max_pages: int = 10,
) -> list[dict]:
    """Fetch action events from the calendar starting at time_from (unix timestamp).

    Paginates using aftereventid to collect all events, up to max_pages pages.
    """
    all_events: list[dict] = []
    after_event_id: int | None = None

    for _ in range(max_pages):
        params: dict[str, object] = {
            "timesortfrom": time_from,
            "limitnum": per_page,
        }
        if after_event_id is not None:
            params["aftereventid"] = after_event_id

        data = await client.call(
            "core_calendar_get_action_events_by_timesort",
            **params,
        )
        if not isinstance(data, dict):
            break

        events = data.get("events", [])
        if not events:
            break

        all_events.extend(events)

        # Stop if we got fewer than a full page (no more pages)
        if len(events) < per_page:
            break

        after_event_id = events[-1].get("id")
        if after_event_id is None:
            break

    return all_events


async def fetch_site_info(client: MoodleRestClient) -> dict:
    """Fetch site info (used for test_connection)."""
    data = await client.call("core_webservice_get_site_info")
    if not isinstance(data, dict):
        return {}
    return data
