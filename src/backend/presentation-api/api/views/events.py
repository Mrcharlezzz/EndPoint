from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db.models import F
from api.models.mirrors import Event
from api.models.identity import UserCourseSelection


def event_data(event):
    created_at = event.published_at or event.created_at

    return {
        "id": str(event.id),
        "title": event.title,
        "body": event.body,
        "author": event.author,
        "course": {
            "id": str(event.course_id),
            "name": event.course.name,
            "code": event.course.code,
        },
        "start_time": event.start_time,
        "end_time": event.end_time,
        "location": event.location,
        "platform": event.ingested_item.platform,
        "source_url": event.ingested_item.source_url,
        "links": [{"url": link.url, "label": link.label} for link in event.links.all()],
        "attachments": [
            {"filename": att.filename, "mime_type": att.mime_type, "url": att.url}
            for att in event.attachments.all()
        ],
        "published_at": event.published_at,
        "created_at": created_at,
    }


@api_view(["GET"])
def list_events(request):
    user = getattr(request._request, "auth_user", None)

    course_id = request.query_params.get("course_id")

    queryset = Event.objects.filter(ingested_item__processing_status="processed")

    if user is not None:
        selections = UserCourseSelection.objects.filter(user=user).values_list(
            "course_id", flat=True
        )

        if course_id and str(course_id) not in [str(sid) for sid in selections]:
            return Response(
                {
                    "error": {
                        "code": "validation_error",
                        "message": "course_id is not in your course selection",
                    }
                },
                status=400,
            )

        queryset = queryset.filter(course_id__in=selections)

    queryset = (
        queryset.exclude(dedup_status="linked")
        .select_related("ingested_item", "course")
        .prefetch_related("links", "attachments")
        .order_by(
            F("start_time").asc(nulls_last=True),
            F("published_at").desc(nulls_last=True),
            "-created_at",
        )
    )

    if course_id:
        queryset = queryset.filter(course_id=course_id)

    data = [event_data(event) for event in queryset]

    return Response({"results": data}, status=200)


@api_view(["GET"])
def get_event(request, id):
    user = getattr(request._request, "auth_user", None)

    event_qs = Event.objects.filter(
        id=id,
        ingested_item__processing_status="processed",
    )
    if user is not None:
        selections = UserCourseSelection.objects.filter(user=user).values_list(
            "course_id", flat=True
        )
        event_qs = event_qs.filter(course_id__in=selections)

    event = (
        event_qs.exclude(dedup_status="linked")
        .select_related("ingested_item", "course")
        .prefetch_related("links", "attachments")
    ).first()

    if not event:
        return Response({"error": {"code": "not_found", "message": "Event not found"}}, status=404)

    return Response(event_data(event), status=200)
