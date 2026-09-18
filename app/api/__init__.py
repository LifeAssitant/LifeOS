from fastapi import APIRouter

from app.api import auth, billing, chat, events, tasks, today, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(tasks.router)
api_router.include_router(events.router)
api_router.include_router(today.router)
api_router.include_router(chat.router)
api_router.include_router(billing.router)
