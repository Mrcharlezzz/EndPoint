from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from processing.application.dedup.comparisons import (
    build_announcement_comparison,
    build_assignment_comparison,
)
from processing.application.dedup.strategies import WeightedMatchStrategy
from processing.models.dedup import CanonicalCandidate
from processing.models.entities import Announcement, Assignment


def test_weighted_match_strategy_links_assignment_candidate():
    strategy = WeightedMatchStrategy()
    item = Assignment(
        title="Homework 1",
        deadline=datetime(2025, 9, 9, 22, 2, tzinfo=timezone.utc),
    )
    candidate = CanonicalCandidate(
        canonical_id=uuid4(),
        entity=Assignment(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        ),
        platform="moodle",
        persisted_primary_item_id=uuid4(),
    )

    matches = strategy.evaluate_assignment(
        item,
        [build_assignment_comparison(item, candidate)],
    )

    assert len(matches) == 1
    assert matches[0].canonical_id == candidate.canonical_id
    assert matches[0].score >= 0.85


def test_weighted_match_strategy_rejects_bad_announcement_match():
    strategy = WeightedMatchStrategy()
    item = Announcement(
        title="Updates",
        body="Last seminar is next week and homework moved to classroom.",
        published_at=datetime(2025, 11, 27, 6, 47, 38, tzinfo=timezone.utc),
    )
    candidate = CanonicalCandidate(
        canonical_id=uuid4(),
        entity=Announcement(
            title="Updates",
            body="Quizzes now count toward practical grade and hw02 was extended.",
            published_at=datetime(2025, 9, 24, 13, 16, 35, tzinfo=timezone.utc),
        ),
        platform="telegram",
        persisted_primary_item_id=uuid4(),
    )

    matches = strategy.evaluate_announcement(
        item,
        [build_announcement_comparison(item, candidate)],
    )

    assert matches == []
