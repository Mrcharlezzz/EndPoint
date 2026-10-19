# Endpoint — Database Schema v5

**Target:** PostgreSQL 16+
**ORM source of truth:** `src/common/db_core/core/models/`
**Scope:** implemented tables and enums only

**Contents**

- [1. Scope And Conventions](#1-scope-and-conventions)
- [2. Enums](#2-enums)
- [3. Implemented Tables](#3-implemented-tables)
- [4. Referential Behavior](#4-referential-behavior)

---

## 1. Scope And Conventions

This document describes the schema currently implemented in `db_core`.

Shared conventions:

- tables backed by `BaseModel` use UUID primary keys plus `created_at` and
  `updated_at`
- `ingested_items` is the staging layer written by ingestion
- `base_classified_items` and its child tables are written by the processor
- `canonical_entities` is a thin-pointer dedup table and stores no merged
  content
- only implemented tables are listed here; planned tables are intentionally
  omitted

---

## 2. Enums

### `ProcessingStatus`

`pending` · `processing` · `processed` · `failed`

### `EntityType`

`assignment` · `announcement` · `event` · `reminder` · `raw_message` ·
`material`

### `Platform`

`moodle` · `teams` · `telegram`

### `ClassificationMethod`

`field_mapping` · `rule_based` · `llm`

### `DedupStatus`

`primary` · `linked` · `promoted` · `skipped` · `failed`

---

## 3. Implemented Tables

### 3.1 `courses`

Canonical, platform-agnostic course record.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `name` | VARCHAR(255) | NOT NULL | |
| `code` | VARCHAR(50) | nullable, indexed | |
| `major` | VARCHAR(255) | nullable, indexed | |
| `semester` | TEXT | nullable | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, auto | |

### 3.2 `course_platform_mappings`

Maps canonical courses to platform-specific course identifiers.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `course_id` | UUID | FK → `courses.id` CASCADE, NOT NULL | |
| `platform` | VARCHAR(50) | NOT NULL | See `Platform` |
| `external_course_id` | VARCHAR(255) | NOT NULL | |
| `external_course_name` | TEXT | nullable | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, auto | |

Indexes and constraints:

- `(course_id)` — `ix_course_platform_course`
- UNIQUE `(platform, external_course_id)` — `uq_course_platform_ext`

### 3.3 `ingested_items`

Staging table owned by ingestion. The processor reads pending rows from here.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `external_id` | VARCHAR(255) | UNIQUE, indexed, NOT NULL | Platform item id |
| `platform` | VARCHAR(50) | NOT NULL | See `Platform` |
| `course_external_id` | VARCHAR(255) | nullable, indexed | Platform course/group id |
| `source_url` | VARCHAR(2048) | nullable | Direct link to the source item when available |
| `schema_version` | VARCHAR(20) | NOT NULL, default `4.0` | Payload schema version |
| `adapter_version` | VARCHAR(50) | NOT NULL, default `""` | Ingestion adapter version |
| `ingestion_mode` | VARCHAR(50) | NOT NULL, default `server_poll` | |
| `processing_status` | VARCHAR(20) | NOT NULL, default `pending` | See `ProcessingStatus` |
| `processing_completed_at` | TIMESTAMPTZ | nullable | Time processing completed |
| `error_message` | TEXT | nullable | Processing failure message |
| `fetched_by_user_email` | VARCHAR(254) | nullable, indexed | User whose credential fetched the item |
| `payload` | JSONB | NOT NULL, default `{}` | Structured normalized content |
| `raw_payload` | JSONB | NOT NULL, default `{}` | Immutable source payload envelope |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, auto | |

Indexes:

- `(platform, processing_status)` — `ix_ingested_platform_status`
- `(processing_status, created_at)` — `ix_ingested_processing_queue`
- `(course_external_id)` — `ix_ingested_course_ext`
- `(fetched_by_user_email, created_at)` — `ix_ingested_fetched_by`

#### 3.3.1 `payload` Contract

The payload carries normalized domain data. Ingestion metadata such as
`external_id`, `platform`, `course_external_id`, `source_url`, `schema_version`,
`adapter_version`, `ingestion_mode`, and `raw_payload` lives outside this JSON
blob in dedicated columns.

Current payload fields consumed by the processor:

| Field | Type | Applies to | Notes |
| --- | --- | --- | --- |
| `content_type` | string | all | Required discriminator |
| `title` | string \| null | all | |
| `author` | string \| null | all | |
| `body` | string | all | Required by ingestion schema |
| `course_name` | string \| null | all | Raw course name |
| `published_at` | ISO 8601 datetime \| null | assignments, announcements, events, materials, raw messages | Original publish time |
| `deadline` | ISO 8601 datetime \| null | assignments | |
| `submission_format` | string \| null | assignments | |
| `start_time` | ISO 8601 datetime \| null | events | |
| `end_time` | ISO 8601 datetime \| null | events | |
| `location` | string \| null | events | |
| `attachments` | array | all | Attachment objects |
| `links` | array | all | Link objects |

Supporting object shapes:

- `attachments[]`: `filename` required, `mime_type` nullable, `url` nullable
- `links[]`: `url` required, `label` nullable

### 3.4 `base_classified_items`

Concrete base table for all processor-created classified entities.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `entity_type` | VARCHAR(50) | NOT NULL | See `EntityType` |
| `author` | VARCHAR(255) | nullable | |
| `ingested_item_id` | UUID | FK → `ingested_items.id` PROTECT, NOT NULL | |
| `canonical_entity_id` | UUID | FK → `canonical_entities.id` SET NULL, nullable | |
| `course_id` | UUID | FK → `courses.id` PROTECT, nullable | |
| `dedup_status` | VARCHAR(20) | NOT NULL, default `skipped` | See `DedupStatus` |
| `title` | VARCHAR(500) | nullable | |
| `body` | TEXT | nullable | |
| `published_at` | TIMESTAMPTZ | nullable, indexed | Original publish time |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, auto | |

Indexes:

- `(course_id, entity_type)` — `ix_base_course_type`
- `(canonical_entity_id)` — `ix_base_canonical`

### 3.5 `assignments`

Multi-table-inheritance child of `base_classified_items`.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `baseclassifieditem_ptr_id` | UUID | PK, FK → `base_classified_items.id` | MTI pointer |
| `deadline` | TIMESTAMPTZ | nullable, indexed | |
| `submission_format` | VARCHAR(255) | nullable | |

### 3.6 `announcements`

Multi-table-inheritance child of `base_classified_items`.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `baseclassifieditem_ptr_id` | UUID | PK, FK → `base_classified_items.id` | MTI pointer |

### 3.7 `materials`

Multi-table-inheritance child of `base_classified_items`.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `baseclassifieditem_ptr_id` | UUID | PK, FK → `base_classified_items.id` | MTI pointer |

### 3.8 `events`

Multi-table-inheritance child of `base_classified_items`.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `baseclassifieditem_ptr_id` | UUID | PK, FK → `base_classified_items.id` | MTI pointer |
| `start_time` | TIMESTAMPTZ | nullable, indexed | |
| `end_time` | TIMESTAMPTZ | nullable | |
| `location` | VARCHAR(500) | nullable | |

### 3.9 `canonical_entities`

Thin-pointer dedup table. Stores canonical identity, not merged content.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `entity_type` | VARCHAR(50) | NOT NULL | See `EntityType` |
| `primary_item_id` | UUID | FK → `base_classified_items.id` PROTECT, NOT NULL | Canonical representative |
| `course_id` | UUID | FK → `courses.id` PROTECT, NOT NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, auto | |

Indexes:

- `(course_id, entity_type)` — `ix_canonical_course_type`

### 3.10 `classification_logs`

Audit record for each processor classification decision.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `ingested_item_id` | UUID | FK → `ingested_items.id` PROTECT, NOT NULL | |
| `entity_type` | VARCHAR(50) | nullable | |
| `classified_item_id` | UUID | FK → `base_classified_items.id` SET NULL, nullable | |
| `classification_method` | VARCHAR(20) | NOT NULL | See `ClassificationMethod` |
| `llm_model` | VARCHAR(200) | nullable | |
| `llm_raw_output` | TEXT | nullable | |
| `confidence` | FLOAT | nullable | |
| `processing_time_ms` | INTEGER | nullable | |
| `processor_version` | VARCHAR(100) | nullable | Processor service version that produced the log |
| `prompt_version` | VARCHAR(100) | nullable | Prompt set version used by the processor |
| `input_tokens` | INTEGER | nullable | |
| `output_tokens` | INTEGER | nullable | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |

### 3.11 `dedup_logs`

Audit record for each processor dedup outcome.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `classified_item_id` | UUID | FK → `base_classified_items.id` PROTECT, NOT NULL | |
| `canonical_entity_id` | UUID | FK → `canonical_entities.id` SET NULL, nullable | |
| `status` | VARCHAR(20) | NOT NULL | See `DedupStatus` |
| `match_strategy` | VARCHAR(100) | nullable | |
| `resolution_strategy` | VARCHAR(100) | nullable | |
| `score` | FLOAT | nullable | |
| `error_message` | TEXT | nullable | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |

### 3.12 `links`

Extracted or normalized links attached to a classified item.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `classified_item_id` | UUID | FK → `base_classified_items.id` CASCADE, NOT NULL | |
| `url` | VARCHAR(2048) | NOT NULL | |
| `label` | VARCHAR(500) | nullable | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |

Indexes:

- `(classified_item_id)` — `ix_links_item`

### 3.13 `attachments`

Normalized file attachments attached to a classified item.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `classified_item_id` | UUID | FK → `base_classified_items.id` CASCADE, NOT NULL | |
| `filename` | VARCHAR(500) | NOT NULL | |
| `mime_type` | VARCHAR(200) | nullable | |
| `url` | VARCHAR(2048) | nullable | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |

Indexes:

- `(classified_item_id)` — `ix_attachments_item`

### 3.14 `platform_credentials`

Per-user, per-platform credential bundle. The application encrypts the
serialized bundle before storing it in `encrypted_payload`.

| Column | Type | Constraints | Notes |
| --- | --- | --- | --- |
| `id` | UUID | PK, default uuid4 | |
| `user_email` | VARCHAR(254) | NOT NULL, indexed | Groups a user's credentials across platforms |
| `display_name` | VARCHAR(255) | NOT NULL, default `""` | |
| `is_paused` | BOOLEAN | NOT NULL, default `false`, indexed | |
| `platform` | VARCHAR(50) | NOT NULL | See `Platform` |
| `encrypted_payload` | BYTEA | NOT NULL | Encrypted credential bundle |
| `refresh_url` | VARCHAR(2048) | NOT NULL, default `""` | |
| `last_refreshed_at` | TIMESTAMPTZ | nullable | |
| `is_expired` | BOOLEAN | NOT NULL, default `false`, indexed | |
| `expired_at` | TIMESTAMPTZ | nullable | |
| `created_at` | TIMESTAMPTZ | NOT NULL, auto | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, auto | |

Indexes and constraints:

- UNIQUE `(user_email, platform)` — `uq_platform_credential_user_email_platform`
- `(platform, is_expired)` — `ix_cred_platform_expired`
- `(user_email)` — `ix_cred_user_email`

## 4. Referential Behavior

| Parent → Child | On Delete |
| --- | --- |
| `courses` → `course_platform_mappings` | CASCADE |
| `ingested_items` → `base_classified_items` | PROTECT |
| `ingested_items` → `classification_logs` | PROTECT |
| `courses` → `base_classified_items` | PROTECT |
| `courses` → `canonical_entities` | PROTECT |
| `base_classified_items` → `links`, `attachments` | CASCADE |
| `base_classified_items` → `canonical_entities.primary_item` | PROTECT |
| `canonical_entities` → `base_classified_items.canonical_entity` | SET NULL |
| `base_classified_items` → `classification_logs.classified_item` | SET NULL |
| `base_classified_items` → `dedup_logs.classified_item` | PROTECT |
| `canonical_entities` → `dedup_logs.canonical_entity` | SET NULL |
