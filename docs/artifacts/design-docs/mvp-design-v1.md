# ENDPOINT - MVP Scope v1

Constructor University · Software Engineering and Design

**Contents**

- [Purpose](#purpose)
- [Platforms](#platforms)
- [What's In](#whats-in)
- [What's Deferred](#whats-deferred)
- [System Goals](#system-goals)

---
## Purpose

This document defines the **minimum viable product** — what we build first, what we defer, and in what order.

For system design details (components, data flows, architecture), see the Design Document. For requirements and user stories, see the Specs.

---

## Platforms

|Platform|Access Method|Ingestion Mode|
|---|---|---|
|Moodle|Web Services API|Server poll|
|Microsoft Teams|Microsoft Graph API (user token)|Server poll|
|Telegram|Bot API|Bot|

Teams and Moodle are the most used academic platforms by Constructor University students and cover the majority of assignments, announcements, and course materials. However, a Moodle–Teams integration is active for some university courses, which means these two platforms often mirror the same content. We decided to also intergate Telegram to ensure the product delivers value from the start to a smaller audience (SDT students).

---

## What's In

### Data Pipeline

Raw data flows from platforms into typed, deduplicated domain entities.

| Component                                    | Design Doc Ref | Scope                                                                                                                                                    |
| -------------------------------------------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **C1 · Authentication & Session Management** | §3 C1          | Microsoft OAuth2 login. Platform connection flow for Moodle, Teams, Telegram. Token storage and refresh. App session tokens.                             |
| **C2 · Platform Connectors & Ingestion**     | §3 C2          | Moodle, Teams, and Telegram adapters. Scheduled sync + on-login sync. Raw payload storage. Incremental sync via per-chat cursors.                        |
| **C3 · Classification & Normalization**      | §3 C3          | Structured field mapping (Moodle/Teams). LLM classification + extraction for unstructured content (Telegram). Queue-worker decoupling between C2 and C3. |
| **C4 · Deduplication & Entity Linking**      | §3 C4          | Cross-platform dedup (especially Moodle ↔ Teams mirroring). Canonical entity creation. Platform entity map for source traceability.                      |

### Dashboards

Students can see their aggregated data. No priority engine, no sorting logic beyond basic ordering.

| Component                       | Design Doc Ref | Scope                                                                                                                                                                                                                                       |
| ------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **C5 · Assignment Feed**        | §3 C5          | Unified assignment list across platforms. Shows course, deadline, status, format, source link. **No priority engine** — raw data ordered by deadline (if available), then by ingestion time. Basic filtering by course and source platform. |
| **C7 · Announcement Dashboard** | §3 C7          | Aggregated announcements grouped by course. Shows title, source, posted date, source link. Category badges if classification data is available. **No extracted entity fields** in MVP.                                                      |
|                                 |                |                                                                                                                                                                                                                                             |

---

## What's Deferred

| Item                                         | Reason                                                                                                                                                         | Design Doc Ref   |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| **C6 · Notification System**                 | Adds complexity without validating core pipeline first. Revisit after pipeline is stable.                                                                      | §3 C6, Specs §2  |
| **Data Deletion Feature**                    | The system is built GDPR-compliant from the start (encrypted tokens, no passwords, course-level content ownership). The user-facing deletion flow is deferred. | §3 C8, Specs §0  |
| **Priority engine / assignment ranking**     | C5 serves raw data only. Priority logic (deadline proximity boosts, user overrides) deferred.                                                                  | §3 C5            |
| **Extracted entity fields on announcements** | Dates, links, locations extracted from announcements into structured fields — deferred.                                                                        | Specs §3 (Could) |
| **Reminder configuration**                   | Custom reminder schedules per assignment.                                                                                                                      | —                |
| **Daily Briefing / Newspaper**               | Post-MVP feature.                                                                                                                                              | Specs §4         |
| **Calendar view**                            | Post-MVP feature.                                                                                                                                              | —                |
| **Universal Search**                         | Post-MVP feature.                                                                                                                                              | —                |

---

## System Goals

1. **Pipeline completeness:** All three platform adapters successfully ingest, classify, and deduplicate content into canonical entities.
2. **Dashboard usability:** A student can log in and see a unified list of assignments and announcements across their enrolled courses.
3. **Data correctness:** Ingested items are traceable to their original source; duplicates across platforms are merged.
