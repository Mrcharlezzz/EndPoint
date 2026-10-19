from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from processing.application.dedup.comparisons import (
    build_announcement_comparison,
    build_assignment_comparison,
)
from processing.models.dedup import CanonicalCandidate
from processing.models.entities import Announcement, Assignment


def test_build_assignment_comparison():
    item = Assignment(
        title="Homework 1",
        deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
    )
    candidate = CanonicalCandidate(
        canonical_id=uuid4(),
        entity=Assignment(
            title=" Homework   1 ",
            deadline=datetime(2025, 9, 9, 22, 2, tzinfo=timezone.utc),
        ),
        platform="moodle",
        persisted_primary_item_id=uuid4(),
    )

    comparison = build_assignment_comparison(item, candidate)

    assert comparison.candidate.canonical_id == candidate.canonical_id
    assert comparison.title_similarity == 1.0
    assert comparison.deadline_similarity is not None
    assert comparison.deadline_similarity > 0.9


def test_build_announcement_comparison():
    item = Announcement(
        title="Course updates",
        body="Homework 2 deadline is extended and quiz grading has been updated.",
        published_at=datetime(2025, 9, 24, 13, 28, tzinfo=timezone.utc),
    )
    candidate = CanonicalCandidate(
        canonical_id=uuid4(),
        entity=Announcement(
            title="Updates",
            body="Homework 2 deadline was extended and quiz grading was updated.",
            published_at=datetime(2025, 9, 24, 13, 16, 35, tzinfo=timezone.utc),
        ),
        platform="telegram",
        persisted_primary_item_id=uuid4(),
    )

    comparison = build_announcement_comparison(item, candidate)

    assert comparison.candidate.canonical_id == candidate.canonical_id
    assert comparison.title_similarity is not None
    assert comparison.body_similarity is not None
    assert comparison.published_at_similarity is not None
    assert comparison.title_similarity > 0.6
    assert comparison.body_similarity > 0.8
    assert comparison.published_at_similarity > 0.9
