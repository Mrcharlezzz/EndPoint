# Connector Reference

> Per-platform details: auth, APIs, data mapping, pagination, quirks,
> and error handling for each adapter.

---

## Microsoft Teams

**Source files:**
- `adapters/teams/adapter.py` — TeamsAdapter orchestrator
- `adapters/teams/csa_fetcher.py` — CSA message fetching
- `adapters/teams/graph_fetcher.py` — Graph structural data
- `adapters/teams/assignments_fetcher.py` — Education Assignments API
- `adapters/teams/normalizer.py` — Raw Teams → NormalizedItem
- `adapters/teams/endpoints.py` — URL builders
- `adapters/teams/credentials.py` — TeamsCredentials TypedDict

**Scopes:** `messages`, `files`, `assignments`
**Auth:** Dual Bearer tokens (Graph + CSA), provided by caller as `dict`

### Three Complementary APIs

Teams uses three separate APIs, all accessed with the same Graph token:

| API | Base URL | What It Provides | Auth |
|-----|----------|------------------|------|
| Microsoft Graph | `graph.microsoft.com/v1.0` | Channels, files, drives, team metadata | `Bearer {graph_token}` |
| Teams CSA | `teams.cloud.microsoft/api/csa/emea/api/v2` | Message content, reactions, threads | `Bearer {csa_token}` |
| Education Assignments | `assignments.edu.cloud.microsoft` | Assignments, due dates, resources, grades | `Bearer {graph_token}` |

CSA and Graph are **complementary, not fallbacks**:
- CSA fetches messages/threads (high-frequency data)
- Graph fetches structural data — channels, files (low-frequency data)
- Both run in **parallel** via `asyncio.gather()` when both tokens available

If CSA token is absent, the MESSAGES scope falls back to Graph for messages
(less detail, but functional).

### Data Mapping

| Platform Field | NormalizedItem Field | Transform |
|---|---|---|
| CSA `id` | `external_id` | Prefixed: `teams_csa_{id}` |
| CSA `content` | `body` | HTML → stripped plain text |
| CSA `imDisplayName` / `fromDisplayNameInToken` | `author` | First non-empty |
| CSA `composeTime` / `originalArrivalTime` | `published_at` | ISO 8601 parse |
| CSA `properties.files[]` | `attachments` | → `FileRef` list |
| Graph message `body.content` | `body` | HTML → stripped plain text |
| Graph message `from.user.displayName` | `author` | Nested extract |
| Graph message `createdDateTime` | `published_at` | ISO 8601 parse |
| Assignment `displayName` | `title` | Direct |
| Assignment `instructions.content` | `body` | HTML → stripped plain text |
| Assignment `dueDateTime` | `deadline` | ISO 8601 parse |
| Assignment `assignedDateTime` | `published_at` | ISO 8601 parse |
| Assignment `_resources[]` | `attachments` | → `FileRef` list (adapter injects) |
| Drive item `name` | `FileRef.filename` | Direct |
| Drive item `@microsoft.graph.downloadUrl` | `FileRef.download_url` | Direct |
| Drive item `file.mimeType` | `FileRef.content_type` | Nested extract |
| Drive item `size` | `FileRef.size_bytes` | Direct |

### API Specifics

**Graph API:**
- Pagination: OData `@odata.nextLink` for paginated responses
- Rate limits: ~10,000 requests/10 minutes per app (throttled at 10 req/s by adapter)
- Endpoint patterns:
  - `/me/joinedTeams` — discover teams
  - `/teams/{id}/channels` — list channels
  - `/teams/{id}/channels/{id}/messages` — channel messages
  - `/groups/{id}/drives` — team drives
  - `/drives/{id}/root/children` — drive root items
  - `/drives/{id}/items/{id}/children` — folder children (recursive)

**CSA API:**
- Pagination: `continuationToken` in response for next page
- Page size: configurable, default 50
- Endpoint: `/teams/{threadId}/channels/{channelId}`
- **Critical quirk:** `threadId` must be the **General channel's ID**, not the
  sub-channel's own ID. The adapter maps this via `general_channel_map`.

**Assignments API:**
- Pagination: standard JSON array (no pagination needed — assignments per class are small)
- Endpoint: `/api/v1.0/edu/classes/{classId}/assignments`
- Resources: `/api/v1.0/edu/classes/{classId}/assignments/{id}/resources`
- Uses the Graph token (same `graph.microsoft.com` audience) despite being a
  different service (`assignments.edu.cloud.microsoft`)

### Fetch Strategy

1. **Discovery:** `fetch_joined_teams()` → build `team_name_map`
2. **Parallel:** `asyncio.gather()` across scopes (messages, files, assignments)
3. **Messages:** For each team → discover channels → for each channel → try CSA, fall back to Graph
4. **Files:** For each team → discover drives → recursive folder traversal
5. **Assignments:** For each team → fetch class assignments → fetch resources per assignment

