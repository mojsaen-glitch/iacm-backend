"""Crew Monthly Flight Hours — computation + Excel workbook.

Turns the manual WATEEN-style monthly matrix into a DB-driven, auto-calculated
report. The HOURS SOURCE is isolated in one place (``_credited_hours``) so it can
later switch from ``flights.duration_hours`` to an actual/scheduled block time or
a manual override without touching the matrix page or the Excel export.

Crediting policy (phase 1, per product decision):
  • operating  → counts toward monthly flight hours (full duration_hours)
  • deadhead   → shown in the cell, NOT credited; reported separately (count/hours)
  • standby    → shown as a day state, counted as Standby Days, 0 flight hours
  • observer / training → shown, 0 credited (a future setting may change this)
"""
from __future__ import annotations
import calendar
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.core.crew_roles import role_category, role_code, CAT_FLIGHT_DECK, CAT_CABIN
from app.core.exceptions import NotFoundError

# Every credited hour is traceable back to this source. Phase-1 of the
# actual-hours model: ACTUAL block (ATA − ATD) when OCC recorded both, else
# the scheduled duration_hours — an explicit per-leg Planned→Actual fallback.
HOURS_SOURCE = "flights.actual(ATA−ATD) → flights.duration_hours"


def _leg_hours(f: dict) -> tuple[float, bool]:
    """(hours, is_actual) for one leg. ACTUAL only when BOTH ATD and ATA are
    recorded and positive; anything else falls back to the scheduled block —
    old rows (no actual columns) behave exactly as before."""
    atd, ata = f.get("actual_departure_time"), f.get("actual_arrival_time")
    if atd and ata:
        try:
            d = datetime.fromisoformat(str(atd).replace("Z", "+00:00"))
            a = datetime.fromisoformat(str(ata).replace("Z", "+00:00"))
            h = (a - d).total_seconds() / 3600.0
            if h > 0:
                return round(h, 2), True
        except (ValueError, TypeError):
            pass
    return float(f.get("duration_hours") or 0), False

# Official monthly reports follow the BAGHDAD calendar (+03:00), not UTC — a
# red-eye departing 01:00 Baghdad on the 1st belongs to the NEW month. This is
# REPORTS-ONLY: compliance/FTL keeps its own independent windows untouched.
_BAGHDAD = timedelta(hours=3)


def _month_bounds_baghdad(year: int, month: int) -> tuple[str, str]:
    """[start, end) of the BAGHDAD calendar month as ISO strings with an
    explicit +03:00 offset — PostgREST compares timestamptz across offsets
    correctly, so the DB window itself follows the Baghdad month."""
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return (f"{year:04d}-{month:02d}-01T00:00:00+03:00",
            f"{end_year:04d}-{end_month:02d}-01T00:00:00+03:00")

# ── build_matrix result cache (per process / warm serverless instance) ───────
# The matrix is read repeatedly with the same params (matrix page + analytics +
# Excel export of the same month). A short TTL avoids recomputing the ~26k-row
# in-Python join each time. Invalidated immediately on a manual hour edit.
_MATRIX_CACHE: dict = {}
_MATRIX_TTL = 45.0     # seconds — short enough that new assignments appear quickly
_MATRIX_MAX = 8        # cap memory (each entry can be ~MB at 3000 crew)


def _matrix_key(company_id, year, month, filters):
    items = tuple(sorted((k, repr(v)) for k, v in (filters or {}).items()))
    return (company_id, year, month, items)


def _matrix_cache_get(key):
    hit = _MATRIX_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    if hit:
        _MATRIX_CACHE.pop(key, None)
    return None


def _matrix_cache_put(key, result):
    now = time.monotonic()
    for k in [k for k, v in _MATRIX_CACHE.items() if v[0] <= now]:
        _MATRIX_CACHE.pop(k, None)
    while len(_MATRIX_CACHE) >= _MATRIX_MAX:
        _MATRIX_CACHE.pop(next(iter(_MATRIX_CACHE)), None)
    _MATRIX_CACHE[key] = (now + _MATRIX_TTL, result)


def invalidate_matrix_cache(company_id=None):
    """Drop cached matrices (all, or just one company) — call after writes that
    change computed hours (manual overrides)."""
    if company_id is None:
        _MATRIX_CACHE.clear()
        return
    for k in [k for k in _MATRIX_CACHE if k[0] == company_id]:
        _MATRIX_CACHE.pop(k, None)

