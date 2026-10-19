from rest_framework.decorators import api_view
from rest_framework.response import Response
from api.models.mirrors import Course
from api.models.identity import UserCourseSelection
from django.db import transaction


@api_view(["GET"])
def list_courses(request):
    courses = Course.objects.all()
    data = list(courses.values("id", "name", "code", "major", "semester"))

    return Response({"results": data}, status=200)


def get_courses_data(course_ids):
    courses = Course.objects.filter(id__in=course_ids)

    data = [
        {
            "course_id": course["id"],
            "name": course["name"],
            "code": course["code"],
            "major": course["major"],
            "semester": course["semester"],
        }
        for course in courses.values("id", "name", "code", "major", "semester")
    ]

    return data


@api_view(["GET", "PUT"])
def user_courses(request):
    user = request._request.auth_user

    if request.method == "GET":
        selections = UserCourseSelection.objects.filter(user=user)
        course_ids = selections.values_list("course_id", flat=True)

        return Response({"results": get_courses_data(course_ids)}, status=200)

    elif request.method == "PUT":
        course_ids = request.data.get("course_ids", [])

        provided_ids = set(course_ids)
        existing_ids = set(
            str(uid)
            for uid in Course.objects.filter(id__in=provided_ids).values_list("id", flat=True)
        )
        invalid_ids = provided_ids - existing_ids
        if invalid_ids:
            return Response(
                {
                    "error": {
                        "code": "validation_error",
                        "message": f"courses not found: {list(invalid_ids)}",
                    }
                },
                status=400,
            )

        with transaction.atomic():  # pyright: ignore[reportGeneralTypeIssues]
            UserCourseSelection.objects.filter(user=user).delete()
            UserCourseSelection.objects.bulk_create(
                [UserCourseSelection(user_id=user.id, course_id=cid) for cid in course_ids]
            )

        return Response({"results": get_courses_data(course_ids)}, status=200)
