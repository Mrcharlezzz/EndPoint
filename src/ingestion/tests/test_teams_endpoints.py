"""Tests for Teams adapter URL builders."""

from __future__ import annotations

from ingestion.adapters.teams.endpoints import (
    csa_channel_messages,
    graph_channel_messages,
    graph_drive_item_children,
    graph_drive_root_children,
    graph_me,
    graph_team_channels,
    graph_team_drives,
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CSA_BASE = "https://teams.cloud.microsoft/api/csa/emea/api/v2"


def test_graph_me() -> None:
    assert graph_me() == f"{GRAPH_BASE}/me"


def test_graph_team_channels() -> None:
    url = graph_team_channels("team-123")
    assert url == f"{GRAPH_BASE}/teams/team-123/channels"


def test_graph_channel_messages() -> None:
    url = graph_channel_messages("team-123", "chan-456")
    assert url == f"{GRAPH_BASE}/teams/team-123/channels/chan-456/messages"


def test_graph_team_drives() -> None:
    url = graph_team_drives("team-123")
    assert url == f"{GRAPH_BASE}/groups/team-123/drives"


def test_graph_drive_root_children() -> None:
    url = graph_drive_root_children("drive-abc")
    assert url == f"{GRAPH_BASE}/drives/drive-abc/root/children"


def test_graph_drive_item_children() -> None:
    url = graph_drive_item_children("drive-abc", "folder-xyz")
    assert url == f"{GRAPH_BASE}/drives/drive-abc/items/folder-xyz/children"


def test_csa_channel_messages_default_base() -> None:
    url = csa_channel_messages("thread-111", "chan-222")
    assert url == f"{CSA_BASE}/teams/thread-111/channels/chan-222"


def test_csa_channel_messages_custom_base() -> None:
    custom = "https://custom.csa.api/v2"
    url = csa_channel_messages("t1", "c1", base_url=custom)
    assert url == f"{custom}/teams/t1/channels/c1"


def test_graph_me_custom_base() -> None:
    url = graph_me(base_url="https://graph.example.com/v1.0")
    assert url == "https://graph.example.com/v1.0/me"
