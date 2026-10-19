"""Typed credentials for the Teams adapter."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class TeamsCredentials(TypedDict):
    graph_token: str
    csa_token: NotRequired[str]
    team_ids: list[str]
    refresh_url: NotRequired[str]
