from rest_framework.decorators import api_view
from rest_framework.response import Response
from api.models.identity import UserCourseSelection
from operator import attrgetter
from itertools import groupby
from api.models.mirrors import Announcement
from django.db.models import F


def announcement_data(a):
    created_at = a.published_at or a.created_at

    return {
        "id": str(a.id),
        "title": a.title,
        "body": a.body,
        "author": a.author,
        "platform": a.ingested_item.platform,
        "source_url": a.ingested_item.source_url,
        "links": [{"url": link.url, "label": link.label} for link in a.links.all()],
        "attachments": [
            {"filename": att.filename, "mime_type": att.mime_type, "url": att.url}
            for att in a.attachments.all()
        ],
        "published_at": a.published_at,
        "created_at": created_at,
    }


@api_view(["GET"])
def list_announcements(request):
    user = getattr(request._request, "auth_user", None)

    course_id = request.query_params.get("course_id")
    platform = request.query_params.get("platform")

    queryset = Announcement.objects.filter(ingested_item__processing_status="processed")
    if user is not None:
        selections = UserCourseSelection.objects.filter(user=user).values_list(
            "course_id", flat=True
        )
        queryset = queryset.filter(course_id__in=selections)

    queryset = (
        queryset.exclude(dedup_status="linked")
        .select_related("ingested_item", "course")
        .prefetch_related("links", "attachments")
        .order_by(
            "course_id",
            F("published_at").desc(nulls_last=True),
            "-created_at",
        )
    )

    if course_id:
        queryset = queryset.filter(course_id=course_id)
    if platform:
        queryset = queryset.filter(ingested_item__platform=platform)

    data = []
    for grouped_course_id, group in groupby(queryset, key=attrgetter("course_id")):
        announcements = list(group)
        course = announcements[0].course
        data.append(
            {
                "course": {
                    "id": str(course.id),
                    "name": course.name,
                    "code": course.code,
                },
                "announcements": [announcement_data(a) for a in announcements],
            }
        )

    return Response({"results": data}, status=200)


@api_view(["GET"])
def get_announcement(request, id):
    user = getattr(request._request, "auth_user", None)

    announcement_qs = Announcement.objects.filter(
        id=id,
        ingested_item__processing_status="processed",
    )
    if user is not None:
        selections = UserCourseSelection.objects.filter(user=user).values_list(
            "course_id", flat=True
        )
        announcement_qs = announcement_qs.filter(course_id__in=selections)

    announcement = (
        announcement_qs.exclude(dedup_status="linked")
        .select_related("ingested_item", "course")
        .prefetch_related("links", "attachments")
    ).first()

    if not announcement:
        return Response(
            {"error": {"code": "not_found", "message": "Announcement not found"}}, status=404
        )

    course = announcement.course
    data = {
        "course": {
            "id": str(course.id),
            "name": course.name,
            "code": course.code,
        },
        **announcement_data(announcement),
    }

    return Response(data, status=200)
