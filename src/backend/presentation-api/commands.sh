# ── VIRTUAL ENV ──────────────────────────────────────────────
source .venv/bin/activate

# ── DATABASE (Docker) ────────────────────────────────────────
docker compose up -d db
docker compose down

# ── MIGRATIONS ───────────────────────────────────────────────
python manage.py makemigrations
python manage.py migrate

# ── RUN SERVER ───────────────────────────────────────────────
export $(cat .env.local | xargs) && python manage.py runserver


# ── PSQL DIRECT ACCESS ───────────────────────────────────────
docker exec -it presentation-api-db-1 psql -U postgres shared_db

# ══════════════════════════════════════════════════════════════
# DB SEED — run all of these inside psql
# ══════════════════════════════════════════════════════════════

# ── SEED: COURSE ─────────────────────────────────────────────
INSERT INTO courses (id, name, code, major, semester, created_at, updated_at)
VALUES (
  gen_random_uuid(),
  'Software Engineering',
  'CS-301',
  'Computer Science',
  'Spring 2026',
  NOW(),
  NOW()
) RETURNING id;
-- copy as COURSE_ID

# ── SEED: INGESTED ITEM (for assignment) ─────────────────────
INSERT INTO ingested_items (
  id, external_id, platform, schema_version, adapter_version,
  ingestion_mode, processing_status, payload, raw_payload,
  created_at, updated_at
)
VALUES (
  gen_random_uuid(),
  'ext-001',
  'moodle',
  '4.0',
  '1.0',
  'server_poll',
  'processed',
  '{"content_type": "assign", "title": null, "author": null, "body": null,
    "source_url": "https://moodle.example.com/assignment/1",
    "course_name": null, "published_at": null,
    "deadline": null, "submission_format": "pdf",
    "max_points": 100, "points": null,
    "start_time": null, "end_time": null, "location": null,
    "attachments": [], "links": [], "dates": []}',
  '{}',
  NOW(),
  NOW()
) RETURNING id;
-- copy as INGESTED_ITEM_ID_1

# ── SEED: BASE CLASSIFIED ITEM (assignment) ───────────────────
INSERT INTO base_classified_items (
  id, entity_type, author, ingested_item_id,
  course_id, title, body, created_at, updated_at
)
VALUES (
  gen_random_uuid(),
  'assignment',
  'Prof. Smith',
  'INGESTED_ITEM_ID_1',
  'COURSE_ID',
  'Final Project',
  'Submit your final project by the deadline.',
  NOW(),
  NOW()
) RETURNING id;
-- copy as BASE_ITEM_ID_1

# ── SEED: ASSIGNMENT ─────────────────────────────────────────
INSERT INTO assignments (
  baseclassifieditem_ptr_id, deadline,
  submission_format, max_points, points, status
)
VALUES (
  'BASE_ITEM_ID_1',
  '2026-06-01 23:59:00+00',
  'pdf',
  100,
  NULL,
  'open'
);

# ── SEED: LINK (for assignment) ───────────────────────────────
INSERT INTO links (id, classified_item_id, url, label, created_at)
VALUES (
  gen_random_uuid(),
  'BASE_ITEM_ID_1',
  'https://moodle.example.com/submit',
  'Submit here',
  NOW()
);

# ── SEED: ATTACHMENT (for assignment) ─────────────────────────
INSERT INTO attachments (id, classified_item_id, filename, mime_type, url, created_at)
VALUES (
  gen_random_uuid(),
  'BASE_ITEM_ID_1',
  'brief.pdf',
  'application/pdf',
  'https://moodle.example.com/brief.pdf',
  NOW()
);

# ── SEED: INGESTED ITEM (for announcement) ───────────────────
INSERT INTO ingested_items (
  id, external_id, platform, schema_version, adapter_version,
  ingestion_mode, processing_status, payload, raw_payload,
  created_at, updated_at
)
VALUES (
  gen_random_uuid(),
  'ext-002',
  'moodle',
  '4.0',
  '1.0',
  'server_poll',
  'processed',
  '{"content_type": "forum", "title": null, "author": null, "body": null,
    "source_url": "https://moodle.example.com/announcement/1",
    "course_name": null, "published_at": null,
    "deadline": null, "submission_format": null,
    "max_points": null, "points": null,
    "start_time": null, "end_time": null, "location": null,
    "attachments": [], "links": [], "dates": []}',
  '{}',
  NOW(),
  NOW()
) RETURNING id;
-- copy as INGESTED_ITEM_ID_2

