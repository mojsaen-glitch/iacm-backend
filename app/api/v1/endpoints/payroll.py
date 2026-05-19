"""Payroll engine — wage rates, payslip generation, payslip viewing.

Money is always represented as Decimal in code; Supabase stores NUMERIC.
Never coerce to float — accumulated rounding errors break audits.

Generation algorithm (per crew member, for one month):
    base_salary            = wage_rate.base_monthly_salary
    position_allowance     = wage_rate.position_allowance_monthly
    hourly_pay             = total_flight_hours      × wage_rate.hour_rate
    international_bonus    = international_hours     × wage_rate.international_hour_bonus
    night_bonus            = night_hours             × wage_rate.night_hour_bonus
    per_diem_total         = days_per_diem_domestic  × wage_rate.per_diem_domestic
                           + days_per_diem_international × wage_rate.per_diem_international
    gross_total            = sum of all components
    net_total              = gross_total - tax - deductions

Inputs come from the assignments + flights joined view for that month.
"""

import logging
import uuid
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/payroll", tags=["Payroll"])
log = logging.getLogger(__name__)

_FINANCE_ROLES = {"super_admin", "admin", "ops_manager"}


def _ensure_finance(user: dict) -> None:
    if user.get("role") not in _FINANCE_ROLES:
        raise ForbiddenError("Only admin / ops manager can manage payroll")


def _D(v) -> Decimal:
    """Coerce arbitrary JSON-number-like input to Decimal safely."""
    if v is None or v == "":
        return Decimal("0")
    return Decimal(str(v))


# ──────────────────────────────────────────────────────────────────────
# Wage rates — CRUD
# ──────────────────────────────────────────────────────────────────────

@router.get("/wage-rates")
async def list_wage_rates(current_user: CurrentUser, sb: SbClient):
    """Anyone in finance/ops can read wage rates. Crew can read their own
    rank's rate (so they can see what they're paid against)."""
    company_id = current_user["company_id"]
    res = sb.table("wage_rates").select("*").eq("company_id", company_id).execute()
    rows = res.data or []
    # Crew sees only their rank
    if current_user.get("role") == "crew":
        crew_id = current_user.get("crew_id")
        if crew_id:
            c = sb.table("crew").select("rank").eq("id", crew_id).execute()
            crew_rank = (c.data or [{}])[0].get("rank") if c.data else None
            rows = [r for r in rows if r.get("rank") == crew_rank]
        else:
            rows = []
    return rows


@router.post("/wage-rates", status_code=201)
async def upsert_wage_rate(data: dict, current_user: CurrentUser, sb: SbClient):
    """Create or update a wage rate. We upsert by (company_id, rank) so the
    admin can save the same form twice without dupes."""
    _ensure_finance(current_user)

    rank = (data.get("rank") or "").strip().lower()
    if not rank:
        raise HTTPException(status_code=422, detail="rank is required")

    company_id = current_user["company_id"]
    existing = sb.table("wage_rates") \
        .select("id") \
        .eq("company_id", company_id) \
        .eq("rank", rank) \
        .execute()

    payload = {
        "company_id":                  company_id,
        "rank":                        rank,
        "currency":                    data.get("currency", "IQD"),
        "base_monthly_salary":         str(_D(data.get("base_monthly_salary"))),
        "position_allowance_monthly":  str(_D(data.get("position_allowance_monthly"))),
        "hour_rate":                   str(_D(data.get("hour_rate"))),
        "international_hour_bonus":    str(_D(data.get("international_hour_bonus"))),
        "night_hour_bonus":            str(_D(data.get("night_hour_bonus"))),
        "per_diem_domestic":           str(_D(data.get("per_diem_domestic"))),
        "per_diem_international":      str(_D(data.get("per_diem_international"))),
        "notes":                       data.get("notes"),
        "is_active":                   data.get("is_active", True),
        "updated_by":                  current_user["id"],
        "updated_at":                  datetime.now(timezone.utc).isoformat(),
    }

    if existing.data:
        result = sb.table("wage_rates").update(payload) \
            .eq("id", existing.data[0]["id"]).execute()
    else:
        payload["id"]         = str(uuid.uuid4())
        payload["created_by"] = current_user["id"]
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        result = sb.table("wage_rates").insert(payload).execute()

    return result.data[0] if result.data else payload


