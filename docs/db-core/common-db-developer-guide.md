# Shared DB Layer — Developer Guide

## What This Is

The database schema for the entire project lives in `src/common/db_core/`. It's a Python package containing Django models, migrations, and a seed command. The actual Postgres database runs as a Docker container defined in the root `docker-compose.yml`.

You install the package once, and then any component you work on can import the models and query the database using Django's ORM.

---

## Project Structure

```
your-project/
├── docker-compose.yml          ← Postgres service
├── .env.example
├── scripts/
│   ├── migrate.sh              ← apply migrations
│   ├── seed.sh                 ← load dev data
│   └── reset_db.sh             ← destroy + recreate everything
├── src/
│   ├── common/
│   │   └── db_core/
│   │       ├── pyproject.toml
│   │       ├── manage.py
│   │       ├── config/         ← Django settings
│   │       └── core/           ← Django app (models, migrations, seed command)
│   ├── data-ingestor/
│   ├── data-processor/
│   └── server/
```

---

## Initial Setup

### 1. Start the database

From the project root:

```bash
cp .env.example .env
docker compose up db -d
```

This starts Postgres on `localhost:5432`. Data persists in a Docker volume across restarts.

### 2. Install the db_core package

```bash
pip install -e ./src/common/db_core
```

This installs Django and psycopg as dependencies, and makes `config` and `core` importable. The `-e` flag means editable — model changes are picked up immediately without reinstalling.

### 3. Run migrations and seed

```bash
./scripts/migrate.sh
./scripts/seed.sh
```

Done. You have a running Postgres with all tables and dev data.

---

## Daily Workflow

### After pulling changes

```bash
./scripts/migrate.sh
./scripts/seed.sh
```

### Full reset (wipes everything, starts fresh)

```bash
./scripts/reset_db.sh
```

This destroys the Docker volume, restarts Postgres, runs migrations, and seeds.

---

## Using Models in Your Component

### If your component is a Django app

Add `"core"` to its `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    "core",
    # ... your other apps
]
```

Then import anywhere:

```python
from core.models import User, Job, Result

pending = Job.objects.filter(status="pending").select_related("created_by")
```

### If your component is NOT a Django app

Initialize Django once at startup before importing models:

```python
import core.bootstrap
core.bootstrap.init()

from core.models import Job

jobs = Job.objects.filter(status="pending")
for job in jobs:
    print(job.name, job.payload)
```

After `init()`, the full ORM is available — filtering, creating, updating, transactions, aggregations, all of it.

---

## Making Schema Changes

1. Edit or create model files in `src/common/db_core/core/models/`.
2. Re-export new models in `core/models/__init__.py`.
3. Generate the migration:
   ```bash
   cd src/common/db_core
   python manage.py makemigrations core
   ```
4. Apply it:
   ```bash
   python manage.py migrate
   ```
5. **Check the seed command.** Open `core/management/commands/seed_all.py` and verify it still works with your changes. If you added a required field, renamed a field, or deleted a model, the seed command needs updating. Run `./scripts/seed.sh` to confirm.
6. Commit the model + migration + seed changes together in one PR.

**Rules:**
- Never edit a migration already merged to main. Create a new one.
- Every model must have `class Meta: db_table = "explicit_name"`.
- Always run `./scripts/seed.sh` after a migration to catch any seed breakage early.

---

## How Seeding Works

The seed command (`seed_all.py`) uses direct ORM calls — the same way you interact with the database in your application code. There are no JSON fixtures or external libraries.

All records use `update_or_create` so the command is idempotent — running it multiple times won't create duplicates, and will update existing records if seed values have changed.

When migrations change, the seed command behaves just like your app code: if a new required field is added, the seed command will fail with a clear error until you add that field to the seed calls. This is intentional — it keeps seeds in sync with the schema.

---

## Testing

### How it works

Tests run with **pytest** via the `pytest-django` plugin (both are included as dependencies of `db_core`). When you run tests, pytest-django automatically:

1. Creates a temporary database (`test_shared_db`)
2. Runs all migrations on it
3. Runs your tests — each test gets a clean, empty database via transaction rollback
4. Destroys the temporary database

The only requirement is that Postgres is running (`docker compose up db -d`).

### Writing tests

Tests create their own data inline. They do NOT depend on the seed command. Mark any test that touches the database with `@pytest.mark.django_db`:

```python
import pytest
from core.models import User, Job

@pytest.mark.django_db
def test_create_job():
    user = User.objects.create(email="test@example.com", role="admin")
    job = Job.objects.create(name="test job", created_by=user)

    assert job.status == "pending"
    assert Job.objects.count() == 1

@pytest.mark.django_db
def test_starts_empty():
    # passes — previous test's data was rolled back
    assert Job.objects.count() == 0
```

### Running tests

```bash
cd src/common/db_core
pytest
```

### Writing tests in other components

Any component that installs `db_core` can write tests against the models. The only extra step is adding the pytest config to that component's `pyproject.toml`:

```toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings"
```

Then tests work the same way — `@pytest.mark.django_db`, plain functions, `pytest` to run.

---

## Managing the Database Container

```bash
# Start
docker compose up db -d

# Stop (data persists)
docker compose down

# Stop and wipe all data
docker compose down -v

# Check status
docker compose ps

# Connect with psql
docker compose exec db psql -U postgres -d shared_db

# View logs
docker compose logs db
```

---

## Quick Reference

| Task | Command |
|---|---|
| Start database | `docker compose up db -d` |
| Install db_core | `pip install -e ./src/common/db_core` |
| Apply migrations | `./scripts/migrate.sh` |
| Seed dev data | `./scripts/seed.sh` |
| Flush + re-seed | `./scripts/seed.sh --flush` |
| Full reset | `./scripts/reset_db.sh` |
| Create a migration | `cd src/common/db_core && python manage.py makemigrations core` |
| Run tests | `cd src/common/db_core && pytest` |
| Django shell | `cd src/common/db_core && python manage.py shell` |
| Psql into DB | `docker compose exec db psql -U postgres -d shared_db` |
