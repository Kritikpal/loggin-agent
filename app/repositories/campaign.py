import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign
from app.repositories.common import count_rows


class CampaignRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, status: str | None, limit: int, offset: int) -> tuple[list[Campaign], int]:
        stmt = select(Campaign)
        if status:
            stmt = stmt.where(Campaign.status == status)
        total = await count_rows(self.session, stmt)
        rows = await self.session.scalars(stmt.order_by(Campaign.created_at.desc(), Campaign.id).limit(limit).offset(offset))
        return list(rows), total

    async def get(self, campaign_id: uuid.UUID) -> Campaign | None:
        return await self.session.get(Campaign, campaign_id)

    async def add(self, campaign: Campaign) -> Campaign:
        self.session.add(campaign)
        await self.session.flush()
        return campaign
