from fastapi import APIRouter

from app.controllers import chat, health, tools

api_router = APIRouter()
for module in (health, tools, chat):
    api_router.include_router(module.router)
