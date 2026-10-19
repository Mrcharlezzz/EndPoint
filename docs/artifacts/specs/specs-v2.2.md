# Product Requirements

**Contents**

- [0 — Account & Login Experience](#0--account--login-experience)
- [1 — Assignment List](#1--assignment-list)
- [2 — Notification System](#2--notification-system)
- [3 — Announcement Dashboard](#3--announcement-dashboard)
- [4 — Daily Briefing / Newspaper](#4--daily-briefing--newspaper)
- [Functional Requirements](#functional-requirements)
- [Non-Functional Requirements](#non-functional-requirements)

---

## 0 — Account & Login Experience

### Must

- As a student, I want to login using my Microsoft account, so that I can access the platform without creating new credentials.
- As a student, I want to reconnect a platform if authentication expires, so that aggregation continues working.
- As a student, I want to be able to delete my data for privacy.

### Should

- As a student, I want to see a loading indicator and a "syncing your account" message during login, so that I understand the platform is working and know what to expect.

### Could

- As a student, I want to see a clear and actionable error message when something goes wrong, so that I can report it or understand what failed.

---

## 1 — Assignment List

### Must

- As a student, I want to see a unified list of all assignments across platforms, so that I don't need to check each platform separately.
- As a student, I want to see the course, deadline, format, and current status of each assignment, so that I can decide what to work on.
- As a student, I want to access the original source of the assignment, so that I can submit or read further details.

### Should

- As a student, I want assignments ordered by deadline, so that urgent work is more visible.
- As a student, I want to filter assignments by course, source, and status, so that I can focus on a specific context.
- As a student, I want to see assignments extracted from announcements, so that hidden deadlines are not missed.

### Could

- As a student, I want to be able to define priority, so that urgent work is more visible.

---

## 2 — Notification System

### Should

- As a student, I want to receive notifications when a new assignment is posted, so that I can plan early.
- As a student, I want to receive notifications when deadlines change, so that I can update my plan.
- As a student, I want to access the platform from the notification, so that I can quickly access the information.
- As a student, I want to see a history of notifications, so that I can review what I may have ignored.
- As a student, I want duplicate announcements consolidated, so that I don't receive redundant notifications.

### Could

- As a student, I want to be able to distinguish read and unread notifications, so that I don't go over the same information again.
- As a student, I want to set up and receive reminders before deadlines, so that I don't miss submissions.
- As a student, I want to be notified when an announcement contains actionable information, so that I don't ignore critical updates.

---

## 3 — Announcement Dashboard

### Must

- As a student, I want announcements from all my enrolled courses aggregated into one dashboard, so that I can see academic updates in a single place.

### Should

- As a student, I want to have access to the source platform of the announcements, so that I can reply or see them in the original context.
- As a student, I want announcements to be categorized (e.g., assignment, class, event, general notice), so that I can quickly assess their relevance.
- As a student, I want announcements grouped by course, so that I can navigate information contextually.

### Could

- As a student, I want dates, links, and locations mentioned in announcements to be extracted into structured fields, so that I can clearly identify important information.

---

## 4 — Daily Briefing / Newspaper

> *TODO — Could*

---

## Functional Requirements

- The system must have platform connectors to fetch academic information (registered courses, assignments, messages, classes) from the specific integrated platforms.
- The system must normalize different data models into a single structured domain representation.
- The system must process unstructured messages from platforms and classify them into assignments, classes, announcements, and events.
- The system must detect and merge or link duplicate announcements posted on multiple platforms into a single logical item (Moodle–Teams integration).
- The system must centralize academic content at the course level, avoiding per-student copies of announcements, assignments, or events.

## Non-Functional Requirements

- **Login responsiveness:** First login may take 1–2 minutes (Teams exploration required); subsequent logins must surface new processed data within ≤10 seconds.
- **Platform independence:** System functionality should be independent of the availability of any particular platform.
- **GDPR compliance:** Do not store data that could harm students if exposed.
- **Incremental sync:** Student data should be incrementally updated and synced on login, or at certain intervals.
- **Auditability:** Every ingested item must be traceable to its original source.
- **UI/UX:** Must be responsive and intuitive to ensure adoption.
