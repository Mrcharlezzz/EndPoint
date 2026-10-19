# pyright: reportAttributeAccessIssue=false

"""Manual live test for temporal LLM extraction semantics.

This file intentionally does not match pytest's default ``test_*.py`` discovery
pattern. Run it explicitly when you want to verify the v2 temporal extraction
contract against a real OpenAI-compatible endpoint.

It checks six event extraction cases:

- three offsets from ``published_at`` that must resolve to UTC instants
- three explicit local clock/date expressions that must resolve as
  Europe/Amsterdam wall-clock times

Steps:
1. Install deps from ``src/processor``: ``uv sync --extra dev``
2. Export the required runtime vars in your shell:
   ``ENDPOINT_LLM_API_KEY=...``
   ``ENDPOINT_LLM_MODEL=gpt-5-nano``  # optional override
   ``ENDPOINT_LLM_TIMEOUT_SECONDS=90``  # optional override
   ``RUN_LLM_LIVE_TESTS=1``
3. Run:
   ``uv run pytest processing/tests/manual/live_llm_temporal_extraction_manual.py -m llm_live -s -v``
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from processing.adapters.llm.classifier import LLMClassifier
from processing.application.extraction_resolution import ExtractionResolutionService
from processing.config import ProcessingConfig
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import Event, RawMessage

REQUIRED_ENV_VARS = (
    "RUN_LLM_LIVE_TESTS",
    "ENDPOINT_LLM_API_KEY",
)


@dataclass(frozen=True)
class TemporalCase:
    case_id: str
    body: str
    published_at: str
    expected_start_time: str
    expected_source_kind: str


TEMPORAL_CASES: tuple[TemporalCase, ...] = (
    TemporalCase(
        case_id="relative_in_5_minutes",
        body="The Coroutines quiz starts in 5 minutes.",
        published_at="2026-04-16T15:10:39Z",
        expected_start_time="2026-04-16T15:15:39+00:00",
        expected_source_kind="published_at_offset_utc",
    ),
    TemporalCase(
        case_id="relative_in_2_hours",
        body="The Threads quiz starts in 2 hours.",
        published_at="2026-04-09T13:16:05Z",
        expected_start_time="2026-04-09T15:16:05+00:00",
        expected_source_kind="published_at_offset_utc",
    ),
    TemporalCase(
        case_id="relative_10_minutes_from_now",
        body="The make-up lab starts 10 minutes from now.",
        published_at="2026-03-31T08:02:10Z",
        expected_start_time="2026-03-31T08:12:10+00:00",
        expected_source_kind="published_at_offset_utc",
    ),
    TemporalCase(
        case_id="explicit_today_at_0945",
        body="Hi, see you today at 09:45 in Google Meet.",
        published_at="2026-03-03T07:55:53Z",
        expected_start_time="2026-03-03T09:45:00+01:00",
        expected_source_kind="explicit_local_time",
    ),
    TemporalCase(
        case_id="explicit_tomorrow_at_1400",
        body="Tomorrow at 14:00 we will have the review session.",
        published_at="2026-04-16T15:10:39Z",
        expected_start_time="2026-04-17T14:00:00+02:00",
        expected_source_kind="explicit_local_time",
    ),
    TemporalCase(
        case_id="explicit_calendar_date_at_0945",
        body="Office hours are on March 19 at 09:45 in the lab.",
        published_at="2026-03-01T10:00:00Z",
        expected_start_time="2026-03-19T09:45:00+01:00",
        expected_source_kind="explicit_local_time",
    ),
)


def _live_env_skip_reason() -> str | None:
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        return "Manual live LLM test requires env vars: " + ", ".join(sorted(missing))
    if os.getenv("RUN_LLM_LIVE_TESTS") != "1":
        return "Set RUN_LLM_LIVE_TESTS=1 to execute the live LLM temporal test"
    return None


SKIP_REASON = _live_env_skip_reason()

pytestmark = [
    pytest.mark.llm_live,
    pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or ""),
]


def _make_envelope(case: TemporalCase) -> ProcessingEnvelope:
    ingested = IngestedItemContext(
        id=uuid4(),
        external_id=f"manual_temporal_{case.case_id}_{uuid4().hex[:8]}",
        platform="telegram",
        course_external_id="manual-temporal-extraction",
        schema_version="4.0",
        adapter_version="manual",
        ingestion_mode="manual",
        processing_status="pending",
        source_url=None,
        payload={
            "content_type": "raw_message",
            "title": None,
            "author": "Manual Temporal Test",
            "body": case.body,
            "course_name": "Manual Temporal Extraction Course",
            "published_at": case.published_at,
            "deadline": None,
            "submission_format": None,
            "start_time": None,
            "end_time": None,
            "location": None,
            "attachments": [],
            "links": [],
        },
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(
            title=None,
            body=case.body,
            author="Manual Temporal Test",
            published_at=datetime.fromisoformat(
                case.published_at.replace("Z", "+00:00")
            ),
        ),
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _datetime_summary(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds")


@pytest.mark.anyio
async def test_live_llm_temporal_extraction_cases() -> None:
    config = ProcessingConfig(llm_prompt_version="entity-inference-v2")
    classifier = LLMClassifier(config)
    resolver = ExtractionResolutionService(
        university_timezone=config.university_timezone
    )

    failures: list[str] = []
    summaries: list[dict[str, str | None]] = []

    for case in TEMPORAL_CASES:
        envelope = _make_envelope(case)
        extraction = await classifier.extract_fields(envelope, "event")
        resolved = resolver.resolve(envelope, "event", extraction)

        assert isinstance(resolved.entity, Event)
        actual_start_time = resolved.entity.start_time
        actual_source_kind = extraction.start_time_source_kind
        expected_start_time = _parse_datetime(case.expected_start_time)

        summaries.append(
            {
                "case_id": case.case_id,
                "body": case.body,
                "published_at": case.published_at,
                "expected_source_kind": case.expected_source_kind,
                "actual_source_kind": actual_source_kind,
                "expected_start_time": expected_start_time.isoformat(
                    timespec="seconds"
                ),
                "actual_start_time": _datetime_summary(actual_start_time),
            }
        )

        if actual_source_kind != case.expected_source_kind:
            failures.append(
                f"{case.case_id}: start_time_source_kind expected "
                f"{case.expected_source_kind!r}, got {actual_source_kind!r}"
            )
        if actual_start_time != expected_start_time:
            failures.append(
                f"{case.case_id}: start_time expected "
                f"{expected_start_time.isoformat(timespec='seconds')!r}, got "
                f"{_datetime_summary(actual_start_time)!r}"
            )
        if case.expected_source_kind == "published_at_offset_utc":
            if (
                actual_start_time is None
                or actual_start_time.tzinfo is not timezone.utc
            ):
                failures.append(
                    f"{case.case_id}: expected UTC tzinfo for published_at offset, "
                    f"got {actual_start_time!r}"
                )

    print(json.dumps(summaries, indent=2, ensure_ascii=False))
    if failures:
        pytest.fail("Temporal extraction mismatches:\n" + "\n".join(failures))
