# Presentation API UI scaffold — change log

This document records changes made to align the Next.js app with:

- `docs/artifacts/design-docs/page-data-focus.md` (screen-level data emphasis)
- `docs/artifacts/design-docs/presentation_api.md` (REST contract)

Paths below are relative to `EndpointNextJS/endpoint-nextjs/` unless noted.

---

## 1. New files

### `lib/presentation-api/types.ts`

TypeScript types aligned with the presentation API design (courses, assignments, announcements, user profile, course selection, shared wrappers like `ResultsWrapper<T>`).

### `lib/presentation-api/scaffold.ts`

**Structural placeholders** (explicit “(placeholder)” copy, sentinel UUIDs, no realistic curriculum). Used only until `NEXT_PUBLIC_PRESENTATION_API_URL` and a valid bearer token reach your team’s Presentation API; then `server-data.ts` returns live JSON instead.

### `lib/presentation-api/server-data.ts`

Async loaders used from **Server Components**:

- `getCourses()`, `getAssignments()`, `getAnnouncements()`, `getCurrentUser()`, `getUserCourses()`
- If `NEXT_PUBLIC_PRESENTATION_API_URL` and `PRESENTATION_API_BEARER_TOKEN` are set and the request succeeds, returns **live** JSON.
- Otherwise returns **scaffold** data.
- Exports `PresentationDataSource` (`"live" | "scaffold"`) for UI hints.

### `lib/presentation-api/mappers.ts`

Pure functions mapping API/scaffold models to view models:

- Assignments → assignment page rows, dashboard lines, calendar marks
- Announcements → `FeedGroup[]` for the feed, dashboard “recent” previews
- User course selections → dashboard course cards

### `lib/presentation-api/announcement-feed.ts`

Types and constants for the announcements UI (`FeedTab`, `FeedPost`, `FeedGroup`, `feedTabLabels`) and `missedItemsFromFeed()` for the sidebar (derived from the feed; not specified in the API doc).

### `.env.presentation-api.example`

Example environment variables for pointing the app at a real Presentation API instance.

---

## 2. Modified files

### `app/dashboard/page.tsx`

- Converted to an **async** Server Component.
- Loads assignments, announcements, and user course selection in parallel via `server-data`.
- Passes mapped props into `DashboardContent` plus a short `dataSourceNote` (live vs scaffold).

### `components/DashboardContent.tsx`

- Accepts **props** instead of hard-coded courses, assignments, and calendar data.
- Calendar marks and side deadline list are driven by **assignment deadlines** from props.
- Adds a **Recent announcements** panel (with link to `/announcements`) per page-data-focus.
- Empty states for no courses / no upcoming assignments / no announcements.

### `app/assignments/page.tsx`

- Converted to an **async** Server Component.
- Uses `getAssignments()` + `splitAssignmentsByDeadline()` for upcoming vs past sections.
- Filter dropdowns populated from the loaded assignment set.

### `app/announcements/page.tsx`

- Converted to an **async** Server Component.
- Uses `getAnnouncements()` and `announcementsToFeedGroups()` for `AnnouncementsFeed`.
- Sidebar “snapshot” / “missed” uses counts and `missedItemsFromFeed()`.

### `components/AnnouncementsFeed.tsx`

- Imports feed types and `feedTabLabels` from `@/lib/presentation-api/announcement-feed` instead of `app/announcements/feed-data`.

### `app/profile/page.tsx`

- Converted to an **async** Server Component.
- Fetches presentation user and selected courses and passes them into `ProfileContent`.

### `components/ProfileContent.tsx`

- New optional props: `presentationUser`, `presentationCourses`, `presentationSource`.
- When the user is signed in (Firebase), renders an extra **Endpoint (Presentation API)** block (profile + selected courses), with copy distinguishing live vs scaffold.

---

## 3. Removed files

### `app/announcements/feed-data.ts`

Replaced by `lib/presentation-api/announcement-feed.ts` and data loaded on the server.

---

## 4. Not changed by this work

- **`/`** (marketing home) — unchanged.
- **`/notifications`** — still static UI; no notifications contract in `presentation_api.md`.
- **Auth** — Firebase sign-in unchanged; Presentation API bearer token is **separate** (server env) until MS login → session token is integrated end-to-end.

---

## 5. Design-doc rename (same conversation thread, repo root)

Under `docs/artifacts/design-docs/`:

- **`Document.md`** was removed and replaced by **`page-data-focus.md`** (same conceptual content, rewritten for clarity).

---

## 6. How to run against a real API

1. Copy `.env.presentation-api.example` into `.env.local` (or merge variables).
2. Set `NEXT_PUBLIC_PRESENTATION_API_URL` to the API origin (no secrets).
3. Set `PRESENTATION_API_BEARER_TOKEN` to a valid session token from `POST /api/v1/auth/login/` (keep server-only; do not commit).

If the token or URL is missing, or the request fails, the UI falls back to **scaffold** data.

---

## 7. Verification

From `EndpointNextJS/endpoint-nextjs`:

- `npm run build` — completed successfully after these changes.
- `npm run lint` — no new errors from the scaffold/mappers work (any pre-existing warnings elsewhere may remain).
