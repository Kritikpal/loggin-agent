from fastapi import APIRouter

from app.api.v1 import ai, campaigns, customers, inventory, products, sales

api_router = APIRouter()
for module in (products, inventory, sales, customers, campaigns, ai):
    api_router.include_router(module.router)
