from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import core.bootstrap

from processing.application.dedup import (
    DedupService,
    FirstSeenResolutionStrategy,
    WeightedMatchStrategy,
)
from processing.adapters.llm.classifier import LLMClassifier
from processing.adapters.postgres.repository import PostgresEntityRepository
from processing.config import ProcessingConfig
from processing.pipeline import ProcessingPipeline

logger = logging.getLogger(__name__)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event
) -> None:
    def _signal(sig: signal.Signals) -> None:
        logger.info("processor signal received: %s", sig.name)
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal, sig)


async def _main_async() -> None:
    shutdown = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), shutdown)

    config = ProcessingConfig()
    if not config.llm_api_key:
        logger.warning(
            "llm_api_key is not set — LLM calls will fail with an auth error"
        )
    repository = PostgresEntityRepository()
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
    await pipeline.run_until_shutdown(shutdown)


def main() -> None:
    core.bootstrap.init()
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
