from django.db import models
from core.models.base import BaseModel
from core.models.enums import Platform, ProcessingStatus


class IngestedItem(BaseModel):
    """Staging record written by the ingestion layer.

    Most normalized domain data lives in the structured `payload` JSONB field,
    while source metadata such as `source_url` remains top-level.
    The processor reads items with processing_status='pending', creates
    classified items from the payload, then marks this row processed.
    """

    external_id = models.CharField(max_length=255, unique=True, db_index=True)
    platform = models.CharField(max_length=50, choices=Platform.choices)
    course_external_id = models.CharField(max_length=255, null=True, blank=True)
    source_url = models.CharField(max_length=2048, null=True, blank=True)
    schema_version = models.CharField(max_length=20, default="4.0")
    adapter_version = models.CharField(max_length=50, default="")
    ingestion_mode = models.CharField(max_length=50, default="server_poll")
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
    )
    processing_completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    # The user whose credential was used to fetch this item. NULL for items with
    # no per-user credential (e.g. telegram, where the bot is operator-run).
    # Attribution for the dashboard Contributions view.
    fetched_by_user_email = models.EmailField(null=True, blank=True, db_index=True)
    payload = models.JSONField(default=dict)
    raw_payload = models.JSONField(default=dict)

    class Meta:
        app_label = "core"
        db_table = "ingested_items"
        indexes = [
            models.Index(
                fields=["platform", "processing_status"],
                name="ix_ingested_platform_status",
            ),
            models.Index(
                fields=["processing_status", "created_at"],
                name="ix_ingested_processing_queue",
            ),
            models.Index(fields=["course_external_id"], name="ix_ingested_course_ext"),
            models.Index(
                fields=["fetched_by_user_email", "created_at"],
                name="ix_ingested_fetched_by",
            ),
        ]
