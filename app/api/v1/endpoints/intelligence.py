"""Intelligence & external integrations — METAR/TAF + AI-lite predictions.

Weather sources, tried in order:
  1. **CheckWX** (paid-tier-like JSON, parsed wxString/clouds/flight_category)
     Used as primary when CHECKWX_API_KEY env var is set.
     Free tier ceiling: 300 calls/day per key — we cache aggressively
     (10 min TTL per ICAO) so 22 stations × multiple dashboards still
     stays inside the quota.
  2. **AviationWeather.gov** (NOAA — keyless, unlimited) — fallback
     when CheckWX is unset, errors, or returns 429.

Three readers:
  • /intelligence/weather?station=XXX        — current METAR/TAF + ops impact
  • /intelligence/flight/{id}/delay-risk      — heuristic delay score
  • /intelligence/crew/{id}/fatigue-risk      — heuristic fatigue score
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple, Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, SbClient

router = APIRouter(prefix="/intelligence", tags=["Intelligence"])
log = logging.getLogger(__name__)


# ── In-memory cache so we don't burn the CheckWX free-tier quota ────
# Keyed on ICAO; entries expire after CACHE_TTL_SECONDS.
# Using typing.Dict/Tuple for compatibility with any Python the deploy
# target lands on, even if we get downgraded to 3.8.
_WEATHER_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 600  # 10 minutes — fresh enough for ops, cheap enough for the quota


# ──────────────────────────────────────────────────────────────────────
# IATA → ICAO mapping for the Iraqi Airways network.
#
# We keep accepting IATA codes from the frontend (which is what every
# legacy UI uses) and translate to ICAO for the AviationWeather call.
# Extend this table as routes are added.
# ──────────────────────────────────────────────────────────────────────
_IATA_TO_ICAO: Dict[str, str] = {
    # Iraq
    "BGW": "ORBI",  # Baghdad (Saddam International / now Baghdad International)
    "BSR": "ORMM",  # Basra
    "EBL": "ORER",  # Erbil
    "NJF": "ORNI",  # Najaf
    "ISU": "ORSU",  # Sulaymaniyah
    # Gulf
    "DXB": "OMDB",  # Dubai
    "AUH": "OMAA",  # Abu Dhabi
    "SHJ": "OMSJ",  # Sharjah
    "DOH": "OTHH",  # Doha (Hamad)
    "KWI": "OKBK",  # Kuwait
    "BAH": "OBBI",  # Bahrain
    "MCT": "OOMS",  # Muscat
    "RUH": "OERK",  # Riyadh (King Khalid)
    "JED": "OEJN",  # Jeddah
    # Middle East
    "AMM": "OJAI",  # Amman (Queen Alia)
    "BEY": "OLBA",  # Beirut
    "DAM": "OSDI",  # Damascus
    "CAI": "HECA",  # Cairo
    # Turkey / Asia
    "IST": "LTFM",  # Istanbul (new airport)
    "SAW": "LTFJ",  # Sabiha Gokcen
    "IKA": "OIIE",  # Tehran Imam Khomeini
    "MHD": "OIMM",  # Mashhad
    "DEL": "VIDP",  # Delhi
    # Europe
    "FRA": "EDDF",  # Frankfurt
    "LHR": "EGLL",  # London Heathrow
    "VIE": "LOWW",  # Vienna
    "ARN": "ESSA",  # Stockholm Arlanda
}

# Reverse lookup so the response can echo whichever the caller sent.
_ICAO_TO_IATA = {v: k for k, v in _IATA_TO_ICAO.items()}


def _resolve_codes(input_code: str) -> Tuple[str, str]:
    """Return (iata, icao) regardless of which the caller passed in."""
    c = (input_code or "").strip().upper()
    if c in _IATA_TO_ICAO:
        return c, _IATA_TO_ICAO[c]
    if c in _ICAO_TO_IATA:
        return _ICAO_TO_IATA[c], c
    # Unknown — return as-is and let AviationWeather decide
    return c, c


# ──────────────────────────────────────────────────────────────────────
# METAR fetching + parsing
# ──────────────────────────────────────────────────────────────────────

async def _fetch_metar_checkwx(icao: str, api_key: str) -> Optional[dict]:
    """CheckWX returns a richer parsed METAR. Field shape differs from
    AviationWeather, so we normalise into the same dict so downstream
    code doesn't care which source served us."""
    url = f"https://api.checkwx.com/metar/{icao}/decoded"
    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            headers={
                "X-API-Key":  api_key,
                "User-Agent": "IACM-FlightOps/1.0",
            },
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 429:
                log.warning("CheckWX quota exhausted for %s — falling back", icao)
                return None
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        log.warning("CheckWX METAR fetch failed for %s: %s", icao, e)
        return None

    rows = payload.get("data") or []
    if not rows:
        return None
    r = rows[0]

    # Normalise CheckWX → AviationWeather-shape dict so the rest of the
    # pipeline (parsing, ops_impact, response shape) doesn't change.
    temp = (r.get("temperature") or {}).get("celsius")
    dewp = (r.get("dewpoint")    or {}).get("celsius")
    wind = r.get("wind") or {}
    wspd_kt = wind.get("speed_kts")
    wgst_kt = wind.get("gust_kts")
    wdir    = wind.get("degrees")
    vis     = (r.get("visibility") or {}).get("meters_float")
    altim   = (r.get("barometer")  or {}).get("hpa")
    clouds  = [{
        "cover": (c.get("code") or "").upper(),
        "base":  c.get("base_feet_agl") or c.get("feet"),
    } for c in (r.get("clouds") or [])]
    wx_str = " ".join(
        (c.get("code") or "").upper()
        for c in (r.get("conditions") or [])
    ) or None

    return {
        "rawOb":    r.get("raw_text"),
        "temp":     temp,
        "dewp":     dewp,
        "wspd":     wspd_kt,
        "wgst":     wgst_kt,
        "wdir":     wdir,
        "visib":    vis,
        "altim":    altim,
        "clouds":   clouds,
        "wxString": wx_str,
        "lat":      (r.get("station") or {}).get("geometry", {}).get("coordinates", [None, None])[1],
        "lon":      (r.get("station") or {}).get("geometry", {}).get("coordinates", [None, None])[0],
        "_source":  "checkwx",
    }


