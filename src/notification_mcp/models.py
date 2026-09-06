from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, field_validator


class Event(StrEnum):
    INFO = "info"
    ACTION_REQUIRED = "action_required"
    REVIEW_REQUESTED = "review_requested"
    ERROR = "error"


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]
    event: Event = Event.INFO
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ""
    source: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ""
    url: HttpUrl | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and (len(str(value)) > 500 or value.username or value.password):
            raise ValueError("URL must be at most 500 characters and contain no credentials")
        return value


class NotificationStatus(BaseModel):
    id: str
    status: Literal["queued", "sending", "sent", "failed"]
    event: Event
    channel: str
    attempts: int
    created_at: str
    updated_at: str
    next_attempt_at: str | None
    sent_at: str | None
    last_error_code: str | None
    last_error: str | None


class EnqueueResult(BaseModel):
    notification: NotificationStatus
    deduplicated: bool = Field(description="True when an existing idempotency key was reused")
