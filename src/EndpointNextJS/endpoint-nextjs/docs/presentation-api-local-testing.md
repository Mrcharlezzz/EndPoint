# Presentation API — local testing & frontend integration

This guide explains how the **Next.js app** checks connectivity to the team’s **Presentation API** (Django), what `lib/presentation-api/commands.sh` is for, and how environment variables tie the two together.

**Related**

- API contract (design): `../../../docs/artifacts/design-docs/presentation_api.md` (repo root)
- UI scaffold / data loading: [presentation-api-ui-scaffold-changelog.md](./presentation-api-ui-scaffold-changelog.md)

---

## Teammate handoff: `commands.sh` as a FE ↔ BE skeleton

Someone on the team shared `lib/presentation-api/commands.sh` as a **skeleton for connecting frontend needs to backend endpoints**. That is a fair way to read it: the file lists **which HTTP routes matter** (health, login, profile, courses, selections, assignments, announcements, events) and shows **example `curl` calls** (method, path, headers, sample JSON). It is a bit **messy on purpose**—it also bundles Docker, Django (`manage.py`), **SQL seed** snippets for `psql`, and loose notes (`pytest`, `git`), because it doubles as a backend runbook.

**How the Next.js side maps onto that skeleton**

| Idea in the skeleton | Where it lives in this app |
|----------------------|----------------------------|
| Base URL, `/health/` | `NEXT_PUBLIC_PRESENTATION_API_URL`; smoke test: `scripts/test-presentation-api-smoke.sh` |
| `Authorization: Bearer` + `/api/v1/...` | **`lib/presentation-api/api.ts`** — one function per endpoint (`presentationGetCourses`, `presentationLogin`, `presentationPutMyCourses`, …). Returns `{ ok, status, data }` with **no** scaffold fallback. |
| Pages that should still render offline / without token | **`lib/presentation-api/server-data.ts`** — `getAssignments`, `getAnnouncements`, … wraps `api.ts` and falls back to **`scaffold.ts`** when the call fails or env is missing. |
| JSON response shapes | `lib/presentation-api/types.ts` and the design doc above (authoritative contract) |

Anything that is **SQL**, **virtualenv**, or **`runserver`** is **backend-only**; the frontend only needs the API up and the env vars set. When the real server differs slightly from the cheat sheet (e.g. login body shape), align with the deployed API and `presentation_api.md`, then adjust `server-data.ts` if needed.

---

## 1. Roles of the two shell files

| File | Purpose |
|------|---------|
| `scripts/test-presentation-api-smoke.sh` | **Runnable** from this app. Pings `/health/` and optionally `/api/v1/courses/` with a bearer token. Safe to run any time. |
| `lib/presentation-api/commands.sh` | **Skeleton / cheat sheet**: maps UI-facing flows to `curl` examples; backend setup + SQL live here too. Copy/paste from the **presentation-api** repo root. **Do not** run the entire file as one `bash` script (see §3). |

---

## 2. Smoke test (frontend / anyone)

From the **`endpoint-nextjs`** directory:

```bash
chmod +x ./scripts/test-presentation-api-smoke.sh   # once
./scripts/test-presentation-api-smoke.sh
```

Override the base URL for a single run:

```bash
BASE_URL=http://127.0.0.1:8000 ./scripts/test-presentation-api-smoke.sh
```

### What it does

1. **Loads `.env`** if present (same folder as `package.json`), so `NEXT_PUBLIC_PRESENTATION_API_URL` and `PRESENTATION_API_BEARER_TOKEN` apply automatically.
2. Resolves the API base URL in order: `BASE_URL` env → `NEXT_PUBLIC_PRESENTATION_API_URL` → `http://localhost:8000`. Trailing slashes are stripped.
3. **`GET {base}/health/`** — expects HTTP **200** or **204**. Any other code (including connection failure → `000`) exits with code **1** and prints a short tip.
4. If **`PRESENTATION_API_BEARER_TOKEN`** is set and non-empty (and not the literal `PASTE_TOKEN_HERE`), it also runs **`GET {base}/api/v1/courses/`** with `Authorization: Bearer …` and pretty-prints part of the JSON with `python3` when available.

### Typical results

- **API not running** — `Failed to connect`, HTTP `000`, exit `1`. Start the Django service (and database) from the backend project, then rerun.
- **API running, no token** — health check passes, script exits `0` and reminds you that the token is optional for authenticated checks.
- **API running + token** — health + courses response codes and a snippet of the courses body.

Response bodies for the health check are written to **`/tmp/endpoint-health.body`**; courses to **`/tmp/endpoint-courses.body`** (for quick inspection; replace with project-local temp files if you prefer not to use `/tmp`).

---

