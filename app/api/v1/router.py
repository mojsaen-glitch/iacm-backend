from fastapi import APIRouter
from app.api.v1.endpoints import (
    auth,
    crew,
    flights,
    assignments,
    dashboard,
    notifications,
    documents,
    messages,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(crew.router)
api_router.include_router(flights.router)
api_router.include_router(assignments.router)
api_router.include_router(dashboard.router)
api_router.include_router(notifications.router)
api_router.include_router(documents.router)
api_router.include_router(messages.router)
