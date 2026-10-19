"""Shared normalizer helpers used by all platform normalizers.

Consolidates duplicated logic: HTML stripping, datetime parsing, envelope wrapping.
"""

from __future__ import annotations

import html as _html_stdlib
import re
from datetime import UTC, datetime

from ingestion.schemas import RawPayloadEnvelope

_STYLE_SCRIPT_RE = re.compile(
    r"<(style|script)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HORIZ_WHITESPACE_RE = re.compile(r"[ \t\xa0]+")
_BLANK_COLLAPSE_RE = re.compile(r"\n{3,}")


def strip_html(html: str) -> str:
    """Return clean plain text from an HTML string.

    Processing order:
    1. Remove <style> and <script> blocks (tags + content).
    2. Remove remaining HTML tags.
    3. Decode HTML entities (&amp; → &, &nbsp; → space, etc.).
    4. Normalise whitespace: CRLF/CR → LF, collapse horizontal
       whitespace per line, collapse runs of blank lines to a single
       blank line (preserving paragraph breaks), strip leading/trailing
       blank lines.
    """
    text = _STYLE_SCRIPT_RE.sub("", html)
    text = _HTML_TAG_RE.sub("", text)
    text = _html_stdlib.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_HORIZ_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_COLLAPSE_RE.sub("\n\n", text)
    return text.strip()


def decode_entities(text: str) -> str:
    """Decode HTML entities in a plain-text string.

    Only decodes entities and strips leading/trailing whitespace.
    Does NOT remove HTML tags or normalise internal whitespace.
    """
    return _html_stdlib.unescape(text).strip()


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string, returning None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def timestamp_to_datetime(ts: int | None) -> datetime | None:
    """Convert a Unix timestamp to a timezone-aware datetime.

    Returns None if ts is None or 0. Moodle uses 0 to mean "not set"
    (e.g. duedate=0 means no due date), and epoch would confuse downstream logic.
    """
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=UTC)


def make_envelope(source: str, original: dict, adapter_version: str) -> dict:
    """Wrap raw API payload in a RawPayloadEnvelope and return as dict."""
    envelope = RawPayloadEnvelope(
        source=source,
        ingestion_mode="server_poll",
        adapter_version=adapter_version,
        original=original,
    )
    return envelope.model_dump(mode="json")
