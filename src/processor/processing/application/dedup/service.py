from __future__ import annotations

import logging
from typing import TypeAlias, cast
from uuid import UUID, uuid4

from processing.application.dedup.comparisons import (
    build_announcement_comparison,
    build_assignment_comparison,
    build_event_comparison,
    build_material_comparison,
)
from processing.models.dedup import (
    AnnouncementComparison,
    AssignmentComparison,
    CandidateMatch,
    CanonicalCandidate,
    CanonicalState,
    DedupDecision,
    DeduplicatedEntity,
    DedupItemStatus,
    EventComparison,
    MaterialComparison,
    ResolutionAction,
)
from processing.models.envelope import ProcessingEnvelope
from processing.models.entities import Announcement, Assignment, Event, Material
from processing.ports.dedup import MatchStrategy, ResolutionStrategy
from processing.ports.repository import EntityRepositoryPort

logger = logging.getLogger(__name__)

SUPPORTED_ENTITY_TYPES = {"assignment", "announcement", "event", "material"}
EntityComparison: TypeAlias = (
    AssignmentComparison | AnnouncementComparison | EventComparison | MaterialComparison
)


class DedupService:
    """Deduplicate processed envelopes by mutating canonical state per batch group."""

    def __init__(
        self,
        repository: EntityRepositoryPort,
        match_strategy: MatchStrategy,
        resolution_strategy: ResolutionStrategy,
    ) -> None:
        self.repository = repository
        self.match_strategy = match_strategy
        self.resolution_strategy = resolution_strategy

    def deduplicate_batch(
        self, envelopes: list[ProcessingEnvelope]
    ) -> list[ProcessingEnvelope]:
        updated = list(envelopes)
        grouped = self._group_envelopes(envelopes, updated)

        for (course_id, entity_type), entries in grouped.items():
            try:
                candidates = self.repository.get_existing_canonicals(
                    course_id, entity_type
                )
            except Exception as exc:
                logger.exception(
                    "Dedup candidate load failed for course=%s entity_type=%s: %s",
                    course_id,
                    entity_type,
                    exc,
                )
                for index, envelope in entries:
                    updated[index] = envelope.model_copy(
                        update={
                            "dedup_status": "failed",
                            "dedup_decision": None,
                        }
                    )
                continue

            self._deduplicate_group(entries, candidates, updated)

        return updated

    def _group_envelopes(
        self,
        envelopes: list[ProcessingEnvelope],
        updated: list[ProcessingEnvelope],
    ) -> dict[tuple[UUID, str], list[tuple[int, ProcessingEnvelope]]]:
        grouped: dict[tuple[UUID, str], list[tuple[int, ProcessingEnvelope]]] = {}
        for index, envelope in enumerate(envelopes):
            course_id = envelope.resolved_course_id
            entity_type = envelope.entity.entity_type
            if course_id is None or entity_type not in SUPPORTED_ENTITY_TYPES:
                updated[index] = envelope.model_copy(
                    update={
                        "dedup_status": "skipped",
                        "dedup_decision": None,
                    }
                )
                continue
            grouped.setdefault((course_id, entity_type), []).append((index, envelope))
        return grouped

    def _deduplicate_group(
        self,
        entries: list[tuple[int, ProcessingEnvelope]],
        candidates: list[CanonicalCandidate],
        updated: list[ProcessingEnvelope],
    ) -> None:
        states = {
            candidate.canonical_id: CanonicalState(
                canonical_id=candidate.canonical_id,
                representative=candidate.entity,
                representative_platform=candidate.platform,
                persisted_primary_item_id=candidate.persisted_primary_item_id,
            )
            for candidate in candidates
        }
        item_assignments: dict[int, UUID] = {}
        item_scores: dict[int, float] = {}
        item_matched_by: dict[int, str] = {}

        for index, envelope in entries:
            try:
                comparisons = self._build_comparisons(envelope, states)
                matches = self._evaluate_matches(envelope, comparisons)
                action = self.resolution_strategy.resolve(
                    envelope.ingested_item.platform, matches
                )
                self._apply_action(
                    action=action,
                    envelope=envelope,
                    batch_index=index,
                    states=states,
                    item_assignments=item_assignments,
                )
                item_scores[index] = matches[0].score if matches else 0.0
                item_matched_by[index] = (
                    matches[0].matched_by if matches else "no_match"
                )
            except Exception as exc:
                logger.exception(
                    "Dedup evaluation failed for ingested item %s: %s",
                    envelope.ingested_item.id,
                    exc,
                )
                updated[index] = envelope.model_copy(
                    update={
                        "dedup_status": "failed",
                        "dedup_decision": None,
                    }
                )

        self._finalize_group(
            entries=entries,
            states=states,
            item_assignments=item_assignments,
            item_scores=item_scores,
            item_matched_by=item_matched_by,
            updated=updated,
        )

    def _build_comparisons(
        self,
        envelope: ProcessingEnvelope,
        states: dict[UUID, CanonicalState],
    ) -> list[EntityComparison]:
        entity = envelope.entity
        if isinstance(entity, Assignment):
            return cast(
                list[EntityComparison],
                self._build_assignment_comparisons(entity, states),
            )
        if isinstance(entity, Announcement):
            return cast(
                list[EntityComparison],
                self._build_announcement_comparisons(entity, states),
            )
        if isinstance(entity, Event):
            return cast(
                list[EntityComparison], self._build_event_comparisons(entity, states)
            )
        if isinstance(entity, Material):
            return cast(
                list[EntityComparison],
                self._build_material_comparisons(entity, states),
            )
        return []

    def _build_assignment_comparisons(
        self,
        entity: Assignment,
        states: dict[UUID, CanonicalState],
    ) -> list[AssignmentComparison]:
        comparisons: list[AssignmentComparison] = []
        for state in states.values():
            if not isinstance(state.representative, Assignment):
                raise RuntimeError(
                    "Assignment dedup group contains non-assignment state"
                )
            comparisons.append(
                build_assignment_comparison(entity, self._candidate_from_state(state))
            )
        return comparisons

    def _build_announcement_comparisons(
        self,
        entity: Announcement,
        states: dict[UUID, CanonicalState],
    ) -> list[AnnouncementComparison]:
        comparisons: list[AnnouncementComparison] = []
        for state in states.values():
            if not isinstance(state.representative, Announcement):
                raise RuntimeError(
                    "Announcement dedup group contains non-announcement state"
                )
            comparisons.append(
                build_announcement_comparison(entity, self._candidate_from_state(state))
            )
        return comparisons

    def _build_event_comparisons(
        self,
        entity: Event,
        states: dict[UUID, CanonicalState],
    ) -> list[EventComparison]:
        comparisons: list[EventComparison] = []
        for state in states.values():
            if not isinstance(state.representative, Event):
                raise RuntimeError("Event dedup group contains non-event state")
            comparisons.append(
                build_event_comparison(entity, self._candidate_from_state(state))
            )
        return comparisons

    def _build_material_comparisons(
        self,
        entity: Material,
        states: dict[UUID, CanonicalState],
    ) -> list[MaterialComparison]:
        comparisons: list[MaterialComparison] = []
        for state in states.values():
            if not isinstance(state.representative, Material):
                raise RuntimeError("Material dedup group contains non-material state")
            comparisons.append(
                build_material_comparison(entity, self._candidate_from_state(state))
            )
        return comparisons

    def _candidate_from_state(self, state: CanonicalState) -> CanonicalCandidate:
        return CanonicalCandidate(
            canonical_id=state.canonical_id,
            entity=state.representative,
            platform=state.representative_platform,
            persisted_primary_item_id=state.persisted_primary_item_id,
        )

    def _deduplicated_entity(self, envelope: ProcessingEnvelope) -> DeduplicatedEntity:
        entity = envelope.entity
        if not isinstance(entity, (Assignment, Announcement, Event, Material)):
            raise RuntimeError("Unsupported entity type in dedup state mutation")
        return entity

    def _evaluate_matches(
        self,
        envelope: ProcessingEnvelope,
        comparisons: list[EntityComparison],
    ) -> list[CandidateMatch]:
        entity = envelope.entity
        if isinstance(entity, Assignment):
            return self.match_strategy.evaluate_assignment(
                entity, cast(list[AssignmentComparison], comparisons)
            )
        if isinstance(entity, Announcement):
            return self.match_strategy.evaluate_announcement(
                entity, cast(list[AnnouncementComparison], comparisons)
            )
        if isinstance(entity, Event):
            return self.match_strategy.evaluate_event(
                entity, cast(list[EventComparison], comparisons)
            )
        if isinstance(entity, Material):
            return self.match_strategy.evaluate_material(
                entity, cast(list[MaterialComparison], comparisons)
            )
        return []

    def _apply_action(
        self,
        *,
        action: ResolutionAction,
        envelope: ProcessingEnvelope,
        batch_index: int,
        states: dict[UUID, CanonicalState],
        item_assignments: dict[int, UUID],
    ) -> None:
        entity = self._deduplicated_entity(envelope)
        if action.kind == "create_new":
            canonical_id = uuid4()
            states[canonical_id] = CanonicalState(
                canonical_id=canonical_id,
                representative=entity,
                representative_platform=envelope.ingested_item.platform,
                persisted_primary_item_id=None,
                pending_primary_batch_index=batch_index,
                member_batch_indexes=[batch_index],
            )
            item_assignments[batch_index] = canonical_id
            return

        if action.kind == "skip":
            return
        if action.kind == "fail":
            raise RuntimeError(action.reason or "Dedup resolution failed")

        canonical_id = action.canonical_id
        if canonical_id is None:
            raise RuntimeError(f"{action.kind} action missing canonical_id")
        state = states.get(canonical_id)
        if state is None:
            raise RuntimeError(f"Unknown canonical_id for {action.kind} action")
        if batch_index not in state.member_batch_indexes:
            state.member_batch_indexes.append(batch_index)
        item_assignments[batch_index] = canonical_id
        if action.kind == "promote":
            state.representative = entity
            state.representative_platform = envelope.ingested_item.platform
            state.pending_primary_batch_index = batch_index

    def _finalize_group(
        self,
        *,
        entries: list[tuple[int, ProcessingEnvelope]],
        states: dict[UUID, CanonicalState],
        item_assignments: dict[int, UUID],
        item_scores: dict[int, float],
        item_matched_by: dict[int, str],
        updated: list[ProcessingEnvelope],
    ) -> None:
        item_statuses: dict[int, DedupItemStatus] = {}
        seen_member_indexes: set[int] = set()
        for state in states.values():
            primary_index = state.pending_primary_batch_index
            for member_index in state.member_batch_indexes:
                if member_index in seen_member_indexes:
                    raise RuntimeError(
                        "Dedup invariant violated: batch item belongs to multiple canonical states"
                    )
                seen_member_indexes.add(member_index)
                item_statuses[member_index] = "linked"
            if primary_index is not None:
                item_statuses[primary_index] = (
                    "primary" if state.persisted_primary_item_id is None else "promoted"
                )

        state_by_canonical = {state.canonical_id: state for state in states.values()}
        for index, envelope in entries:
            if updated[index].dedup_status == "failed":
                continue
            canonical_id = item_assignments.get(index)
            status = item_statuses.get(index, "skipped")
            if canonical_id is None or status == "skipped":
                updated[index] = envelope.model_copy(
                    update={"dedup_status": "skipped", "dedup_decision": None}
                )
                continue

            state = state_by_canonical[canonical_id]
            decision_type = (
                "new_canonical"
                if status == "primary" and state.persisted_primary_item_id is None
                else "link_existing"
            )
            updated[index] = envelope.model_copy(
                update={
                    "dedup_status": status,
                    "dedup_decision": DedupDecision(
                        decision=decision_type,
                        canonical_entity_id=canonical_id,
                        matched_by=item_matched_by.get(index, "no_match"),
                        score=item_scores.get(index, 0.0),
                        match_strategy=type(self.match_strategy).__name__,
                        resolution_strategy=type(self.resolution_strategy).__name__,
                    ),
                }
            )
