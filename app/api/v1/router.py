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
    compliance,
    incompatibility,
    training,
    leave_requests,
    om,
    payroll,
    maintenance,
    aircraft,
    safety,
    irops,
    bidding,
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
api_router.include_router(compliance.router)
api_router.include_router(incompatibility.router)
api_router.include_router(training.router)
api_router.include_router(leave_requests.router)
api_router.include_router(om.router)
api_router.include_router(payroll.router)
api_router.include_router(maintenance.router)
api_router.include_router(aircraft.router)
api_router.include_router(safety.router)
api_router.include_router(irops.router)
api_router.include_router(bidding.router)
