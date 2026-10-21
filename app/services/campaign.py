import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Campaign
from app.repositories.campaign import CampaignRepository
from app.schemas.campaign import CampaignCreate, CampaignRead
from app.schemas.common import Page, Pagination


class CampaignService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.campaigns = CampaignRepository(session)

    async def list(self, status: str | None, pagination: Pagination) -> Page[CampaignRead]:
        rows, total = await self.campaigns.list(status, pagination.page_size, pagination.offset)
        return Page.of([CampaignRead.model_validate(c) for c in rows], total, pagination)

    async def get(self, campaign_id: uuid.UUID) -> CampaignRead:
        campaign = await self.campaigns.get(campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        return CampaignRead.model_validate(campaign)

    async def create(self, data: CampaignCreate) -> CampaignRead:
        """Persists a DRAFT only. Sending messages / applying discounts is deliberately out of scope."""
        campaign = await self.campaigns.add(
            Campaign(
                name=data.name,
                message=data.message,
                discount_percent=data.discount_percent,
                status="draft",
            )
        )
        await self.session.commit()
        return CampaignRead.model_validate(campaign)
