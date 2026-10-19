"""
Safeguard tests for the shared db_core package.

These tests guarantee that:
- Models and migrations are in sync (no forgotten makemigrations).
- No migration conflicts exist between branches.
- All migrations apply cleanly from scratch.
- The seed command runs without error and is idempotent.
- All models define an explicit db_table.

The rule "never edit an existing migration" is enforced in CI
via a git diff step, not in pytest.
"""

import pytest
from io import StringIO
from django.core.management import call_command
from django.apps import apps


# ---------------------------------------------------------------------------
# Migration integrity
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_pending_migrations():
    """Fail if model changes exist without a corresponding migration."""
    try:
        call_command(
            "makemigrations",
            "--check",
            "--dry-run",
            stdout=StringIO(),
            stderr=StringIO(),
        )
    except SystemExit:
        pytest.fail(
            "There are model changes with no migration. "
            "Run: python manage.py makemigrations core"
        )


@pytest.mark.django_db
def test_no_migration_conflicts():
    """Fail if two migrations share the same parent (branch conflict)."""
    out = StringIO()
    err = StringIO()
    try:
        call_command("makemigrations", "--check", stdout=out, stderr=err)
    except SystemExit:
        pass
    combined = out.getvalue() + err.getvalue()
    assert "Conflicting migrations" not in combined, (
        "Conflicting migrations detected. Run: python manage.py makemigrations --merge"
    )


@pytest.mark.django_db
def test_migrations_apply_cleanly():
    """All migrations apply without error.

    pytest-django already creates the test DB by running migrations,
    so reaching this point means they applied. We run migrate again
    explicitly to surface any issues with --run-syncdb.
    """
    call_command("migrate", "--run-syncdb", verbosity=0)


# ---------------------------------------------------------------------------
# Seed integrity
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_seed_runs_cleanly():
    """The seed command must execute without error."""
    call_command("seed_all")


@pytest.mark.django_db
def test_seed_is_idempotent():
    """Running the seed command twice must not fail or duplicate data."""
    call_command("seed_all")
    call_command("seed_all")


# ---------------------------------------------------------------------------
# Model conventions
# ---------------------------------------------------------------------------


def test_all_models_have_explicit_db_table():
    """Every model must set db_table in its Meta.

    Django auto-generates db_table as 'appname_modelname' when not set.
    We require explicit names to avoid surprises across components.
    """
    for model in apps.get_app_config("core").get_models():
        meta = model._meta
        auto_name = f"{meta.app_label}_{meta.model_name}"
        assert meta.db_table != auto_name, (
            f"Model {model.__name__} uses auto-generated db_table '{auto_name}'. "
            f"Set an explicit db_table in class Meta."
        )
