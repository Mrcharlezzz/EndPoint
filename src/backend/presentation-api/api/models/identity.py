import uuid
from datetime import timedelta

import secrets
from django.conf import settings
from django.db import models
from django.utils import timezone


class User(models.Model):
    class Meta:
        db_table = "presentation_api_users"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=255)
    preferences = models.JSONField(blank=True, default=dict)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


def generate_token():
    return secrets.token_urlsafe(64)


def get_expiry():
    return timezone.now() + timedelta(hours=settings.SESSION_TTL_HOURS)


class Session(models.Model):
    class Meta:
        db_table = "presentation_api_sessions"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    token = models.CharField(max_length=128, unique=True, default=generate_token)
    expires_at = models.DateTimeField(default=get_expiry)
    created_at = models.DateTimeField(auto_now_add=True)


class UserCourseSelection(models.Model):
    class Meta:
        db_table = "presentation_api_user_course_selections"
        constraints = [models.UniqueConstraint(fields=["user", "course_id"], name="uq_user_course")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    course_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)


class ItemReadState(models.Model):
    class Meta:
        db_table = "presentation_api_item_read_state"
        constraints = [
            models.UniqueConstraint(fields=["user", "item_id"], name="uq_user_item_read")
        ]
        indexes = [models.Index(fields=["user"], name="ix_item_read_user")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="read_states")
    item_id = models.UUIDField()
    read_at = models.DateTimeField(default=timezone.now)
