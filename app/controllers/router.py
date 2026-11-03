from fastapi import APIRouter

from app.controllers import chat, health, projects

api_router = APIRouter()
for module in (health, projects, chat):
    api_router.include_router(module.router)
