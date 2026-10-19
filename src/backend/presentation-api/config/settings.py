"""
Django settings for the Endpoint Presentation API.
"""

from pathlib import Path
import os
import sys
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "")
DEBUG = os.environ.get("DEBUG", "False") == "True"


def _validate_secret_key(value: str) -> None:
    """Reject Django SECRET_KEY values that are empty, too short, or look
    like a known placeholder (case-insensitive substring match against
    common dev defaults).

    Fires unconditionally, including in DEBUG=True, to catch dev mistakes
    before they make it to a real deploy. Operators who need to opt out
    (CI, throwaway local boots, ephemeral test runners) can set
    ALLOW_INSECURE_SECRET_KEY=1 in the environment; the validator will
    then emit a single warning to stderr and continue.

    Never includes the SECRET_KEY value (or any prefix of it) in the
    error message: Django sometimes surfaces ImproperlyConfigured
    messages in tracebacks that get logged or shipped to error
    trackers, and we don't want a partial leak.
    """
    allow_insecure = os.environ.get("ALLOW_INSECURE_SECRET_KEY", "") == "1"
    insecure_markers = (
        "django-insecure",
        "change-me",
        "changeme",
        "default",
        "example",
    )

    reasons: list[str] = []
    if not value or not value.strip():
        reasons.append("SECRET_KEY is empty or whitespace-only")
    else:
        if len(value) < 50:
            reasons.append(f"SECRET_KEY must be at least 50 characters (got {len(value)})")
        lowered = value.lower()
        for marker in insecure_markers:
            if marker in lowered:
                reasons.append(
                    f"SECRET_KEY contains insecure placeholder substring "
                    f"{marker!r} (case-insensitive)"
                )
                break

    if not reasons:
        return

    message = (
        "Insecure SECRET_KEY rejected: " + "; ".join(reasons) + ". Generate a strong value with: "
        'python -c "import secrets; print(secrets.token_urlsafe(50))" '
        "and set PRESENTATION_API_SECRET_KEY in your .env. "
        "Set ALLOW_INSECURE_SECRET_KEY=1 to bypass this check in "
        "CI / local-only environments."
    )
    if allow_insecure:
        print(
            "WARNING: " + message + " (bypass active via ALLOW_INSECURE_SECRET_KEY=1)",
            file=sys.stderr,
        )
        return
    raise ImproperlyConfigured(message)


_validate_secret_key(SECRET_KEY)

if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost").split(",")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "corsheaders",
    "rest_framework",
    "api",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "api.middleware.SessionAuthMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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

CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
# Cookie-based session auth requires credentialed CORS.
CORS_ALLOW_CREDENTIALS = True

MS_GRAPH_BASE_URL = os.environ.get("MS_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "24"))

# Endpoint session cookie (httpOnly).
SESSION_COOKIE_NAME = os.environ.get("ENDPOINT_SESSION_COOKIE_NAME", "endpoint_session")
SESSION_COOKIE_SECURE = (
    os.environ.get("ENDPOINT_SESSION_COOKIE_SECURE", "True" if not DEBUG else "False") == "True"
)
SESSION_COOKIE_SAMESITE = os.environ.get("ENDPOINT_SESSION_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_DOMAIN = os.environ.get("ENDPOINT_SESSION_COOKIE_DOMAIN") or None

# Headless Teams-login service (server-to-server token handoff).
HEADLESS_INTERNAL_URL = os.environ.get("HEADLESS_INTERNAL_URL", "http://headless:8000")
HEADLESS_INTERNAL_SECRET = os.environ.get("ENDPOINT_HEADLESS__INTERNAL_SECRET", "")

# Moodle host (single tenant for now).
MOODLE_BASE_URL = os.environ.get("MOODLE_BASE_URL", "https://elearning.constructor.university")

# Fernet key for PlatformCredential payload encryption (shared with the daemon).
ENDPOINT_FERNET_KEY = os.environ.get("ENDPOINT_FERNET_KEY", "")

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
}

# ---------------------------------------------------------------------------
# Sentry / GlitchTip
#
# The sentry-sdk speaks the Sentry wire protocol; GlitchTip implements the
# same protocol on the server side, so we point a normal sentry_sdk.init at
# our self-hosted GlitchTip DSN. Browser-side `sentry-trace` and `baggage`
# headers from @sentry/nextjs are picked up automatically by the Django
# integration here, so a single user action shows up as a single linked
# trace spanning frontend -> presentation-api in GlitchTip.
# ---------------------------------------------------------------------------
SENTRY_DSN = os.environ.get("PRESENTATION_API_SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.environ.get("PRESENTATION_API_SENTRY_ENVIRONMENT", "development"),
        integrations=[
            DjangoIntegration(),
            LoggingIntegration(),
        ],
        # Sample 10% of transactions in production, all in dev.
        # Treat an empty env var the same as unset (os.environ.get only
        # falls back when the key is missing, not when it's empty).
        traces_sample_rate=float(
            (os.environ.get("PRESENTATION_API_SENTRY_TRACES_SAMPLE_RATE") or "").strip()
            or ("1.0" if DEBUG else "0.1")
        ),
        # Continue traces propagated by the frontend SDK (sentry-trace header).
        propagate_traces=True,
        # Privacy: no user emails / IPs / cookies / auth headers in payloads.
        send_default_pii=False,
    )
