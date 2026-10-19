from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import main


def test_main_wires_dedup_service_when_enabled(monkeypatch):
    events: dict[str, object] = {}

    class FakeConfig:
        def __init__(self):
            self.llm_api_key = ""
            self.dedup_enabled = True

    class FakeRepository:
        pass

    class FakeClassifier:
        def __init__(self, config):
            events["classifier_config"] = config

    class FakePipeline:
        def __init__(self, repository, classifier, config, dedup_service=None):
            events["dedup_service"] = dedup_service

        async def run_until_shutdown(self, shutdown):
            events["run_loop"] = True
            events["shutdown_is_event"] = isinstance(shutdown, asyncio.Event)

    monkeypatch.setattr(
        main.core.bootstrap, "init", lambda: events.setdefault("init", True)
    )
    monkeypatch.setattr(main, "ProcessingConfig", FakeConfig)
    monkeypatch.setattr(main, "PostgresEntityRepository", FakeRepository)
    monkeypatch.setattr(main, "LLMClassifier", FakeClassifier)
    monkeypatch.setattr(main, "ProcessingPipeline", FakePipeline)
    monkeypatch.setattr(main, "WeightedMatchStrategy", lambda: "match_strategy")
    monkeypatch.setattr(
        main, "FirstSeenResolutionStrategy", lambda: "resolution_strategy"
    )
    monkeypatch.setattr(
        main,
        "DedupService",
        lambda repository, match_strategy, resolution_strategy: SimpleNamespace(
            repository=repository,
            match_strategy=match_strategy,
            resolution_strategy=resolution_strategy,
        ),
    )

    main.main()

    assert events["init"] is True
    assert events["run_loop"] is True
    assert events["shutdown_is_event"] is True
    dedup_service = events["dedup_service"]
    assert isinstance(dedup_service, SimpleNamespace)
    assert dedup_service.match_strategy == "match_strategy"
    assert dedup_service.resolution_strategy == "resolution_strategy"


def test_main_skips_dedup_service_when_disabled(monkeypatch):
    events: dict[str, object] = {}

    class FakeConfig:
        def __init__(self):
            self.llm_api_key = ""
            self.dedup_enabled = False

    class FakeRepository:
        pass

    class FakeClassifier:
        def __init__(self, config):
            events["classifier_config"] = config

    class FakePipeline:
        def __init__(self, repository, classifier, config, dedup_service=None):
            events["dedup_service"] = dedup_service

        async def run_until_shutdown(self, shutdown):
            events["run_loop"] = True
            events["shutdown_is_event"] = isinstance(shutdown, asyncio.Event)

    monkeypatch.setattr(
        main.core.bootstrap, "init", lambda: events.setdefault("init", True)
    )
    monkeypatch.setattr(main, "ProcessingConfig", FakeConfig)
    monkeypatch.setattr(main, "PostgresEntityRepository", FakeRepository)
    monkeypatch.setattr(main, "LLMClassifier", FakeClassifier)
    monkeypatch.setattr(main, "ProcessingPipeline", FakePipeline)

    main.main()

    assert events["init"] is True
    assert events["run_loop"] is True
    assert events["shutdown_is_event"] is True
    assert events["dedup_service"] is None


def test_main_dev_uses_service_start_for_dev_repository(monkeypatch):
    events: dict[str, object] = {}
    service_start = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)

    main_dev = importlib.import_module("main_dev")

    class FakeDatetime:
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return service_start

    class FakeConfig:
        def __init__(self):
            self.llm_api_key = ""
            self.dedup_enabled = True

    class FakeRepository:
        def __init__(self, *, service_start):
            events["service_start"] = service_start

    class FakeClassifier:
        def __init__(self, config):
            events["classifier_config"] = config

    class FakePipeline:
        def __init__(self, repository, classifier, config, dedup_service=None):
            events["repository"] = repository
            events["dedup_service"] = dedup_service

        def run_loop(self):
            events["run_loop"] = True

    monkeypatch.setattr(
        main_dev.core.bootstrap, "init", lambda: events.setdefault("init", True)
    )
    monkeypatch.setattr(main_dev, "datetime", FakeDatetime)
    monkeypatch.setattr(main_dev, "ProcessingConfig", FakeConfig)
    monkeypatch.setattr(main_dev, "DevPostgresEntityRepository", FakeRepository)
    monkeypatch.setattr(main_dev, "LLMClassifier", FakeClassifier)
    monkeypatch.setattr(main_dev, "ProcessingPipeline", FakePipeline)
    monkeypatch.setattr(main_dev, "WeightedMatchStrategy", lambda: "match_strategy")
    monkeypatch.setattr(
        main_dev, "FirstSeenResolutionStrategy", lambda: "resolution_strategy"
    )
    monkeypatch.setattr(
        main_dev,
        "DedupService",
        lambda repository, match_strategy, resolution_strategy: SimpleNamespace(
            repository=repository,
            match_strategy=match_strategy,
            resolution_strategy=resolution_strategy,
        ),
    )

    main_dev.main()

    assert events["init"] is True
    assert events["service_start"] == service_start
    assert isinstance(events["repository"], FakeRepository)
    assert events["run_loop"] is True
