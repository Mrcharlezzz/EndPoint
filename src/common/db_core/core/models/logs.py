from django.db import models
import uuid

from core.models.enums import ClassificationMethod, DedupStatus, EntityType
from core.models.ingested import IngestedItem


class ClassificationLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingested_item = models.ForeignKey(
        IngestedItem, on_delete=models.PROTECT, related_name="classification_logs"
    )
    entity_type = models.CharField(
        max_length=50, choices=EntityType.choices, null=True, blank=True
    )
    classified_item = models.ForeignKey(
        "core.BaseClassifiedItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="classification_logs",
    )
    classification_method = models.CharField(
        max_length=20, choices=ClassificationMethod.choices
    )
    llm_model = models.CharField(max_length=200, null=True, blank=True)
    llm_raw_output = models.TextField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    processing_time_ms = models.IntegerField(null=True, blank=True)
    processor_version = models.CharField(max_length=100, null=True, blank=True)
    prompt_version = models.CharField(max_length=100, null=True, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"
        db_table = "classification_logs"


class DedupLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    classified_item = models.ForeignKey(
        "core.BaseClassifiedItem",
        on_delete=models.PROTECT,
        related_name="dedup_logs",
    )
    canonical_entity = models.ForeignKey(
        "core.CanonicalEntity",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dedup_logs",
    )
    status = models.CharField(max_length=20, choices=DedupStatus.choices)
    match_strategy = models.CharField(max_length=100, null=True, blank=True)
    resolution_strategy = models.CharField(max_length=100, null=True, blank=True)
    score = models.FloatField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"
        db_table = "dedup_logs"
