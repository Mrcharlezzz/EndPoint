from pydantic import BaseModel, ConfigDict


class ExtractedLink(BaseModel):
    """A URL extracted from the item body.
    Field names match the links ORM table."""

    model_config = ConfigDict(from_attributes=True)

    url: str
    label: str | None = None


class ExtractedAttachment(BaseModel):
    """A file attachment identified in the item.
    Field names match the attachments ORM table."""

    model_config = ConfigDict(from_attributes=True)

    filename: str
    mime_type: str | None = None
    url: str | None = None
