import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.common import text

PHONE_PATTERN = r"^\+?[0-9][0-9 ()\-]{5,18}[0-9]$"


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: text(150)
    phone: str | None = Field(None, pattern=PHONE_PATTERN, max_length=20)
    email: EmailStr | None = Field(None, max_length=255)
    marketing_consent: bool = False


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phone: str | None
    email: str | None
    marketing_consent: bool
    created_at: datetime