All channel/drive/assignment fetches are semaphore-bounded (`max_concurrent_requests`).

### Error Handling

- `TokenExpiredError` (401): Propagated immediately, marks token expired
- `InsufficientScopeError` (403): Channel/drive skipped, error logged (common for SDT course teams)
- CSA failures: Falls back to Graph for that channel
- Graph 403 on channel listing: Returns empty channel list, logs compact error
- Normalization failures: Item skipped, `FetchError` collected

### Known Limitations

- Graph 403 on some SDT course teams — admin-level restriction, not fixable without admin
- CSA token lifetime ~24h (rolling), Graph token ~90min — both refreshed via headless Playwright
- `content_type` is always `raw_message` for messages (LLM classification is Django's job)
- No reaction/reply threading in NormalizedItem — preserved in `raw_payload` only

---

## Moodle

**Source files:**
- `adapters/moodle/adapter.py` — MoodleAdapter orchestrator
- `adapters/moodle/fetcher.py` — WS function wrappers
- `adapters/moodle/client.py` — MoodleRestClient (POST-based HTTP)
- `adapters/moodle/normalizer.py` — Raw Moodle → NormalizedItem
- `adapters/moodle/credentials.py` — MoodleCredentials TypedDict

**Scopes:** `assignments`, `forums`, `materials`, `events`, `files`
**Auth:** Moodle web service token (`wstoken`), obtained via `/login/token.php`

### API

- **Base URL:** `https://elearning.constructor.university` (configurable)
- **Protocol:** REST, all requests are POST with `wstoken`, `wsfunction`, `moodlewsrestformat=json`
- **Rate limits:** Not formally documented; adapter rate-limits at 10 req/s
- **Auth failure:** 200 response with `{"exception": "...", "errorcode": "invalidtoken"}` —
  caught by `MoodleRestClient._parse_response()`

### Web Service Functions Used

| Function | Scope | Returns |
|----------|-------|---------|
| `core_webservice_get_site_info` | auth test | Site info + user ID |
| `core_enrol_get_users_courses` | discovery | Enrolled courses |
| `mod_assign_get_assignments` | assignments | Assignments grouped by course |
| `mod_forum_get_forums_by_courses` | forums | Forum list (filtered by scope) |
| `mod_forum_get_forum_discussions` | forums | Discussions per forum |
| `core_calendar_get_action_events_by_timesort` | events | Calendar events (time-filtered) |
| `core_course_get_contents` | materials, files | Course sections with modules |

### Data Mapping

| Platform Field | NormalizedItem Field | Transform |
|---|---|---|
| Assignment `id` | `external_id` | `moodle_assign_{id}` |
| Assignment `name` | `title` | Direct |
| Assignment `intro` | `body` | HTML → stripped plain text |
| Assignment `duedate` | `deadline` | Unix timestamp → UTC datetime. `0` = `null` |
| Assignment `timemodified` | `published_at` | Unix timestamp → UTC datetime. `0` = `null` |
| Assignment `_course_name` | `course_name` | Injected by adapter from course list |
| Forum discussion `discussion` | `external_id` | `moodle_forum_{id}` |
| Forum discussion `name` | `title` | Direct |
| Forum discussion `message` | `body` | HTML → stripped plain text |
| Forum discussion `userfullname` | `author` | Direct |
| Forum discussion `timemodified` | `published_at` | Unix timestamp → UTC. `0` = `null` |
| Material module `id` | `external_id` | `moodle_material_{id}` |
| Material module `name` | `title` | Direct |
| Material module `description` | `body` | HTML → stripped plain text |
| Material module `contents[]` | `attachments` | Files with `type=file` → `FileRef` |
| Calendar event `id` | `external_id` | `moodle_event_{id}` |
| Calendar event `name` | `title` | Direct |
| Calendar event `description` | `body` | HTML → stripped plain text |
| Calendar event `timesort` | `start_time` | Unix timestamp → UTC |
| Calendar event `timemodified` | `published_at` | Unix timestamp → UTC datetime. `0` = `null` |
| Calendar event `course.fullname` | `course_name` | Nested in event response |

### Fetch Strategy

1. **Discovery:** `fetch_user_courses()` → course list + name map
2. **Prefetch:** If `materials` or `files` scope active, `fetch_course_contents()` for all courses (parallel, once)
3. **Parallel:** `asyncio.gather()` across assignments, forums, events
4. **Materials/files:** Extracted synchronously from prefetched data (no extra API calls)

**Key optimization:** Course contents are fetched **once** and shared between
materials and files scopes, avoiding doubled API calls.

### Error Handling

- 200 with error body: `MoodleRestClient._parse_response()` detects `invalidtoken`, `accessexception`
- `invalidtoken`: Raises `TokenExpiredError`
- `accessexception`: Raises `InsufficientScopeError`
- Forum fetch failures: Individual forum skipped, error collected
- Normalization failures: Item skipped, `FetchError` collected

### Known Limitations

- `author` is `null` for assignments and materials (Moodle API doesn't expose creator)
- `published_at` uses `timemodified` for assignments, materials, and calendar events when Moodle provides it
- `materials` `_course_name` is always `""` — by design, course context is not available during synchronous extraction from prefetched data
- Forum scope is configurable: `"announcements"` (default) filters to news forums only
- `semester_start` config controls how far back events are fetched (default: 90-day rolling)

---

## Telegram

**Source files:**
- `adapters/telegram/adapter.py` — TelegramAdapter orchestrator
- `adapters/telegram/bot_client.py` — Telegram Bot API client
- `adapters/telegram/normalizer.py` — Raw Telegram → NormalizedItem
- `adapters/telegram/credentials.py` — TelegramCredentials TypedDict

**Scopes:** `messages` (only)
**Auth:** Bot API token (from @BotFather)

### API

- **Base URL:** `https://api.telegram.org` (configurable)
- **Protocol:** REST, GET requests to `/bot{token}/{method}`
- **Polling method:** `getUpdates` with offset-based pagination
- **Rate limits:** Strict — adapter rate-limits at 1 req/s (configurable)
- **Auth test:** `getMe` endpoint

### Key Methods Used

| Method | Purpose |
|--------|---------|
| `getUpdates` | Poll for new messages (offset, limit, timeout) |
| `getMe` | Validate bot token |

### Data Mapping

| Platform Field | NormalizedItem Field | Transform |
|---|---|---|
| `message_id` + `chat.id` | `external_id` | `tg_{chatId}_{messageId}` |
| `text` / `caption` | `body` | HTML stripped (usually plain text) |
| `chat.title` | `course_name` | Group/supergroup title |
| `chat.id` | `course_external_id` | String of chat ID |
| `from.first_name` + `from.last_name` | `author` | Joined with space |
| `forward_origin.sender_user` | `author` (forwarded) | Original author, not forwarder |
| `date` | `published_at` | Unix timestamp → UTC datetime |
| `document` | `attachments[0]` | → `FileRef` |
| `photo` (largest) | `attachments[0]` | → `FileRef` (largest photo size selected) |
| `video` | `attachments[0]` | → `FileRef` |

### Fetch Strategy

1. Load `last_update_id` from cursor
2. Call `getUpdates(offset=last_update_id + 1)`
3. Filter to `group` / `supergroup` chats only (private messages skipped)
4. Process both `message` and `edited_message` updates
5. Track `max_update_id` for next cursor

### Forwarded Message Attribution

| `forward_origin.type` | `author` resolved from |
|---|---|
| `user` | `sender_user.first_name` + `last_name` |
| `hidden_user` | `sender_user_name` |
| `channel` | `chat.title` |
| `chat` | `chat.title` |
| (not forwarded) | `from.first_name` + `last_name` |

Forwarder info is preserved in `raw_payload` for downstream use.

### Error Handling

- `TokenExpiredError`: Bot token invalid, cursor preserved (not advanced)
- `IngestionError`: Platform issue, cursor preserved
- Unexpected exceptions: Caught, cursor preserved, error logged
- Non-group messages: Silently filtered (by design)

### Known Limitations

- **Privacy mode must be OFF** — prerequisite for the bot to see group messages
- `content_type` is always `raw_message` — LLM classification happens downstream in Django
- `title` is always `null` — messages don't have titles
- File download URLs contain the bot token (`/file/bot{token}/{file_id}`) — accepted and documented
- No reaction/thread data — only `message` and `edited_message` updates processed
- Edited messages produce same `external_id` as originals — triggers upsert overwrite

---

## Platform Field Mapping Summary

| Field | Moodle | Teams | Telegram |
|-------|--------|-------|----------|
| `external_id` | `moodle_{type}_{id}` | `teams_{source}_{id}` | `tg_{chatId}_{msgId}` |
| `content_type` | `assignment`, `announcement`, `material`, `event` | `raw_message`, `assignment` | `raw_message` |
| `title` | Assignment/forum/material name | Assignment `displayName` only | Always `null` |
| `author` | Forum: `userfullname`. Others: `null` | CSA: `imDisplayName`. Graph: `from.user.displayName` | `from` or `forward_origin` |
| `published_at` | `timemodified` (unix) | `composeTime`/`createdDateTime` (ISO) | `date` (unix) |
| `deadline` | `duedate` (unix, 0=null) | `dueDateTime` (ISO) | Always `null` |
| `start_time` | `timesort` for events | N/A | Always `null` |
| `body` | Stripped from `intro`/`message`/`description` | Stripped from `content`/`body.content`/`instructions.content` | From `text`/`caption` |
