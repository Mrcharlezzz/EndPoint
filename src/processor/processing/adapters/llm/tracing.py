from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langsmith.wrappers import wrap_openai

from processing.models.envelope import ProcessingEnvelope


def maybe_wrap_openai_client(
    client: Any,
    *,
    wrap_client: Callable[[Any], Any] = wrap_openai,
) -> Any:
    """Always wrap the OpenAI client; LangSmith controls trace emission."""
    return wrap_client(client)


def build_langsmith_extra(
    envelope: ProcessingEnvelope,
    *,
    operation: str,
    classified_type: str | None = None,
    prompt_version: str | None = None,
    prompt_fingerprint: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "component": "processor",
        "operation": operation,
        "ingested_item_id": str(envelope.ingested_item.id),
        "external_id": envelope.ingested_item.external_id,
        "platform": envelope.ingested_item.platform,
        "course_external_id": envelope.ingested_item.course_external_id,
        "schema_version": envelope.ingested_item.schema_version,
        "adapter_version": envelope.ingested_item.adapter_version,
        "ingestion_mode": envelope.ingested_item.ingestion_mode,
        "entity_type": envelope.entity.entity_type,
    }
    if classified_type is not None:
        metadata["classified_type"] = classified_type
    if prompt_version is not None:
        metadata["prompt_version"] = prompt_version
    if prompt_fingerprint is not None:
        metadata["prompt_fingerprint"] = prompt_fingerprint

    tags = [
        "processor",
        "llm",
        operation,
        f"platform:{envelope.ingested_item.platform}",
        f"schema:{envelope.ingested_item.schema_version}",
    ]
    if prompt_version is not None:
        tags.append(f"prompt:{prompt_version}")

    return {
        "metadata": metadata,
        "tags": tags,
    }
