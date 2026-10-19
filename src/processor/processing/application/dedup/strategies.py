from __future__ import annotations

from processing.models.dedup import (
    AnnouncementComparison,
    AssignmentComparison,
    CandidateMatch,
    EventComparison,
    MaterialComparison,
)
from processing.models.entities import Announcement, Assignment, Event, Material


class WeightedMatchStrategy:
    """Weighted matcher using typed comparison inputs per entity."""

    def evaluate_assignment(
        self,
        item: Assignment,
        comparisons: list[AssignmentComparison],
    ) -> list[CandidateMatch]:
        del item
        return self._collect_matches(
            comparisons,
            threshold=0.85,
            score_fn=lambda comparison: (
                0.6 * (comparison.title_similarity or 0.0)
                + 0.4 * (comparison.deadline_similarity or 0.0)
            ),
        )

    def evaluate_announcement(
        self,
        item: Announcement,
        comparisons: list[AnnouncementComparison],
    ) -> list[CandidateMatch]:
        del item
        return self._collect_matches(
            comparisons,
            threshold=0.82,
            score_fn=lambda comparison: (
                0.25 * (comparison.title_similarity or 0.0)
                + 0.4 * (comparison.body_similarity or 0.0)
                + 0.35 * (comparison.published_at_similarity or 0.0)
            ),
        )

    def evaluate_event(
        self,
        item: Event,
        comparisons: list[EventComparison],
    ) -> list[CandidateMatch]:
        del item
        return self._collect_matches(
            comparisons,
            threshold=0.85,
            score_fn=lambda comparison: (
                0.55 * (comparison.title_similarity or 0.0)
                + 0.45 * (comparison.start_time_similarity or 0.0)
            ),
        )

    def evaluate_material(
        self,
        item: Material,
        comparisons: list[MaterialComparison],
    ) -> list[CandidateMatch]:
        del item
        return self._collect_matches(
            comparisons,
            threshold=0.88,
            score_fn=lambda comparison: (
                0.7 * (comparison.title_similarity or 0.0)
                + 0.3 * (comparison.body_similarity or 0.0)
            ),
        )

    def _collect_matches(
        self,
        comparisons,
        *,
        threshold: float,
        score_fn,
    ) -> list[CandidateMatch]:
        matches: list[CandidateMatch] = []
        for comparison in comparisons:
            score = score_fn(comparison)
            if score < threshold:
                continue
            matches.append(
                CandidateMatch(
                    canonical_id=comparison.candidate.canonical_id,
                    candidate=comparison.candidate,
                    score=score,
                    matched_by="weighted",
                )
            )
        return matches
