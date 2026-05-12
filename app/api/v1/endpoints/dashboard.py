from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from app.api.deps import SbClient, CurrentUser

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats")
async def get_dashboard_stats(current_user: CurrentUser, sb: SbClient):
    company_id = current_user["company_id"]
    today = datetime.now(timezone.utc).date().isoformat()
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    warning_date = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()

    # Crew stats
    crew_result = sb.table("crew").select("status", count="exact").eq("company_id", company_id).execute()
    all_crew = crew_result.data or []
    total_crew = len(all_crew)
    active_crew = sum(1 for c in all_crew if c["status"] in ["active", "in_flight", "standby"])
    blocked_crew = sum(1 for c in all_crew if c["status"] == "blocked")
    on_leave = sum(1 for c in all_crew if c["status"] == "on_leave")

    # Flight stats today
    flights_result = sb.table("flights").select("status").eq("company_id", company_id)\
        .gte("departure_time", today).lt("departure_time", tomorrow).execute()
    flights_today = flights_result.data or []
    total_flights_today = len(flights_today)
    flights_in_air = sum(1 for f in flights_today if f["status"] == "in_air")
    flights_scheduled = sum(1 for f in flights_today if f["status"] in ["scheduled", "boarding"])

    # Document alerts
    docs_expiring = sb.table("documents").select("id", count="exact")\
        .lte("expiry_date", warning_date).gte("expiry_date", today).execute()
    docs_expired = sb.table("documents").select("id", count="exact")\
        .lt("expiry_date", today).execute()

    compliance_rate = round(((total_crew - blocked_crew) / total_crew * 100) if total_crew > 0 else 100.0, 1)

    return {
        "total_crew": total_crew,
        "active_crew": active_crew,
        "blocked_crew": blocked_crew,
        "on_leave_crew": on_leave,
        "total_flights_today": total_flights_today,
        "flights_in_air": flights_in_air,
        "flights_scheduled": flights_scheduled,
        "expiring_documents": docs_expiring.count or 0,
        "expired_documents": docs_expired.count or 0,
        "compliance_rate": compliance_rate,
        "unassigned_flights": 0,
    }
