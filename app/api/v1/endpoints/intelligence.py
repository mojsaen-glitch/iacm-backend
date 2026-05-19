"""Intelligence & external integrations — Weather + AI-lite predictions.

The "AI" here is intentionally **rule-based**, not ML. With a fleet this
size you don't have enough training data to outperform good heuristics,
and a rule engine is auditable in a way a model isn't. We surface the
factors that drive the score so the dispatcher can override it if their
gut says different.

Three readers:
  • /intelligence/weather?station=XXX        — current conditions
  • /intelligence/flight/{id}/delay-risk      — heuristic delay score
  • /intelligence/crew/{id}/fatigue-risk      — heuristic fatigue score
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, SbClient

router = APIRouter(prefix="/intelligence", tags=["Intelligence"])
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Station coordinates — needed by the weather API.
# Hard-coded for the Iraqi Airways network; extend as routes change.
# ──────────────────────────────────────────────────────────────────────
_STATION_COORDS: dict[str, tuple[float, float]] = {
    "BGW": (33.2625, 44.2346),   # Baghdad
    "BSR": (30.5491, 47.6621),   # Basra
    "EBL": (36.2376, 43.9632),   # Erbil
    "NJF": (31.9890, 44.4042),   # Najaf
    "ISU": (35.5617, 45.3147),   # Sulaymaniyah
    "DXB": (25.2532, 55.3657),   # Dubai
    "AUH": (24.4330, 54.6511),
    "DOH": (25.2731, 51.6080),
    "KWI": (29.2267, 47.9689),
    "BAH": (26.2708, 50.6336),
    "MCT": (23.5933, 58.2844),
    "RUH": (24.9576, 46.6988),
    "JED": (21.6796, 39.1565),
    "AMM": (31.7226, 35.9936),
    "BEY": (33.8209, 35.4884),
    "DAM": (33.4114, 36.5156),
    "CAI": (30.1219, 31.4056),
    "IST": (41.2753, 28.7519),
    "IKA": (35.4161, 51.1522),
    "DEL": (28.5562, 77.1000),
    "FRA": (50.0379,  8.5622),
    "LHR": (51.4770, -0.4613),
}


@router.get("/weather")
async def weather(station: str = Query(..., description="IATA code, e.g. BGW"),
                    current_user: CurrentUser = None, sb: SbClient = None):
    """Current weather for a station via Open-Meteo (free, no key).

    Returns: temperature, wind speed + direction, visibility-relevant
    weather code, and an `ops_impact` summary the UI can colour.
    """
    code = (station or "").strip().upper()
    coords = _STATION_COORDS.get(code)
    if not coords:
        raise HTTPException(status_code=404,
            detail=f"Station '{code}' not in weather lookup table")

    lat, lon = coords
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&current=temperature_2m,wind_speed_10m,wind_direction_10m,"
           f"weather_code,visibility,relative_humidity_2m"
           f"&timezone=UTC")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("Open-Meteo lookup failed for %s: %s", code, e)
        raise HTTPException(status_code=502,
            detail="Weather service unavailable. Try again in a minute.")

    cur = data.get("current", {})
    wmo = cur.get("weather_code", 0)
    vis_m = cur.get("visibility", 99999)
    wind_kmh = cur.get("wind_speed_10m", 0) or 0
    impact = _ops_impact(wmo, vis_m, wind_kmh)

    return {
        "station":          code,
        "lat":              lat,
        "lon":              lon,
        "temperature_c":    cur.get("temperature_2m"),
        "wind_speed_kmh":   wind_kmh,
        "wind_direction":   cur.get("wind_direction_10m"),
        "humidity_pct":     cur.get("relative_humidity_2m"),
        "visibility_m":     vis_m,
        "weather_code":     wmo,
        "condition":        _wmo_label(wmo),
        "ops_impact":       impact,
        "fetched_at":       datetime.now(timezone.utc).isoformat(),
    }


def _wmo_label(code: int) -> str:
    """Open-Meteo WMO code → short Arabic label."""
    if code == 0: return "صافي"
    if code in (1, 2, 3): return "غائم جزئياً"
    if code in (45, 48): return "ضباب"
    if code in (51, 53, 55): return "رذاذ"
    if code in (61, 63, 65, 80, 81, 82): return "مطر"
    if code in (71, 73, 75, 85, 86): return "ثلج"
    if code in (95, 96, 99): return "عواصف رعدية"
    return f"WMO {code}"


def _ops_impact(wmo: int, vis_m: float, wind_kmh: float) -> dict:
    """Heuristic — does this weather impede flight ops?"""
    factors = []
    risk = "low"

    if vis_m is not None and vis_m < 1500:
        factors.append(f"رؤية {int(vis_m)}م < CAT-I")
        risk = "high"
    elif vis_m is not None and vis_m < 3000:
        factors.append(f"رؤية محدودة {int(vis_m)}م")
        if risk != "high": risk = "medium"

    if wind_kmh and wind_kmh >= 55:
        factors.append(f"رياح {int(wind_kmh)} كم/س")
        risk = "high"
    elif wind_kmh and wind_kmh >= 35:
        factors.append(f"رياح {int(wind_kmh)} كم/س")
        if risk != "high": risk = "medium"

    if wmo in (95, 96, 99):
        factors.append("عواصف رعدية")
        risk = "high"
    elif wmo in (71, 73, 75, 85, 86):
        factors.append("هطول ثلوج")
        if risk != "high": risk = "medium"
    elif wmo in (45, 48):
        factors.append("ضباب")
        if risk == "low": risk = "medium"

    return {
        "risk":   risk,                                      # low | medium | high
        "factors": factors or ["ظروف طبيعية"],
    }


# ──────────────────────────────────────────────────────────────────────
# Delay-risk prediction (heuristic, not ML)
# ──────────────────────────────────────────────────────────────────────

@router.get("/flight/{flight_id}/delay-risk")
async def delay_risk(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Score 0-100 of delay risk based on:
        • Weather at origin
        • Aircraft AOG history (defects in last 30d)
        • Time of day (night = higher)
        • Connecting turnaround tightness
    The UI shows the score AND the factors so dispatchers can argue with it.
    """
    company_id = current_user["company_id"]
    f = sb.table("flights").select("*").eq("id", flight_id) \
        .eq("company_id", company_id).execute()
    if not f.data:
        raise HTTPException(status_code=404, detail="Flight not found")
    flight = f.data[0]

    score = 0
    factors: list[dict] = []

    # 1. Weather at origin (max +35)
    origin = (flight.get("origin_code") or "").upper()
    if origin in _STATION_COORDS:
        try:
            w = await weather(station=origin, current_user=current_user, sb=sb)
            impact = w.get("ops_impact", {})
            risk = impact.get("risk", "low")
            if risk == "high":
                score += 35
                factors.append({"label": "طقس صعب في المغادرة", "weight": 35,
                                 "detail": ", ".join(impact.get("factors", []))})
            elif risk == "medium":
                score += 15
                factors.append({"label": "طقس متوسط في المغادرة", "weight": 15,
                                 "detail": ", ".join(impact.get("factors", []))})
        except Exception as e:
            log.info("weather lookup skipped: %s", e)

    # 2. Aircraft defects history (max +25)
    reg = flight.get("aircraft_registration")
    if reg:
        try:
            ac = sb.table("aircraft").select("id,operational_status") \
                .eq("registration", reg) \
                .eq("company_id", company_id).execute()
            if ac.data:
                aid = ac.data[0]["id"]
                op = ac.data[0].get("operational_status", "active")
                if op == "aog":
                    score += 25
                    factors.append({"label": "الطائرة AOG حالياً", "weight": 25,
                                     "detail": "تتطلب swap قبل الإقلاع"})
                elif op == "maintenance":
                    score += 15
                    factors.append({"label": "الطائرة في صيانة دورية", "weight": 15,
                                     "detail": "قد لا تكون جاهزة في الوقت"})
                # Open defects count
                cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                defects = sb.table("defects").select("id", count="exact") \
                    .eq("aircraft_id", aid) \
                    .gte("reported_at", cutoff) \
                    .execute()
                cnt = defects.count or 0
                if cnt >= 3:
                    score += 10
                    factors.append({"label": "$cnt عيوب في آخر 30 يوم", "weight": 10,
                                     "detail": "تاريخ تقني نشط"})
        except Exception as e:
            log.info("aircraft history skipped: %s", e)

    # 3. Time of day (max +15) — night ops higher risk
    try:
        dep = datetime.fromisoformat(flight["departure_time"].replace("Z", "+00:00"))
        h = dep.hour
        if h >= 22 or h < 5:
            score += 15
            factors.append({"label": "إقلاع ليلي", "weight": 15,
                             "detail": f"{dep.strftime('%H:%M')} UTC"})
        elif 5 <= h < 7 or 19 <= h < 22:
            score += 5
            factors.append({"label": "إقلاع في ساعة ذروة", "weight": 5})
    except Exception:
        pass

    # 4. Tight turnaround (max +20)
    try:
        dep_dt = datetime.fromisoformat(flight["departure_time"].replace("Z", "+00:00"))
        recent_arrival = sb.table("flights").select("arrival_time") \
            .eq("aircraft_registration", reg or "") \
            .eq("company_id", company_id) \
            .lt("arrival_time", dep_dt.isoformat()) \
            .order("arrival_time", desc=True).limit(1).execute()
        if recent_arrival.data:
            arr_str = recent_arrival.data[0].get("arrival_time")
            if arr_str:
                arr = datetime.fromisoformat(arr_str.replace("Z", "+00:00"))
                gap_min = (dep_dt - arr).total_seconds() / 60
                if 0 < gap_min < 45:
                    score += 20
                    factors.append({"label": "turnaround ضيق", "weight": 20,
                                     "detail": f"{int(gap_min)} د منذ آخر هبوط"})
                elif 45 <= gap_min < 60:
                    score += 10
                    factors.append({"label": "turnaround محدود", "weight": 10,
                                     "detail": f"{int(gap_min)} د"})
    except Exception:
        pass

    risk_band = "low" if score < 25 else "medium" if score < 50 else "high"
    return {
        "flight_id":      flight_id,
        "flight_number":  flight.get("flight_number"),
        "score":          min(score, 100),
        "risk_band":      risk_band,
        "factors":        factors,
        "computed_at":    datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────
# Fatigue-risk prediction (heuristic)
# ──────────────────────────────────────────────────────────────────────

@router.get("/crew/{crew_id}/fatigue-risk")
async def fatigue_risk(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Score 0-100 of fatigue risk for the next assignment.

    Factors:
      • Hours used vs ICAO 28-day cap (100h)
      • Consecutive duty days
      • Recent night flights
      • Self-reported fatigue in last 14d
      • Pending sick report
    """
    company_id = current_user["company_id"]
    c = sb.table("crew").select("*").eq("id", crew_id) \
        .eq("company_id", company_id).execute()
    if not c.data:
        raise HTTPException(status_code=404, detail="Crew not found")
    crew = c.data[0]

    score = 0
    factors: list[dict] = []

    # 1. 28-day hours vs 100h cap
    h28 = float(crew.get("last_28day_hours") or crew.get("monthly_flight_hours") or 0)
    pct = (h28 / 100.0) * 100
    if pct >= 90:
        score += 40
        factors.append({"label": f"{int(h28)}س / 28 يوم (≥ 90%)", "weight": 40,
                         "detail": "قريب جداً من سقف ICAO"})
    elif pct >= 75:
        score += 25
        factors.append({"label": f"{int(h28)}س / 28 يوم", "weight": 25})
    elif pct >= 60:
        score += 10
        factors.append({"label": f"{int(h28)}س / 28 يوم", "weight": 10})

    # 2. Self-reported fatigue in last 14d (notifications table)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        u = sb.table("users").select("id").eq("crew_id", crew_id).execute()
        if u.data:
            n = sb.table("notifications").select("id", count="exact") \
                .eq("reference_id", crew_id) \
                .eq("reference_type", "crew") \
                .gte("created_at", cutoff).execute()
            if (n.count or 0) > 0:
                score += 30
                factors.append({"label": "تقرير إجهاد/مرض ذاتي مؤخراً", "weight": 30,
                                 "detail": f"{n.count} بلاغ في آخر 14 يوم"})
    except Exception:
        pass

    # 3. Consecutive duty days
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        a = sb.table("assignments") \
            .select("flight:flight_id(departure_time)") \
            .eq("crew_id", crew_id).execute()
        days = set()
        for row in (a.data or []):
            flt = row.get("flight") or {}
            t = flt.get("departure_time")
            if t and t >= cutoff:
                try:
                    days.add(datetime.fromisoformat(
                        t.replace("Z", "+00:00")).date())
                except Exception:
                    pass
        consec = len(days)
        if consec >= 6:
            score += 15
            factors.append({"label": f"{consec} أيام عمل في 7 أيام", "weight": 15})
        elif consec >= 5:
            score += 8
            factors.append({"label": f"{consec} أيام عمل في 7 أيام", "weight": 8})
    except Exception:
        pass

    # 4. Blocked / on rest
    status = crew.get("status")
    if status == "blocked":
        score += 30
        factors.append({"label": "حالة محظور", "weight": 30,
                         "detail": "موقوف عن الطيران"})
    elif status == "on_rest":
        score += 20
        factors.append({"label": "في فترة راحة", "weight": 20,
                         "detail": "يحتاج وقت إضافي قبل التكليف"})

    risk_band = "low" if score < 30 else "medium" if score < 60 else "high"
    return {
        "crew_id":        crew_id,
        "name":           crew.get("full_name_ar") or crew.get("full_name_en"),
        "score":          min(score, 100),
        "risk_band":      risk_band,
        "factors":        factors,
        "hours_28day":    h28,
        "computed_at":    datetime.now(timezone.utc).isoformat(),
    }
