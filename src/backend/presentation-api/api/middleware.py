from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

from api.models.identity import Session


class SessionAuthMiddleware:
    EXEMPT_PATHS = [
        "/health/",
        "/api/v1/auth/login/",
        "/api/v1/auth/teams/finalize/",
        "/api/v1/auth/moodle/",
        "/api/v1/auth/logout/",
    ]
    AUTH_OPTIONAL_PREFIXES = [
        "/api/v1/assignments/",
        "/api/v1/announcements/",
        "/api/v1/events/",
        "/api/v1/courses/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def _extract_token(self, request):
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            return auth_header.split(" ", 1)[1]

        return request.COOKIES.get(settings.SESSION_COOKIE_NAME)

    def _try_authenticate(self, request):
        token = self._extract_token(request)
        if not token:
            return

        try:
            session = Session.objects.select_related("user").get(token=token)
        except Session.DoesNotExist:
            return

        if session.expires_at < timezone.now():
            return

        request.auth_user = session.user

    def __call__(self, request):
        if request.path in self.EXEMPT_PATHS:
            return self.get_response(request)

        if request.method == "GET" and any(
            request.path.startswith(prefix) for prefix in self.AUTH_OPTIONAL_PREFIXES
        ):
            self._try_authenticate(request)
            return self.get_response(request)

        token = self._extract_token(request)
        if not token:
            return JsonResponse(
                {
                    "error": {
                        "code": "authentication_required",
                        "message": "Missing or invalid session credentials",
                    }
                },
                status=401,
            )

        try:
            session = Session.objects.select_related("user").get(token=token)
        except Session.DoesNotExist:
            return JsonResponse(
                {"error": {"code": "authentication_required", "message": "Invalid token"}},
                status=401,
            )

        if session.expires_at < timezone.now():
            return JsonResponse(
                {"error": {"code": "authentication_required", "message": "Token expired"}},
                status=401,
            )

        request.auth_user = session.user
        return self.get_response(request)
