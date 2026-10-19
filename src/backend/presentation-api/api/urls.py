from django.urls import path
from api.views.auth import logout, moodle_login, teams_finalize
from api.views.users import me, read_state
from api.views.courses import list_courses, user_courses
from api.views.assignments import list_assignments, get_assignment
from api.views.announcements import list_announcements, get_announcement
from api.views.events import list_events, get_event
from api.views.connections import (
    connections_list,
    disconnect,
    moodle_connect,
    reconnect,
    teams_finalize as connect_teams_finalize,
)
from api.views.health import health_check

urlpatterns = [
    path("health/", health_check),
    path("auth/teams/finalize/", teams_finalize),
    path("auth/moodle/", moodle_login),
    path("auth/logout/", logout),
    path("users/me/", me),
    path("users/me/read-state/", read_state),
    path("courses/", list_courses),
    path("users/me/courses/", user_courses),
    path("connections/", connections_list),
    path("connections/teams/finalize/", connect_teams_finalize),
    path("connections/moodle/", moodle_connect),
    path("connections/<str:platform>/", disconnect),
    path("connections/<str:platform>/reconnect/", reconnect),
    path("assignments/", list_assignments),
    path("assignments/<uuid:id>/", get_assignment),
    path("announcements/", list_announcements),
    path("announcements/<uuid:id>/", get_announcement),
    path("events/", list_events),
    path("events/<uuid:id>/", get_event),
]