@router.delete("/wage-rates/{rate_id}", status_code=204)
async def delete_wage_rate(rate_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_finance(current_user)
    res = sb.table("wage_rates").delete().eq("id", rate_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("Wage rate", rate_id)
    return None


# ──────────────────────────────────────────────────────────────────────
# Payslip generation
# ──────────────────────────────────────────────────────────────────────

def _month_bounds(month_str: str) -> tuple[str, str]:
    """Return ISO date strings for [first day, last day] of the month."""
    try:
        year, month = month_str.split("-")
        y, m = int(year), int(month)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="month must be 'YYYY-MM'")
    if m < 1 or m > 12:
        raise HTTPException(status_code=422, detail="invalid month")
    first = date(y, m, 1).isoformat()
    last_year, last_month = (y, m + 1) if m < 12 else (y + 1, 1)
    last = date(last_year, last_month, 1).isoformat()
    return first, last  # last is the first of NEXT month (exclusive upper bound)


def _flight_metrics_for_crew(sb, company_id: str, crew_id: str,
                              start: str, end: str) -> dict:
    """Aggregate flight hours, sectors, day-counts for a crew member in [start, end).

    Joins assignments → flights and tallies:
      total_hours, international_hours, night_hours, sectors,
      day_per_diem (domestic), day_per_diem (international)

    NIGHT = any portion of duty between 22:00–06:00 UTC counts the WHOLE
    flight as a night-hours contribution. Simpler than splitting the
    flight and close to how Iraqi Airways accounts for it.
    """
    # Pull assignments + nested flight info in one shot via PostgREST FK
    res = sb.table("assignments") \
        .select("flight_id, flight:flights!inner(id,departure_time,arrival_time,"
                "flight_type,origin_code,destination_code,company_id,status)") \
        .eq("crew_id", crew_id) \
        .gte("flight.departure_time", start) \
        .lt("flight.departure_time", end) \
        .execute()

    total_h, intl_h, night_h = Decimal("0"), Decimal("0"), Decimal("0")
    sectors = 0
    days_dom, days_int = set(), set()

    for row in (res.data or []):
        f = row.get("flight")
        if not f or f.get("company_id") != company_id:
            continue
        if (f.get("status") or "") == "cancelled":
            continue  # cancelled flights pay no hours

        try:
            dep = datetime.fromisoformat(f["departure_time"].replace("Z", "+00:00"))
            arr = datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue

        dur_h = Decimal((arr - dep).total_seconds()) / Decimal("3600")
        if dur_h <= 0:
            continue

        total_h += dur_h
        sectors += 1
        is_intl = (f.get("flight_type") or "domestic").lower() == "international"
        if is_intl:
            intl_h += dur_h

        # Night = any duty hour falls inside 22:00–06:00 UTC
        # Walk in 30-minute steps; cheap and accurate enough for finance.
        h, total_step = dep, dep
        while total_step < arr:
            hour_utc = total_step.hour
            if hour_utc >= 22 or hour_utc < 6:
                night_h += Decimal("0.5")
            total_step = total_step.replace(microsecond=0) \
                + (arr - total_step if (arr - total_step).total_seconds() < 1800
                   else (arr - total_step).__class__(seconds=1800))
            if total_step <= h:  # safety
                break
            h = total_step

        # Per-diem: every UTC date covered by the duty earns one day.
        day = dep.date()
        last_day = arr.date()
        while day <= last_day:
            (days_int if is_intl else days_dom).add(day)
            day = day.fromordinal(day.toordinal() + 1)

    return {
        "total_flight_hours":          total_h.quantize(Decimal("0.01")),
        "domestic_hours":              (total_h - intl_h).quantize(Decimal("0.01")),
        "international_hours":         intl_h.quantize(Decimal("0.01")),
        "night_hours":                 night_h.quantize(Decimal("0.01")),
        "sectors_flown":               sectors,
        "days_per_diem_domestic":      len(days_dom),
        "days_per_diem_international": len(days_int),
    }


@router.post("/generate/{month}", status_code=201)
async def generate_payroll(month: str, current_user: CurrentUser, sb: SbClient,
                            force: bool = Query(False, description="Re-generate even if rows exist (skip finalized)")):
    """Run the payroll generator for the given month.

    For each ACTIVE crew member: aggregate flight metrics, look up the
    wage rate for their rank, compute components, upsert a payslip row.
    Finalized rows are never touched, so accountants who already paid
    don't get their figures rewritten.
    """
    _ensure_finance(current_user)
    company_id = current_user["company_id"]
    start, end = _month_bounds(month)

    crew_res = sb.table("crew").select("id,rank,full_name_ar,full_name_en") \
        .eq("company_id", company_id).eq("status", "active").execute()
    rates_res = sb.table("wage_rates").select("*") \
        .eq("company_id", company_id).eq("is_active", True).execute()
    rates_by_rank = { (r.get("rank") or "").lower(): r for r in (rates_res.data or []) }

    existing_res = sb.table("payslips").select("id,crew_id,finalized") \
        .eq("company_id", company_id).eq("month", month).execute()
    existing_by_crew = { r["crew_id"]: r for r in (existing_res.data or []) }

    generated, skipped, missing_rate = 0, 0, []
    now_iso = datetime.now(timezone.utc).isoformat()

    for c in (crew_res.data or []):
        crew_id = c["id"]
        rank    = (c.get("rank") or "").lower()
        rate    = rates_by_rank.get(rank)
        old     = existing_by_crew.get(crew_id)

        # Don't overwrite finalized rows
        if old and old.get("finalized"):
            skipped += 1
            continue

        if not rate:
            missing_rate.append({"crew_id": crew_id, "rank": rank,
                                 "name": c.get("full_name_ar") or c.get("full_name_en")})
            continue

        m = _flight_metrics_for_crew(sb, company_id, crew_id, start, end)

        base   = _D(rate["base_monthly_salary"])
        posall = _D(rate["position_allowance_monthly"])
        hr     = _D(rate["hour_rate"])
        ihb    = _D(rate["international_hour_bonus"])
        nhb    = _D(rate["night_hour_bonus"])
        pd_dom = _D(rate["per_diem_domestic"])
        pd_int = _D(rate["per_diem_international"])

        hourly_pay   = (hr * m["total_flight_hours"]).quantize(Decimal("0.01"))
        intl_bonus   = (ihb * m["international_hours"]).quantize(Decimal("0.01"))
        night_bonus  = (nhb * m["night_hours"]).quantize(Decimal("0.01"))
        per_diem_tot = (pd_dom * m["days_per_diem_domestic"]
                        + pd_int * m["days_per_diem_international"]).quantize(Decimal("0.01"))

        gross = (base + posall + hourly_pay + intl_bonus
                 + night_bonus + per_diem_tot).quantize(Decimal("0.01"))
        net   = gross  # tax/deductions stay at 0 unless edited later

        payload = {
            "id":                          old["id"] if old else str(uuid.uuid4()),
            "company_id":                  company_id,
            "crew_id":                     crew_id,
            "month":                       month,
            "currency":                    rate.get("currency", "IQD"),
            "total_flight_hours":          str(m["total_flight_hours"]),
            "domestic_hours":              str(m["domestic_hours"]),
            "international_hours":         str(m["international_hours"]),
            "night_hours":                 str(m["night_hours"]),
            "sectors_flown":               m["sectors_flown"],
            "days_per_diem_domestic":      m["days_per_diem_domestic"],
            "days_per_diem_international": m["days_per_diem_international"],
            "base_salary":                 str(base),
            "position_allowance":          str(posall),
            "hourly_pay":                  str(hourly_pay),
            "international_bonus":         str(intl_bonus),
            "night_bonus":                 str(night_bonus),
            "per_diem_total":              str(per_diem_tot),
            "other_additions":             "0",
            "deductions":                  "0",
            "tax":                         "0",
            "gross_total":                 str(gross),
            "net_total":                   str(net),
            "finalized":                   False,
            "generated_at":                now_iso,
            "updated_at":                  now_iso,
        }

        if old:
            sb.table("payslips").update(payload).eq("id", old["id"]).execute()
        else:
            sb.table("payslips").insert(payload).execute()
        generated += 1

    # Mark the period as 'generated'
    period_existing = sb.table("payroll_periods").select("id") \
        .eq("company_id", company_id).eq("month", month).execute()
    period_payload = {
        "company_id":   company_id,
        "month":        month,
        "status":       "generated",
        "generated_at": now_iso,
        "updated_at":   now_iso,
    }
    if period_existing.data:
        sb.table("payroll_periods").update(period_payload) \
            .eq("id", period_existing.data[0]["id"]).execute()
    else:
        period_payload["id"]         = str(uuid.uuid4())
        period_payload["created_at"] = now_iso
        sb.table("payroll_periods").insert(period_payload).execute()

    return {
        "month":         month,
        "generated":     generated,
        "skipped":       skipped,
        "missing_rate":  missing_rate,
    }


# ──────────────────────────────────────────────────────────────────────
# Payslip viewing
# ──────────────────────────────────────────────────────────────────────

@router.get("/payslips")
async def list_payslips(current_user: CurrentUser, sb: SbClient,
                        month: Optional[str] = Query(None, description="YYYY-MM"),
                        crew_id: Optional[str] = None):
    """Finance/ops can list; crew can only list their own row."""
    company_id = current_user["company_id"]

    q = sb.table("payslips") \
        .select("*, crew:crew_id(full_name_ar,full_name_en,rank,employee_id)") \
        .eq("company_id", company_id)

    # Crew can only see their own rows
    if current_user.get("role") == "crew":
        own = current_user.get("crew_id")
        if not own:
            return []
        q = q.eq("crew_id", own)
    elif crew_id:
        q = q.eq("crew_id", crew_id)

    if month:
        q = q.eq("month", month)

    res = q.order("month", desc=True).execute()
    return res.data or []


@router.get("/payslips/{payslip_id}")
async def get_payslip(payslip_id: str, current_user: CurrentUser, sb: SbClient):
    res = sb.table("payslips") \
        .select("*, crew:crew_id(full_name_ar,full_name_en,rank,employee_id)") \
        .eq("id", payslip_id) \
        .eq("company_id", current_user["company_id"]) \
        .execute()
    if not res.data:
        raise NotFoundError("Payslip", payslip_id)
    row = res.data[0]
    # Crew can only fetch their own
    if current_user.get("role") == "crew" and row.get("crew_id") != current_user.get("crew_id"):
        raise ForbiddenError("Not your payslip")
    return row


@router.post("/payslips/{payslip_id}/finalize")
async def finalize_payslip(payslip_id: str, current_user: CurrentUser, sb: SbClient):
    """Lock a payslip from further generator runs (typically called once paid)."""
    _ensure_finance(current_user)
    company_id = current_user["company_id"]
    res = sb.table("payslips").update({
        "finalized":    True,
        "finalized_at": datetime.now(timezone.utc).isoformat(),
        "finalized_by": current_user["id"],
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }).eq("id", payslip_id).eq("company_id", company_id).execute()
    if not res.data:
        raise NotFoundError("Payslip", payslip_id)
    return res.data[0]


@router.patch("/payslips/{payslip_id}")
async def update_payslip(payslip_id: str, data: dict,
                          current_user: CurrentUser, sb: SbClient):
    """Adjust deductions / tax / additions on a non-finalized payslip."""
    _ensure_finance(current_user)
    existing = sb.table("payslips").select("*") \
        .eq("id", payslip_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Payslip", payslip_id)
    row = existing.data[0]
    if row.get("finalized"):
        raise HTTPException(status_code=409,
            detail="Cannot edit a finalized payslip — un-finalize first")

    editable = {"other_additions", "deductions", "tax", "notes"}
    update = {k: data[k] for k in editable if k in data}
    if not update:
        return row

    # Recompute gross / net after edits
    gross = (_D(row["base_salary"]) + _D(row["position_allowance"])
             + _D(row["hourly_pay"]) + _D(row["international_bonus"])
             + _D(row["night_bonus"]) + _D(row["per_diem_total"])
             + _D(update.get("other_additions", row["other_additions"])))
    deductions = _D(update.get("deductions", row["deductions"]))
    tax        = _D(update.get("tax",        row["tax"]))
    net = (gross - deductions - tax).quantize(Decimal("0.01"))

    update["gross_total"] = str(gross.quantize(Decimal("0.01")))
    update["net_total"]   = str(net)
    update["updated_at"]  = datetime.now(timezone.utc).isoformat()

    result = sb.table("payslips").update(update).eq("id", payslip_id).execute()
    return result.data[0] if result.data else row
