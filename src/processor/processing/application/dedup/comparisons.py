from __future__ import annotations

from processing.application.dedup.distances import (
    body_similarity,
    datetime_similarity,
    text_similarity,
)
from processing.models.dedup import (
    AnnouncementComparison,
    AssignmentComparison,
    CanonicalCandidate,
    EventComparison,
    MaterialComparison,
)
from processing.models.entities import Announcement, Assignment, Event, Material


def build_assignment_comparison(
    item: Assignment, candidate: CanonicalCandidate
) -> AssignmentComparison:
    candidate_entity = _as_assignment(candidate)
    return AssignmentComparison(
        candidate=candidate,
        title_similarity=text_similarity(item.title, candidate_entity.title),
        deadline_similarity=datetime_similarity(
            item.deadline,
            candidate_entity.deadline,
            max_delta_seconds=86400,
        ),
    )


def build_announcement_comparison(
    item: Announcement, candidate: CanonicalCandidate
) -> AnnouncementComparison:
    candidate_entity = _as_announcement(candidate)
    return AnnouncementComparison(
        candidate=candidate,
        title_similarity=text_similarity(item.title, candidate_entity.title),
        body_similarity=body_similarity(item.body, candidate_entity.body),
        published_at_similarity=datetime_similarity(
            item.published_at,
            candidate_entity.published_at,
            max_delta_seconds=604800,
        ),
    )


def build_event_comparison(
    item: Event, candidate: CanonicalCandidate
) -> EventComparison:
    candidate_entity = _as_event(candidate)
    return EventComparison(
        candidate=candidate,
        title_similarity=text_similarity(item.title, candidate_entity.title),
        start_time_similarity=datetime_similarity(
            item.start_time,
            candidate_entity.start_time,
            max_delta_seconds=3600,
        ),
    )


def build_material_comparison(
    item: Material, candidate: CanonicalCandidate
) -> MaterialComparison:
    candidate_entity = _as_material(candidate)
    return MaterialComparison(
        candidate=candidate,
        title_similarity=text_similarity(item.title, candidate_entity.title),
        body_similarity=body_similarity(item.body, candidate_entity.body),
    )


def _as_assignment(candidate: CanonicalCandidate) -> Assignment:
    entity = candidate.entity
    if not isinstance(entity, Assignment):
        raise TypeError("Expected assignment canonical candidate")
    return entity


def _as_announcement(candidate: CanonicalCandidate) -> Announcement:
    entity = candidate.entity
    if not isinstance(entity, Announcement):
        raise TypeError("Expected announcement canonical candidate")
    return entity


def _as_event(candidate: CanonicalCandidate) -> Event:
    entity = candidate.entity
    if not isinstance(entity, Event):
        raise TypeError("Expected event canonical candidate")
    return entity


def _as_material(candidate: CanonicalCandidate) -> Material:
    entity = candidate.entity
    if not isinstance(entity, Material):
        raise TypeError("Expected material canonical candidate")
    return entity
