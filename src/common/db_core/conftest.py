import os

# Set defaults before Django reads them — no .env needed for tests.
# If .env has already been sourced or vars are set, setdefault won't overwrite.
os.environ.setdefault("POSTGRES_DB", "shared_db")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("PG_PORT", "5432")