def _dh_factor(dh_credit) -> float:
    """Deadhead crediting policy as a multiplier (view-time setting)."""
    return {"half": 0.5, "full": 1.0}.get((dh_credit or "none"), 0.0)


def _credited_hours(duty_type: str, duration_hours: float, dh_factor: float = 0.0) -> float:
    """Single source of truth for "how many hours does this leg credit".
    operating → full; deadhead → duration × dh_factor (default 0); else → 0.
    Swap the body here when actual/scheduled block or overrides are introduced."""
    d = float(duration_hours or 0)
    if duty_type == "operating":
        return d
    if duty_type == "deadhead":
        return round(d * dh_factor, 2)
    return 0.0


def hm(hours: float) -> str:
    """Decimal hours → 'H:MM' (e.g. 4.5 → '4:30'). '' for zero/None."""
    if not hours:
        return ""
    total_min = int(round(float(hours) * 60))
    return f"{total_min // 60}:{total_min % 60:02d}"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _fetch_all(make_query, page_size: int = 1000) -> list[dict]:
    """Page through a PostgREST query (Supabase caps each response). `make_query`
    is a factory returning a FRESH builder so each page starts clean."""
    rows: list[dict] = []
    start = 0
    while True:
        res = make_query().range(start, start + page_size - 1).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _as_naive_utc(dt: datetime) -> datetime:
    """Normalize a (possibly tz-aware) datetime to naive UTC for safe comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def crew_flight_hours(sb, company_id: str, crew_id: str, dh_credit=None) -> dict:
    """Per-crew credited flight hours bucketed by period — month / last 28 days /
    year / total — using the SAME crediting policy as the matrix (operating full,
    deadhead × dh_factor) so the figures line up with the Monthly Hours report.

    Crew-scoped (only this member's assignments + their flights), so it stays cheap
    enough to call from the crew profile.
    """
    dh_factor = _dh_factor(dh_credit)

    asgs = _fetch_all(lambda: sb.table("assignments")
                      .select("flight_id, duty_type").eq("crew_id", crew_id))
    flight_ids = list({a.get("flight_id") for a in asgs if a.get("flight_id")})

    flight_by_id: dict[str, dict] = {}
    for chunk in _chunks(flight_ids, 100):
        # Cancelled flights never credit hours — excluded at the ENGINE level so
        # every consumer (matrix / Excel / crew profile) agrees.
        for f in _fetch_all(lambda ch=chunk: sb.table("flights")
                            .select("id, departure_time, duration_hours, "
                                    "actual_departure_time, actual_arrival_time")
                            .eq("company_id", company_id)
                            .neq("status", "cancelled").in_("id", ch)):
            flight_by_id[f["id"]] = f

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bag_now = now + _BAGHDAD
    # Month/year buckets follow the BAGHDAD calendar: compute the boundary on
    # the Baghdad wall clock, then shift back to naive-UTC for comparison
    # against the (naive-UTC) departure instants. last-28-days stays rolling.
    month_start = bag_now.replace(day=1, hour=0, minute=0,
                                  second=0, microsecond=0) - _BAGHDAD
    year_start = bag_now.replace(month=1, day=1, hour=0, minute=0,
                                 second=0, microsecond=0) - _BAGHDAD
    last28 = now - timedelta(days=28)

    # Last 6 calendar months (oldest → newest) for the profile trend chart.
    pairs: list[tuple[int, int]] = []
    y, m = bag_now.year, bag_now.month
    for _ in range(6):
        pairs.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    pairs.reverse()
    series_bucket = {p: 0.0 for p in pairs}

    total = year = last_28 = month = 0.0
    for a in asgs:
        f = flight_by_id.get(a.get("flight_id"))
        if not f:
            continue
        dt = _parse_dt(f.get("departure_time"))
        if dt is None:
            continue
        dt = _as_naive_utc(dt)
        credited = _credited_hours(a.get("duty_type") or "operating",
                                   _leg_hours(f)[0], dh_factor)
        if credited <= 0:
            continue
        total += credited
        if dt >= year_start:
            year += credited
        if dt >= last28:
            last_28 += credited
        if dt >= month_start:
            month += credited
        dtb = dt + _BAGHDAD            # series bucket = BAGHDAD calendar month
        key = (dtb.year, dtb.month)
        if key in series_bucket:
            series_bucket[key] += credited

    series = [{"year": yy, "month": mm, "hours": round(series_bucket[(yy, mm)], 2)}
              for (yy, mm) in pairs]

    return {
        "month": round(month, 2),
        "last_28_days": round(last_28, 2),
        "year": round(year, 2),
        "total": round(total, 2),
        "series": series,
    }


def month_hours_by_crew(sb, company_id: str, dh_credit=None) -> dict[str, float]:
    """Current-month CREDITED hours per crew for the whole company in ONE paged
    join query — for rankers (e.g. IROPS fairness) that must never loop a
    per-crew call. Same engine policy as the matrix: ``_credited_hours`` +
    cancelled flights excluded. Crew with no credited month hours are absent
    (treat missing as 0.0)."""
    dh = _dh_factor(dh_credit)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Baghdad month boundary, expressed as naive-UTC for the DB comparison.
    month_start = (now + _BAGHDAD).replace(day=1, hour=0, minute=0,
                                           second=0, microsecond=0) - _BAGHDAD
    rows = _fetch_all(lambda: sb.table("assignments")
                      .select("crew_id, duty_type, "
                              "flights!inner(duration_hours, departure_time, status, "
                              "actual_departure_time, actual_arrival_time)")
                      .eq("flights.company_id", company_id)
                      .neq("flights.status", "cancelled")
                      .gte("flights.departure_time", month_start.isoformat()))
    out: dict[str, float] = {}
    for r in rows:
        cid = r.get("crew_id")
        f = r.get("flights") or {}
        if not cid or not f:
            continue
        out[cid] = out.get(cid, 0.0) + _credited_hours(
            r.get("duty_type") or "operating",
            _leg_hours(f)[0], dh)
    return {k: round(v, 2) for k, v in out.items()}


def _crew_code(c: dict) -> str:
    return (c.get("roster_name") or c.get("nickname") or c.get("employee_id") or "").strip()


def _is_grounded(c: dict) -> bool:
    return bool(c.get("block_reason")) or bool(c.get("blocked_on")) \
        or (c.get("status") or "active") not in ("active", "available", "on_duty")


def _crew_type_of(rank: str) -> str:
    cat = role_category(rank)
    if cat == CAT_FLIGHT_DECK:
        return "pilots"
    if cat == CAT_CABIN:
        return "cabin"
    return "other"


def build_matrix(sb, company_id: str, year: int, month: int, filters: dict | None = None) -> dict:
    """Build the full monthly matrix for a company. Computation only — no I/O
    beyond the Supabase reads. Returns a JSON-serialisable dict."""
    filters = filters or {}
    _ckey = _matrix_key(company_id, year, month, filters)
    _cached = _matrix_cache_get(_ckey)
    if _cached is not None:
        return _cached
    dh_factor = _dh_factor(filters.get("dh_credit"))
    days_in_month = calendar.monthrange(year, month)[1]
    start, end = _month_bounds_baghdad(year, month)
    end_month, end_year = (1, year + 1) if month == 12 else (month + 1, year)

    # ── Reference data ──────────────────────────────────────────────────────
    crew_rows = _fetch_all(lambda: sb.table("crew").select(
        "id, full_name_en, full_name_ar, nickname, roster_name, employee_id, rank, "
        "base, status, block_reason, blocked_on, aircraft_qualifications, max_monthly_hours"
    ).eq("company_id", company_id))
    crew_by_id = {c["id"]: c for c in crew_rows}

    # Cancelled flights are excluded at the ENGINE level — they neither credit
    # hours nor appear as duty cells (matrix + Excel + analytics stay agreed).
    flights = _fetch_all(lambda: sb.table("flights").select(
        "id, flight_number, origin_code, destination_code, departure_time, "
        "arrival_time, duration_hours, aircraft_type, aircraft_id, "
        "actual_departure_time, actual_arrival_time"
    ).eq("company_id", company_id).neq("status", "cancelled")
     .gte("departure_time", start).lt("departure_time", end))
    flight_by_id = {f["id"]: f for f in flights}

    aircraft = _fetch_all(lambda: sb.table("aircraft").select("id, registration")
                          .eq("company_id", company_id))
    reg_by_ac = {a["id"]: (a.get("registration") or "") for a in aircraft}

    # Assignments for this month's flights (chunked by flight_id).
    assignments: list[dict] = []
    flight_ids = list(flight_by_id.keys())
    for chunk in _chunks(flight_ids, 100):
        # select * so a DB without the duty_type migration still works
        # (duty_type defaults to 'operating' when the column is absent).
        assignments.extend(_fetch_all(
            lambda ch=chunk: sb.table("assignments")
            .select("*").in_("flight_id", ch)))

    # ── Per-crew accumulation ───────────────────────────────────────────────
    rows_by_crew: dict[str, dict] = {}

    def _blank_row(c: dict) -> dict:
        return {
            "crew_id": c["id"],
            "name": c.get("full_name_en") or c.get("full_name_ar") or "",
            "name_ar": c.get("full_name_ar") or "",
            "code": _crew_code(c),
            "rank": c.get("rank") or "",
            "rank_code": role_code(c.get("rank")),
            "crew_type": _crew_type_of(c.get("rank")),
            "base": c.get("base") or "",
            "aircraft_qualifications": c.get("aircraft_qualifications") or "",
            "blocked": _is_grounded(c),
            "block_reason": c.get("block_reason") or "",
            "blocked_on": c.get("blocked_on"),
            "max_monthly_hours": float(c.get("max_monthly_hours") or 0),
            "days": {},                 # day(str) -> {legs:[...], day_hours}
            "aircraft_types": set(),
            "has_overrides": False,
        }

    for a in assignments:
        f = flight_by_id.get(a.get("flight_id"))
        cid = a.get("crew_id")
        c = crew_by_id.get(cid)
        if not f or not c:
            continue
        dt = _parse_dt(f.get("departure_time"))
        if dt is None:
            continue
        day = (dt + _BAGHDAD).day      # day cell follows the BAGHDAD calendar
        sta = _parse_dt(f.get("arrival_time"))
        duty = a.get("duty_type") or "operating"
        duration, is_actual = _leg_hours(f)   # ACTUAL block when ATD+ATA recorded
        credited = _credited_hours(duty, duration, dh_factor)
        reg = reg_by_ac.get(f.get("aircraft_id"), "")

        row = rows_by_crew.setdefault(cid, _blank_row(c))
        if f.get("aircraft_type"):
            row["aircraft_types"].add(f["aircraft_type"])
        leg = {
            "route": f"{f.get('origin_code', '')}-{f.get('destination_code', '')}",
            "flight_no": f.get("flight_number") or "",
            "duty_type": duty,
            "hours": round(credited, 2),
            "block": round(duration, 2),
            "std": dt.strftime("%H:%M"),
            "sta": sta.strftime("%H:%M") if sta else "",
            "aircraft_type": f.get("aircraft_type") or "",
            "registration": reg,
            "flight_id": f.get("id"),
            "assignment_id": a.get("id"),
            "actual": is_actual,            # Planned vs Actual, never mixed silently
        }
        d = row["days"].setdefault(str(day), {"legs": [], "day_hours": 0.0})
        d["legs"].append(leg)
        d["day_hours"] = round(d["day_hours"] + credited, 2)

    # ── Manual overrides — a super-admin edit REPLACES a day's credited hours ─
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{end_year:04d}-{end_month:02d}-01"
    try:
        overrides = _fetch_all(lambda: sb.table("crew_hours_overrides")
                               .select("crew_id, duty_date, override_hours")
                               .eq("company_id", company_id)
                               .gte("duty_date", start_date).lt("duty_date", end_date))
    except Exception:
        overrides = []   # table not created yet → overrides simply inactive
    for ov in overrides:
        c = crew_by_id.get(ov.get("crew_id"))
        if not c:
            continue
        ds = str(ov.get("duty_date") or "")
        try:
            day = int(ds[8:10])
        except (ValueError, IndexError):
            continue
        if not (1 <= day <= days_in_month):
            continue
        row = rows_by_crew.setdefault(ov["crew_id"], _blank_row(c))
        d = row["days"].setdefault(str(day), {"legs": [], "day_hours": 0.0})
        d["computed_hours"] = d["day_hours"]
        d["day_hours"] = float(ov.get("override_hours") or 0)
        d["override"] = True
        row["has_overrides"] = True

    # ── Finalise rows + per-crew aggregates ─────────────────────────────────
    def _finalise(row: dict) -> dict:
        first = second = 0.0
        flights_count = work_days = deadhead_count = standby_days = 0
        deadhead_hours = 0.0
        for day_str, d in row["days"].items():
            day = int(day_str)
            dh = d["day_hours"]
            if day <= 15:
                first += dh
            else:
                second += dh
            has_op = has_stby = False
            for leg in d["legs"]:
                dt_ = leg["duty_type"]
                if dt_ == "operating":
                    flights_count += 1
                    has_op = True
                elif dt_ == "deadhead":
                    deadhead_count += 1
                    deadhead_hours += leg["block"]
                elif dt_ == "standby":
                    has_stby = True
            if has_op:
                work_days += 1
            if has_stby:
                standby_days += 1
        month_total = round(first + second, 2)
        row["first_half"] = round(first, 2)
        row["second_half"] = round(second, 2)
        row["month_total"] = month_total
        row["flights_count"] = flights_count
        row["work_days"] = work_days
        row["deadhead_count"] = deadhead_count
        row["deadhead_hours"] = round(deadhead_hours, 2)
        row["standby_days"] = standby_days
        row["over_limit"] = bool(row["max_monthly_hours"]) and month_total > row["max_monthly_hours"]
        row["aircraft_types"] = sorted(row["aircraft_types"])
        return row

    # Rows shown: crew with activity this month OR grounded (zero-activity, non
    # grounded crew are summarised but not listed unless include_inactive).
    include_inactive = bool(filters.get("include_inactive"))
    all_active_ids = set(rows_by_crew.keys())

    display_rows: list[dict] = []
    without_hours: list[dict] = []
    for cid, c in crew_by_id.items():
        if cid in rows_by_crew:
            row = _finalise(rows_by_crew[cid])
        else:
            row = _finalise(_blank_row(c))
        # crew-dimension filters
        if not _passes_filters(row, c, filters):
            continue
        if row["month_total"] <= 0 and not row["blocked"]:
            without_hours.append({
                "name": row["name"], "code": row["code"],
                "rank": row["rank"], "base": row["base"],
            })
        active = cid in all_active_ids
        if not active and not row["blocked"] and not include_inactive:
            # zero-activity, not grounded → omit from the visible matrix
            if not filters.get("only_with_hours"):
                continue
        if filters.get("only_with_hours") and row["month_total"] <= 0:
            continue
        if filters.get("violations_only") and not row["over_limit"]:
            continue
        if filters.get("show_grounded") is False and row["blocked"]:
            continue
        display_rows.append(row)

    display_rows.sort(key=lambda r: (-r["month_total"], r["name"]))

    # ── Summary (over the FULL crew set, respecting dimension filters) ───────
    summary = _summary(crew_by_id, rows_by_crew, filters)

    # Advanced-dashboard breakdowns (from the displayed rows).
    with_hours = [r for r in display_rows if r["month_total"] > 0]
    summary["top10"] = [
        {"name": r["name"], "code": r["code"], "hours": r["month_total"]}
        for r in sorted(with_hours, key=lambda r: -r["month_total"])[:10]
    ]
    summary["bottom10"] = [
        {"name": r["name"], "code": r["code"], "hours": r["month_total"]}
        for r in sorted(with_hours, key=lambda r: r["month_total"])[:10]
    ]
    by_rank, by_base, by_ac = {}, {}, {}
    for r in display_rows:
        rk = r["rank_code"] or r["rank"]
        if rk:
            by_rank[rk] = round(by_rank.get(rk, 0.0) + r["month_total"], 2)
        if r["base"]:
            by_base[r["base"]] = round(by_base.get(r["base"], 0.0) + r["month_total"], 2)
        for d in r["days"].values():
            for leg in d["legs"]:
                if leg["hours"] > 0 and leg["aircraft_type"]:
                    by_ac[leg["aircraft_type"]] = round(by_ac.get(leg["aircraft_type"], 0.0) + leg["hours"], 2)
    summary["by_rank"] = [{"key": k, "hours": v} for k, v in sorted(by_rank.items(), key=lambda x: -x[1])]
    summary["by_aircraft"] = [{"key": k, "hours": v} for k, v in sorted(by_ac.items(), key=lambda x: -x[1])]
    summary["by_base"] = [{"key": k, "hours": v} for k, v in sorted(by_base.items(), key=lambda x: -x[1])]
    summary["dh_credit"] = filters.get("dh_credit") or "none"

    without_hours.sort(key=lambda r: r["name"])
    result = {
        "year": year, "month": month, "days_in_month": days_in_month,
        "rows": display_rows,
        "summary": summary,
        "without_hours": without_hours,
        "blocked": _blocked_list(crew_by_id, filters),
    }
    _matrix_cache_put(_ckey, result)
    return result


def _passes_filters(row: dict, c: dict, filters: dict) -> bool:
    ct = filters.get("crew_type")
    if ct and ct != "all" and row["crew_type"] != ct:
        return False
    if filters.get("rank") and (c.get("rank") or "").lower() != filters["rank"].lower():
        return False
    if filters.get("base") and (c.get("base") or "").lower() != filters["base"].lower():
        return False
    ac = filters.get("aircraft_type")
    if ac and ac not in row["aircraft_types"] and ac not in (row["aircraft_qualifications"] or ""):
        return False
    q = (filters.get("search") or "").strip().lower()
    if q:
        hay = f"{row['name']} {row['name_ar']} {row['code']}".lower()
        if q not in hay:
            return False
    return True


def _summary(crew_by_id: dict, rows_by_crew: dict, filters: dict) -> dict:
    totals = []
    blocked = without = 0
    total_hours = deadhead_hours = 0.0
    total_flights = standby_days = 0
    warnings = 0
    for cid, c in crew_by_id.items():
        # mirror dimension filters so summary matches the table scope
        probe = {
            "crew_type": _crew_type_of(c.get("rank")),
            "name": c.get("full_name_en") or "", "name_ar": c.get("full_name_ar") or "",
            "code": _crew_code(c), "aircraft_types": [], "aircraft_qualifications": c.get("aircraft_qualifications") or "",
        }
        if not _passes_filters(probe, c, {k: v for k, v in filters.items()
                                          if k in ("crew_type", "rank", "base", "search")}):
            continue
        row = rows_by_crew.get(cid)
        mt = row["month_total"] if row else 0.0
        grounded = _is_grounded(c)
        if grounded:
            blocked += 1
        if mt > 0:
            totals.append((mt, c))
            total_hours += mt
        elif not grounded:
            # grounded zero-hour crew are reported under blocked, not "without hours"
            without += 1
        if row:
            total_flights += row.get("flights_count", 0)
            deadhead_hours += row.get("deadhead_hours", 0.0)
            standby_days += row.get("standby_days", 0)
            if row.get("over_limit"):
                warnings += 1
    highest = max((t[0] for t in totals), default=0.0)
    lowest = min((t[0] for t in totals), default=0.0)
    return {
        "total_hours": round(total_hours, 2),
        "active_crew": len(totals),
        "crew_without_hours": without,
        "blocked_crew": blocked,
        "highest_hours": round(highest, 2),
        "lowest_hours": round(lowest, 2),
        "total_flights": total_flights,
        "deadhead_hours": round(deadhead_hours, 2),
        "standby_days": standby_days,
        "compliance_warnings": warnings,
    }


def _blocked_list(crew_by_id: dict, filters: dict) -> list[dict]:
    out = []
    for c in crew_by_id.values():
        if not _is_grounded(c):
            continue
        out.append({
            "crew_id": c["id"],
            "name": c.get("full_name_en") or c.get("full_name_ar") or "",
            "code": _crew_code(c),
            "rank": c.get("rank") or "",
            "reason": c.get("block_reason") or "",
            "blocked_on": c.get("blocked_on"),
            "status": c.get("status") or "",
        })
    out.sort(key=lambda r: r["name"])
    return out


# ── Crew Hours Legal Statement (per-crew traceable breakdown) ────────────────
def _route_chain(legs) -> str:
    """Full day route, e.g. BGW-MED-BGW, merged from consecutive leg routes."""
    parts: list[str] = []
    for leg in legs:
        r = (leg.get("route") or "").split("-")
        if len(r) == 2 and r[0]:
            if not parts:
                parts += [r[0], r[1]]
            else:
                if parts[-1] != r[0]:
                    parts.append(r[0])
                parts.append(r[1])
    return "-".join(parts)


def _inclusion(duty: str, duration: float, dh_factor: float = 0.0):
    """(included_in_total, credited_hours, reason, incomplete) per crediting policy."""
    incomplete = duration <= 0
    if duty == "operating":
        if incomplete:
            return False, 0.0, "Operating but duration is missing/zero — excluded pending verification", True
        return True, round(duration, 2), "Operating — counted in full toward monthly flight hours", False
    if duty == "deadhead":
        if dh_factor > 0 and not incomplete:
            return (True, round(duration * dh_factor, 2),
                    f"Deadhead credited at {int(dh_factor * 100)}% (policy setting)", False)
        return False, 0.0, "Deadhead — shown for the record, NOT counted in operating hours", incomplete
    if duty == "standby":
        return False, 0.0, "Standby — counted as a Standby day, not flight hours", incomplete
    if duty == "training":
        return False, 0.0, "Training — shown separately, not counted", incomplete
    if duty == "observer":
        return False, 0.0, "Observer — shown separately, not counted", incomplete
    return False, 0.0, f"{duty} — not counted", incomplete


def build_statement(sb, company_id: str, crew_id: str, year: int, month: int,
                    dh_credit=None) -> dict:
    """A fully traceable per-crew hours statement: every credited hour links back to
    a flight record (flight_id) + assignment (assignment_id) + source duration. Uses
    the same `_credited_hours` policy as the matrix, so totals match exactly."""
    dh_factor = _dh_factor(dh_credit)
    days_in_month = calendar.monthrange(year, month)[1]
    start, end = _month_bounds_baghdad(year, month)
    end_month, end_year = (1, year + 1) if month == 12 else (month + 1, year)
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{end_year:04d}-{end_month:02d}-01"

    cres = sb.table("crew").select(
        "id, full_name_en, full_name_ar, nickname, roster_name, employee_id, rank, "
        "base, status, block_reason, blocked_on, max_monthly_hours"
    ).eq("id", crew_id).eq("company_id", company_id).limit(1).execute()
    if not cres.data:
        raise NotFoundError("crew", crew_id)
    c = cres.data[0]

    asgs = _fetch_all(lambda: sb.table("assignments").select("*").eq("crew_id", crew_id))
    flight_ids = list({a.get("flight_id") for a in asgs if a.get("flight_id")})
    flights: list[dict] = []
    for chunk in _chunks(flight_ids, 100):
        # Same engine-level rule: cancelled flights never credit hours.
        flights.extend(_fetch_all(
            lambda ch=chunk: sb.table("flights").select(
                "id, flight_number, origin_code, destination_code, departure_time, "
                "arrival_time, duration_hours, aircraft_type, aircraft_id, "
                "actual_departure_time, actual_arrival_time")
            .eq("company_id", company_id).neq("status", "cancelled")
            .gte("departure_time", start)
            .lt("departure_time", end).in_("id", ch)))
    flight_by_id = {f["id"]: f for f in flights}
    aircraft = _fetch_all(lambda: sb.table("aircraft").select("id, registration")
                          .eq("company_id", company_id))
    reg_by_ac = {a["id"]: (a.get("registration") or "") for a in aircraft}

    legs: list[dict] = []
    day_computed: dict[int, float] = {}
    operating_hours = deadhead_hours = 0.0
    deadhead_count = training_count = observer_count = flights_count = 0
    work_days = set()
    standby_set = set()

    for a in asgs:
        f = flight_by_id.get(a.get("flight_id"))
        if not f:
            continue
        dt = _parse_dt(f.get("departure_time"))
        if dt is None:
            continue
        sta = _parse_dt(f.get("arrival_time"))
        duty = a.get("duty_type") or "operating"
        duration, is_actual = _leg_hours(f)   # ACTUAL block when ATD+ATA recorded
        included, credited, reason, incomplete = _inclusion(duty, duration, dh_factor)
        day = (dt + _BAGHDAD).day      # day cell follows the BAGHDAD calendar
        legs.append({
            "date": f"{year:04d}-{month:02d}-{day:02d}", "day": day,
            "flight_no": f.get("flight_number") or "",
            "duty_type": duty,
            "route": f"{f.get('origin_code', '')}-{f.get('destination_code', '')}",
            "from": f.get("origin_code") or "", "to": f.get("destination_code") or "",
            "aircraft_type": f.get("aircraft_type") or "",
            "registration": reg_by_ac.get(f.get("aircraft_id"), ""),
            "std": dt.strftime("%H:%M"), "sta": sta.strftime("%H:%M") if sta else "",
            "duration_hours": round(duration, 2),
            "hours_source": "actual" if is_actual else "scheduled",
            "credited_hours": credited,
            "included": included,
            "reason": reason,
            # Per-leg traceability: actual legs name the ATD/ATA source;
            # scheduled legs keep the historical value (back-compat).
            "source": "flights.actual(ATA-ATD)" if is_actual
                      else "flights.duration_hours",
            "flight_id": f.get("id"), "assignment_id": a.get("id"),
            "incomplete": incomplete,
        })
        if included:
            day_computed[day] = day_computed.get(day, 0.0) + credited
        if duty == "operating" and included:
            operating_hours += credited
            flights_count += 1
            work_days.add(day)
        elif duty == "deadhead":
            deadhead_count += 1
            deadhead_hours += duration
            if included:
                work_days.add(day)
        elif duty == "standby":
            standby_set.add(day)
        elif duty == "training":
            training_count += 1
        elif duty == "observer":
            observer_count += 1

    # day route chains (full multi-sector route per day)
    by_day = defaultdict(list)
    for leg in legs:
        by_day[leg["day"]].append(leg)
    for leg in legs:
        leg["day_route"] = _route_chain(sorted(by_day[leg["day"]], key=lambda x: x["std"]))

    # overrides for this crew/month
    try:
        overrides = _fetch_all(lambda: sb.table("crew_hours_overrides").select(
            "duty_date, override_hours, old_value, reason, note, created_by_name, created_at")
            .eq("company_id", company_id).eq("crew_id", crew_id)
            .gte("duty_date", start_date).lt("duty_date", end_date))
    except Exception:
        overrides = []
    override_by_day: dict[int, float] = {}
    for ov in overrides:
        ds = str(ov.get("duty_date") or "")
        try:
            override_by_day[int(ds[8:10])] = float(ov.get("override_hours") or 0)
        except (ValueError, IndexError):
            continue

    # OFFICIAL credited total = effective day hours (override replaces computed).
    effective_total = 0.0
    for d in range(1, days_in_month + 1):
        if d in override_by_day:
            effective_total += override_by_day[d]
            if override_by_day[d] > 0:
                work_days.add(d)
        else:
            effective_total += day_computed.get(d, 0.0)

    try:
        ares = sb.table("crew_hours_audit_log").select("*") \
            .eq("company_id", company_id).eq("crew_id", crew_id) \
            .order("created_at", desc=True).limit(200).execute()
        audit = ares.data or []
    except Exception:
        audit = []

    legs.sort(key=lambda x: (x["day"], x["std"]))
    return {
        "crew": {
            "crew_id": c["id"],
            "name": c.get("full_name_en") or c.get("full_name_ar") or "",
            "name_ar": c.get("full_name_ar") or "",
            "code": _crew_code(c),
            "rank": c.get("rank") or "",
            "rank_code": role_code(c.get("rank")),
            "base": c.get("base") or "",
            "company_id": company_id,
            "max_monthly_hours": float(c.get("max_monthly_hours") or 0),
            "blocked": _is_grounded(c),
            "block_reason": c.get("block_reason") or "",
        },
        "period": {"year": year, "month": month, "days_in_month": days_in_month},
        "summary": {
            "operating_hours": round(operating_hours, 2),
            "credited_total": round(effective_total, 2),
            "deadhead_hours": round(deadhead_hours, 2),
            "deadhead_count": deadhead_count,
            "standby_days": len(standby_set),
            "training_count": training_count,
            "observer_count": observer_count,
            "flights_count": flights_count,
            "work_days": len(work_days),
            "has_overrides": bool(override_by_day),
            "dh_credit": dh_credit or "none",
        },
        "legs": legs,
        "overrides": overrides,
        "audit": audit,
        "source": HOURS_SOURCE,
    }
