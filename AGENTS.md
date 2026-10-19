# AGENTS.md

## Tool Chain

Use `uv`, not `pip`. Use `uv run` to execute anything.
```bash
cd src/<component> && uv sync && uv run <cmd>
```

Copy `.env.example` to `.env` before first run. Docker must be running for any database work (`docker compose up db -d`).

## Component Isolation

- `src/ingestion/` has ZERO Django dependencies. Do NOT add Django imports.
- `src/common/db_core/` is schema-only. No views, no serializers, no business logic.
- Cross-component imports go through `db_core` models only.
- Non-Django components MUST call `core.bootstrap.init()` before importing models.
- Config env vars use the `ENDPOINT_` prefix (e.g. `ENDPOINT_TEAMS__GRAPH_BASE_URL`).

## Definition of Done

Applies to any task that **modifies code**. Pure-doc, config-only, or scaffolding tasks are exempt from the code-quality checks but still owe a docs pass (see step 2).

1. **Code-quality checks** — ALL of these must exit 0 in the affected component:
   ```bash
   bash scripts/run_component_checks.sh <target>
   ```
   Targets:
   - `ingestion` — runs Ruff, format, pyright, and `pytest tests/ -m "not e2e" -v`
   - `ingestion-e2e` — runs `pytest tests/e2e/ -v -m e2e` only; run separately and only when you explicitly need the daemon E2E suite
   - `presentation-api`
   - `db-core`
   - `processor`
   - `ui` — runs `npm run lint`, `npm run test`, and `npm run build` in `src/EndpointNextJS/endpoint-nextjs`
   - `all` — runs the safe non-E2E checks across all components, including the student UI

   Examples:
   ```bash
   bash scripts/run_component_checks.sh ingestion
   bash scripts/run_component_checks.sh presentation-api
   bash scripts/run_component_checks.sh ui
   bash scripts/run_component_checks.sh ingestion-e2e
   ```
   If any fail, stop and report — do NOT claim "done", do NOT use `--no-verify`.

2. **Docs pass** — once the checks above are green, invoke the `update-docs` skill to align documentation with the change. The skill walks the doc layers, applies edits, and reports back; the developer validates before committing.

3. **UI prod handoff** — if the task changed UI code, offer to rebuild and smoke-test the affected UI image using `docs/UI_PROD_REBUILD.md` before the developer runs prod again.

A task is "done" only after the applicable steps complete.

## When Stuck

- 3 failed attempts at the same fix: stop, report what you tried.
- Missing dependency: check `pyproject.toml`, then ask.
- Merge conflict: stop, show files, ask.
- Unclear requirements: ask. Do NOT guess.
- Tests pass locally but CI fails: check `.github/workflows/` — CI uses a Postgres service container.

## Git Conventions

Commits: `<type>(<scope>): <description>`
- Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
- Scopes: `ingestion`, `db-core`, `processing`, or omit for cross-cutting

Branches: `<type>/<scope>-<short-description>`



## NEVER

- Commit `.env`, tokens, credentials, or secrets.
- Modify a migration already merged to `main`.
- Push directly to `main`.
- Add dependencies without asking.
- Import across component boundaries except through `db_core`.
- Store plaintext tokens.
- Run `scripts/reset_db.sh` without asking — it drops the entire database.

## ASK FIRST

- Crossing a component boundary.
- Changing a Protocol interface in `ports.py` — breaks all consumers.
- Adding a new platform adapter or content type.
- Modifying `docker-compose.yml` or shared `scripts/`.
- Upgrading a dependency version.

## Agent-Facing Docs

Read the relevant docs BEFORE working on a component:

| When working on | Read first |
|-----------------|-----------|
| Ingestion internals | `docs/ingestion/CONVENTIONS.md`, `docs/ingestion/OVERVIEW.md` |
| Consuming ingestion from Django | `docs/ingestion/INTEGRATION_GUIDE.md` |
| Adding a new connector | `docs/ingestion/ADDING_A_CONNECTOR.md` |
| DB Core | `docs/db-core/common-db-agents.md`, `docs/db-core/common-db-developer-guide.md` |
| Creating/restructuring a component | `docs/COMPONENT_STRUCTURE.md` |
| DB schema | `docs/artifacts/db-schema/db-schema-v4.md` |

## Component Rules

See `src/ingestion/AGENTS.md` and `src/common/db_core/AGENTS.md`.

## Notes

`CLAUDE.md` is a symlink to this file. Windows users: run `git config core.symlinks true` before cloning, or enable Developer Mode.
