from datetime import UTC, datetime

from ingestion.schemas import ContentType, FileRef, LinkRef, NormalizedItem


def _base_item(**overrides: object) -> NormalizedItem:
    defaults: dict[str, object] = {
        "external_id": "moodle_assign_1",
        "platform": "moodle",
        "content_type": ContentType.ASSIGNMENT,
        "title": "Homework 1",
        "body": "Solve all problems.",
    }
    defaults.update(overrides)
    return NormalizedItem.model_validate(defaults)


def test_payload_excludes_top_level_columns() -> None:
    """Per v5 §3.3.1 — source_url, course_external_id, external_id, platform
    and ingestion metadata live in dedicated columns, not the payload JSON."""
    item = _base_item(
        source_url="https://moodle.test/mod/assign/view.php?id=41249",
        course_external_id="8413",
    )

    payload = item.to_payload()

    assert "source_url" not in payload
    assert "course_external_id" not in payload
    assert "external_id" not in payload
    assert "platform" not in payload
    assert "ingestion_mode" not in payload
    assert "raw_payload" not in payload


def test_payload_includes_v5_base_and_type_fields() -> None:
    """Base + per-type fields that the processor consumes."""
    deadline = datetime(2026, 3, 20, 23, 59, tzinfo=UTC)
    published = datetime(2026, 2, 28, 15, 32, 12, tzinfo=UTC)
    item = _base_item(
        author="Ada Lovelace",
        course_name="Matrix Algebra",
        published_at=published,
        deadline=deadline,
        submission_format="file",
    )

    payload = item.to_payload()

    assert payload["content_type"] == "assignment"
    assert payload["title"] == "Homework 1"
    assert payload["author"] == "Ada Lovelace"
    assert payload["body"] == "Solve all problems."
    assert payload["course_name"] == "Matrix Algebra"
    assert payload["published_at"] == "2026-02-28T15:32:12Z"
    assert payload["deadline"] == "2026-03-20T23:59:00Z"
    assert payload["submission_format"] == "file"


def test_payload_event_fields_serialize() -> None:
    item = _base_item(
        content_type=ContentType.EVENT,
        title="Lecture",
        body="Room booked.",
        start_time=datetime(2026, 1, 15, 21, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 15, 23, 0, tzinfo=UTC),
        location="Room 101",
    )

    payload = item.to_payload()

    assert payload["start_time"] == "2026-01-15T21:00:00Z"
    assert payload["end_time"] == "2026-01-15T23:00:00Z"
    assert payload["location"] == "Room 101"


def test_payload_attachments_use_v5_shape() -> None:
    """Per v5 §3.3.1 — attachments[] are {filename, mime_type, url}. FileRef's
    internal fields (external_id, size_bytes, is_media) stay in-memory only."""
    item = _base_item(
        attachments=[
            FileRef(
                external_id="moodle_file_42_slides.pdf",
                filename="slides.pdf",
                download_url="https://moodle.test/pluginfile.php/42/slides.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                is_media=False,
            ),
        ],
    )

    payload = item.to_payload()
    attachments = payload["attachments"]
    assert isinstance(attachments, list)
    assert attachments == [
        {
            "filename": "slides.pdf",
            "mime_type": "application/pdf",
            "url": "https://moodle.test/pluginfile.php/42/slides.pdf",
        }
    ]


def test_payload_attachment_empty_download_url_becomes_null() -> None:
    """Processor invariants reject attachments with empty string URLs; the
    payload must expose them as null so the invariant layer handles it."""
    item = _base_item(
        attachments=[
            FileRef(
                external_id="placeholder",
                filename="attachment",
                download_url="",
                content_type=None,
            ),
        ],
    )

    payload = item.to_payload()
    attachments = payload["attachments"]
    assert isinstance(attachments, list)
    assert attachments[0]["url"] is None
    assert attachments[0]["mime_type"] is None


def test_payload_links_match_v5_shape() -> None:
    """Per v5 §3.3.1 — links[] are {url, label}."""
    item = _base_item(
        links=[
            LinkRef(url="https://example.com/spec", label="Spec"),
            LinkRef(url="https://example.com/raw"),
        ],
    )

    payload = item.to_payload()

    assert payload["links"] == [
        {"url": "https://example.com/spec", "label": "Spec"},
        {"url": "https://example.com/raw", "label": None},
    ]


def test_payload_datetime_fields_null_when_absent() -> None:
    item = _base_item(content_type=ContentType.RAW_MESSAGE, body="hi")

    payload = item.to_payload()

    assert payload["published_at"] is None
    assert payload["deadline"] is None
    assert payload["start_time"] is None
    assert payload["end_time"] is None


def test_payload_has_exactly_the_v5_keys() -> None:
    """Guard against accidentally adding fields the processor doesn't expect."""
    item = _base_item()

    payload = item.to_payload()

    expected_keys = {
        "content_type",
        "title",
        "author",
        "body",
        "course_name",
        "published_at",
        "deadline",
        "submission_format",
        "start_time",
        "end_time",
        "location",
        "attachments",
        "links",
    }
    assert set(payload.keys()) == expected_keys
