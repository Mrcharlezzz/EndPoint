from rest_framework.decorators import api_view
from rest_framework.response import Response
from api.models.mirrors import Assignment
from api.models.identity import UserCourseSelection

from django.db.models import F


def assignment_data(assignment):
    created_at = assignment.published_at or assignment.created_at

    return {
        "id": assignment.id,
        "title": assignment.title,
        "body": assignment.body,
        "author": assignment.author,
        "course": {
            "id": assignment.course_id,
            "name": assignment.course.name,
            "code": assignment.course.code,
        },
        "deadline": assignment.deadline,
        "submission_format": assignment.submission_format,
        "max_points": None,
        "points": None,
        "status": None,
        "platform": assignment.ingested_item.platform,
        "source_url": assignment.ingested_item.source_url,
        "links": [{"url": link.url, "label": link.label} for link in assignment.links.all()],
        "attachments": [
            {
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "url": attachment.url,
            }
            for attachment in assignment.attachments.all()
        ],
        "published_at": assignment.published_at,
        "created_at": created_at,
    }


@api_view(["GET"])
def list_assignments(request):
    user = getattr(request._request, "auth_user", None)

    course_id = request.query_params.get("course_id")
    platform = request.query_params.get("platform")

    queryset = Assignment.objects.filter(ingested_item__processing_status="processed")

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
    )

    if course_id:
        queryset = queryset.filter(course_id=course_id)
    if platform:
        queryset = queryset.filter(ingested_item__platform=platform)

    queryset = queryset.order_by(
        F("deadline").asc(nulls_last=True),
        F("published_at").desc(nulls_last=True),
        "-created_at",
    )

    data = [assignment_data(assignment) for assignment in queryset]

    return Response({"results": data}, status=200)


@api_view(["GET"])
def get_assignment(request, id):
    user = getattr(request._request, "auth_user", None)

    assignments = Assignment.objects.filter(
        id=id,
        ingested_item__processing_status="processed",
    )

    if user is not None:
        selections = UserCourseSelection.objects.filter(user=user).values_list(
            "course_id", flat=True
        )
        assignments = assignments.filter(course_id__in=selections)

    assignment = (
        assignments.exclude(dedup_status="linked")
        .select_related("ingested_item", "course")
        .prefetch_related("links", "attachments")
        .first()
    )

    if not assignment:
        return Response(
            {"error": {"code": "not_found", "message": "Assignment not found"}}, status=404
        )

    return Response(assignment_data(assignment), status=200)
