"""URL builders for Microsoft Graph API and Teams CSA API endpoints."""

from __future__ import annotations

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_CSA_BASE = "https://teams.cloud.microsoft/api/csa/emea/api/v2"


# --- Graph API ---


def graph_me(*, base_url: str = _GRAPH_BASE) -> str:
    return f"{base_url}/me"


def graph_team_channels(team_id: str, *, base_url: str = _GRAPH_BASE) -> str:
    return f"{base_url}/teams/{team_id}/channels"


def graph_channel_messages(team_id: str, channel_id: str, *, base_url: str = _GRAPH_BASE) -> str:
    return f"{base_url}/teams/{team_id}/channels/{channel_id}/messages"


def graph_team_drives(team_id: str, *, base_url: str = _GRAPH_BASE) -> str:
    return f"{base_url}/groups/{team_id}/drives"


def graph_drive_root_children(drive_id: str, *, base_url: str = _GRAPH_BASE) -> str:
    return f"{base_url}/drives/{drive_id}/root/children"


def graph_drive_item_children(drive_id: str, item_id: str, *, base_url: str = _GRAPH_BASE) -> str:
    return f"{base_url}/drives/{drive_id}/items/{item_id}/children"


def graph_drive_delta(drive_id: str, *, base_url: str = _GRAPH_BASE) -> str:
    return f"{base_url}/drives/{drive_id}/root/delta"


# --- Teams CSA API ---


def csa_channel_messages(thread_id: str, channel_id: str, *, base_url: str = _CSA_BASE) -> str:
    return f"{base_url}/teams/{thread_id}/channels/{channel_id}"


# --- Microsoft Education Assignments API ---

_EDU_BASE = "https://assignments.edu.cloud.microsoft"


def edu_class_assignments(class_id: str, *, base_url: str = _EDU_BASE) -> str:
    return f"{base_url}/api/v1.0/edu/classes/{class_id}/assignments"


def edu_class_assignment_resources(
    class_id: str,
    assignment_id: str,
    *,
    base_url: str = _EDU_BASE,
) -> str:
    return f"{base_url}/api/v1.0/edu/classes/{class_id}/assignments/{assignment_id}/resources"
