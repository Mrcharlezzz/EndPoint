# Processing Engine — Overview v4.3

The processor is a standalone Python service that reads pending ingested items
from the database, applies deterministic normalization and LLM-based entity
inference where needed, optionally deduplicates normalized entities into
thin-pointer canonicals, and writes the resulting relational records back.

**Code location:** `src/processor/processing/`

**Entry point:** `ProcessingPipeline.run_loop()`

---

## Pipeline Flow

```
┌───────────────┐
│ FETCH         │  Fetch pending batch from DB
├───────────────┤
│ PROCESS       │  Concurrent processing
│  ├─ Classify  │   (LLM for raw_message, deterministic for structured)
│  ├─ Normalize │   (text cleanup before LLM, course resolution later)
│  └─ Validate  │   (invariant checking)
├───────────────┤
│ PARTITION     │  Successes vs failures
├───────────────┤
│ DEDUP         │  Enabled by default; disable with ENDPOINT_DEDUP_ENABLED=false
│               │  Group successes by course + entity type
│               │   - load canonical candidates
│               │   - compare, match, resolve
│               │   - finalize dedup status + canonical decision
├───────────────┤
│ PERSIST       │  Batch write in one transaction
│               │   - classified rows
│               │   - supporting rows
│               │   - canonical create/update/link
│               │   - classification logs
│               │   - dedup logs
├───────────────┤
│ MARK FAILED   │  Mark processing failures on ingested items
└───────────────┘
```

**Async model:** PROCESS runs concurrently through `asyncio.Semaphore`. LLM
calls are native async. Sync ORM calls run through `asyncio.to_thread`.

**Concurrency:** Bounded by `config.max_concurrency`.

**Persistence:** Successful items are persisted in one batch transaction with
retry/fallback handling for transient vs row-scoped failures.

---

## Entity Types

| Type | Description |
| --- | --- |
| `assignment` | Submission task with deadline and submission details |
| `announcement` | Informational message from an instructor or course channel |
| `material` | Durable resource such as slides, files, or reference links |
| `event` | Scheduled occurrence such as lecture, exam, or office hours |
| `raw_message` | Unclassified message awaiting LLM classification |

All processor entities are Pydantic models in a discriminated union keyed by
`entity_type`.

---

## Classification

**Structured items** bypass the LLM path. Their typed fields come directly from
`ingested_items.payload`.

**`raw_message` items** usually take the LLM path:

1. classify into one of the supported typed entity families
2. extract type-specific fields into the corresponding structured model

Very short raw messages without URL evidence are classified as `raw_message`
by a rule-based prefilter before any model call. If the model still returns
`raw_message`, the processor skips publishable entity persistence and keeps the
item as unclassified output.

Both calls use structured output models. LangSmith metadata is attached for
live observability.

---

## Normalization

Normalization runs first for text cleanup, then course resolution happens only
for publishable entities.

Current deterministic normalization includes:

- whitespace and line-ending cleanup on text fields before any LLM prompt
- course resolution after a publishable entity is known
- pass-through of supporting payload data already present on the envelope
- propagation of normalized type-specific fields such as
  assignment `deadline`, event times, and base `published_at`

Dedup consumes only these normalized typed entities.

---

## Deduplication

Dedup runs only on successful processed envelopes.

Current characteristics:

- enabled by default
- groups by `(resolved_course_id, entity_type)`
- deduplicates `assignment`, `announcement`, `event`, and `material`
- uses typed comparisons and field-distance functions
- applies an entity-specific match strategy plus a generic resolution strategy
- maintains in-memory canonical state during the batch
- supports same-batch promotion of a better representative
- persists `dedup_status` and `DedupLog` for auditability

Current default wiring:

- `WeightedMatchStrategy`
- `FirstSeenResolutionStrategy`

When dedup is disabled, items persist with `dedup_status="skipped"` and no
canonical link.

The processor owns canonical deduplication directly when enabled.

---

## Persistence

The persistence subsystem writes:

- `base_classified_items`
- typed child rows
- links and attachments
- classification logs
- canonical entity creation or promotion
- dedup logs
- `ingested_items.processing_status`
- processed-but-unclassified raw messages

Canonical writes are driven by finalized dedup decisions, not by per-item
eager updates.

---

## Ownership Boundary

### Processor owns

- payload parsing
- entity typing
- normalization
- deduplication and canonical linking
- classified row creation
- supporting record creation
- classification logs
- dedup logs
- source `ingested_items` status transitions

### Processor does not own

- platform API access
- ingestion scheduling
- initial `ingested_items` writes
- student-facing canonical presentation logic
- semantic or embedding-based clustering

---

## Current Scope

**Active:**

- Entity inference (LLM classification + extraction + deterministic resolution)
- Normalization
- Canonical deduplication
- Batched persistence with error recovery
- Invariant checking
- Dedup audit logging

**Deferred:**

- embedding-based dedup
- cross-course dedup
- cross-entity-type dedup
- multi-worker locking

---

## Related Documentation

| Doc | What it covers |
| --- | --- |
| `docs/processing-engine/v4/processor-architecture-v4.1.md` | Tech stack, package boundaries, ports, adapters |
| `docs/processing-engine/v4/processor-runtime-v4.1.md` | Runtime entry points, polling loop, persistence policy |
| `docs/processing-engine/v4/processor-classification-v4.1.md` | LLM classification and extraction flow |
| `docs/processing-engine/v4/processor-normalization-v4.1.md` | Deterministic normalization scope |
| `docs/processing-engine/v4/processor-dedup-v4.1.md` | Dedup behavior, match/resolution model, extension points |
| `docs/processing-engine/v4/processor-persistence-v4.md` | Repository behavior, canonical writes, error handling |
| `docs/artifacts/db-schema/db-schema-v4.2.md` | Current database schema source of truth |
| `docs/processing-engine/v4/processor-testing-v4.1.md` | Processor test setup and coverage |
