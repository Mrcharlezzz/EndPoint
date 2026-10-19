from __future__ import annotations

from datetime import datetime
import re

from rapidfuzz import fuzz


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def text_similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    return fuzz.token_sort_ratio(_normalize(left), _normalize(right)) / 100.0


def body_similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    return fuzz.token_set_ratio(_normalize(left), _normalize(right)) / 100.0


def datetime_similarity(
    left: datetime | None,
    right: datetime | None,
    *,
    max_delta_seconds: float,
) -> float:
    if left is None or right is None:
        return 0.0
    delta_seconds = abs((left - right).total_seconds())
    if delta_seconds >= max_delta_seconds:
        return 0.0
    return 1.0 - (delta_seconds / max_delta_seconds)
