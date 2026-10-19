# pyright: reportAttributeAccessIssue=false

"""Manual SIGTERM smoke test for the processor main entrypoint.

This file intentionally does not match pytest's default ``test_*.py`` discovery
pattern, so it will not run during the normal processor suite. Run it
explicitly when you want to observe the real ``main.main()`` signal path
without a real database or LLM backend.

Steps:
1. From ``src/processor``, install deps if needed: ``uv sync --extra dev``
2. Optionally override runtime knobs in your shell:
   ``RUN_SIGTERM_MANUAL_TESTS=1``
   ``MANUAL_SIGTERM_ITEM_COUNT=30``
   ``MANUAL_SIGTERM_LLM_SLEEP_SECONDS=3``
   ``MANUAL_SIGTERM_MAX_CONCURRENCY=10``
   ``MANUAL_SIGTERM_AFTER_SECONDS=1``
3. Run:
   ``uv run pytest processing/tests/manual/sigterm_main_manual.py -v -s``

What this verifies:
- ``main.py`` installs signal handlers and exits on SIGTERM
- an active batch keeps draining after SIGTERM
- the processor does not fetch another batch after shutdown is requested
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

SKIP_REASON = (
    None
    if os.getenv("RUN_SIGTERM_MANUAL_TESTS") == "1"
    else "Set RUN_SIGTERM_MANUAL_TESTS=1 to execute the manual SIGTERM smoke test"
)

pytestmark = [
    pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or ""),
]


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("MANUAL_SIGTERM_ITEM_COUNT", "30")
    env.setdefault("MANUAL_SIGTERM_LLM_SLEEP_SECONDS", "3")
    env.setdefault("MANUAL_SIGTERM_MAX_CONCURRENCY", "10")
    env.setdefault("MANUAL_SIGTERM_AFTER_SECONDS", "1")
    env.setdefault("ENDPOINT_DEDUP_ENABLED", "false")
    return env


def test_processor_main_drains_active_batch_after_sigterm() -> None:
    env = _child_env()
    child = subprocess.Popen(
        [sys.executable, __file__, "--child"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert child.stdout is not None

    captured: list[str] = []
    saw_work_started = False
    for line in child.stdout:
        captured.append(line)
        if "MANUAL_WORK_STARTED" in line:
            saw_work_started = True
            break

    if saw_work_started:
        child.send_signal(signal.SIGTERM)

    try:
        remainder, _ = child.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        child.kill()
        remainder, _ = child.communicate(timeout=5)
        captured.append(remainder)
        pytest.fail(
            "manual SIGTERM child timed out\n\n" + "".join(captured),
            pytrace=False,
        )

    captured.append(remainder)
    output = "".join(captured)

    assert saw_work_started, output
    assert child.returncode == 0, output
    assert "MANUAL_WORK_STARTED" in output, output
    assert "processor signal received: SIGTERM" in output, output
    assert "Processor shutdown complete" in output, output
    assert "MANUAL_FETCH_CALLS 1" in output, output
    assert f"MANUAL_PERSISTED_COUNT {env['MANUAL_SIGTERM_ITEM_COUNT']}" in output, (
        output
    )


def _child_main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    processor_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(processor_root))

    import main
    from processing.models.classification import AssignmentPatch, ClassificationResult
    from processing.models.course import Course
    from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
    from processing.models.entities import RawMessage

    item_count = _env_int("MANUAL_SIGTERM_ITEM_COUNT", 30)
    llm_sleep_seconds = _env_float("MANUAL_SIGTERM_LLM_SLEEP_SECONDS", 3.0)
    max_concurrency = _env_int("MANUAL_SIGTERM_MAX_CONCURRENCY", 10)
    work_started = False

    def build_pending_batch() -> list[ProcessingEnvelope]:
        batch: list[ProcessingEnvelope] = []
        for index in range(item_count):
            ingested_id = uuid.uuid4()
            external_id = f"manual_sigterm_{index}_{uuid.uuid4().hex[:8]}"
            body = f"Homework {index + 1} is due tomorrow at 23:59. Upload a PDF."
            payload = {
                "content_type": "raw_message",
                "title": f"Reminder {index + 1}",
                "author": "Manual Test",
                "body": body,
                "course_name": "SYNTHETIC EXAMPLE COURSE",
                "published_at": "2025-10-02T07:47:19+00:00",
                "deadline": None,
                "submission_format": None,
                "start_time": None,
                "end_time": None,
                "location": None,
                "attachments": [],
                "links": [],
            }
            batch.append(
                ProcessingEnvelope(
                    id=ingested_id,
                    ingested_item=IngestedItemContext(
                        id=ingested_id,
                        external_id=external_id,
                        platform="teams",
                        course_external_id="manual-course",
                        schema_version="4.0",
                        adapter_version="v0",
                        ingestion_mode="server_poll",
                        processing_status="pending",
                        source_url=None,
                        payload=payload,
                    ),
                    entity=RawMessage(
                        title=payload["title"],
                        body=body,
                        author="Manual Test",
                    ),
                )
            )
        return batch

    class FakeRepository:
        def __init__(self) -> None:
            self._pending_batches = [build_pending_batch(), []]
            self.fetch_calls = 0
            self.persisted_ids: list[uuid.UUID] = []
            self.failed_ids: list[uuid.UUID] = []
            self.courses: list[Course] = []
            self.course_mappings: dict[tuple[str, str], Course] = {}

        def get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
            del limit
            self.fetch_calls += 1
            if not self._pending_batches:
                return []
            return self._pending_batches.pop(0)

        def get_existing_canonicals(self, course_id: uuid.UUID, entity_type: str):
            del course_id, entity_type
            return []

        def persist_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
            self.persisted_ids.extend(
                envelope.ingested_item.id for envelope in envelopes
            )
            print(f"MANUAL_PERSIST_BATCH {len(envelopes)}", flush=True)

        def persist_unclassified_batch(
            self, envelopes: list[ProcessingEnvelope]
        ) -> None:
            self.persisted_ids.extend(
                envelope.ingested_item.id for envelope in envelopes
            )

        def mark_failed(self, ingested_item_id: uuid.UUID, error: str) -> None:
            del error
            self.failed_ids.append(ingested_item_id)

        def get_all_courses(self) -> list[Course]:
            return list(self.courses)

        def create_course(self, name: str, code: str | None = None) -> Course:
            course = Course(id=uuid.uuid4(), name=name, code=code)
            self.courses.append(course)
            return course

        def update_course_code(self, course_id: uuid.UUID, code: str) -> None:
            self.courses = [
                course.model_copy(update={"code": code})
                if course.id == course_id
                else course
                for course in self.courses
            ]

        def get_course_by_platform_mapping(
            self, platform: str, external_course_id: str
        ) -> Course | None:
            return self.course_mappings.get((platform, external_course_id))

        def upsert_course_platform_mapping(
            self,
            course_id: uuid.UUID,
            platform: str,
            external_course_id: str,
            external_course_name: str | None,
        ) -> None:
            del external_course_name
            for course in self.courses:
                if course.id == course_id:
                    self.course_mappings[(platform, external_course_id)] = course
                    return

    class SlowClassifier:
        def __init__(self, config) -> None:
            self.model = config.llm_model
            self.prompt_version = config.llm_prompt_version

        async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
            nonlocal work_started
            if not work_started:
                work_started = True
                print("MANUAL_WORK_STARTED", flush=True)
            await asyncio.sleep(llm_sleep_seconds)
            return ClassificationResult(
                entity_type="assignment",
                confidence=0.95,
                reasoning=f"classified {envelope.ingested_item.external_id}",
            )

        async def extract_fields(
            self, envelope: ProcessingEnvelope, classified_type: str
        ) -> AssignmentPatch:
            assert classified_type == "assignment"
            await asyncio.sleep(llm_sleep_seconds)
            return AssignmentPatch(
                title=f"Extracted {envelope.ingested_item.external_id}",
                deadline=datetime(2025, 10, 3, 21, 59, tzinfo=timezone.utc),
                submission_format="file",
                confidence=0.91,
                reasoning="manual harness extraction",
            )

    repository = FakeRepository()

    main.core.bootstrap.init = lambda: None
    main.PostgresEntityRepository = lambda: repository
    main.LLMClassifier = SlowClassifier

    original_config_factory = main.ProcessingConfig

    def config_factory():
        config = original_config_factory()
        config.max_concurrency = max_concurrency
        return config

    main.ProcessingConfig = config_factory

    try:
        main.main()
    finally:
        print(f"MANUAL_FETCH_CALLS {repository.fetch_calls}", flush=True)
        print(f"MANUAL_PERSISTED_COUNT {len(repository.persisted_ids)}", flush=True)
        print(f"MANUAL_FAILED_COUNT {len(repository.failed_ids)}", flush=True)

    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(_child_main())
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(pytest.main([__file__, "-v", "-s"]))
