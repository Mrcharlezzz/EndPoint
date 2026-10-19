# DB Core — Agent Rules

Read `docs/db-core/common-db-agents.md` for the full spec. This file covers what breaks silently.

## When Changing Schema

- NEVER edit a migration already merged to `main`. Generate a new one.
- Use `python manage.py makemigrations core` — always specify `core`, not bare `makemigrations`.
- Commit model + migration + seed changes together. A model change without its migration will break CI.
- Every model MUST set `class Meta: db_table = "explicit_name"`.
- All model classes MUST be re-exported from `core/models/__init__.py` or they are invisible to Django.

## When Running Locally

Docker must be running: `docker compose up db -d`.
Wait for healthcheck before running migrations — the `reset_db.sh` script handles this, but manual runs don't.
The test database is auto-created/destroyed by `pytest-django` — do NOT create fixtures for it.

## When Modifying Seeds

`seed_all.py` MUST be idempotent via `update_or_create`. Running it twice must not create duplicates.
When you add a required field without a default, the seed command will break — update it in the same commit.

## Non-Django Consumers

Any component outside Django that imports models must call `core.bootstrap.init()` first.
This is idempotent but MUST happen before any `from core.models import ...` line.
Forgetting this produces a cryptic `django.core.exceptions.AppRegistryNotReady` error.
