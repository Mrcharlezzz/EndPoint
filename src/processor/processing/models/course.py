from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Course(BaseModel):
    """A course record from the DB.
    Field names match the courses ORM table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    code: str | None = None
    major: str | None = None
    semester: str | None = None


class CourseMatch(BaseModel):
    """Result of course resolution — which course was matched and how."""

    course_id: UUID
    matched_by: str  # 'code_exact' | 'title_levenshtein' | 'created_new'
    course_code: str | None = None
    course_name: str
