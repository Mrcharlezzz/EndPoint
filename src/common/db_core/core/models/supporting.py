import uuid
from django.db import models
from core.models.classified import BaseClassifiedItem


class Link(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    classified_item = models.ForeignKey(
        BaseClassifiedItem, on_delete=models.CASCADE, related_name="links"
    )
    url = models.CharField(max_length=2048)
    label = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"
        db_table = "links"
        indexes = [models.Index(fields=["classified_item"], name="ix_links_item")]


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    classified_item = models.ForeignKey(
        BaseClassifiedItem, on_delete=models.CASCADE, related_name="attachments"
    )
    filename = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=200, null=True, blank=True)
    url = models.CharField(max_length=2048, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"
        db_table = "attachments"
        indexes = [models.Index(fields=["classified_item"], name="ix_attachments_item")]
