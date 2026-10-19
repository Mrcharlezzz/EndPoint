import uuid

from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from api.models.identity import ItemReadState


@api_view(["GET", "DELETE"])
def me(request):
    user = request._request.auth_user
    if request.method == "GET":
        return Response(
            {
                "id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "created_at": user.created_at,
            }
        )
    elif request.method == "DELETE":
        user.delete()
        return Response(status=204)


@api_view(["GET", "POST", "DELETE"])
def read_state(request):
    user = request._request.auth_user

    if request.method == "GET":
        rows = ItemReadState.objects.filter(user=user).values_list("item_id", "read_at")
        return Response(
            {str(item_id): read_at.isoformat() for item_id, read_at in rows}, status=200
        )

    if request.method == "DELETE":
        ItemReadState.objects.filter(user=user).delete()
        return Response(status=204)

    raw_item_ids = request.data.get("item_ids")
    if not isinstance(raw_item_ids, list):
        return Response(
            {
                "error": {
                    "code": "validation_error",
                    "message": "item_ids must be a list of UUID strings",
                }
            },
            status=400,
        )

    item_ids: list[uuid.UUID] = []
    try:
        item_ids = [uuid.UUID(str(item_id)) for item_id in raw_item_ids]
    except (TypeError, ValueError):
        return Response(
            {
                "error": {
                    "code": "validation_error",
                    "message": "item_ids must contain only valid UUID strings",
                }
            },
            status=400,
        )

    read_at = timezone.now()
    ItemReadState.objects.bulk_create(
        [
            ItemReadState(user_id=user.id, item_id=item_id, read_at=read_at)
            for item_id in dict.fromkeys(item_ids)
        ],
        update_conflicts=True,
        update_fields=["read_at"],
        unique_fields=["user", "item_id"],
    )
    return Response(status=204)
