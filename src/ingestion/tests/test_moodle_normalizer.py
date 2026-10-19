"""Tests for MoodleNormalizer — raw Moodle API dicts → NormalizedItem + FileRef."""

from __future__ import annotations

import pytest
from ingestion.adapters.moodle.normalizer import MoodleNormalizer
from ingestion.schemas import ContentType, FileRef, NormalizedItem


@pytest.fixture
def normalizer() -> MoodleNormalizer:
    return MoodleNormalizer(
        adapter_version="1.0.0",
        base_url="https://moodle.test",
    )


# --- Assignment normalization ---


def _assignment_raw(
    *,
    assign_id: int = 5897,
    name: str = "Homework 1# Building Logic Gates",
    intro: str = "<p>Build basic logic gates from <b>NAND</b></p>",
    duedate: int = 1771541940,
    course_name: str = "DSCA Tutorial",
    course_id: int = 9273,
    grade: int = 10,
    cmid: int | None = 41249,
) -> dict:
    raw: dict = {
        "id": assign_id,
        "name": name,
        "intro": intro,
        "introformat": 1,
        "duedate": duedate,
        "allowsubmissionsfromdate": 1770966000,
        "grade": grade,
        "timemodified": 1772115132,
        "cutoffdate": 1772751540,
        "_course_name": course_name,
        "_course_id": course_id,
    }
    if cmid is not None:
        raw["cmid"] = cmid
    return raw


