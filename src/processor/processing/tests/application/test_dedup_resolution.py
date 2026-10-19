from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from processing.application.dedup.resolution import (
    FirstSeenResolutionStrategy,
    PlatformPriorityResolutionStrategy,
)
from processing.models.dedup import CandidateMatch, CanonicalCandidate
from processing.models.entities import Assignment


def _match(*, platform: str, score: float = 0.9) -> CandidateMatch:
    candidate = CanonicalCandidate(
        canonical_id=uuid4(),
        entity=Assignment(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        ),
        platform=platform,
        persisted_primary_item_id=uuid4(),
    )
    return CandidateMatch(
        canonical_id=candidate.canonical_id,
        candidate=candidate,
        score=score,
        matched_by="weighted",
    )


def test_first_seen_resolution_creates_when_no_matches():
    strategy = FirstSeenResolutionStrategy()

    action = strategy.resolve(incoming_platform="telegram", matches=[])

    assert action.kind == "create_new"
    assert action.canonical_id is None


def test_first_seen_resolution_attaches_to_first_match():
    first = _match(platform="teams", score=0.85)
    second = _match(platform="moodle", score=0.95)
    strategy = FirstSeenResolutionStrategy()

    action = strategy.resolve(incoming_platform="telegram", matches=[first, second])

    assert action.kind == "attach"
    assert action.canonical_id == first.canonical_id


def test_platform_priority_promotes_when_incoming_has_higher_priority():
    match = _match(platform="teams")
    strategy = PlatformPriorityResolutionStrategy(["moodle", "teams", "telegram"])

    action = strategy.resolve(incoming_platform="moodle", matches=[match])

    assert action.kind == "promote"
    assert action.canonical_id == match.canonical_id


def test_platform_priority_attaches_when_candidate_has_higher_priority():
    match = _match(platform="moodle")
    strategy = PlatformPriorityResolutionStrategy(["moodle", "teams", "telegram"])

    action = strategy.resolve(incoming_platform="telegram", matches=[match])

    assert action.kind == "attach"
    assert action.canonical_id == match.canonical_id
