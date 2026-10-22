"""CREATE_CAMPAIGN: what the planner may propose, and the deterministic rules a proposal must pass before a human
is asked to approve it. No model is involved in validation."""

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.database import get_session
from app.repositories.product import ProductFilters
from app.schemas.ai import ProposedAction
from app.schemas.campaign import CampaignCreate
from app.schemas.common import Pagination
from app.services.product import ProductService

MAX_DISCOUNT_PERCENT = Decimal("50")  # guardrail: deeper promotions are not something the assistant may propose
CENTS = Decimal("0.01")
REQUIRED = ("name", "message", "category", "discount_percent")


class ActionPlan(BaseModel):
    """The single action the user's request amounts to."""

    action_type: Literal["CREATE_CAMPAIGN", "UNSUPPORTED"] = Field(
        description="CREATE_CAMPAIGN for a promotional campaign; UNSUPPORTED for any other change"
    )
    name: str | None = Field(default=None, description="Short campaign name")
    message: str | None = Field(default=None, description="Customer-facing campaign message")
    category: str | None = Field(default=None, description="Product category the campaign targets")
    discount_percent: float | None = Field(default=None, description="Percentage off, e.g. 15 for 15%")
    reason: str | None = Field(default=None, description="Only for UNSUPPORTED: why it cannot be done")


async def validate_plan(plan: ActionPlan | None) -> tuple[ProposedAction | None, list[str]]:
    """(proposal, []) when every rule passes, otherwise (None, reasons)."""
    if plan is None:
        return None, ["The request could not be turned into an action."]
    if plan.action_type != "CREATE_CAMPAIGN":
        return None, [plan.reason or "Only creating a campaign is supported right now."]

    values = plan.model_dump(include=set(REQUIRED))
    missing = [k for k in REQUIRED if values[k] is None or (isinstance(values[k], str) and not values[k].strip())]
    if missing:
        return None, [f"Missing: {', '.join(missing)}."]

    try:  # reuse the campaign API's own field rules (lengths, 0-100, two decimals)
        campaign = CampaignCreate(
            name=plan.name, message=plan.message, discount_percent=Decimal(str(plan.discount_percent))
        )
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]

    errors = []
    if not 0 < campaign.discount_percent <= MAX_DISCOUNT_PERCENT:
        errors.append(f"Discount must be more than 0% and at most {MAX_DISCOUNT_PERCENT}%.")
    category = plan.category.strip()
    async with asynccontextmanager(get_session)() as session:
        found = await ProductService(session).list(ProductFilters(category=category), Pagination(page=1, page_size=1))
    if not found.items:
        errors.append(f"category: there are no products in the category '{category}'.")
    if errors:
        return None, errors

    return ProposedAction(
        name=campaign.name,
        message=campaign.message,
        category=found.items[0].category,  # the catalogue's own spelling
        discount_percent=campaign.discount_percent.quantize(CENTS),
    ), []
