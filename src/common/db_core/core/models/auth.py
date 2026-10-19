from django.db import models

from core.models.base import BaseModel
from core.models.enums import Platform


class PlatformCredential(BaseModel):
    """Per-user, per-platform credential bundle.

    The bundle payload (Teams: graph_token + csa_token + refresh_token +
    team_ids + cookies; Moodle: token + user_id; Telegram: bot_token) is
    serialized to JSON and encrypted at the application layer with Fernet
    before being written to ``encrypted_payload``. The ingestor daemon is
    currently the only reader. See
    ``src/ingestion/ingestion/auth/types.py`` for the full Teams field
    inventory.

    `user_email` is the grouping key across platforms — pausing or removing
    a user is a bulk update filtered by email. User-level metadata
    (display_name, is_paused) is carried on every credential row rather than
    a separate User table to keep db_core minimal.
    """

    user_email = models.EmailField(db_index=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    is_paused = models.BooleanField(default=False, db_index=True)
    platform = models.CharField(max_length=50, choices=Platform.choices)
    encrypted_payload = models.BinaryField()
    refresh_url = models.CharField(max_length=2048, blank=True, default="")
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    is_expired = models.BooleanField(default=False, db_index=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "core"
        db_table = "platform_credentials"
        constraints = [
            models.UniqueConstraint(
                fields=["user_email", "platform"],
                name="uq_platform_credential_user_email_platform",
            ),
        ]
        indexes = [
            models.Index(
                fields=["platform", "is_expired"],
                name="ix_cred_platform_expired",
            ),
            models.Index(
                fields=["user_email"],
                name="ix_cred_user_email",
            ),
        ]
