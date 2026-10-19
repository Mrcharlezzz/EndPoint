import uuid

from django.db import models


class Course(models.Model):
    id = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50, null=True)
    major = models.CharField(max_length=255, null=True)
    semester = models.TextField(null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "courses"


class IngestedItem(models.Model):
    id = models.UUIDField(primary_key=True)
    external_id = models.CharField(max_length=255)
    platform = models.CharField(max_length=50)
    course_external_id = models.CharField(max_length=255, null=True)
    source_url = models.CharField(max_length=2048, null=True)
    schema_version = models.CharField(max_length=20)
    adapter_version = models.CharField(max_length=50)
    ingestion_mode = models.CharField(max_length=50)
    processing_status = models.CharField(max_length=20)
    error_message = models.TextField(null=True)
    payload = models.JSONField()
    raw_payload = models.JSONField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "ingested_items"


class PlatformCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    user_email = models.EmailField(db_index=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    is_paused = models.BooleanField(default=False, db_index=True)
    platform = models.CharField(max_length=50)
    encrypted_payload = models.BinaryField()
    refresh_url = models.CharField(max_length=2048, blank=True, default="")
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    is_expired = models.BooleanField(default=False, db_index=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "platform_credentials"


class BaseClassifiedItem(models.Model):
    id = models.UUIDField(primary_key=True)
    entity_type = models.CharField(max_length=50)
    author = models.CharField(max_length=255, null=True)
    ingested_item = models.ForeignKey(
        IngestedItem,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
    )
    canonical_entity = models.ForeignKey(
        "CanonicalEntity",
        on_delete=models.DO_NOTHING,
        null=True,
        db_constraint=False,
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.DO_NOTHING,
        null=True,
        db_constraint=False,
    )
    dedup_status = models.CharField(max_length=20, default="skipped")
    title = models.CharField(max_length=500, null=True)
    body = models.TextField(null=True)
    published_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "base_classified_items"


class CanonicalEntity(models.Model):
    id = models.UUIDField(primary_key=True)
    entity_type = models.CharField(max_length=50)
    primary_item = models.ForeignKey(
        BaseClassifiedItem,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
    )
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "canonical_entities"


class Assignment(BaseClassifiedItem):
    deadline = models.DateTimeField(null=True)
    submission_format = models.CharField(max_length=255, null=True)

    class Meta:
        managed = False
        db_table = "assignments"


class Announcement(BaseClassifiedItem):
    class Meta:
        managed = False
        db_table = "announcements"


class Event(BaseClassifiedItem):
    start_time = models.DateTimeField(null=True)
    end_time = models.DateTimeField(null=True)
    location = models.CharField(max_length=500, null=True)

    class Meta:
        managed = False
        db_table = "events"


class Material(BaseClassifiedItem):
    class Meta:
        managed = False
        db_table = "materials"


class Link(models.Model):
    id = models.UUIDField(primary_key=True)
    classified_item = models.ForeignKey(
        BaseClassifiedItem,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="links",
    )
    url = models.CharField(max_length=2048)
    label = models.CharField(max_length=500, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "links"


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True)
    classified_item = models.ForeignKey(
        BaseClassifiedItem,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="attachments",
    )
    filename = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=200, null=True)
    url = models.CharField(max_length=2048, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "attachments"
