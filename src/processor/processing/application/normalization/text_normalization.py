from __future__ import annotations

import re

from processing.models.envelope import ProcessingEnvelope


class TextNormalizer:
    """Applies generic text cleanup to already-canonical entity fields."""

    def normalize_body(self, body: str | None) -> str | None:
        if body is None:
            return None
        normalized = (
            body.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
        )
        normalized = re.sub(r"[ \t]+", " ", normalized)
        normalized = re.sub(r" *\n *", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        normalized = normalized.strip()
        return normalized or None

    def normalize_title(self, title: str | None) -> str | None:
        if title is None:
            return None
        normalized = re.sub(r"\s+", " ", title).strip()
        return normalized or None

    def normalize_author(self, author: str | None) -> str | None:
        if author is None:
            return None
        normalized = re.sub(r"\s+", " ", author).strip()
        return normalized or None

    def normalize_envelope(self, envelope: ProcessingEnvelope) -> ProcessingEnvelope:
        new_entity = envelope.entity.model_copy(
            update={
                "title": self.normalize_title(envelope.entity.title),
                "body": self.normalize_body(envelope.entity.body),
                "author": self.normalize_author(envelope.entity.author),
            }
        )
        return envelope.model_copy(update={"entity": new_entity})
