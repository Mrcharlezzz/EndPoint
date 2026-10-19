"""Tests for Teams normalizer — raw API dicts → NormalizedItem + FileRef."""

from __future__ import annotations

import pytest
from ingestion.adapters.teams.normalizer import TeamsNormalizer
from ingestion.schemas import ContentType, FileRef, LinkRef, NormalizedItem


@pytest.fixture
def normalizer() -> TeamsNormalizer:
    return TeamsNormalizer(adapter_version="1.0.0")


# --- CSA message normalization ---


def _csa_message_raw(
    *,
    msg_id: str = "msg-1",
    sender: str = "Alice",
    content: str = "<p>Hello world</p>",
    compose_time: str = "2026-03-12T10:00:00Z",
) -> dict:
    return {
        "id": msg_id,
        "imDisplayName": sender,
        "content": content,
        "messageType": "RichText/Html",
        "composeTime": compose_time,
        "containerId": "chan-1",
        "properties": {},
    }


class TestCSAMessageNormalization:
    def test_basic_fields(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        item = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert isinstance(item, NormalizedItem)
        assert item.external_id == "teams_csa_msg-1"
        assert item.platform == "teams"
        assert item.content_type == ContentType.RAW_MESSAGE
        assert item.author == "Alice"
        assert item.course_name == "CS101"
        assert item.course_external_id == "chan-1"

    def test_html_stripping(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content="<p>Hello <b>world</b></p>")
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.body == "Hello world"

    def test_raw_payload_envelope(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        payload = item.raw_payload
        assert payload["schema_version"] == "2"
        assert payload["source"] == "teams_csa"
        assert payload["adapter_version"] == "1.0.0"
        assert payload["original"]["id"] == "msg-1"

    def test_published_at_parsed(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(compose_time="2026-03-12T10:00:00Z")
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.published_at is not None
        assert item.published_at.year == 2026

    def test_csa_attachments_to_file_refs(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {
            "files": [
                {
                    "id": "file-1",
                    "title": "notes.pdf",
                    "objectUrl": "https://example.com/notes.pdf",
                    "fileType": "application/pdf",
                    "fileInfo": {"sizeInBytes": 1024},
                }
            ]
        }
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert len(item.attachments) == 1
        att = item.attachments[0]
        assert isinstance(att, FileRef)
        assert att.filename == "notes.pdf"
        assert att.download_url == "https://example.com/notes.pdf"
        assert att.size_bytes == 1024

    def test_empty_content(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content="")
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.body == ""


# --- Graph message normalization ---


def _graph_message_raw(
    *,
    msg_id: str = "graph-1",
    sender: str = "Bob",
    content: str = "<div>Test</div>",
    created: str = "2026-03-12T11:00:00Z",
) -> dict:
    return {
        "id": msg_id,
        "from": {"user": {"displayName": sender}},
        "body": {"content": content, "contentType": "html"},
        "createdDateTime": created,
        "attachments": [],
    }


class TestGraphMessageNormalization:
    def test_basic_fields(self, normalizer: TeamsNormalizer) -> None:
        raw = _graph_message_raw()
        item = normalizer.normalize(
            raw, source="graph_message", team_name="CS102", channel_id="ch-2"
        )
        assert item.external_id == "teams_graph_graph-1"
        assert item.platform == "teams"
        assert item.content_type == ContentType.RAW_MESSAGE
        assert item.author == "Bob"
        assert item.course_name == "CS102"

    def test_html_stripping(self, normalizer: TeamsNormalizer) -> None:
        raw = _graph_message_raw(content="<div>Hello <em>world</em></div>")
        item = normalizer.normalize(raw, source="graph_message", team_name="T", channel_id="c")
        assert item.body == "Hello world"

    def test_missing_sender(self, normalizer: TeamsNormalizer) -> None:
        raw = _graph_message_raw()
        raw["from"] = None
        item = normalizer.normalize(raw, source="graph_message", team_name="T", channel_id="c")
        assert item.author == ""


# --- System message filtering ---


class TestSystemMessageFiltering:
    def test_skips_call_event_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(
            content="<p>callStarted orgid:abc flightproxy.teams.microsoft.com</p>"
        )
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_call_ended_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content="<p>callEnded orgid:abc flightproxy.teams.microsoft.com</p>")
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_calendar_blob(self, normalizer: TeamsNormalizer) -> None:
        content = "<p>user@example.com Scheduled 2026-04-05T10:00:00Z callStarted</p>"
        raw = _csa_message_raw(content=content)
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_bare_scheduled_meeting_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content="Scheduled a meeting", sender="")
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_call_recording_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(
            content=(
                '<URIObject type="Video.2/CallRecording.1">'
                "<Title>Meeting in General</Title>"
                '<a href="">Play</a></URIObject>'
            ),
            sender="",
        )
        raw["messageType"] = "RichText/Media_CallRecording"
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_scope_json_blob(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content='{"scopeId":"abc-123","type":"storage"}')
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_escaped_scope_json_blob(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(
            content=r"{\"scopeId\":\"abc-123\",\"storageId\":\"file-1\",\"callId\":\"call-1\"}"
        )
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_bare_meeting_generalplay_recording_title(
        self, normalizer: TeamsNormalizer
    ) -> None:
        raw = _csa_message_raw(content="Meeting in GeneralPlay", sender="")
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_skips_assignment_card_placeholder(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(
            content="<p>This content requires go.skype.com/cards.unsupported</p>"
        )
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert result is None

    def test_keeps_legitimate_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content="<p>Hello, please submit your assignment by Friday.</p>")
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert isinstance(result, NormalizedItem)
        assert result.body == "Hello, please submit your assignment by Friday."

    def test_skips_graph_system_event_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _graph_message_raw()
        raw["messageType"] = "systemEventMessage"
        result = normalizer.normalize(
            raw, source="graph_message", team_name="CS101", channel_id="chan-1"
        )
        assert result is None

    def test_keeps_graph_normal_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _graph_message_raw(content="<p>Regular user message</p>")
        result = normalizer.normalize(
            raw, source="graph_message", team_name="CS101", channel_id="chan-1"
        )
        assert isinstance(result, NormalizedItem)

    def test_keeps_empty_body_message(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content="")
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert isinstance(result, NormalizedItem)

    def test_keeps_json_without_scope_id(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw(content='{"type":"user_message","text":"hello"}')
        result = normalizer.normalize(raw, source="csa", team_name="CS101", channel_id="chan-1")
        assert isinstance(result, NormalizedItem)


# --- Assignment normalization ---


def _assignment_raw(
    *,
    assign_id: str = "asgn-1",
    name: str = "Homework 1",
    instructions: str = "<p>Do problems 1-5</p>",
    due: str = "2026-03-20T23:59:00Z",
    web_url: str | None = "https://teams.microsoft.com/l/entity/66aeee93/classroom?context=xyz",
) -> dict:
    raw: dict = {
        "id": assign_id,
        "displayName": name,
        "instructions": {"content": instructions, "contentType": "html"},
        "dueDateTime": due,
        "resources": [{"displayName": "handout.pdf"}, {"id": "res-2"}],
    }
    if web_url is not None:
        raw["webUrl"] = web_url
    return raw


class TestAssignmentNormalization:
    def test_basic_fields(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        item = normalizer.normalize(
            raw, source="assignment", class_name="Algorithms", class_id="cls-1"
        )
        assert item.external_id == "teams_assignment_asgn-1"
        assert item.content_type == ContentType.ASSIGNMENT
        assert item.title == "Homework 1"
        assert item.course_name == "Algorithms"
        assert item.course_external_id == "cls-1"

    def test_html_stripped_from_instructions(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw(instructions="<p>Do <b>problems</b> 1-5</p>")
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.body == "Do problems 1-5"

    def test_deadline_parsed(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw(due="2026-03-20T23:59:00Z")
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.deadline is not None
        assert item.deadline.day == 20

    def test_published_at_parsed(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        raw["assignedDateTime"] = "2026-03-12T10:00:00Z"
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.published_at is not None
        assert item.published_at.year == 2026

    def test_missing_instructions(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        raw["instructions"] = None
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.body == ""

    def test_raw_payload_source(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.raw_payload["source"] == "teams_edu"

    def test_source_url_from_web_url(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw(web_url="https://teams.microsoft.com/l/entity/abc/classroom")
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.source_url == "https://teams.microsoft.com/l/entity/abc/classroom"

    def test_source_url_none_when_missing(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw(web_url=None)
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.source_url is None


class TestAssignmentSchemaContract:
    def test_grading_data_is_not_promoted_to_schema_fields(
        self, normalizer: TeamsNormalizer
    ) -> None:
        raw = _assignment_raw()
        raw["grading"] = {"maxPoints": 100.0}
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert not hasattr(item, "max_points")
        assert not hasattr(item, "points")

    def test_points_text_remains_body_content(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw(instructions="<p>This assignment is worth Points: 85</p>")
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.body == "This assignment is worth Points: 85"
        assert not hasattr(item, "points")

    def test_missing_instructions_do_not_create_points_fallback(
        self, normalizer: TeamsNormalizer
    ) -> None:
        raw = _assignment_raw()
        raw["instructions"] = None
        raw["grading"] = {"maxPoints": 50.0}
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert item.body == ""
        assert not hasattr(item, "max_points")


# --- File normalization ---


def _drive_item_raw(
    *,
    item_id: str = "file-1",
    name: str = "lecture.pdf",
    size: int = 2048,
    download_url: str = "https://example.com/dl",
    web_url: str = "https://example.com/web",
) -> dict:
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "@microsoft.graph.downloadUrl": download_url,
        "webUrl": web_url,
        "file": {"mimeType": "application/pdf"},
    }


class TestFileNormalization:
    def test_make_file_ref(self, normalizer: TeamsNormalizer) -> None:
        raw = _drive_item_raw()
        ref = normalizer.make_file_ref(raw)
        assert isinstance(ref, FileRef)
        assert ref.external_id == "file-1"
        assert ref.filename == "lecture.pdf"
        assert ref.download_url == "https://example.com/dl"
        assert ref.size_bytes == 2048
        assert ref.is_media is False

    def test_media_file_flagged(self, normalizer: TeamsNormalizer) -> None:
        raw = _drive_item_raw(name="video.mp4")
        ref = normalizer.make_file_ref(raw)
        assert ref.is_media is True

    def test_custom_skip_extensions(self) -> None:
        norm = TeamsNormalizer(
            adapter_version="1.0.0",
            skip_media_extensions=frozenset({".wav"}),
        )
        raw = _drive_item_raw(name="audio.wav")
        ref = norm.make_file_ref(raw)
        assert ref.is_media is True

        raw2 = _drive_item_raw(name="video.mp4")
        ref2 = norm.make_file_ref(raw2)
        assert ref2.is_media is False  # .mp4 not in custom set


# --- Unknown source ---


class TestUnknownSource:
    def test_unknown_source_raises(self, normalizer: TeamsNormalizer) -> None:
        with pytest.raises(ValueError, match="Unknown source"):
            normalizer.normalize({}, source="bogus")


# --- Assignment resource MIME type inference ---


class TestAssignmentResourceMimeType:
    def test_assignment_resource_pdf_mime(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        raw["_resources"] = [
            {
                "displayName": "Homework_9.pdf",
                "fileUrl": "https://example.com/Homework_9.pdf",
                "@odata.type": "#microsoft.education.assignments.api.educationFileResource",
            }
        ]
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert len(item.attachments) == 1
        assert item.attachments[0].content_type == "application/pdf"

    def test_assignment_resource_pptx_mime(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        raw["_resources"] = [
            {
                "displayName": "slides.pptx",
                "fileUrl": "https://example.com/slides.pptx",
                "@odata.type": "#microsoft.education.assignments.api.educationFileResource",
            }
        ]
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert len(item.attachments) == 1
        assert (
            item.attachments[0].content_type
            == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )

    def test_assignment_resource_no_extension(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        raw["_resources"] = [
            {
                "displayName": "notes",
                "fileUrl": "https://example.com/notes",
                "@odata.type": "#microsoft.education.assignments.api.educationFileResource",
            }
        ]
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert len(item.attachments) == 1
        assert item.attachments[0].content_type is None

    def test_assignment_resource_empty_filename(self, normalizer: TeamsNormalizer) -> None:
        raw = _assignment_raw()
        raw["_resources"] = [
            {
                "displayName": "",
                "fileUrl": "https://example.com/file",
                "@odata.type": "#microsoft.education.assignments.api.educationFileResource",
            }
        ]
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert len(item.attachments) == 1
        assert item.attachments[0].content_type is None

    def test_assignment_resource_no_odata_type_in_content_type(
        self, normalizer: TeamsNormalizer
    ) -> None:
        raw = _assignment_raw()
        raw["_resources"] = [
            {
                "displayName": "hw.pdf",
                "fileUrl": "https://example.com/hw.pdf",
                "@odata.type": "#microsoft.education.assignments.api.educationFileResource",
            }
        ]
        item = normalizer.normalize(raw, source="assignment", class_name="A", class_id="c")
        assert len(item.attachments) == 1
        # Ensure @odata.type string is NOT stored in content_type
        assert "#microsoft" not in (item.attachments[0].content_type or "")


# --- CSA link extraction ---


class TestCSALinkExtraction:
    def test_extracts_sharepoint_link(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {
            "links": '[{"@type":"http://schema.skype.com/HyperLink","title":"Final_Exam_2025.pdf","url":"https://constructoruniversity.sharepoint.com/sites/f25_ctms22/Final_Exam_2025.pdf"}]'
        }
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert len(item.links) == 1
        link = item.links[0]
        assert isinstance(link, LinkRef)
        assert (
            link.url
            == "https://constructoruniversity.sharepoint.com/sites/f25_ctms22/Final_Exam_2025.pdf"
        )
        assert link.label == "Final_Exam_2025.pdf"

    def test_filters_flightproxy_url(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {
            "links": '[{"@type":"http://schema.skype.com/HyperLink","title":"Call","url":"https://api.flightproxy.teams.microsoft.com/api/v2/ep/abc123"}]'
        }
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links == []

    def test_filters_skype_cards_url(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {
            "links": '[{"@type":"http://schema.skype.com/HyperLink","title":"Unsupported","url":"https://go.skype.com/cards.unsupported"}]'
        }
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links == []

    def test_mixed_links_filtered(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {
            "links": '[{"@type":"http://schema.skype.com/HyperLink","title":"Internal","url":"https://api.flightproxy.teams.microsoft.com/ep/xyz"},{"@type":"http://schema.skype.com/HyperLink","title":"Exam","url":"https://constructoruniversity.sharepoint.com/sites/f25_ctms22/Exam.pdf"}]'
        }
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert len(item.links) == 1
        assert (
            item.links[0].url
            == "https://constructoruniversity.sharepoint.com/sites/f25_ctms22/Exam.pdf"
        )

    def test_empty_links_json(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {"links": "[]"}
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links == []

    def test_missing_links_key(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {}
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links == []

    def test_invalid_links_json(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {"links": "not valid json"}
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links == []

    def test_link_label_from_title(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {
            "links": '[{"@type":"http://schema.skype.com/HyperLink","title":"Assignment.pdf","url":"https://example.com/Assignment.pdf"}]'
        }
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links[0].label == "Assignment.pdf"

    def test_link_no_title(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {
            "links": '[{"@type":"http://schema.skype.com/HyperLink","url":"https://example.com/file.pdf"}]'
        }
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert len(item.links) == 1
        assert item.links[0].url == "https://example.com/file.pdf"
        assert item.links[0].label is None

    def test_links_property_is_not_string(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        raw["properties"] = {"links": 123}
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links == []

    def test_empty_properties(self, normalizer: TeamsNormalizer) -> None:
        raw = _csa_message_raw()
        item = normalizer.normalize(raw, source="csa", team_name="T", channel_id="c")
        assert item.links == []
