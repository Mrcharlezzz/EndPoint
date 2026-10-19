# Endpoint Student Frontend

Next.js student-facing app for Endpoint. This is the app served at
`http://localhost:3000` by the prod-shaped root launcher.

## Role

The frontend owns:

- onboarding for Moodle and Teams connections
- course selection and preferences
- assignments, announcements, events, and material views
- read-state, filtering, and search
- settings for account, platforms, courses, preferences, and data controls

It talks to the presentation API under `/api/v1/*` and proxies Teams headless
auth traffic through `/headless/*`.

The Vite app at `src/ingestion/dashboard/` is separate. That dashboard is for
daemon operators, not students.

## Local Development

For the full app with all backend services, use the repo-root launcher:

```bash
scripts/prod
```

For frontend-only iteration after the backend is already running:

```bash
cd src/EndpointNextJS/endpoint-nextjs
npm install
npm run dev
```

Open `http://localhost:3000`.

## Commands

```bash
bash ../../../scripts/run_component_checks.sh ui
```

When UI source changes need to be handed to prod-shaped compose, rebuild the
frontend image as described in [`../../../docs/UI_PROD_REBUILD.md`](../../../docs/UI_PROD_REBUILD.md).
