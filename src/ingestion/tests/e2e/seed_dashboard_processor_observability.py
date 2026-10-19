"""Seed processor observability rows for dashboard Playwright coverage.

This is intentionally test-only. The dashboard bootstrap already seeds
credential, dead-letter, and failed-sync state; this companion script adds
Postgres processor outcome rows so `/api/processor/*` returns a stable,
non-empty window during e2e runs.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_INGESTION_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _INGESTION_ROOT.parent.parent
_DB_CORE_ROOT = _REPO_ROOT / "src" / "common" / "db_core"
for path in (str(_DB_CORE_ROOT), str(_INGESTION_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
_load_env_file(_REPO_ROOT / ".env")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import core.bootstrap  # noqa: E402

core.bootstrap.init()

from core.models import ClassificationLog, IngestedItem  # noqa: E402
from django.utils import timezone  # noqa: E402

PREFIX = "e2e_processor_"


def main() -> int:
    ClassificationLog.objects.filter(ingested_item__external_id__startswith=PREFIX).delete()
    IngestedItem.objects.filter(external_id__startswith=PREFIX).delete()

    now = timezone.now()
    processed = IngestedItem.objects.create(
        external_id=f"{PREFIX}processed",
        platform="teams",
        fetched_by_user_email="sara@e2e.test",
        course_external_id="course-101",
        payload={"content_type": "assignment"},
        raw_payload={},
        processing_status="processed",
        processing_completed_at=now - timedelta(minutes=4),
    )
    unclassified = IngestedItem.objects.create(
        external_id=f"{PREFIX}unclassified",
        platform="moodle",
        fetched_by_user_email="pavel@e2e.test",
        course_external_id="course-202",
        payload={"content_type": "raw_message"},
        raw_payload={},
        processing_status="processed",
        processing_completed_at=now - timedelta(minutes=2),
    )
    IngestedItem.objects.create(
        external_id=f"{PREFIX}failed",
        platform="teams",
        fetched_by_user_email="olek@e2e.test",
        course_external_id="course-123",
        payload={"content_type": "raw_message"},
        raw_payload={},
        processing_status="failed",
        error_message="Classification call failed: timeout",
        processing_completed_at=now - timedelta(minutes=1),
    )
    IngestedItem.objects.create(
        external_id=f"{PREFIX}pending",
        platform="telegram",
        fetched_by_user_email="kenji@e2e.test",
        payload={"content_type": "assignment"},
        raw_payload={},
        processing_status="pending",
    )

    ClassificationLog.objects.create(
        ingested_item=processed,
        entity_type="assignment",
        classification_method="llm",
        input_tokens=100,
        output_tokens=20,
    )
    ClassificationLog.objects.create(
        ingested_item=unclassified,
        entity_type="raw_message",
        classification_method="llm",
        input_tokens=40,
        output_tokens=9,
    )

    print("seed_dashboard_processor_observability: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
