"""User shape update + new ItemReadState and UserTelegramGroup tables.

The ``api/models/mirrors.py`` models (Course, IngestedItem, etc.) are
intentionally excluded; they are unmanaged read-only views over tables
owned by the ``db_core`` app.
"""

import uuid

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(model_name="user", name="ms_oid"),
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(max_length=254, unique=True),
        ),
        migrations.AddField(
            model_name="user",
            name="preferences",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="user",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="ItemReadState",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, primary_key=True, serialize=False
                    ),
                ),
                ("item_id", models.UUIDField()),
                ("read_at", models.DateTimeField(default=timezone.now)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="read_states",
                        to="api.user",
                    ),
                ),
            ],
            options={
                "db_table": "presentation_api_item_read_state",
                "constraints": [
                    models.UniqueConstraint(
                        fields=["user", "item_id"], name="uq_user_item_read"
                    )
                ],
                "indexes": [models.Index(fields=["user"], name="ix_item_read_user")],
            },
        ),
        migrations.CreateModel(
            name="UserTelegramGroup",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, primary_key=True, serialize=False
                    ),
                ),
                ("group_chat_id", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="telegram_groups",
                        to="api.user",
                    ),
                ),
            ],
            options={
                "db_table": "presentation_api_user_telegram_groups",
                "constraints": [
                    models.UniqueConstraint(
                        fields=["user", "group_chat_id"],
                        name="uq_user_telegram_group",
                    )
                ],
            },
        ),
    ]
