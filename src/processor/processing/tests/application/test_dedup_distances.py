from __future__ import annotations

from datetime import datetime, timedelta, timezone

from processing.application.dedup.distances import (
    body_similarity,
    datetime_similarity,
    text_similarity,
)


def test_text_similarity_normalizes_case_and_whitespace():
    score = text_similarity(" Homework   1 ", "homework 1")

    assert score == 1.0


def test_body_similarity_handles_partial_overlap():
    score = body_similarity(
        "Homework 2 deadline was extended and quiz grading was updated.",
        "Homework 2 deadline is extended and quiz grading has been updated.",
    )

    assert score > 0.8


def test_datetime_similarity_uses_linear_decay():
    base = datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc)

    assert datetime_similarity(base, base, max_delta_seconds=3600) == 1.0
    assert (
        datetime_similarity(
            base,
            base + timedelta(minutes=30),
            max_delta_seconds=3600,
        )
        == 0.5
    )
    assert (
        datetime_similarity(
            base,
            base + timedelta(hours=1),
            max_delta_seconds=3600,
        )
        == 0.0
    )
