from processing.application.dedup.comparisons import (
    build_announcement_comparison,
    build_assignment_comparison,
    build_event_comparison,
    build_material_comparison,
)
from processing.application.dedup.distances import (
    body_similarity,
    datetime_similarity,
    text_similarity,
)
from processing.application.dedup.resolution import (
    FirstSeenResolutionStrategy,
    PlatformPriorityResolutionStrategy,
)
from processing.application.dedup.strategies import WeightedMatchStrategy
from processing.application.dedup.service import DedupService

__all__ = [
    "body_similarity",
    "build_announcement_comparison",
    "build_assignment_comparison",
    "build_event_comparison",
    "build_material_comparison",
    "datetime_similarity",
    "FirstSeenResolutionStrategy",
    "PlatformPriorityResolutionStrategy",
    "WeightedMatchStrategy",
    "DedupService",
    "text_similarity",
]
