<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Endpoint Agent Rules

## Scope
- Primary app code is under `EndpointNextJS/endpoint-nextjs`.
- Do not edit files outside this app unless the user explicitly asks.

## Package Manager
- Use `npm` in this project.
- Preferred commands:
  - `npm install`
  - `npm run dev`
  - `npm run build`
  - `npm run lint`

## Change Discipline
- Make focused edits only for the request.
- Do not perform drive-by refactors.
- Preserve existing naming/style patterns (`announce-*`, `assign-*`, `dashboard-*`, `navbar-*`).
- Avoid editing Docker files unless requested.

## UI and Accessibility
- Keep desktop and mobile behavior consistent.
- Maintain responsive layouts; avoid regressions on navbar/sidebar pages.
- Ensure interactive elements have accessible labels.

## Auth and Secrets
- Never expose or log secrets (Firebase keys, ngrok tokens, credentials).
- For OAuth issues on ngrok, verify Firebase authorized domains first.

## Git Workflow
- Do not create commits unless the user explicitly asks.
- Avoid destructive git commands unless explicitly requested.

## Verification
- After substantial changes, run `npm run build`.
- If build/lint cannot run, explain why and provide the exact blocker.
