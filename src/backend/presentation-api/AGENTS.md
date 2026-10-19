# Presentation API — Agent Rules

Read the root `AGENTS.md` for cross-component rules. This file covers what is presentation-API-specific.

## Main Documentation

The canonical design doc for this component is the Technical Design Document at `docs/artifacts/design-docs/TDD-endpoint-presentation-api-v1.1.md` (repo root). Read it before making non-trivial changes — it covers architecture, data layer, auth/session management, the API contract, error handling, and configuration.

## When Writing Code

This component owns views, serializers, services, URLs, and its own `presentation_api_*` tables. Shared content schema lives in `db_core`. If you need a shared content model change, do it in `db_core`; presentation-api may keep `managed=False` mirror models plus state-only migrations so Django tracks those mirrors without creating or altering db-core tables.
