from django.db import models
from core.models.base import BaseModel
from core.models.enums import EntityType
from core.models.courses import Course


class CanonicalEntity(BaseModel):
    """Thin pointer. Created by C4 dedup. Stores no content."""

    entity_type = models.CharField(max_length=50, choices=EntityType.choices)
    primary_item = models.ForeignKey(
        "core.BaseClassifiedItem",
        on_delete=models.PROTECT,
        related_name="canonical_as_primary",
    )
    course = models.ForeignKey(
        Course, on_delete=models.PROTECT, related_name="canonicals"
    )

    class Meta:
        app_label = "core"
        db_table = "canonical_entities"
        indexes = [
            models.Index(
                fields=["course", "entity_type"], name="ix_canonical_course_type"
            )
        ]
