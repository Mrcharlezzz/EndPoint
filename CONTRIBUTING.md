# Contributing to Endpoint

This guide covers the practical steps to land a change. The authoritative
component carve-outs, ASK-FIRST list, and NEVER list live in [`AGENTS.md`](./AGENTS.md).

## Setup

```bash
# Clone the repo. Windows: run `git config core.symlinks true` before cloning
# so the AGENTS.md / CLAUDE.md symlink resolves correctly.
git clone https://github.com/<org>/endpoint.git
cd endpoint

# Copy the example env and fill the required secrets documented inside it.
cp .env.example .env

# Optional local hooks.
uv tool install pre-commit
pre-commit install
```

For the project layout and component guides, start with the
[documentation index](./docs/README.md). Configuration templates and local
check commands are described in the [root README](./README.md).

## Branches And Commits

Branches: `<type>/<scope>-<short-description>`, for example
`feat/ingestion-runbook` or `fix/api-cors-credentials`.

Commits: `<type>(<scope>): <description>`.

| Type | When |
| --- | --- |
| `feat` | New user-visible capability. |
| `fix` | Bug fix. |
| `refactor` | Restructure without behavior change. |
| `test` | Tests-only changes. |
| `docs` | Documentation. |
| `chore` | Tooling, dependencies, CI, or maintenance. |
| `perf` | Performance improvements. |
| `ci` | CI workflow or pipeline only. |

Common scopes are `ingestion`, `db-core`, `processor`, `presentation-api`,
`frontend`, and `infra`.

## Workflow

Keep changes component-scoped where possible. If a change crosses component
boundaries, call that out in the PR and verify each affected component. Shared
schema changes belong in `src/common/db_core/`; non-Django components must call
`core.bootstrap.init()` before importing models.

Do not modify shared scripts, compose files, dependency versions, protocol
interfaces, or new platform/content-type boundaries without an explicit
decision first. See `AGENTS.md` for the full list.

## Test And Lint Gates

A code change is done only when the affected component target exits 0:

```bash
bash scripts/run_component_checks.sh <target>
```

Targets:

| Target | Scope |
| --- | --- |
| `ingestion` | Ruff, format, pyright, and non-E2E ingestion tests. |
| `ingestion-e2e` | Daemon E2E tests only; run when the daemon E2E suite is in scope. |
| `presentation-api` | Presentation API checks. |
| `db-core` | Shared schema checks. |
| `processor` | Processor checks. |
| `ui` | Student frontend lint, tests, and build. |
| `all` | Safe non-E2E checks across components, including the student UI. |

Do not skip checks with `--no-verify`.

If an ingestion router signature changes, regenerate the OpenAPI snapshot in the
same change:

```bash
cd src/ingestion
uv run python scripts/dump_openapi.py --out ../../docs/ingestion/openapi.json
```

## Documentation

Every code change needs a docs pass after checks are green. Start with
[`docs/README.md`](./docs/README.md) to find the right documentation layer.

Keep current operating docs separate from historical planning notes. Archive
documents that are useful history but should not guide new work.

## Code Review

Reviews should focus on behavior, boundaries, operability, tests, and docs.
For component-specific rules, read the relevant `AGENTS.md` file and the docs
listed in the root `AGENTS.md`.

## Reporting Issues

Bug reports should include:

- component (`ingestion`, `presentation-api`, `frontend`, `processor`, etc.)
- reproduction steps
- expected vs. actual behavior
- relevant logs with `request_id`, `sync_id`, or run directory when available
- whether there is a workaround

Do not open public issues for secrets or security-sensitive reports.
