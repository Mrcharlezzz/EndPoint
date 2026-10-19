from django.db import models
from core.models.base import BaseModel
from core.models.enums import DedupStatus, EntityType
from core.models.ingested import IngestedItem
from core.models.courses import Course


class BaseClassifiedItem(BaseModel):
    """Concrete base table for all classified entities.

    Created by the processor from ingested_items.payload.
    Presentation layer reads from this table and its typed children.
    Processing status is tracked on ingested_items, not here.
    """

    entity_type = models.CharField(max_length=50, choices=EntityType.choices)
    author = models.CharField(max_length=255, null=True, blank=True)
    ingested_item = models.ForeignKey(
        IngestedItem,
        on_delete=models.PROTECT,
        related_name="classified_items",
    )
    canonical_entity = models.ForeignKey(
        "core.CanonicalEntity",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="classified_items",
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="classified_items",
    )
    dedup_status = models.CharField(
        max_length=20,
        choices=DedupStatus.choices,
        default=DedupStatus.SKIPPED,
    )
    title = models.CharField(max_length=500, null=True, blank=True)
    body = models.TextField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        app_label = "core"
        db_table = "base_classified_items"
        indexes = [
            models.Index(fields=["course", "entity_type"], name="ix_base_course_type"),
            models.Index(fields=["canonical_entity"], name="ix_base_canonical"),
        ]


class Assignment(BaseClassifiedItem):
    deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    submission_format = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        app_label = "core"
        db_table = "assignments"


class Announcement(BaseClassifiedItem):
    class Meta:
        app_label = "core"
        db_table = "announcements"


class Material(BaseClassifiedItem):
    class Meta:
        app_label = "core"
        db_table = "materials"


class Event(BaseClassifiedItem):
    start_time = models.DateTimeField(null=True, blank=True, db_index=True)
    end_time = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        app_label = "core"
        db_table = "events"