async def _fetch_metar_avwx(icao: str) -> Optional[dict]:
    """AviationWeather.gov fallback — keyless, unlimited."""
    url = ("https://aviationweather.gov/api/data/metar"
           f"?ids={icao}&format=json&hours=2")
    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            headers={"User-Agent": "IACM-FlightOps/1.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("AviationWeather METAR fetch failed for %s: %s", icao, e)
        return None
    if not data or not isinstance(data, list):
        return None
    result = data[0]
    result["_source"] = "aviationweather"
    return result


async def _fetch_metar(icao: str) -> Optional[dict]:
    """Source-aware fetch with cache. Tries CheckWX first if a key is
    configured, falls back to AviationWeather, returns whichever wins."""
    # Cache hit?
    now = time.time()
    cached = _WEATHER_CACHE.get(icao)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    api_key = os.getenv("CHECKWX_API_KEY", "").strip()
    metar: Optional[dict] = None
    if api_key:
        metar = await _fetch_metar_checkwx(icao, api_key)
    if metar is None:
        metar = await _fetch_metar_avwx(icao)

    if metar is not None:
        _WEATHER_CACHE[icao] = (now, metar)
    return metar


async def _fetch_taf(icao: str) -> Optional[str]:
    """Pull the latest raw TAF — kept as a string for display. We don't
    parse the forecast windows here; the dispatcher reads it raw."""
    url = (f"https://aviationweather.gov/api/data/taf"
           f"?ids={icao}&format=json")
    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            headers={"User-Agent": "IACM-FlightOps/1.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    if not data or not isinstance(data, list):
        return None
    return data[0].get("rawTAF")


def _parse_visibility(visib) -> Optional[float]:
    """METAR visibility comes back as either a number (metres or statute
    miles) or a string like '10+', '6SM', '9999'. Return metres."""
    if visib is None: return None
    if isinstance(visib, (int, float)):
        # Heuristic: AviationWeather uses metres for non-US stations,
        # statute miles for US. Anything > 50 has to be metres because
        # 50 SM = 80 km — outside any METAR meaningfully reports.
        return float(visib) * (1609.34 if visib < 50 else 1.0)
    s = str(visib).strip().replace("+", "")
    # SM (statute miles)
    if "SM" in s.upper():
        try:
            n = float(s.upper().replace("SM", "").strip())
            return n * 1609.34
        except ValueError:
            return None
    # Plain number (assume metres for non-US)
    try:
        n = float(s)
        return n * (1609.34 if n < 50 else 1.0)
    except ValueError:
        return None


def _weather_code_from_wx(wx_string: Optional[str], clouds: list) -> int:
    """Approximate WMO weather code from METAR wxString + clouds.
    Mirrors Open-Meteo's coding so the frontend icon picker keeps working."""
    wx = (wx_string or "").upper()
    if "TS" in wx: return 95
    if "FZ" in wx and ("RA" in wx or "DZ" in wx): return 65
    if "SN" in wx: return 75
    if "RA" in wx or "SHRA" in wx: return 65
    if "DZ" in wx: return 53
    if "FG" in wx: return 45
    if "BR" in wx: return 48  # mist
    if "HZ" in wx: return 48  # haze
    # Fall back to cloud cover
    if clouds:
        max_cover = max((_cloud_octas(c.get("cover", "")) for c in clouds), default=0)
        if max_cover >= 8: return 3
        if max_cover >= 5: return 2
        if max_cover >= 3: return 1
    return 0  # clear


def _cloud_octas(cover: str) -> int:
    return {"SKC": 0, "CLR": 0, "NCD": 0, "FEW": 2, "SCT": 4,
            "BKN": 6, "OVC": 8, "VV":  8}.get(cover.upper(), 0)


def _wmo_label(code: int) -> str:
    if code == 0: return "صافي"
    if code in (1, 2, 3): return "غائم جزئياً"
    if code in (45, 48): return "ضباب"
    if code in (51, 53, 55): return "رذاذ"
    if code in (61, 63, 65, 80, 81, 82): return "مطر"
    if code in (71, 73, 75, 85, 86): return "ثلج"
    if code in (95, 96, 99): return "عواصف رعدية"
    return f"WMO {code}"


def _cloud_ceiling_ft(clouds: list) -> Optional[int]:
    """Lowest BKN/OVC layer base — the official 'ceiling' in aviation."""
    if not clouds: return None
    bases = [c.get("base") for c in clouds
             if c.get("cover", "").upper() in {"BKN", "OVC", "VV"}
             and isinstance(c.get("base"), (int, float))]
    return int(min(bases)) if bases else None


def _ops_impact(wmo: int, vis_m: Optional[float], wind_kmh: Optional[float],
                gust_kmh: Optional[float], ceiling_ft: Optional[int]) -> dict:
    """Aviation-grade impact heuristic.

    Thresholds:
      • CAT-I minima: ceiling ≥ 200 ft, visibility ≥ 800 m
      • Lower than CAT-I → flag as high
      • Wind > 55 km/h sustained OR gusts > 75 km/h → high
      • Thunderstorms → high
    """
    factors = []  # list[str]
    risk = "low"

    # Visibility
    if vis_m is not None:
        if vis_m < 1500:
            factors.append(f"رؤية {int(vis_m)}م — تحت CAT-I")
            risk = "high"
        elif vis_m < 3000:
            factors.append(f"رؤية محدودة {int(vis_m)}م")
            if risk != "high": risk = "medium"

    # Cloud ceiling
    if ceiling_ft is not None:
        if ceiling_ft < 200:
            factors.append(f"سقف غيوم {ceiling_ft} قدم — تحت CAT-I")
            risk = "high"
        elif ceiling_ft < 500:
            factors.append(f"سقف غيوم منخفض {ceiling_ft} قدم")
            if risk != "high": risk = "medium"

    # Wind (sustained)
    if wind_kmh and wind_kmh >= 55:
        factors.append(f"رياح ثابتة {int(wind_kmh)} كم/س")
        risk = "high"
    elif wind_kmh and wind_kmh >= 35:
        factors.append(f"رياح {int(wind_kmh)} كم/س")
        if risk != "high": risk = "medium"

    # Gusts (often the real ops constraint)
    if gust_kmh and gust_kmh >= 75:
        factors.append(f"هبّات {int(gust_kmh)} كم/س")
        risk = "high"
    elif gust_kmh and gust_kmh >= 55:
        factors.append(f"هبّات {int(gust_kmh)} كم/س")
        if risk != "high": risk = "medium"

    # Weather phenomena
    if wmo in (95, 96, 99):
        factors.append("عواصف رعدية")
        risk = "high"
    elif wmo in (71, 73, 75, 85, 86):
        factors.append("هطول ثلوج")
        if risk != "high": risk = "medium"
    elif wmo in (45, 48):
        factors.append("ضباب/شبورة")
        if risk == "low": risk = "medium"

    return {
        "risk":    risk,
        "factors": factors or ["ظروف طبيعية"],
    }


@router.get("/weather")
async def weather(
    current_user: CurrentUser,
    sb: SbClient,
    station: str = Query(..., description="IATA or ICAO code"),
):
    """Live METAR for the station + parsed ops impact + raw TAF.

    Returns same shape the existing UI expects, with these extras:
      • icao            — the ICAO code used for lookup
      • cloud_ceiling_ft — lowest BKN/OVC base
      • altimeter_hpa   — QNH
      • raw_metar       — original METAR text (always include for pilots)
      • raw_taf         — original TAF forecast text
      • gust_kmh        — peak gust if reported
    """
    iata, icao = _resolve_codes(station)
    if not icao or len(icao) != 4:
        raise HTTPException(status_code=422,
            detail=f"'{station}' is not a recognised IATA/ICAO code")

    metar = await _fetch_metar(icao)
    if not metar:
        raise HTTPException(status_code=502,
            detail=f"No METAR available for {icao}. Station may be offline.")

    # ── Pull fields safely ─────────────────────────────────────────
    temp = metar.get("temp")
    wspd_kt = metar.get("wspd")            # knots
    wgst_kt = metar.get("wgst")            # knots
    wdir    = metar.get("wdir")
    vis_m   = _parse_visibility(metar.get("visib"))
    clouds  = metar.get("clouds") or []
    wx_str  = metar.get("wxString")
    altim   = metar.get("altim")           # already hPa for non-US
    raw     = metar.get("rawOb")

    wmo = _weather_code_from_wx(wx_str, clouds)
    ceiling = _cloud_ceiling_ft(clouds)

    wind_kmh = (wspd_kt * 1.852) if wspd_kt is not None else None
    gust_kmh = (wgst_kt * 1.852) if wgst_kt is not None else None

    impact = _ops_impact(wmo, vis_m, wind_kmh, gust_kmh, ceiling)

    # TAF (best-effort — don't fail the whole call if missing)
    taf = await _fetch_taf(icao)

    return {
        "station":          iata or icao,   # echo what the UI expects
        "icao":             icao,
        "lat":              metar.get("lat"),
        "lon":              metar.get("lon"),
        "temperature_c":    temp,
        "dew_point_c":      metar.get("dewp"),
        "wind_speed_kmh":   round(wind_kmh, 1) if wind_kmh is not None else None,
        "wind_direction":   wdir,
        "wind_gust_kmh":    round(gust_kmh, 1) if gust_kmh is not None else None,
        "visibility_m":     int(vis_m) if vis_m is not None else None,
        "cloud_ceiling_ft": ceiling,
        "altimeter_hpa":    round(altim, 1) if altim else None,
        "weather_code":     wmo,
        "condition":        _wmo_label(wmo),
        "raw_metar":        raw,
        "raw_taf":          taf,
        "ops_impact":       impact,
        "source":           metar.get("_source", "unknown"),
        "fetched_at":       datetime.now(timezone.utc).isoformat(),
    }


# Legacy export — used by other modules that just need the coordinate set.
# Kept as IATA-keyed so existing call sites don't need refactoring.
_STATION_COORDS = {k: (None, None) for k in _IATA_TO_ICAO.keys()}


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
    factors = []  # list[dict]

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
    factors = []  # list[dict]

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
