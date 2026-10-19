from __future__ import annotations

from typing import Protocol

from processing.models.dedup import (
    AnnouncementComparison,
    AssignmentComparison,
    CandidateMatch,
    EventComparison,
    MaterialComparison,
    ResolutionAction,
)
from processing.models.entities import Announcement, Assignment, Event, Material


class MatchStrategy(Protocol):
    def evaluate_assignment(
        self,
        item: Assignment,
        comparisons: list[AssignmentComparison],
    ) -> list[CandidateMatch]: ...

    def evaluate_announcement(
        self,
        item: Announcement,
        comparisons: list[AnnouncementComparison],
    ) -> list[CandidateMatch]: ...

    def evaluate_event(
        self,
        item: Event,
        comparisons: list[EventComparison],
    ) -> list[CandidateMatch]: ...

    def evaluate_material(
        self,
        item: Material,
        comparisons: list[MaterialComparison],
    ) -> list[CandidateMatch]: ...


class ResolutionStrategy(Protocol):
    def resolve(
        self, incoming_platform: str, matches: list[CandidateMatch]
    ) -> ResolutionAction: ...
