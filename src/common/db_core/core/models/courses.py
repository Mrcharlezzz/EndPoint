from django.db import models
from core.models.base import BaseModel
from core.models.enums import Platform


class Course(BaseModel):
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    major = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    semester = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "core"
        db_table = "courses"


class CoursePlatformMapping(BaseModel):
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="platform_mappings",
    )
    platform = models.CharField(max_length=50, choices=Platform.choices)
    external_course_id = models.CharField(max_length=255)
    external_course_name = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "core"
        db_table = "course_platform_mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "external_course_id"],
                name="uq_course_platform_ext",
            )
        ]
        indexes = [
            models.Index(
                fields=["course"],
                name="ix_course_platform_course",
            )
        ]
