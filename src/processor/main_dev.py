from __future__ import annotations

import logging
from datetime import UTC, datetime

import core.bootstrap

from processing.application.dedup import (
    DedupService,
    FirstSeenResolutionStrategy,
    WeightedMatchStrategy,
)
from processing.adapters.llm.classifier import LLMClassifier
from processing.adapters.postgres.dev_repository import DevPostgresEntityRepository
from processing.config import ProcessingConfig
from processing.pipeline import ProcessingPipeline

logger = logging.getLogger(__name__)


def main() -> None:
    core.bootstrap.init()

    service_start = datetime.now(UTC)
    config = ProcessingConfig()
    if not config.llm_api_key:
        logger.warning(
            "llm_api_key is not set — LLM calls will fail with an auth error"
        )
    repository = DevPostgresEntityRepository(service_start=service_start)
    classifier = LLMClassifier(config)
    dedup_service = None
    if config.dedup_enabled:
        dedup_service = DedupService(
            repository,
            WeightedMatchStrategy(),
            FirstSeenResolutionStrategy(),
        )
    pipeline = ProcessingPipeline(
        repository,
        classifier,
        config,
        dedup_service=dedup_service,
    )
    pipeline.run_loop()


if __name__ == "__main__":
    main()
