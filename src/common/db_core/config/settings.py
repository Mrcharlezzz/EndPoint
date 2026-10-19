import os

SECRET_KEY = "django-insecure-dev-only-key-do-not-use-in-production"
DEBUG = True
INSTALLED_APPS = ["core"]
ROOT_URLCONF = "config.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "shared_db"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.environ.get("PG_HOST", "localhost"),
        "PORT": os.environ.get("PG_PORT", "5432"),
    }
}