## 3. Team cheat sheet (`lib/presentation-api/commands.sh`)

This file is **documentation embedded in shell comments and commands**, intended for the **Django presentation-api** repository, not for the Next.js root.

### Why it is not one executable script

- **SQL** (`INSERT INTO …`) is **not** valid bash when left bare. It must be run inside **psql** (or a SQL client). In this repo the SQL is wrapped in a **here-doc** (`: <<'SEED_SQL_DOCS' …`) so `bash -n` succeeds and the SQL stays readable as reference only.
- **`docker compose up -d db`** followed immediately by **`docker compose down`** would tear the stack down if both were run blindly—treat those lines as notes and adjust to your actual workflow.
- **`python3 manage.py runserver`** **blocks** the terminal until you stop the server; `curl` lines must be run in **another** terminal (or after the server is already up).
- Trailing sections (`pytest`, `git add` / `commit` / `push`) are operational reminders, not something to automate from the frontend tree.

### How backend devs should use it

1. `cd` to the backend project root (`manage.py`, `docker-compose.yml`, `.venv`).
2. Copy/paste **one section at a time** (venv, docker, migrations, server, curls, psql seeds).
3. Run **seed SQL** only inside `psql` (e.g. `docker exec -it … psql …`), pasting from the documented block in the file.

The file header in `commands.sh` repeats this guidance.

---

## 4. Environment variables (Next.js)

Configure these in **`.env`** or **`.env.local`** in `endpoint-nextjs/` (see also `.env.presentation-api.example`).

| Variable | Scope | Purpose |
|----------|--------|---------|
| `NEXT_PUBLIC_PRESENTATION_API_URL` | Public (browser + server) | API origin, e.g. `http://localhost:8000`. No trailing slash required. |
| `PRESENTATION_API_BEARER_TOKEN` | Server only | Session token from your auth flow (e.g. `POST /api/v1/auth/login/`). Used by Server Components in `lib/presentation-api/server-data.ts` for live fetches. |

If the URL or token is missing, or the request fails, the app falls back to **scaffold** data (see scaffold changelog).

**Security:** Do not commit real tokens. This repo’s `.gitignore` ignores `.env*` by default; keep secrets local or in a secure secret store.

### Frontend URL vs API URL (common mix-up)

| URL | Typical example | Role |
|-----|-----------------|------|
| **Frontend (Next.js)** | `http://localhost:3000` | What you **type in the browser** after `npm run dev`. This is *not* where `NEXT_PUBLIC_PRESENTATION_API_URL` should point. |
| **Presentation API (backend)** | `http://localhost:8000` | Where **`NEXT_PUBLIC_PRESENTATION_API_URL`** and the smoke script’s `BASE_URL` should point — the Django (or team) service that serves `/health/` and `/api/v1/...`. |

Assignments, dashboard, announcements, and profile load data using **`fetch` on the Next.js server** (Server Components). That request goes **Next server → API URL** (e.g. 3000’s process → 8000). You still **browse** the site at the frontend URL; you do **not** replace the API env var with the frontend URL.

If you later call the API from **client-side** React (`useEffect`, etc.), the **browser** would talk to the API host (or a same-origin proxy). Then the backend must allow your frontend origin via **CORS** (e.g. `http://localhost:3000`).

---

## 5. Aligning smoke test with the app

Use the **same** base URL in:

- `NEXT_PUBLIC_PRESENTATION_API_URL` (for Next.js `fetch` in `server-data.ts`)
- `./scripts/test-presentation-api-smoke.sh` (via `.env` or `BASE_URL=…`)

After login, paste the session token into **`PRESENTATION_API_BEARER_TOKEN`** to verify authenticated routes with the smoke script and to enable live data in server-rendered pages.

---

## 6. Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Smoke test cannot connect | API process listening on expected host/port; firewall; correct `BASE_URL` / `NEXT_PUBLIC_PRESENTATION_API_URL`. |
| Health returns non-200 | Backend route `/health/` implementation; reverse proxy path. |
| Courses returns 401 | Token missing, expired, or wrong `Authorization: Bearer` format. |
| Next.js still shows placeholder data | `server-data.ts` only uses the bearer on the **server**; ensure `PRESENTATION_API_BEARER_TOKEN` is set for `next dev` / `next build` environment and restart the dev server after changing `.env`. |

---

## 7. Quick reference

```bash
# From endpoint-nextjs/
./scripts/test-presentation-api-smoke.sh

# Optional one-off URL
BASE_URL=http://127.0.0.1:8000 ./scripts/test-presentation-api-smoke.sh
```

Design contract for endpoints and payloads: **`../../../docs/artifacts/design-docs/presentation_api.md`** (relative to this `docs/` folder, under the Endpoint repo root).