# ── SEED: BASE CLASSIFIED ITEM (announcement) ─────────────────
INSERT INTO base_classified_items (
  id, entity_type, author, ingested_item_id,
  course_id, title, body, created_at, updated_at
)
VALUES (
  gen_random_uuid(),
  'announcement',
  'Prof. Smith',
  'INGESTED_ITEM_ID_2',
  'COURSE_ID',
  'Exam next week',
  'There will be an exam next Friday at 10am.',
  NOW(),
  NOW()
) RETURNING id;
-- copy as BASE_ITEM_ID_2

# ── SEED: ANNOUNCEMENT ───────────────────────────────────────
INSERT INTO announcements (baseclassifieditem_ptr_id)
VALUES ('BASE_ITEM_ID_2');

# ── SEED: INGESTED ITEM (for event) ──────────────────────────
INSERT INTO ingested_items (
  id, external_id, platform, schema_version, adapter_version,
  ingestion_mode, processing_status, payload, raw_payload,
  created_at, updated_at
)
VALUES (
  gen_random_uuid(),
  'ext-003',
  'teams',
  '4.0',
  '1.0',
  'server_poll',
  'processed',
  '{"content_type": "event", "title": null, "author": null, "body": null,
    "source_url": "https://teams.example.com/event/1",
    "course_name": null, "published_at": null,
    "deadline": null, "submission_format": null,
    "max_points": null, "points": null,
    "start_time": "2026-05-01T09:00:00Z",
    "end_time": "2026-05-01T10:30:00Z",
    "location": "Room 101",
    "attachments": [], "links": [], "dates": []}',
  '{}',
  NOW(),
  NOW()
) RETURNING id;
-- copy as INGESTED_ITEM_ID_3

# ── SEED: BASE CLASSIFIED ITEM (event) ────────────────────────
INSERT INTO base_classified_items (
  id, entity_type, author, ingested_item_id,
  course_id, title, body, created_at, updated_at
)
VALUES (
  gen_random_uuid(),
  'event',
  'Prof. Smith',
  'INGESTED_ITEM_ID_3',
  'COURSE_ID',
  'Lecture: Software Architecture',
  'Weekly lecture on software architecture patterns.',
  NOW(),
  NOW()
) RETURNING id;
-- copy as BASE_ITEM_ID_3

# ── SEED: EVENT ───────────────────────────────────────────────
INSERT INTO events (
  baseclassifieditem_ptr_id,
  start_time,
  end_time,
  location
)
VALUES (
  'BASE_ITEM_ID_3',
  '2026-05-01T09:00:00+00',
  '2026-05-01T10:30:00+00',
  'Room 101'
);

# ══════════════════════════════════════════════════════════════
# API TESTS — run these in terminal with server running
# ══════════════════════════════════════════════════════════════

# ── HEALTH CHECK ─────────────────────────────────────────────
curl http://localhost:8000/health/

# ── AUTH: LOGIN ──────────────────────────────────────────────
curl -s -X POST http://localhost:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"email": "sample1@example.edu"}' | python3 -m json.tool

TOKEN="PASTE_TOKEN_HERE"

# ── USERS: GET PROFILE ───────────────────────────────────────
curl -s http://localhost:8000/api/v1/users/me/ \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── USERS: DELETE ACCOUNT ────────────────────────────────────
curl -s -X DELETE http://localhost:8000/api/v1/users/me/ \
  -H "Authorization: Bearer $TOKEN"

# ── COURSES: LIST ALL ────────────────────────────────────────
curl -s http://localhost:8000/api/v1/courses/ \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── COURSES: GET MY SELECTIONS ───────────────────────────────
curl -s http://localhost:8000/api/v1/users/me/courses/ \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── COURSES: SET SELECTIONS ──────────────────────────────────
curl -s -X PUT http://localhost:8000/api/v1/users/me/courses/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"course_ids": ["COURSE_ID"]}' | python3 -m json.tool

