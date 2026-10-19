"""Normalize raw Moodle API responses into NormalizedItem and FileRef."""

from __future__ import annotations

from pathlib import PurePosixPath

from ingestion.config import SKIP_MEDIA_EXTENSIONS
from ingestion.normalize import decode_entities, make_envelope, strip_html, timestamp_to_datetime
from ingestion.schemas import (
    ContentType,
    FileRef,
    NormalizedItem,
)


class MoodleNormalizer:
    """Convert raw Moodle API dicts to NormalizedItem and FileRef.

    Implements the Normalizer protocol from ports.
    """

    def __init__(
        self,
        adapter_version: str = "",
        skip_media_extensions: frozenset[str] = SKIP_MEDIA_EXTENSIONS,
        base_url: str = "",
    ) -> None:
        self._adapter_version = adapter_version
        self._skip_media_extensions = skip_media_extensions
        self._base_url = base_url.rstrip("/")

    def normalize(self, raw: dict, **context: object) -> NormalizedItem:
        """Dispatch to source-specific normalizer."""
        source = str(context.get("source", ""))
        if source == "assignment":
            return self._normalize_assignment(raw)
        if source == "forum_discussion":
            return self._normalize_forum_discussion(raw)
        if source == "material":
            return self._normalize_material(raw)
        if source == "event":
            return self._normalize_event(raw)
        msg = f"Unknown source: {source!r}"
        raise ValueError(msg)

    def make_file_ref(self, raw: dict, module_id: int = 0) -> FileRef:
        """Convert a Moodle course content file dict to a FileRef."""
        filename = str(raw.get("filename", ""))
        ext = PurePosixPath(filename).suffix.lower()
        return FileRef(
            external_id=f"moodle_file_{module_id}_{filename}",
            filename=filename,
            download_url=str(raw.get("fileurl", "")),
            content_type=raw.get("mimetype"),
            size_bytes=raw.get("filesize"),
            is_media=ext in self._skip_media_extensions,
        )

    def _normalize_assignment(self, raw: dict) -> NormalizedItem:
        assign_id = raw.get("id", 0)
        title = str(raw.get("name", ""))
        course_name = str(raw.get("_course_name", ""))
        course_id = str(raw.get("_course_id", ""))

        html_content = str(raw.get("intro", ""))
        body = strip_html(html_content)
        deadline = timestamp_to_datetime(raw.get("duedate"))
        published_at = timestamp_to_datetime(raw.get("timemodified"))

        cmid = raw.get("cmid")
        source_url = (
            f"{self._base_url}/mod/assign/view.php?id={cmid}" if self._base_url and cmid else None
        )

        return NormalizedItem(
            external_id=f"moodle_assign_{assign_id}",
            platform="moodle",
            content_type=ContentType.ASSIGNMENT,
            title=title or None,
            body=body,
            source_url=source_url,
            course_name=decode_entities(course_name) or None,
            course_external_id=course_id or None,
            published_at=published_at,
            deadline=deadline,
            raw_payload=make_envelope("moodle_assign", raw, self._adapter_version),
        )

    def _extract_forum_attachments(self, raw: dict, discussion_id: int) -> list[FileRef]:
        """Extract FileRef list from forum discussion attachments."""
        attachments: list[FileRef] = []
        for raw_attachment in raw.get("attachments", []):
            if not isinstance(raw_attachment, dict):
                continue
            filename = str(raw_attachment.get("filename", ""))
            fileurl = raw_attachment.get("fileurl")
            mimetype = raw_attachment.get("mimetype")
            # Skip placeholder: filename=="attachment" AND no fileurl AND no mimetype
            if filename == "attachment" and not fileurl and not mimetype:
                continue
            # Skip if filename is falsy
            if not filename:
                continue
            attachments.append(
                FileRef(
                    external_id=f"moodle_forum_attach_{discussion_id}_{filename}",
                    filename=filename,
                    download_url=str(fileurl) if fileurl else "",
                    content_type=mimetype,
                    size_bytes=raw_attachment.get("filesize"),
                )
            )
        return attachments

    def _normalize_forum_discussion(self, raw: dict) -> NormalizedItem:
        discussion_id = raw.get("discussion", 0)
        title = str(raw.get("name", ""))
        course_name = str(raw.get("_course_name", ""))
        course_id = str(raw.get("_course_id", ""))
        author = str(raw.get("userfullname", ""))

        html_content = str(raw.get("message", ""))
        body = strip_html(html_content)
        published_at = timestamp_to_datetime(raw.get("timemodified"))

        attachments = self._extract_forum_attachments(raw, discussion_id)

        source_url = (
            f"{self._base_url}/mod/forum/discuss.php?d={discussion_id}"
            if self._base_url and discussion_id
            else None
        )

        return NormalizedItem(
            external_id=f"moodle_forum_{discussion_id}",
            platform="moodle",
            content_type=ContentType.ANNOUNCEMENT,
            title=title or None,
            body=body,
            source_url=source_url,
            course_name=decode_entities(course_name) or None,
            course_external_id=course_id or None,
            author=author or None,
            published_at=published_at,
            raw_payload=make_envelope("moodle_forum", raw, self._adapter_version),
            attachments=attachments,
        )

    def _normalize_material(self, raw: dict) -> NormalizedItem:
        module_id = raw.get("id", 0)
        title = str(raw.get("name", ""))
        course_name = str(raw.get("_course_name", ""))
        course_id = str(raw.get("_course_id", ""))

        html_content = str(raw.get("description", "") or "")
        body = strip_html(html_content)
        published_at = timestamp_to_datetime(raw.get("timemodified"))

        attachments = [
            self.make_file_ref(c, module_id=module_id)
            for c in raw.get("contents", [])
            if isinstance(c, dict) and c.get("type") == "file"
        ]

        source_url = raw.get("url")

        return NormalizedItem(
            external_id=f"moodle_material_{module_id}",
            platform="moodle",
            content_type=ContentType.MATERIAL,
            title=title or None,
            body=body,
            source_url=str(source_url) if source_url else None,
            course_name=decode_entities(course_name) or None,
            course_external_id=course_id or None,
            published_at=published_at,
            raw_payload=make_envelope("moodle_material", raw, self._adapter_version),
            attachments=attachments,
        )

    def _normalize_event(self, raw: dict) -> NormalizedItem:
        event_id = raw.get("id", 0)
        title = str(raw.get("name", ""))

        course_data = raw.get("course") or {}
        course_name = str(course_data.get("fullname", ""))
        course_id = str(course_data.get("id", ""))

        description = str(raw.get("description", "") or "")
        body = strip_html(description)
        start_time = timestamp_to_datetime(raw.get("timesort"))
        published_at = timestamp_to_datetime(raw.get("timemodified"))

        source_url = raw.get("url")

        return NormalizedItem(
            external_id=f"moodle_event_{event_id}",
            platform="moodle",
            content_type=ContentType.EVENT,
            title=title or None,
            body=body,
            source_url=str(source_url) if source_url else None,
            course_name=decode_entities(course_name) or None,
            course_external_id=course_id or None,
            published_at=published_at,
            start_time=start_time,
            raw_payload=make_envelope("moodle_calendar", raw, self._adapter_version),
        )
