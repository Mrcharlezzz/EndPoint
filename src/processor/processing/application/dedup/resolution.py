from __future__ import annotations

from processing.models.dedup import CandidateMatch, ResolutionAction


class FirstSeenResolutionStrategy:
    def resolve(
        self, incoming_platform: str, matches: list[CandidateMatch]
    ) -> ResolutionAction:
        del incoming_platform
        if not matches:
            return ResolutionAction(kind="create_new")
        return ResolutionAction(kind="attach", canonical_id=matches[0].canonical_id)


class PlatformPriorityResolutionStrategy:
    def __init__(self, platform_priority: list[str]):
        self.priority_index = {
            platform.casefold(): index
            for index, platform in enumerate(platform_priority)
        }
        self.default_rank = len(platform_priority)

    def resolve(
        self, incoming_platform: str, matches: list[CandidateMatch]
    ) -> ResolutionAction:
        if not matches:
            return ResolutionAction(kind="create_new")

        best_match = min(
            matches,
            key=lambda match: (
                self._rank(match.candidate.platform),
                -match.score,
            ),
        )
        if self._rank(incoming_platform) < self._rank(best_match.candidate.platform):
            return ResolutionAction(
                kind="promote",
                canonical_id=best_match.canonical_id,
            )
        return ResolutionAction(kind="attach", canonical_id=best_match.canonical_id)

    def _rank(self, platform: str | None) -> int:
        return self.priority_index.get((platform or "").casefold(), self.default_rank)
