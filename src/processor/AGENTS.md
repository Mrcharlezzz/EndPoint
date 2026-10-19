# Processor — Agent Rules

Read the root `AGENTS.md` for cross-component rules. For the processor itself, read these before working on it:

| When working on | Read |
|-----------------|------|
| Anything | `docs/processing-engine/v4/processor-overview-v4.3.md` |
| Internal structure / boundaries | `docs/processing-engine/v4/processor-architecture-v4.1.md` |
| How it runs in production | `docs/processing-engine/v4/processor-runtime-v4.1.md` |
| Operational tasks / on-call | `docs/processing-engine/v4/processor-maintenance-v4.1.md` |
| Naming and code patterns | `docs/processing-engine/v4/processor-conventions-v4.md` |

Stage-specific docs live alongside these (`processor-classification-*`, `processor-dedup-*`, `processor-normalization-*`, `processor-persistence-*`, `processor-course-resolution-*`, `processor-pipeline-async-batch-design-*`, `processor-testing-*`) — read the relevant one when touching that stage.

This file covers what is processor-specific beyond the docs above.

## When Writing Code

The pipeline (`processing/pipeline.py`) MUST NEVER import an adapter directly. Callers inject adapters via the Protocol interfaces under `processing/ports/`.
