from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0002_user_email_unique_preferences_deleted_read_state_telegram"),
    ]

    operations = [
        migrations.DeleteModel(
            name="UserTelegramGroup",
        ),
    ]