# ── COURSES: VERIFY SELECTIONS ───────────────────────────────
curl -s http://localhost:8000/api/v1/users/me/courses/ \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── COURSES: INVALID UUID → 400 ──────────────────────────────
curl -s -X PUT http://localhost:8000/api/v1/users/me/courses/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"course_ids": ["00000000-0000-0000-0000-000000000000"]}' | python3 -m json.tool

# ── ASSIGNMENTS: LIST ALL ────────────────────────────────────
curl -s "http://localhost:8000/api/v1/assignments/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ASSIGNMENTS: FILTER BY COURSE ────────────────────────────
curl -s "http://localhost:8000/api/v1/assignments/?course_id=COURSE_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ASSIGNMENTS: FILTER BY PLATFORM ──────────────────────────
curl -s "http://localhost:8000/api/v1/assignments/?platform=moodle" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ASSIGNMENTS: FILTER BY STATUS ────────────────────────────
curl -s "http://localhost:8000/api/v1/assignments/?status=open" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ASSIGNMENTS: WRONG PLATFORM → empty ──────────────────────
curl -s "http://localhost:8000/api/v1/assignments/?platform=telegram" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ASSIGNMENTS: GET ONE ─────────────────────────────────────
curl -s "http://localhost:8000/api/v1/assignments/BASE_ITEM_ID_1/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ASSIGNMENTS: GET ONE → 404 ───────────────────────────────
curl -s "http://localhost:8000/api/v1/assignments/00000000-0000-0000-0000-000000000000/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ANNOUNCEMENTS: LIST ──────────────────────────────────────
curl -s "http://localhost:8000/api/v1/announcements/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ANNOUNCEMENTS: FILTER BY COURSE ──────────────────────────
curl -s "http://localhost:8000/api/v1/announcements/?course_id=COURSE_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ANNOUNCEMENTS: FILTER BY PLATFORM ────────────────────────
curl -s "http://localhost:8000/api/v1/announcements/?platform=moodle" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ANNOUNCEMENTS: WRONG PLATFORM → empty ────────────────────
curl -s "http://localhost:8000/api/v1/announcements/?platform=telegram" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ANNOUNCEMENTS: GET ONE ───────────────────────────────────
curl -s "http://localhost:8000/api/v1/announcements/BASE_ITEM_ID_2/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ANNOUNCEMENTS: GET ONE → 404 ─────────────────────────────
curl -s "http://localhost:8000/api/v1/announcements/00000000-0000-0000-0000-000000000000/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── EVENTS: LIST ─────────────────────────────────────────────
curl -s "http://localhost:8000/api/v1/events/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── EVENTS: FILTER BY COURSE ─────────────────────────────────
curl -s "http://localhost:8000/api/v1/events/?course_id=COURSE_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── EVENTS: WRONG COURSE → empty ─────────────────────────────
curl -s "http://localhost:8000/api/v1/events/?course_id=00000000-0000-0000-0000-000000000000" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── EVENTS: GET ONE ──────────────────────────────────────────
curl -s "http://localhost:8000/api/v1/events/BASE_ITEM_ID_3/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── EVENTS: GET ONE → 404 ────────────────────────────────────
curl -s "http://localhost:8000/api/v1/events/00000000-0000-0000-0000-000000000000/" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ── ISOLATION TEST: other user cannot see data ────────────────
curl -s -X POST http://localhost:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"email": "sample2@example.edu"}' | python3 -m json.tool
TOKEN2="PASTE_TOKEN_HERE"
curl -s "http://localhost:8000/api/v1/assignments/" \
  -H "Authorization: Bearer $TOKEN2" | python3 -m json.tool
# should return {"results": []}

# ── TESTS ────────────────────────────────────────────────────
pytest
pytest -v
pytest tests/test_health.py -v

# ── GIT ──────────────────────────────────────────────────────
git add .
git commit -m "feat(presentation-api): "
git push origin presentation-api