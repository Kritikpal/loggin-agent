import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.campaign import CAMPAIGN_STATUSES
from app.schemas.common import Text, text

CampaignStatus = Annotated[str, Field(pattern="^(" + "|".join(CAMPAIGN_STATUSES) + ")$")]


class CampaignCreate(BaseModel):
    """No status field: every campaign is created as a draft. Nothing is sent or applied in Phase 1."""

    model_config = ConfigDict(extra="forbid")

    name: text(200)
    message: Text = Field(max_length=5000)
    discount_percent: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    message: str
    discount_percent: Decimal
    status: str
    created_at: datetime
