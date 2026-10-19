# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from processing.adapters.postgres import mapper
from processing.application.normalization.text_normalization import TextNormalizer
from processing.models.classification import ClassificationLogEntry
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import (
    Announcement,
    Assignment,
    Event,
    Material,
    RawMessage,
)
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)
from processing.models.invariants import (
    InvariantWarning,
    Severity,
    check_envelope,
)

SIMULATED_RESOLVED_COURSE_ID = UUID("00000000-0000-0000-0000-000000000001")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan processor data quality.")
    parser.add_argument(
        "--source",
        choices=("db", "fixtures"),
        required=True,
        help="Data source to scan.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Fixture JSON path for --source fixtures.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show warnings as well as errors.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.source == "db":
        envelopes = load_from_db()
    else:
        if args.file is None:
            raise SystemExit("--file is required when --source fixtures is used")
        envelopes = load_from_fixtures(args.file)

    findings_by_item = collect_findings(envelopes)
    print(format_report(len(envelopes), findings_by_item, verbose=args.verbose))
    return 0


def collect_findings(
    envelopes: list[ProcessingEnvelope],
) -> dict[str, list[InvariantWarning]]:
    findings_by_item: dict[str, list[InvariantWarning]] = {}
    for envelope in envelopes:
        warnings = check_envelope(envelope)
        if warnings:
            findings_by_item[str(envelope.ingested_item.id)] = warnings
    return findings_by_item


def format_report(
    total_items: int,
    findings_by_item: dict[str, list[InvariantWarning]],
    *,
    verbose: bool,
) -> str:
    all_findings = [
        (item_id, warning)
        for item_id, warnings in findings_by_item.items()
        for warning in warnings
    ]
    warning_items = {
        item_id
        for item_id, warning in all_findings
        if warning.severity == Severity.WARNING
    }
    error_items = {
        item_id
        for item_id, warning in all_findings
        if warning.severity == Severity.ERROR
    }
    clean_count = total_items - len(findings_by_item)

    by_code = Counter((warning.code, warning.severity) for _, warning in all_findings)
    lines = [
        "=== Data Quality Report ===",
        f"Scanned: {total_items} items",
        f"Warnings: {sum(1 for _, w in all_findings if w.severity == Severity.WARNING)} "
        f"(across {len(warning_items)} items)",
        f"Errors: {sum(1 for _, w in all_findings if w.severity == Severity.ERROR)} "
        f"(across {len(error_items)} items)",
        f"Clean: {clean_count} items ({_percent(clean_count, total_items):.1f}%)",
        "",
        "--- By check ---",
    ]
    for (code, severity), count in sorted(
        by_code.items(), key=lambda item: item[0][0].value
    ):
        lines.append(f"  {code.value:<28} {count:>3} items ({severity.value})")

    lines.extend(["", "--- Errors (always shown) ---"])
    errors = [
        (item_id, warning)
        for item_id, warning in all_findings
        if warning.severity == Severity.ERROR
    ]
    if errors:
        for item_id, warning in errors:
            lines.append(
                f"  {item_id} [{warning.severity.value}] {warning.field}: {warning.message}"
            )
    else:
        lines.append("  none")

    lines.extend(["", "--- Warnings (--verbose) ---"])
    warning_lines = [
        (item_id, warning)
        for item_id, warning in all_findings
        if warning.severity == Severity.WARNING
    ]
    if verbose:
        if warning_lines:
            for item_id, warning in warning_lines:
                lines.append(
                    f"  {item_id} [{warning.severity.value}] {warning.field}: {warning.message}"
                )
        else:
            lines.append("  none")
    else:
        lines.append("  [hidden, use --verbose to show]")

    return "\n".join(lines)


def load_from_db() -> list[ProcessingEnvelope]:
    import core.bootstrap

    core.bootstrap.init()

    from core.models import BaseClassifiedItem

    queryset = (
        BaseClassifiedItem.objects.select_related("ingested_item", "course")
        .prefetch_related("links", "attachments", "classification_logs")
        .order_by("created_at")
    )
    return [_classified_item_to_envelope(item) for item in queryset]


def load_from_fixtures(path: str | Path) -> list[ProcessingEnvelope]:
    fixture_path = Path(path)
    payload = json.loads(fixture_path.read_text())
    normalizer = TextNormalizer()

    raw_envelopes = [
        _fixture_item_to_envelope(item) for item in payload["ingested_items"]
    ]
    envelopes = [
        _simulate_structured_state(normalizer.normalize_envelope(envelope))
        for envelope in raw_envelopes
    ]
    return envelopes


def _fixture_item_to_envelope(item: dict[str, Any]) -> ProcessingEnvelope:
    ingested_item = IngestedItemContext.model_validate(item)
    payload = ingested_item.payload
    return ProcessingEnvelope(
        id=ingested_item.id,
        ingested_item=ingested_item,
        entity=_fixture_payload_to_entity(payload),
        extracted_links=_payload_list_to_models(
            payload.get("links", []), ExtractedLink
        ),
        extracted_attachments=_payload_list_to_models(
            payload.get("attachments", []), ExtractedAttachment
        ),
    )


def _fixture_payload_to_entity(payload: dict[str, Any]):
    try:
        return mapper.payload_to_entity(payload)
    except ValueError:
        return Announcement(
            title=payload.get("title"),
            body=payload.get("body"),
            author=payload.get("author"),
        )


def _classified_item_to_envelope(classified_item) -> ProcessingEnvelope:
    ingested_item = IngestedItemContext.model_validate(classified_item.ingested_item)
    classification_log = classified_item.classification_logs.order_by(
        "created_at"
    ).first()
    return ProcessingEnvelope(
        id=classified_item.ingested_item_id,
        ingested_item=ingested_item,
        entity=_orm_classified_to_entity(classified_item),
        resolved_course_id=classified_item.course_id,
        extracted_links=[
            ExtractedLink.model_validate(link) for link in classified_item.links.all()
        ],
        extracted_attachments=[
            ExtractedAttachment.model_validate(attachment)
            for attachment in classified_item.attachments.all()
        ],
        classification_log=(
            ClassificationLogEntry.model_validate(classification_log)
            if classification_log is not None
            else None
        ),
    )


def _orm_classified_to_entity(classified_item):
    base_fields = {
        "title": classified_item.title,
        "body": classified_item.body,
        "author": classified_item.author,
        "published_at": classified_item.published_at,
    }
    if classified_item.entity_type == "assignment":
        child = classified_item.assignment
        return Assignment(
            **base_fields,
            deadline=child.deadline,
            submission_format=child.submission_format,
        )
    if classified_item.entity_type == "event":
        child = classified_item.event
        return Event(
            **base_fields,
            start_time=child.start_time,
            end_time=child.end_time,
            location=child.location,
        )
    if classified_item.entity_type == "announcement":
        return Announcement(**base_fields)
    if classified_item.entity_type == "material":
        return Material(**base_fields)
    return RawMessage(**base_fields)


def _payload_list_to_models(raw_items: list[dict[str, Any]], model):
    return [model.model_validate(item) for item in raw_items]


def _simulate_structured_state(envelope: ProcessingEnvelope) -> ProcessingEnvelope:
    if isinstance(envelope.entity, RawMessage):
        return envelope
    updates: dict = {
        "classification_log": ClassificationLogEntry(
            ingested_item_id=envelope.ingested_item.id,
            classified_item_id=uuid4(),
            entity_type=envelope.entity.entity_type,
            classification_method="deterministic",
            confidence=1.0,
        )
    }
    if envelope.ingested_item.payload.get("course_name"):
        updates["resolved_course_id"] = SIMULATED_RESOLVED_COURSE_ID
    return envelope.model_copy(update=updates)


def _percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return (part / total) * 100


if __name__ == "__main__":
    raise SystemExit(main())
