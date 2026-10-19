from django.db import models


class ProcessingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class EntityType(models.TextChoices):
    ASSIGNMENT = "assignment", "Assignment"
    ANNOUNCEMENT = "announcement", "Announcement"
    EVENT = "event", "Event"
    REMINDER = "reminder", "Reminder"
    RAW_MESSAGE = "raw_message", "Raw Message"
    MATERIAL = "material", "Material"


class Platform(models.TextChoices):
    MOODLE = "moodle", "Moodle"
    TEAMS = "teams", "Teams"
    TELEGRAM = "telegram", "Telegram"


class ClassificationMethod(models.TextChoices):
    FIELD_MAPPING = "field_mapping", "Field Mapping"
    RULE_BASED = "rule_based", "Rule Based"
    LLM = "llm", "LLM"


class DedupStatus(models.TextChoices):
    PRIMARY = "primary", "Primary"
    LINKED = "linked", "Linked"
    PROMOTED = "promoted", "Promoted"
    SKIPPED = "skipped", "Skipped"
    FAILED = "failed", "Failed"
