# Processing Engine — Architecture v4.1

> Tech stack, package boundaries, and runtime structure for the processor
> aligned with the v4 ingestion contract and async batch pipeline.

---

## Tech Stack

| Component | Technology | Rationale |
|---|---|---|
| Language | Python 3.12+ with asyncio | Team standard, good LLM ecosystem |
| ORM access | Django ORM via `db_core` bootstrap | Reuses the shared schema package without a processor-owned Django app |
| Database | PostgreSQL 16 | Shared database across components |
| Trigger | DB polling on `ingested_items.processing_status` | Simple MVP pickup model |
| Validation / domain models | Pydantic v2 | Strong in-memory contracts |
| LLM client | OpenAI-compatible async SDK | Supports structured output and local/remote compatible providers |
| LLM observability | LangSmith | Per-call tracing and inspection |
| Settings | `pydantic-settings` with `ENDPOINT_` env vars | Matches repo-wide configuration conventions |
| Architecture | Hexagonal | Keeps domain logic separate from infra adapters |

---

## Deployment Model

The processor is a standalone component at `src/processor/`. It is not a
Django app. It imports `core.bootstrap.init()` from `db_core` before touching
ORM models.

There is no processor-owned `django_setup.py` or `django_settings.py`.

---

## Runtime Flow

1. Bootstrap Django ORM through `core.bootstrap.init()`.
2. Run async polling loop via `ProcessingPipeline.run_loop()`.
3. Fetch pending `IngestedItem` rows in batches ordered by `created_at`.
4. Process items concurrently: normalize text first, classify/extract `raw_message` items, resolve course for publishable entities, check invariants.
5. Deduplicate successful normalized items by `(course_id, entity_type)` when dedup is enabled by default.
6. Persist all successes in a single `transaction.atomic()`. Retry on transient errors, fall back to per-item persistence on row-scoped errors.
7. Mark failures with `processing_status='failed'` and a durable error message.

---

## Hexagonal Boundaries

### Domain models

Live in `processing/models/`.

- `entities.py` — discriminated union: Assignment, Announcement, Material, Event, RawMessage
- `envelope.py` — ProcessingEnvelope (unit of work through the pipeline)
- `classification.py` — ClassificationResult and sparse ExtractionPatch models
- `extracted.py` — ExtractedLink, ExtractedAttachment
- `course.py` — Course, CourseMatch
- `invariants.py` — InvariantWarning, check_envelope()

These are processor-internal Pydantic models. They mirror the relational schema
where useful, but they are not ORM classes.

### Application services

Live in `processing/application/`.

- `classification.py` — ClassificationOrchestrator
- `dedup/service.py` — DedupService
- `dedup/comparisons.py` — typed comparison builders
- `dedup/distances.py` — field distance functions
- `dedup/strategies.py` — match strategies
- `dedup/resolution.py` — resolution strategies
- `normalization/service.py` — NormalizationService
- `normalization/course_resolution.py` — CourseResolver
- `normalization/text_normalization.py` — TextNormalizer

These coordinate processor use cases and may depend on ports, but not on Django ORM directly.

### Ports

Live in `processing/ports/`.

- `classifier.py` — EntityInferencePort
- `dedup.py` — MatchStrategy, ResolutionStrategy
- `repository.py` — EntityRepositoryPort

Ports speak in processor domain models, not ORM models.

### Adapters

Live in `processing/adapters/`.

- `llm/` — LLMClassifier, prompts, LangSmith tracing wrapper
- `postgres/` — PostgresEntityRepository, mapper, db_errors

This is the only layer where external systems are touched directly.

---

## Core Ports

### `EntityInferencePort`

Responsible for:
- classifying `RawMessage` envelopes into a typed entity type
- extracting type-specific fields
- running the integrated `classify`, `extract`, and `classify_and_extract`
  flows used by the processor and evaluation runner

Async interface — the only async-facing boundary in the current design.

### `EntityRepositoryPort`

Responsible for:
- fetching pending `IngestedItem` rows
- batched persistence of classified entities and supporting records
- writing classification logs
- marking ingested items processed or failed
- course reads, course creation, and course code updates used by course resolution

---

## Package Layout

```
src/processor/
├── pyproject.toml
├── uv.lock
├── docs/
│   ├── GUARDRAILS.md
│   ├── postgres-persistence.md
│   ├── processor-implementation-spec-v3.1.md
│   ├── archive/
│   └── research/
│
├── scripts/
│   └── scan_data_quality.py
│
└── processing/
    ├── config.py
    ├── exceptions.py
    ├── pipeline.py
    │
    ├── models/
    │   ├── entities.py
    │   ├── envelope.py
    │   ├── extracted.py
    │   ├── classification.py
    │   ├── course.py
    │   └── invariants.py
    │
    ├── application/
    │   ├── classification.py
    │   ├── dedup/
    │   │   ├── service.py
    │   │   ├── comparisons.py
    │   │   ├── distances.py
    │   │   ├── strategies.py
    │   │   └── resolution.py
    │   ├── normalization/
    │   │   ├── service.py
    │   │   ├── course_resolution.py
    │   │   ├── extraction_resolution.py
    │   │   └── text_normalization.py
    │   └── tests/
    │
    ├── ports/
    │   ├── classifier.py
    │   ├── dedup.py
    │   └── repository.py
    │
    ├── adapters/
    │   ├── llm/
    │   │   ├── classifier.py
    │   │   ├── prompts.py
    │   │   ├── tracing.py
    │   │   └── tests/
    │   └── postgres/
    │       ├── repository.py
    │       ├── mapper.py
    │       ├── db_errors.py
    │       └── tests/
    │
    └── tests/
        ├── conftest.py
        ├── fixtures.py
        ├── test_pipeline.py
        ├── test_models.py
        ├── test_invariants.py
        ├── test_data_quality_scan.py
        └── manual/
            └── live_llm_pipeline_manual.py
```

---

## Related Documentation

| Doc | What it covers |
|---|---|
| `docs/processing-engine/v4/processor-overview-v4.3.md` | Purpose, flow, ownership |
| `docs/processing-engine/v4/processor-runtime-v4.1.md` | Runtime entry points and persistence policy |
| `docs/processing-engine/v4/processor-dedup-v4.1.md` | Dedup behavior, extension points, canonical state |
| `docs/processing-engine/v4/processor-persistence-v4.md` | Repository behavior and canonical writes |
| `src/processor/docs/processor-implementation-spec-v3.1.md` | Detailed contracts and code shapes |
| `docs/artifacts/db-schema/db-schema-v4.2.md` | Database source of truth |
