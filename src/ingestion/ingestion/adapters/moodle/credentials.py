"""Typed credentials for the Moodle adapter."""

from __future__ import annotations

from typing import TypedDict


class MoodleCredentials(TypedDict):
    token: str
    user_id: int
