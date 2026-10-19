# Endpoint

> A unified academic information platform for Constructor University students.

**Team:** 4 developers (full-stack, shared workload)
**Course:** Software Engineering and Design
**Date:** 20th February 2026

**Contents**

- [Problems](#problems)
- [Current Behaviour](#current-behaviour)
- [Goals / Changed Behaviour](#goals--changed-behaviour)
- [Solution](#solution)
- [Assumptions](#assumptions)
- [Risks & Mitigation](#risks--mitigation)
- [Landscape Analysis](#landscape-analysis)
- [Feasibility](#feasibility)
- [Resource Requirements](#resource-requirements)
- [Project Scope](#project-scope)
- [Next Steps](#next-steps)
- [Learning Goals](#learning-goals)

---

## Problems

Academic information for students at CU is fragmented across multiple platforms. Students must navigate parallel information streams to reconstruct their academic workflow, increasing cognitive load and the risk that time-sensitive or critical information is overlooked.

- **Platforms:**
  - Moodle
  - Telegram
  - Teams
  - Course Websites
  - LMS, E-Learning
  - LMS-JB
- **Information:**
  - Assignments
  - Announcements
  - News
  - Reminders
  - Deadlines
  - Study Materials
  - Lecture Notes
  - Recordings

---

## Current Behaviour

**Students:**

- Log into multiple platforms independently (Moodle, Teams, Telegram, LMS, course websites).
- Check each platform separately for new assignments, updated deadlines, announcements, and uploaded materials.
- Rely on memory or habit to decide when to check each platform.
- Consolidate information manually into personal systems or miss assignment deadlines.
- Post clarification questions in chats when announcements are overlooked.

---

## Goals / Changed Behaviour

1. Allow students to comfortably manage their academic workflow.
2. Improve student awareness of relevant information.
3. Surface time-sensitive information proactively so nothing is missed.
4. Reduce the cognitive overhead of navigating multiple disconnected platforms.
5. Make institutional knowledge accessible and searchable.

---

## Solution

Create a unified management platform that aggregates fragmented university systems into a single interface.

Consolidate assignments, announcements, deadlines, and materials from institutional platforms into a continuously updated unified feed. Present information ordered first by priority and then chronologically within each priority level.

Allow filtering by course, content type, and original source to preserve structure while removing fragmentation.

Automatically extract deadlines and events from connected platforms and translate them into a synchronized calendar view, paired with proactive push notifications and reminder alerts to ensure users are consistently aware of approaching due dates and schedule changes.

---

### Initial Success Metrics

1. **User Adoption:** 10–20 active student users.
2. **Engagement Frequency:** Average of 1 usage session per user per workday.
3. **Retention:** Evidence of recurring usage

---

## Assumptions

- Students use more than one platform for their courses.
- It is tedious and time-consuming for students to organize this information themselves.
- This situation leads to negative consequences such as missed deadlines.
- We can obtain sufficient API access or approved data retrieval mechanisms from target platforms.
- We can receive support and consent from professors and the university.

---

## Risks & Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| Low adoption | High | Proactive multi-channel promotion (university platforms, student groups, events). Short, intuitive demo video clearly showing the core value and workflow benefits. |
| CU IT denies API access to Moodle/Teams | High | Engage early with IT; explore approved alternative data retrieval methods if necessary. |
| Lack of lecturers' consent for necessary integration | Medium | Conduct proactive outreach from the start; only aggregate from channels where consent is obtained. |
| Platform changes API or restricts data access | Low | Build a modular integration layer; isolate each platform adapter so it can be replaced independently. |

## Landscape Analysis

There exists an official Moodle–Teams integration that enables assignment synchronization, calendar event syncing, and announcement mirroring between platforms. Assignments and due dates created in Moodle can be surfaced in Teams, calendar events can be shared across systems, and announcements can be reflected in both environments. This reduces platform switching and improves visibility within the Microsoft ecosystem.

However, additional value can be created by extending beyond synchronization by extracting deadlines from unstructured announcements and generating consolidated briefing-style summaries across sources.

We plan to obtain further information on how this integration is configured and currently used at Constructor University through communication with Constructor IT.

---

## Feasibility

- All target platforms expose public APIs or appear technically retrievable, pending policy validation.
  - Moodle: Web Services API (documented, widely used)
  - Teams: Microsoft Graph API
  - Telegram: Bot API (mature, well-documented)
  - Course Websites: Structured scraping or manual configuration, subject to policy compliance
  - LMS platforms: API availability to be confirmed; alternative retrieval mechanisms evaluated if necessary

- **Primary feasibility risk:** CU IT cooperation for Moodle and Teams API access
  - Mitigation: early engagement and alignment with university policy

---

## Resource Requirements

### Infrastructure

- **Hosting:** Hetzner (EU-based)
  - Web server, API server, background workers, database, broker
  - Estimated cost: low tier VPS sufficient for MVP (~€8.45/month)
- **LLM API:** External provider (to be decided)
  - Cost scales with usage (per-token pricing)
  - MVP usage with ~10-20 potential users: estimated low to moderate cost depending on query volume
  - Embedding model for RAG: one-time cost per document ingestion, minimal ongoing cost for queries
- **Domain + TLS:** Minimal cost (~€10-15/year)

### Total Estimated MVP Infrastructure Cost

- ~€15-20/month depending on LLM usage volume
- No hardware requirements beyond personal development machines

---

## Project Scope

### In Scope (MVP)
#### Phase 1

- Unified feed aggregating the most used platforms (Teams, Moodle,  Telegram)
	- Courses Dashboard
	- Assignments Dashboard
- Notification System
#### Phase 2
- Daily briefing and newspaper
- Calendar view
- Universal Search

### Out of Scope

- Write-back to source platforms (submitting assignments, replying to messages)
- Grade tracking or GPA calculation
- Social features (messaging between students, forums)
- Real-time chat integration (only batch-processed summaries from Telegram)
- Multi-university support

### Scope Boundaries

- Endpoint is a **read-only aggregation layer**, not a replacement for any existing platform
- Users still interact with source platforms for actions (submissions, replies)
- Platform integrations are modular; if a source becomes inaccessible, the rest of the system continues functioning

---

## Next Steps

### Establish university partnerships

- Reach out to CU IT to discuss API access for Moodle and Teams.
- Coordinate with professors and lecturers to secure consent for content aggregation from their channels.

### Validate assumptions through user research

- Design and distribute a student survey.
- Conduct interviews with students across different majors to capture variation in platform usage.

### Define and lock MVP scope and technical requirements

- Translate validated user needs into concrete feature requirements.
- Define the unified data schema across all platform integrations.
- Establish system architecture, assign responsibilities across the team, and begin development.

---

## Learning Goals

- Project Management
- Agentic Development
- Full-Stack Engineering
- Product Management
