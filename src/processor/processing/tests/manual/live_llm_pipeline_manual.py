"""SYNTHETIC EXAMPLE manual LLM smoke test.

Run explicitly with local credentials and database.
"""

from __future__ import annotations

import os
import uuid

import pytest

from core.models.ingested import IngestedItem
from processing.adapters.llm.classifier import LLMClassifier
from processing.adapters.postgres import mapper
from processing.adapters.postgres.repository import PostgresEntityRepository
from processing.config import ProcessingConfig
from processing.pipeline import ProcessingPipeline

REQUIRED_ENV_VARS = (
    "RUN_LLM_LIVE_TESTS",
    "ENDPOINT_LLM_API_KEY",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
)

VALID_ENTITY_TYPES = {"assignment", "announcement", "event", "material"}

# SYNTHETIC EXAMPLE: one safe sample for optional manual LLM smoke testing.
LIVE_RAW_MESSAGE_CASES = [
    {
        "id": "synthetic-example-1",
        "external_id_prefix": "manual_live_synthetic_example",
        "platform": "teams",
        "course_external_id": "synthetic-course",
        "schema_version": "4.0", "adapter_version": "v0",
        "ingestion_mode": "server_poll", "source_url": None,
        "payload": {
            "content_type": "raw_message", "title": None,
            "author": "Example Teacher",
            "body": "Please submit the example worksheet by Friday at 17:00.",
            "course_name": "SYNTHETIC EXAMPLE COURSE",
            "published_at": "2025-01-01T09:00:00+00:00",
            "deadline": None, "submission_format": None,
            "start_time": None, "end_time": None, "location": None,
            "attachments": [], "links": [],
        },
        "raw_payload": {"schema_version": "2", "source": "teams_csa",
                        "ingestion_mode": "server_poll", "original": {"messageType": "Text"}},
    },
]


def _live_env_skip_reason() -> str | None:
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        return "Manual live LLM test requires env vars: " + ", ".join(sorted(missing))
    if os.getenv("RUN_LLM_LIVE_TESTS") != "1":
        return "Set RUN_LLM_LIVE_TESTS=1 to execute the live LLM smoke test"
    if os.getenv("LANGSMITH_TRACING", "").lower() not in {"1", "true", "yes", "on"}:
        return "Set LANGSMITH_TRACING=true to verify LangSmith tracing manually"
    return None


SKIP_REASON = _live_env_skip_reason()

pytestmark = [
    pytest.mark.llm_live,
    pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or ""),
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "sample",
    LIVE_RAW_MESSAGE_CASES,
    ids=[case["id"] for case in LIVE_RAW_MESSAGE_CASES],
)
def test_processes_curated_raw_message_with_live_llm_and_langsmith(
    sample: dict[str, object],
) -> None:
    ingested_item = IngestedItem.objects.create(
        external_id=f"{sample['external_id_prefix']}_{uuid.uuid4().hex[:8]}",
        platform=sample["platform"],
        course_external_id=sample["course_external_id"],
        schema_version=sample["schema_version"],
        adapter_version=sample["adapter_version"],
        ingestion_mode=sample["ingestion_mode"],
        processing_status="pending",
        payload=sample["payload"],
        raw_payload=sample["raw_payload"],
    )
    config = ProcessingConfig()
    repository = PostgresEntityRepository()
    classifier = LLMClassifier(config)
    pipeline = ProcessingPipeline(repository, classifier, config)

    pipeline.process_item(mapper.orm_to_envelope(ingested_item))

    ingested_item.refresh_from_db()
    assert ingested_item.processing_status == "processed"

    classified_items = list(ingested_item.classified_items.all())
    assert len(classified_items) == 1
    assert classified_items[0].entity_type in VALID_ENTITY_TYPES
    assert classified_items[0].course_id is not None

    logs = list(ingested_item.classification_logs.all())
    assert len(logs) == 1
    assert logs[0].classification_method == "llm"
    assert logs[0].llm_model == config.llm_model
    assert logs[0].entity_type in VALID_ENTITY_TYPES
    assert logs[0].entity_type == classified_items[0].entity_type
    assert logs[0].confidence is not None
