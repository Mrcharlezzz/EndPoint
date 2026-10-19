# Telegram / Teams LLM Evaluation Dataset

This directory contains the hand-labeled gold dataset for processor LLM classification and raw-message field extraction.

## Files

- `message_index.csv` is the canonical classification table. Every row is one raw message candidate with `entity_type_gold` as the expected classifier output.
- `assignment_extraction.csv` contains assignment-only extraction gold for fields the LLM is expected to infer from message text.
- `event_extraction.csv` contains event-only extraction gold for fields the LLM is expected to infer from message text.
- `announcement_extraction.csv` is a reserved extraction table for future announcement-owned fields. It keeps candidate rows but has no active expected values in the current contract.
- `material_extraction.csv` is a reserved extraction table for future material-owned fields. It keeps candidate rows but has no active expected values in the current contract.
- `audit_excluded.csv` records reviewed source messages that were intentionally left out of the gold set.

Announcements and materials are still evaluated for classification through `message_index.csv`, but their extraction tables currently contain no active LLM-owned expected values.

## Classification Contract

`message_index.csv` has this schema:

```csv
id,external_id,platform,course_name,published_at,body,entity_type_gold,difficulty,coverage_group,coverage_case,slice_tags_json,notes
```

Rules:

- `platform` is required and is part of the dataset contract. Current values include `telegram` and `teams`.
- `entity_type_gold` is the expected classifier output: `assignment`, `event`, `announcement`, `material`, or `raw_message`.
- `entity_type_gold=raw_message` means the message should remain unclassified/noise and should not produce a publishable course entity.
- There is no `publishable_gold` column; publishability is implied by `entity_type_gold != raw_message`.
- There is no dev/test split. The dataset is one curated evaluation set.
- Teams rows in this dataset must come from raw Teams messages. Structured Teams assignment objects are not classification rows.

## Extraction Contract

The extraction CSVs intentionally contain only fields owned by the LLM.

Assignment extraction schema:

```csv
id,course_name,published_at,body,expected_deadline,notes
```

Event extraction schema:

```csv
id,course_name,published_at,body,expected_start_time,expected_end_time,expected_location,notes
```

Reserved announcement/material extraction schema:

```csv
id,course_name,published_at,body,expected_title,expected_dates_json,notes
```

Rules:

- Extraction fields must be grounded in the message body.
- `published_at` may be used only to resolve temporal phrases from the body.
- If an assignment deadline gives a date but no time, the expected deadline uses `23:59` local academic time on that date.
- Local academic time is `Europe/Amsterdam`, so offsets follow daylight saving time for the target date.
- Expected datetimes encode the temporal contract directly: explicit clock/date times use local academic time, while offsets from `published_at` such as `in 5 minutes` or `in 2 hours` use UTC instants.
- Links are not LLM extraction gold. URL extraction belongs to deterministic normalization.
- Attachments are not LLM extraction gold. Attachment records must come from structured platform payload metadata.
- Assignment `max_points`, `points`, and `status` are not LLM extraction gold. `status` should be derived dynamically; grading fields should be structured-payload-only if retained.

Rows with multiple equally important deadlines or events may leave the scalar expected datetime blank and document the reason in `notes`.
