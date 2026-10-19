# Component Structure

Endpoint project layout and component boundaries.

**Target audience:** Developers and agents orienting in the repo or adding a
new component.

## Current Layout

Python components use a flat package layout: the Python package directory sits
directly inside the component root, not inside a nested `src/` subdirectory.

| Component | Path | Purpose |
| --- | --- | --- |
| Ingestion daemon, connectors, headless auth, operator API | `src/ingestion/` | Fetches Teams, Moodle, and Telegram data; owns sync scheduling and runtime state. |
| Operator dashboard | `src/ingestion/dashboard/` | Vite app for daemon observability and operator controls. |
| Shared DB core | `src/common/db_core/` | Django ORM schema layer shared by ingestion, processor, and presentation API. |
| Processor | `src/processor/` | Classifies, normalizes, resolves courses, deduplicates, and persists processed items. |
| Presentation API | `src/backend/presentation-api/` | Django/DRF API for the student frontend, auth/session state, user preferences, and read state. |
| Student frontend | `src/EndpointNextJS/endpoint-nextjs/` | Next.js app for onboarding, assignments, announcements, settings, and search. |

The shared database layer follows its own convention at `src/common/db_core/`.
Do not restructure it casually.

## Repository Layout

```text
Endpoint/
├── .github/workflows/          # CI workflows
├── .env.example                # documented env surface
├── docker-compose.yml          # shared dev compose stack
├── docker-compose.prod.yml     # prod-shaped override
├── docker/                     # service Dockerfiles
├── scripts/                    # shared ops scripts
├── docs/
│   ├── README.md               # docs map and archive policy
│   ├── artifacts/              # design docs, schemas, specs, research
│   ├── integration/            # runnable workflows and data runbooks
│   ├── ingestion/              # ingestion component documentation
│   ├── db-core/                # shared DB documentation
│   ├── processing-engine/      # processor documentation
│   ├── archive/                # historical docs
│   └── COMPONENT_STRUCTURE.md  # this file
└── src/
    ├── common/db_core/
    ├── ingestion/
    ├── processor/
    ├── backend/presentation-api/
    └── EndpointNextJS/endpoint-nextjs/
```

## Naming

| Item | Convention | Example |
| --- | --- | --- |
| Component directory | Lowercase, single word, underscore-separated, or established framework name. | `ingestion`, `processor`, `presentation-api`, `endpoint-nextjs` |
| Python package | Flat under its component root. | `src/ingestion/ingestion/`, `src/processor/processing/` |
| `pyproject.toml` name | Existing package name; prefer kebab-case for new components. | `presentation-api`, `processor` |
| Test files | `test_<module>.py` | `test_pipeline.py` |
| Component checks | `bash scripts/run_component_checks.sh <target>` | `bash scripts/run_component_checks.sh processor` |
| Docs directory | Nearest stable component or subsystem name. | `docs/ingestion/`, `docs/processing-engine/` |

## Python Component Template

New Python components should follow this shape unless the component has a
framework-specific reason to differ:

```text
src/<component>/
├── pyproject.toml
├── <package>/
│   ├── __init__.py
│   └── ...
├── tests/
│   ├── conftest.py
│   └── test_*.py
└── scripts/
```

Use `uv` for local setup and execution:

```bash
cd src/<component>
uv sync
uv run pytest
```

## Import Convention

Internal imports use package names directly:

```python
from ingestion.adapters.moodle import MoodleAdapter
from processing.application.pipeline import process_batch
```

Cross-component imports go through `db_core` models only:

```python
from core.models import Course, IngestedItem, BaseClassifiedItem
```

Non-Django components initialize the ORM before importing models:

```python
import core.bootstrap

core.bootstrap.init()

from core.models import Course
```

## Validation Checklist

After adding or restructuring a component:

- [ ] Component root is under `src/`.
- [ ] Python package is flat under the component root.
- [ ] `pyproject.toml` is at the component root for Python components.
- [ ] `uv sync` succeeds from the component root.
- [ ] Imports work without a `src.` prefix.
- [ ] Tests live under the component or its established framework directory.
- [ ] New docs are linked from `docs/README.md` or the nearest subsystem README.
- [ ] `bash scripts/run_component_checks.sh <target>` passes for the affected target.
