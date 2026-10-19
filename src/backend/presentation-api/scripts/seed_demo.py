"""Seed a demo course + processed assignment into the shared db.

Raw SQL so we don't go through the read-only mirrors (base_classified_items
carries NOT NULL columns the mirrors don't declare). Idempotent via ON CONFLICT.

Run inside the presentation-api container:
    docker compose run --rm presentation-api python scripts/seed_demo.py
"""

import json
import os
import sys
import uuid
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connection  # noqa: E402

DEMO_COURSE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEMO_INGESTED_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
DEMO_ASSIGN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def main() -> None:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO courses (id, name, code, major, semester, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            [str(DEMO_COURSE_ID), "Intro to Demo", "DEMO-101", "Computer Science", "Spring 2026"],
        )

        cur.execute(
            """
            INSERT INTO ingested_items (
                id, external_id, platform, course_external_id,
                schema_version, adapter_version, ingestion_mode, processing_status,
                payload, raw_payload, created_at, updated_at
            )
            VALUES (
                %s, %s, 'moodle', %s,
                '4.0', 'demo', 'server_poll', 'processed',
                %s::jsonb, %s::jsonb, NOW(), NOW()
            )
            ON CONFLICT (id) DO NOTHING
            """,
            [
                str(DEMO_INGESTED_ID),
                "moodle_assign_demo_1",
                "DEMO-101",
                json.dumps(
                    {
                        "content_type": "assignment",
                        "source_url": "https://moodle.example.com/assign/1",
                    }
                ),
                json.dumps({}),
            ],
        )

        cur.execute(
            """
            INSERT INTO base_classified_items (
                id, entity_type, author, ingested_item_id, course_id,
                title, body, dedup_status, created_at, updated_at
            )
            VALUES (
                %s, 'assignment', 'Prof. Demo', %s, %s,
                %s, %s, 'primary', NOW(), NOW()
            )
            ON CONFLICT (id) DO NOTHING
            """,
            [
                str(DEMO_ASSIGN_ID),
                str(DEMO_INGESTED_ID),
                str(DEMO_COURSE_ID),
                "Demo Assignment 1",
                "Complete the demo questionnaire and submit via Moodle.",
            ],
        )

        cur.execute(
            """
            INSERT INTO assignments (baseclassifieditem_ptr_id, deadline, submission_format)
            VALUES (%s, NOW() + INTERVAL '7 days', 'pdf')
            ON CONFLICT (baseclassifieditem_ptr_id) DO NOTHING
            """,
            [str(DEMO_ASSIGN_ID)],
        )

    print(
        f"seeded: course={DEMO_COURSE_ID} ingested={DEMO_INGESTED_ID} assignment={DEMO_ASSIGN_ID}"
    )


if __name__ == "__main__":
    main()
