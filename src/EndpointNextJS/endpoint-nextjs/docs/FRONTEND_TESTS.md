# Frontend tests (`endpoint-nextjs`)

How the automated tests in this Next app are organized, what each file covers, and whether it is **unit** or **integration** level.

**Base path:** everything below lives under `src/EndpointNextJS/endpoint-nextjs/`.

---

## Table of contents

- [Frontend tests (`endpoint-nextjs`)](#frontend-tests-endpoint-nextjs)
  - [Table of contents](#table-of-contents)
  - [How to run](#how-to-run)
  - [Unit vs integration (how we label it here)](#unit-vs-integration-how-we-label-it-here)
  - [`__tests__/smoke.test.tsx`](#__tests__smoketesttsx)
  - [`features/cards/Card.test.tsx`](#featurescardscardtesttsx)
  - [`lib/api/client.test.ts`](#libapiclienttestts)
  - [`lib/api/modules.test.ts`](#libapimodulestestts)
  - [`lib/queries/useSession.integration.test.tsx`](#libqueriesusesessionintegrationtesttsx)
  - [`domain/filter.test.ts`](#domainfiltertestts)
  - [`features/filters/store.test.ts`](#featuresfiltersstoretestts)
  - [`app/onboarding/flow-config.test.ts`](#apponboardingflow-configtestts)
  - [`app/onboarding/auth-status.test.ts`](#apponboardingauth-statustestts)
  - [Quick reference](#quick-reference)

---


## How to run

| Suite | Command | Notes |
|-------|---------|--------|
| **Vitest** (most files) | From `endpoint-nextjs/`: `npm run test` | Includes RTL, `lib/api`, domain, filters, cards, integration hook test. |
| **Vitest watch** | `npm run test:watch` | Same tests, watch mode. |
| **Coverage** | `npm run test:coverage` | HTML + JSON under `coverage/` (gitignored). |
| **`node:test` onboarding** | Not part of `npm run test` | See [Onboarding (`node:test`)](#apponboardingflow-configtestts); run with your TS + `node:test` setup (e.g. `node --test` with a TS loader). |

---

## Unit vs integration (how we label it here)

| Label | Meaning in this repo |
|-------|----------------------|
| **Unit** | One module or one UI component; dependencies are **mocked** or **stubbed** (`fetch`, hooks), or logic is **pure** (no real network, no multi-hook React stack). |
| **Integration** | Several **real** layers wired together (e.g. React Query + hooks + `apiFetch`); only the **network** (`fetch`) is stubbed. |

---

## `__tests__/smoke.test.tsx`

**Type: unit**

| Test | What it checks |
|------|----------------|
| Renders copy | Vitest + React Testing Library run; minimal render smoke. |

---

## `features/cards/Card.test.tsx`

**Type: unit** (`useSession`, `useMarkRead`, and `track` are mocked; only **Card** is under test.)

| Test | What it checks |
|------|----------------|
| Shows unread affordance when authed and unread | Unread dot when `isRead={false}` and session mocked authed. |
| Hides unread affordance when read | No unread dot when `isRead={true}`. |
| Does not mark read when anonymous | Click card → `mutate` not called when session mocked anonymous. |
| Marks read on card click when authed and unread | Click card → `markRead` `mutate` called with `["item-1"]`. |

---

## `lib/api/client.test.ts`

**Type: unit** (`fetch` stubbed; **`apiFetch`** is the real code under test.)

| Test | What it checks |
|------|----------------|
| Joins relative paths + credentials | Full URL + `credentials: "include"`. |
| Returns parsed JSON on 200 | JSON body parsed and returned. |
| Returns undefined on 204 | No-body success path. |
| Throws ApiError using JSON `detail` | Error shape / message from JSON error body. |
| Throws ApiError for non-JSON error | Generic message path for non-JSON error body. |

---

## `lib/api/modules.test.ts`

**Type: unit** (HTTP / client **contract** tests: real `lib/api/*` helpers, **`fetch` stubbed**; no real server, no React Query.)

Each test calls one API helper and asserts URL, method, and/or body (or return behavior for `getMe`).

| Area | Tests (short names) |
|------|---------------------|
| **courses** | `fetchCatalog`, `fetchTrackedCourses`, `setTrackedCourses` (PUT + `course_ids`) |
| **connections** | `fetchConnections`, `disconnectPlatform`, `reconnectPlatform`, `attachTeamsConnection`, `attachMoodleConnection` |
| **users** | `fetchMe`, `deleteMe` |
| **assignments** | `fetchAssignments` (no query + with `course_id` / `platform`), `fetchAssignment` |
| **announcements** | `fetchAnnouncements` with query string |
| **readState** | `fetchReadState`, `markRead`, `clearReadState` |
| **preferences** | `fetchPreferences`, `setPreferences` |
| **events** | `fetchEvents` with `course_id` |
| **auth** | `teamsFinalize`, `moodleLogin`, `logout`, `getMe` returns `null` on 401, `getMe` rethrows non-401 `ApiError` |

---

## `lib/queries/useSession.integration.test.tsx`

**Type: integration** (real **`QueryClient`**, **`useSession`**, **`useConnections`**, **`apiFetch`**; only **`fetch`** stubbed.)

| Test | What it checks |
|------|----------------|
| Treats 200 connections as authenticated | Stub 200 + JSON → `isResolved` / `isAuthed` true; request hits connections URL. |
| Treats 401 on connections as anonymous (resolved, not authed) | Stub 401 → `isResolved` true, `isAuthed` false. |

---

## `domain/filter.test.ts`

**Type: unit** (pure **`matchesFilters`** / **`hasActiveFilters`** on in-memory items.)

| Test | What it checks |
|------|----------------|
| Empty course set → all pass | No course filter = all items match. |
| Course id in selection → keep | Item with matching `course.id` passes. |
| Course id not in selection → drop | Item filtered out. |
| Multi-select courses | Item passes if `course.id` is in the selected set. |
| `hasActiveFilters` false / true | No active filters vs at least one course selected. |

---

## `features/filters/store.test.ts`

**Type: unit** (Zustand **filter store** only; no UI, no network.)

| Test | What it checks |
|------|----------------|
| `toggleCourse` adds then removes a course id | `courses` set updates on toggle. |
| `setCourses` replaces the selection | Full replace of selected course ids. |

---

## `app/onboarding/flow-config.test.ts`

**Type: unit** — **`node:test`**, not Vitest (not included in `npm run test` unless you add it.)

| Test | What it checks |
|------|----------------|
| Full flow skips telegram… | `getOnboardingFlowConfig` steps / initial for cold-start scenario. |
| Returning users without tracked courses… | Same helper for returning-user scenario. |

---

## `app/onboarding/auth-status.test.ts`

**Type: unit** — **`node:test`**, not Vitest; **`hasValidSessionToken`** with **injected `fetch`**, no real network.

| Test | What it checks |
|------|----------------|
| No session token | `undefined` token → false. |
| Missing base URL | Blank `baseUrl` → false. |
| API accepts token | Stubbed 200 → true; request shape asserted. |
| API rejects token | 401 → false. |
| Lookup throws | `fetch` throws → false. |

---

## Quick reference

| Type | Files |
|------|--------|
| **Integration** | `lib/queries/useSession.integration.test.tsx` |
| **Unit** | `__tests__/smoke.test.tsx`, `features/cards/Card.test.tsx`, `lib/api/client.test.ts`, `lib/api/modules.test.ts`, `domain/filter.test.ts`, `features/filters/store.test.ts`, `app/onboarding/flow-config.test.ts`, `app/onboarding/auth-status.test.ts` |

| You want… | Go to… |
|------------|--------|
| Full Vitest run from repo | `cd src/EndpointNextJS/endpoint-nextjs && npm run test` |
| Coverage report | `npm run test:coverage` → open `coverage/index.html` |
| Card + session UI behavior | `features/cards/Card.test.tsx` |
| Raw `apiFetch` behavior | `lib/api/client.test.ts` |
| Per-endpoint URLs / methods | `lib/api/modules.test.ts` |
| Session + connections stack | `lib/queries/useSession.integration.test.tsx` |
| Course filter rules in lists | `domain/filter.test.ts` |
| Filter sidebar store | `features/filters/store.test.ts` |
| Onboarding config / session helper | `app/onboarding/*.test.ts` (run outside Vitest unless wired in) |
