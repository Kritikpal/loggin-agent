import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import NOT_FOUND, CampaignServiceDep, PaginationDep
from app.schemas.campaign import CampaignCreate, CampaignRead, CampaignStatus
from app.schemas.common import Page

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])


@router.get("", response_model=Page[CampaignRead], summary="List campaigns (newest first)")
async def list_campaigns(
    service: CampaignServiceDep,
    pagination: PaginationDep,
    campaign_status: Annotated[CampaignStatus | None, Query(alias="status")] = None,
) -> Page[CampaignRead]:
    return await service.list(campaign_status, pagination)


@router.get("/{campaign_id}", response_model=CampaignRead, responses=NOT_FOUND, summary="Get a campaign")
async def get_campaign(campaign_id: uuid.UUID, service: CampaignServiceDep) -> CampaignRead:
    return await service.get(campaign_id)


@router.post(
    "",
    response_model=CampaignRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a DRAFT campaign (nothing is sent or applied)",
)
async def create_campaign(data: CampaignCreate, service: CampaignServiceDep) -> CampaignRead:
    return await service.create(data)
