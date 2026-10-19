"""Typed builders for the per-platform credential blobs.

The shapes mirror ``ingestion/auth/types.py`` so the daemon's refresh loop
can read what we write.
"""

from __future__ import annotations

from typing import Any, TypedDict


class TeamsBlob(TypedDict, total=False):
    graph_token: str
    csa_token: str
    refresh_token: str
    cookies: list[dict[str, Any]]
    team_ids: list[str]


class MoodleBlob(TypedDict, total=False):
    token: str
    user_id: int


def build_teams_blob(payload: dict[str, Any]) -> TeamsBlob:
    """Project a headless internal /payload response into a Teams blob."""
    tokens = payload.get("tokens") or {}
    blob: TeamsBlob = {}
    if isinstance(tokens.get("graph_token"), str):
        blob["graph_token"] = tokens["graph_token"]
    if isinstance(tokens.get("csa_token"), str):
        blob["csa_token"] = tokens["csa_token"]
    rt = payload.get("refresh_token")
    if isinstance(rt, str) and rt:
        blob["refresh_token"] = rt
    cookies = payload.get("cookies")
    if isinstance(cookies, list):
        blob["cookies"] = cookies
    team_ids = payload.get("team_ids")
    if isinstance(team_ids, list):
        blob["team_ids"] = [str(t) for t in team_ids]
    return blob


def build_moodle_blob(token: str, user_id: int) -> MoodleBlob:
    return {"token": token, "user_id": user_id}
