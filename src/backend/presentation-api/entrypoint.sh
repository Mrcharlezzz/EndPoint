#!/bin/bash
set -e

echo "Running migrations..."
/app/.venv/bin/python manage.py migrate --noinput

echo "Starting server..."
exec /app/.venv/bin/gunicorn config.wsgi:application \
  --workers 3 \
  --bind 0.0.0.0:8000 \
  --access-logfile - \
  --error-logfile -