class TestAssignmentNormalization:
    def test_basic_fields(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw()
        item = normalizer.normalize(raw, source="assignment")
        assert isinstance(item, NormalizedItem)
        assert item.external_id == "moodle_assign_5897"
        assert item.platform == "moodle"
        assert item.content_type == ContentType.ASSIGNMENT
        assert item.title == "Homework 1# Building Logic Gates"
        assert item.course_name == "DSCA Tutorial"
        assert item.course_external_id == "9273"

    def test_html_stripping(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw(intro="<p>Build <b>gates</b> from NAND</p>")
        item = normalizer.normalize(raw, source="assignment")
        assert item.body == "Build gates from NAND"

    def test_deadline_from_unix_timestamp(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw(duedate=1771541940)
        item = normalizer.normalize(raw, source="assignment")
        assert item.deadline is not None
        assert item.deadline.year == 2026

    def test_assignment_published_at_from_timemodified(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw()
        item = normalizer.normalize(raw, source="assignment")
        assert item.published_at is not None
        assert item.published_at.year == 2026

    def test_zero_duedate_is_none(self, normalizer: MoodleNormalizer) -> None:
        """Moodle uses duedate=0 to mean 'no due date', not epoch."""
        raw = _assignment_raw(duedate=0)
        item = normalizer.normalize(raw, source="assignment")
        assert item.deadline is None

    def test_raw_payload_envelope(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw()
        item = normalizer.normalize(raw, source="assignment")
        payload = item.raw_payload
        assert payload["schema_version"] == "2"
        assert payload["source"] == "moodle_assign"
        assert payload["adapter_version"] == "1.0.0"
        assert payload["original"]["id"] == 5897

    def test_empty_intro(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw(intro="")
        item = normalizer.normalize(raw, source="assignment")
        assert item.body == ""

    def test_source_url_from_cmid(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw(cmid=41249)
        item = normalizer.normalize(raw, source="assignment")
        assert item.source_url == "https://moodle.test/mod/assign/view.php?id=41249"

    def test_source_url_none_without_cmid(self, normalizer: MoodleNormalizer) -> None:
        raw = _assignment_raw(cmid=None)
        item = normalizer.normalize(raw, source="assignment")
        assert item.source_url is None

    def test_source_url_none_without_base_url(self) -> None:
        normalizer = MoodleNormalizer(adapter_version="1.0.0", base_url="")
        raw = _assignment_raw(cmid=41249)
        item = normalizer.normalize(raw, source="assignment")
        assert item.source_url is None


# --- Forum discussion normalization ---


def _forum_discussion_raw(
    *,
    discussion_id: int = 101,
    name: str = "Welcome to the course",
    message: str = "<p>Welcome! Please read the <a href='#'>syllabus</a>.</p>",
    userfullname: str = "Prof. Schmidt",
    timemodified: int = 1710288000,
    course_name: str = "Core Algorithms",
    forum_name: str = "Announcements",
    course_id: int = 9296,
    attachments: list[dict] | None = None,
) -> dict:
    result: dict = {
        "discussion": discussion_id,
        "name": name,
        "message": message,
        "messageformat": 1,
        "userfullname": userfullname,
        "timemodified": timemodified,
        "created": 1710200000,
        "subject": name,
        "_course_name": course_name,
        "_forum_name": forum_name,
        "_course_id": course_id,
    }
    if attachments is not None:
        result["attachments"] = attachments
    return result


class TestForumDiscussionNormalization:
    def test_basic_fields(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw()
        item = normalizer.normalize(raw, source="forum_discussion")
        assert isinstance(item, NormalizedItem)
        assert item.external_id == "moodle_forum_101"
        assert item.platform == "moodle"
        assert item.content_type == ContentType.ANNOUNCEMENT
        assert item.title == "Welcome to the course"
        assert item.author == "Prof. Schmidt"
        assert item.course_name == "Core Algorithms"

    def test_html_stripping(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(message="<p>Hello <b>world</b></p>")
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.body == "Hello world"

    def test_published_at_from_timemodified(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(timemodified=1710288000)
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.published_at is not None
        assert item.published_at.year == 2024

    def test_raw_payload_source(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw()
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.raw_payload["source"] == "moodle_forum"

    def test_source_url_constructed(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw()
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.source_url == "https://moodle.test/mod/forum/discuss.php?d=101"

    def test_source_url_none_without_base_url(self) -> None:
        normalizer = MoodleNormalizer(adapter_version="1.0.0", base_url="")
        raw = _forum_discussion_raw()
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.source_url is None


class TestForumAttachments:
    def test_forum_attachment_extracted(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(
            discussion_id=42,
            attachments=[
                {
                    "filename": "report.pdf",
                    "fileurl": "https://moodle.test/pluginfile.php/report.pdf",
                    "mimetype": "application/pdf",
                    "filesize": 512000,
                }
            ],
        )
        item = normalizer.normalize(raw, source="forum_discussion")
        assert len(item.attachments) == 1
        att = item.attachments[0]
        assert isinstance(att, FileRef)
        assert att.filename == "report.pdf"
        assert att.download_url == "https://moodle.test/pluginfile.php/report.pdf"
        assert att.content_type == "application/pdf"
        assert att.size_bytes == 512000

    def test_forum_placeholder_attachment_dropped(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(
            discussion_id=99,
            attachments=[{"filename": "attachment"}],
        )
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.attachments == []

    def test_forum_no_attachments(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(discussion_id=7)
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.attachments == []

    def test_forum_empty_attachments(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(discussion_id=8, attachments=[])
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.attachments == []

    def test_forum_mixed_attachments(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(
            discussion_id=55,
            attachments=[
                {
                    "filename": "valid.pdf",
                    "fileurl": "https://moodle.test/a.pdf",
                    "mimetype": "application/pdf",
                },
                {"filename": "attachment"},  # placeholder
                {
                    "filename": "also_valid.docx",
                    "fileurl": "https://moodle.test/b.docx",
                    "mimetype": (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                },
            ],
        )
        item = normalizer.normalize(raw, source="forum_discussion")
        assert len(item.attachments) == 2
        filenames = [a.filename for a in item.attachments]
        assert "valid.pdf" in filenames
        assert "also_valid.docx" in filenames

    def test_forum_attachment_external_id(self, normalizer: MoodleNormalizer) -> None:
        raw = _forum_discussion_raw(
            discussion_id=123,
            attachments=[
                {
                    "filename": "diagram.png",
                    "fileurl": "https://moodle.test/img.png",
                    "mimetype": "image/png",
                    "filesize": 12345,
                }
            ],
        )
        item = normalizer.normalize(raw, source="forum_discussion")
        assert item.attachments[0].external_id == "moodle_forum_attach_123_diagram.png"

    def test_forum_placeholder_with_valid_url_kept(self, normalizer: MoodleNormalizer) -> None:
        """Placeholder with valid fileurl is KEPT — only drop when both url AND mime are falsy."""
        raw = _forum_discussion_raw(
            discussion_id=77,
            attachments=[
                {
                    "filename": "attachment",
                    "fileurl": "https://moodle.test/real.pdf",
                    "mimetype": "application/pdf",
                    "filesize": 999,
                }
            ],
        )
        item = normalizer.normalize(raw, source="forum_discussion")
        assert len(item.attachments) == 1
        assert item.attachments[0].filename == "attachment"
        assert item.attachments[0].download_url == "https://moodle.test/real.pdf"


# --- Material normalization (course content modules) ---


def _material_raw(
    *,
    module_id: int = 40000,
    name: str = "Lecture 1 Slides",
    modname: str = "resource",
    description: str = "<p>Slides for lecture 1</p>",
    course_name: str = "JVM Languages",
    course_id: int = 9295,
    timemodified: int = 1710288000,
    url: str | None = "https://moodle.test/mod/resource/view.php?id=40000",
) -> dict:
    raw: dict = {
        "id": module_id,
        "name": name,
        "modname": modname,
        "instance": 3000,
        "description": description,
        "contents": [
            {
                "type": "file",
                "filename": "lecture1.pdf",
                "filesize": 2048000,
                "fileurl": "https://moodle.test/file.php/lecture1.pdf?forcedownload=1",
                "mimetype": "application/pdf",
            }
        ],
        "_course_name": course_name,
        "_course_id": course_id,
        "timemodified": timemodified,
    }
    if url is not None:
        raw["url"] = url
    return raw


class TestMaterialNormalization:
    def test_basic_fields(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw()
        item = normalizer.normalize(raw, source="material")
        assert isinstance(item, NormalizedItem)
        assert item.external_id == "moodle_material_40000"
        assert item.platform == "moodle"
        assert item.content_type == ContentType.MATERIAL
        assert item.title == "Lecture 1 Slides"
        assert item.course_name == "JVM Languages"

    def test_source_url_from_raw(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw(url="https://moodle.test/mod/resource/view.php?id=40000")
        item = normalizer.normalize(raw, source="material")
        assert item.source_url == "https://moodle.test/mod/resource/view.php?id=40000"

    def test_material_published_at_from_timemodified(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw(timemodified=1710288000)
        item = normalizer.normalize(raw, source="material")
        assert item.published_at is not None
        assert item.published_at.year == 2024

    def test_source_url_none_when_missing(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw(url=None)
        item = normalizer.normalize(raw, source="material")
        assert item.source_url is None

    def test_html_stripping_description(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw(description="<p>Read <em>chapter 3</em></p>")
        item = normalizer.normalize(raw, source="material")
        assert item.body == "Read chapter 3"

    def test_empty_description(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw(description="")
        item = normalizer.normalize(raw, source="material")
        assert item.body == ""

    def test_file_attachments_extracted(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw()
        item = normalizer.normalize(raw, source="material")
        assert len(item.attachments) == 1
        att = item.attachments[0]
        assert isinstance(att, FileRef)
        assert att.filename == "lecture1.pdf"
        assert att.content_type == "application/pdf"
        assert att.size_bytes == 2048000

    def test_raw_payload_source(self, normalizer: MoodleNormalizer) -> None:
        raw = _material_raw()
        item = normalizer.normalize(raw, source="material")
        assert item.raw_payload["source"] == "moodle_material"

    def test_material_course_context_populated(self, normalizer: MoodleNormalizer) -> None:
        """Material with _course_name and _course_id produces correct course fields."""
        raw = _material_raw(course_name="Advanced Python", course_id=12345)
        item = normalizer.normalize(raw, source="material")
        assert item.course_name == "Advanced Python"
        assert item.course_external_id == "12345"

    def test_material_without_course_context(self, normalizer: MoodleNormalizer) -> None:
        """Material without _course_name/_course_id produces None, not empty string."""
        # Simulate adapter not injecting context (dict without underscore keys)
        raw = {
            "id": 40000,
            "name": "Lecture 1 Slides",
            "modname": "resource",
            "instance": 3000,
            "description": "<p>Test</p>",
        }
        item = normalizer.normalize(raw, source="material")
        assert item.course_name is None
        assert item.course_external_id is None


# --- Calendar event normalization ---


def _event_raw(
    *,
    event_id: int = 5180405,
    name: str = "HW5 Building the Hack Computer is due",
    description: str = "",
    timesort: int = 1773961140,
    course_fullname: str = "DSCA Tutorial",
    course_id: int = 9273,
    timemodified: int = 1710288000,
    url: str | None = "https://moodle.test/mod/assign/view.php?id=41249",
) -> dict:
    raw: dict = {
        "id": event_id,
        "name": name,
        "description": description,
        "descriptionformat": 1,
        "timesort": timesort,
        "timemodified": timemodified,
        "course": {"id": course_id, "fullname": course_fullname},
        "action": {"name": "Add submission", "url": "https://moodle.test/mod/assign/view.php"},
    }
    if url is not None:
        raw["url"] = url
    return raw


class TestEventNormalization:
    def test_basic_fields(self, normalizer: MoodleNormalizer) -> None:
        raw = _event_raw()
        item = normalizer.normalize(raw, source="event")
        assert isinstance(item, NormalizedItem)
        assert item.external_id == "moodle_event_5180405"
        assert item.platform == "moodle"
        assert item.content_type == ContentType.EVENT
        assert item.title == "HW5 Building the Hack Computer is due"
        assert item.course_name == "DSCA Tutorial"

    def test_start_time_from_timesort(self, normalizer: MoodleNormalizer) -> None:
        raw = _event_raw(timesort=1773961140)
        item = normalizer.normalize(raw, source="event")
        assert item.start_time is not None
        assert item.start_time.year == 2026

    def test_event_published_at_from_timemodified(self, normalizer: MoodleNormalizer) -> None:
        raw = _event_raw(timemodified=1710288000)
        item = normalizer.normalize(raw, source="event")
        assert item.published_at is not None
        assert item.published_at.year == 2024

    def test_source_url_from_raw(self, normalizer: MoodleNormalizer) -> None:
        raw = _event_raw(url="https://moodle.test/mod/assign/view.php?id=41249")
        item = normalizer.normalize(raw, source="event")
        assert item.source_url == "https://moodle.test/mod/assign/view.php?id=41249"

    def test_source_url_none_when_missing(self, normalizer: MoodleNormalizer) -> None:
        raw = _event_raw(url=None)
        item = normalizer.normalize(raw, source="event")
        assert item.source_url is None

    def test_raw_payload_source(self, normalizer: MoodleNormalizer) -> None:
        raw = _event_raw()
        item = normalizer.normalize(raw, source="event")
        assert item.raw_payload["source"] == "moodle_calendar"

    def test_event_deadline_has_no_end_time(self, normalizer: MoodleNormalizer) -> None:
        raw = _event_raw()
        raw["timeduration"] = 0
        item = normalizer.normalize(raw, source="event")
        assert item.end_time is None

    def test_event_with_timeduration_end_time_not_computed(
        self, normalizer: MoodleNormalizer
    ) -> None:
        # TODO: normalizer does not yet compute end_time from timeduration.
        # When implemented: end_time = start_time + timedelta(seconds=timeduration).
        # See data quality finding #5.
        raw = _event_raw()
        raw["timeduration"] = 3600  # 1 hour
        item = normalizer.normalize(raw, source="event")
        assert item.start_time is not None
        assert item.end_time is None  # Not yet implemented


# --- File ref extraction ---


class TestMakeFileRef:
    def test_basic_file_ref(self, normalizer: MoodleNormalizer) -> None:
        raw = {
            "type": "file",
            "filename": "notes.pdf",
            "filesize": 1024,
            "fileurl": "https://moodle.test/file.php/notes.pdf?forcedownload=1",
            "mimetype": "application/pdf",
        }
        ref = normalizer.make_file_ref(raw, module_id=40000)
        assert isinstance(ref, FileRef)
        assert ref.filename == "notes.pdf"
        assert ref.download_url == "https://moodle.test/file.php/notes.pdf?forcedownload=1"
        assert ref.content_type == "application/pdf"
        assert ref.size_bytes == 1024
        assert ref.is_media is False

    def test_media_file_flagged(self, normalizer: MoodleNormalizer) -> None:
        raw = {
            "type": "file",
            "filename": "demo.mp4",
            "filesize": 50000000,
            "fileurl": "https://moodle.test/file.php/demo.mp4",
            "mimetype": "video/mp4",
        }
        ref = normalizer.make_file_ref(raw, module_id=40001)
        assert ref.is_media is True


# --- Unknown source ---


class TestUnknownSource:
    def test_unknown_source_raises(self, normalizer: MoodleNormalizer) -> None:
        with pytest.raises(ValueError, match="Unknown source"):
            normalizer.normalize({}, source="bogus")
