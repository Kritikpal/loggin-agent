from fastapi import APIRouter

from app.controllers import chat, health, ingest, logs, projects

api_router = APIRouter()
for module in (health, projects, ingest, chat, logs):
    api_router.include_router(module.router)
