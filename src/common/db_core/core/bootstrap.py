import os
import django
from django.conf import settings


def init() -> None:
    """Initialize Django ORM for standalone (non-web) use.

    Call once at process startup before importing any ORM models.
    Safe to call multiple times — skips if already configured.
    """
    if settings.configured:
        return
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
