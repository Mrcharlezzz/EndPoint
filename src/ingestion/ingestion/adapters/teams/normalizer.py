"""Normalize raw Teams API responses into NormalizedItem and FileRef."""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import PurePosixPath

from ingestion.config import SKIP_MEDIA_EXTENSIONS
from ingestion.normalize import decode_entities, make_envelope, parse_iso_datetime, strip_html
from ingestion.schemas import (
    ContentType,
    FileRef,
    LinkRef,
    NormalizedItem,
)

_POINTS_RE = re.compile(r"Points:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


class TeamsNormalizer:
    """Convert raw Teams API dicts to NormalizedItem and FileRef.

    Implements the Normalizer protocol from ports.
    """

    def __init__(
        self,
        adapter_version: str = "",
        skip_media_extensions: frozenset[str] = SKIP_MEDIA_EXTENSIONS,
    ) -> None:
        self._adapter_version = adapter_version
        self._skip_media_extensions = skip_media_extensions

    def normalize(self, raw: dict, **context: object) -> NormalizedItem | None:
        """Dispatch to source-specific normalizer.

        Required context kwargs by source:
            csa:           team_name, channel_id
            graph_message: team_name, channel_id
            assignment:    class_name, class_id

        Returns None for platform-generated system events.
        """
        source = str(context.get("source", ""))
        if source == "csa":
            return self._normalize_csa_message(raw, **context)
        if source == "graph_message":
            return self._normalize_graph_message(raw, **context)
        if source == "assignment":
            return self._normalize_assignment(raw, **context)
        msg = f"Unknown source: {source!r}"
        raise ValueError(msg)

    def make_file_ref(self, raw: dict) -> FileRef:
        """Convert a Graph drive item dict to a FileRef."""
        name = str(raw.get("name", ""))
        ext = PurePosixPath(name).suffix.lower()
        return FileRef(
            external_id=str(raw.get("id", "")),
            filename=name,
            download_url=str(raw.get("@microsoft.graph.downloadUrl", "")),
            content_type=_get_mime_type(raw),
            size_bytes=raw.get("size"),
            is_media=ext in self._skip_media_extensions,
        )

    # --- Private normalizers ---

    def _normalize_csa_message(self, raw: dict, **context: object) -> NormalizedItem | None:
        msg_id = str(raw.get("id", ""))
        team_name = str(context.get("team_name", ""))
        channel_id = str(context.get("channel_id", ""))

        html_content = str(raw.get("content", ""))
        body = strip_html(html_content)
        if _is_system_message(body, message_type=str(raw.get("messageType", ""))):
            return None
        sender = str(raw.get("imDisplayName") or raw.get("fromDisplayNameInToken") or "")
        published_at = parse_iso_datetime(raw.get("composeTime") or raw.get("originalArrivalTime"))

        attachments = _extract_csa_attachments(raw)
        links = _extract_csa_links(raw)

        return NormalizedItem(
            external_id=f"teams_csa_{msg_id}",
            platform="teams",
            content_type=ContentType.RAW_MESSAGE,
            body=body,
            course_name=decode_entities(team_name) or None,
            course_external_id=channel_id or None,
            author=sender,
            published_at=published_at,
            raw_payload=make_envelope("teams_csa", raw, self._adapter_version),
            attachments=attachments,
            links=links,
        )

    def _normalize_graph_message(self, raw: dict, **context: object) -> NormalizedItem | None:
        if raw.get("messageType") == _GRAPH_SYSTEM_EVENT_TYPE:
            return None
        msg_id = str(raw.get("id", ""))
        team_name = str(context.get("team_name", ""))
        channel_id = str(context.get("channel_id", ""))

        body_data = raw.get("body") or {}
        html_content = str(body_data.get("content", ""))
        body = strip_html(html_content)

        from_data = raw.get("from") or {}
        user = from_data.get("user") or {} if isinstance(from_data, dict) else {}
        sender = str(user.get("displayName", "")) if isinstance(user, dict) else ""

        published_at = parse_iso_datetime(raw.get("createdDateTime"))

        return NormalizedItem(
            external_id=f"teams_graph_{msg_id}",
            platform="teams",
            content_type=ContentType.RAW_MESSAGE,
            body=body,
            course_name=decode_entities(team_name) or None,
            course_external_id=channel_id or None,
            author=sender,
            published_at=published_at,
            raw_payload=make_envelope("teams_graph", raw, self._adapter_version),
        )

    def _normalize_assignment(self, raw: dict, **context: object) -> NormalizedItem:
        assign_id = str(raw.get("id", ""))
        class_name = str(context.get("class_name", ""))
        class_id = str(context.get("class_id", ""))
        title = str(raw.get("displayName", ""))

        instructions_data = raw.get("instructions")
        html_content = ""
        if isinstance(instructions_data, dict):
            html_content = str(instructions_data.get("content", ""))
        body = strip_html(html_content)

        # NOTE(v5): keep legacy grading-signal extraction logic available for
        # future reuse/refactoring, but do not emit these values into
        # NormalizedItem or mutate the normalized body. The active ingestion
        # contract intentionally excludes grading/status fields.
        _extract_assignment_grading_signals(raw, body)

        deadline = parse_iso_datetime(raw.get("dueDateTime"))
        published_at = parse_iso_datetime(raw.get("assignedDateTime") or raw.get("assignDateTime"))

        # Build attachments from _resources (injected by adapter)
        attachments = _extract_assignment_resources(raw.get("_resources", []))

        web_url = raw.get("webUrl")

        return NormalizedItem(
            external_id=f"teams_assignment_{assign_id}",
            platform="teams",
            content_type=ContentType.ASSIGNMENT,
            title=title or None,
            body=body,
            source_url=str(web_url) if web_url else None,
            course_name=decode_entities(class_name) or None,
            course_external_id=class_id or None,
            published_at=published_at,
            deadline=deadline,
            raw_payload=make_envelope("teams_edu", raw, self._adapter_version),
            attachments=attachments,
        )


# --- Helpers ---


_CALL_EVENT_MARKERS = ("callStarted", "callEnded")
_CALL_RECORDING_MESSAGE_TYPE = "RichText/Media_CallRecording"
_CALL_RECORDING_TITLES = ("Meeting in GeneralPlay",)
_CARD_PLACEHOLDER_URL = "go.skype.com/cards.unsupported"
_GRAPH_SYSTEM_EVENT_TYPE = "systemEventMessage"
_SYSTEM_JSON_MARKERS = (
    '"scopeId"',
    '"storageId"',
    '"callId"',
    r"\"scopeId\"",
    r"\"storageId\"",
    r"\"callId\"",
)

# Observed in Teams CSA traffic — these URLs appear in auto-generated cards,
# call routing, and internal Skype bridge payloads. Not user-facing content.
_INTERNAL_URL_PATTERNS = (
    "flightproxy.teams.microsoft.com",
    ".conv.skype.com",
    "go.skype.com/cards.unsupported",
)


def _extract_assignment_grading_signals(
    raw: dict, body: str
) -> tuple[float | None, float | None, str]:
    """Return legacy Teams grading signals without wiring them into the schema.

    TODO(v5): move this into a dedicated legacy/compatibility module or delete
    it once the team is sure no future migration or downstream reuse needs the
    preserved parsing behavior.
    """

    points_match = _POINTS_RE.search(body)
    body_points = float(points_match.group(1)) if points_match else None

    grading = raw.get("grading") or {}
    raw_max = grading.get("maxPoints")
    max_points = float(raw_max) if isinstance(raw_max, (int, float)) else None

    fallback_body = body
    if not fallback_body and max_points is not None:
        fallback_body = f"Points: {max_points}"

    return max_points, body_points, fallback_body


def _is_internal_url(url: str) -> bool:
    """Return True if the URL matches a known Teams-internal pattern."""
    lower = url.lower()
    return any(pattern in lower for pattern in _INTERNAL_URL_PATTERNS)


def _extract_csa_links(raw: dict) -> list[LinkRef]:
    """Extract LinkRef list from CSA message properties.links, filtering internal URLs."""
    links: list[LinkRef] = []
    props = raw.get("properties") or {}
    raw_links = props.get("links", "")
    if not raw_links:
        return []
    try:
        parsed = json.loads(str(raw_links))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        if _is_internal_url(str(url)):
            continue
        title = item.get("title")
        links.append(LinkRef(url=str(url), label=str(title) if title else None))
    return links


def _is_system_message(body: str, *, message_type: str = "") -> bool:
    """Return True if the message is a platform-generated system event."""
    if message_type == _CALL_RECORDING_MESSAGE_TYPE:
        return True
    if any(marker in body for marker in _CALL_EVENT_MARKERS):
        return True
    if body == "Scheduled a meeting":
        return True
    if body in _CALL_RECORDING_TITLES:
        return True
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "scopeId" in parsed:
                return True
        except (json.JSONDecodeError, ValueError):
            return any(marker in body for marker in _SYSTEM_JSON_MARKERS)
    return _CARD_PLACEHOLDER_URL in body


def _extract_csa_attachments(raw: dict) -> list[FileRef]:
    """Extract FileRef list from CSA message properties.files."""
    attachments: list[FileRef] = []
    props = raw.get("properties") or {}
    for f in props.get("files") or []:
        if not isinstance(f, dict):
            continue
        file_info = f.get("fileInfo") if isinstance(f.get("fileInfo"), dict) else {}
        attachments.append(
            FileRef(
                external_id=str(f.get("id", "")),
                filename=str(f.get("title") or f.get("fileName") or ""),
                download_url=str(f.get("objectUrl", "")),
                content_type=str(f.get("fileType", "")),
                size_bytes=file_info.get("sizeInBytes") if file_info else None,
            )
        )
    return attachments


def _extract_assignment_resources(resources: list[dict]) -> list[FileRef]:
    """Extract FileRef list from assignment _resources (injected by adapter)."""
    attachments: list[FileRef] = []
    for res in resources:
        name = str(res.get("displayName", ""))
        file_url = str(res.get("fileUrl", ""))
        if not name and not file_url:
            continue
        attachments.append(
            FileRef(
                external_id=file_url or name,
                filename=name,
                download_url=file_url,
                content_type=_mime_from_extension(name),
            )
        )
    return attachments


def _mime_from_extension(filename: str) -> str | None:
    """Infer MIME type from filename extension using stdlib mimetypes."""
    if not filename:
        return None
    mime, _ = mimetypes.guess_type(filename)
    return mime


def _get_mime_type(raw: dict) -> str | None:
    """Extract MIME type from a Graph drive item."""
    file_data = raw.get("file")
    if isinstance(file_data, dict):
        return file_data.get("mimeType")
    return None
