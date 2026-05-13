from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from app.api.deps import SbClient, CurrentUser

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats")
async def get_dashboard_stats(current_user: CurrentUser, sb: SbClient):
    company_id = current_user["company_id"]
    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()
    warning_date = (today + timedelta(days=30)).isoformat()
    week_start = (today - timedelta(days=6)).isoformat()

    # Crew stats
    crew_result = sb.table("crew").select("status", count="exact").eq("company_id", company_id).execute()
    all_crew = crew_result.data or []
    total_crew = len(all_crew)
    active_crew = sum(1 for c in all_crew if c["status"] in ["active", "in_flight", "standby"])
    blocked_crew = sum(1 for c in all_crew if c["status"] == "blocked")
    on_leave = sum(1 for c in all_crew if c["status"] == "on_leave")

    # Flight stats today (fetch id + status so we can later join assignments)
    flights_result = sb.table("flights").select("id,status").eq("company_id", company_id)\
        .gte("departure_time", today_str).lt("departure_time", tomorrow_str).execute()
    flights_today = flights_result.data or []
    total_flights_today = len(flights_today)
    flights_in_air = sum(1 for f in flights_today if f["status"] == "in_air")
    flights_scheduled = sum(1 for f in flights_today if f["status"] in ["scheduled", "boarding"])

    # Document alerts
    docs_expiring = sb.table("documents").select("id", count="exact")\
        .lte("expiry_date", warning_date).gte("expiry_date", today_str).execute()
    docs_expired = sb.table("documents").select("id", count="exact")\
        .lt("expiry_date", today_str).execute()

    compliance_rate = round(((total_crew - blocked_crew) / total_crew * 100) if total_crew > 0 else 100.0, 1)

    # 7-day flight activity (single query, grouped in Python)
    week_result = sb.table("flights").select("status,departure_time")\
        .eq("company_id", company_id)\
        .gte("departure_time", week_start).execute()
    week_data = week_result.data or []

    # days_ar index maps Python weekday(): Mon=0..Sun=6
    days_ar = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
    weekly_flights = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        d_str = d.isoformat()
        day_rows = [f for f in week_data if (f.get("departure_time") or "").startswith(d_str)]
        weekly_flights.append({
            "date":      d_str,
            "day_ar":    days_ar[d.weekday()],
            "total":     len(day_rows),
            "completed": sum(1 for f in day_rows if f["status"] in ["completed", "landed"]),
            "cancelled": sum(1 for f in day_rows if f["status"] == "cancelled"),
            "in_air":    sum(1 for f in day_rows if f["status"] == "in_air"),
        })

    # Assignment coverage: today's flights that have ≥1 assignment
    today_flight_ids = [f["id"] for f in flights_today]
    flights_assigned = 0
    if today_flight_ids:
        assign_result = sb.table("assignments").select("flight_id")\
            .in_("flight_id", today_flight_ids).execute()
        assigned_flight_ids = {r["flight_id"] for r in (assign_result.data or [])}
        flights_assigned = len(assigned_flight_ids)

    return {
        "total_crew":           total_crew,
        "active_crew":          active_crew,
        "blocked_crew":         blocked_crew,
        "on_leave_crew":        on_leave,
        "total_flights_today":  total_flights_today,
        "flights_in_air":       flights_in_air,
        "flights_scheduled":    flights_scheduled,
        "flights_assigned":     flights_assigned,
        "expiring_documents":   docs_expiring.count or 0,
        "expired_documents":    docs_expired.count or 0,
        "compliance_rate":      compliance_rate,
        "unassigned_flights":   max(0, total_flights_today - flights_assigned),
        "weekly_flights":       weekly_flights,
    }
