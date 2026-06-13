"""On-Time Performance (OTP) — READ-ONLY analytics over existing flight rows.

Compares the SCHEDULED layer (STD/STA = departure_time/arrival_time) against
the ACTUAL layer (ATD/ATA = actual_departure_time/actual_arrival_time). A
flight with no actual time recorded is counted as "missing actual" — it never
breaks the report and is excluded from on-time ratios (which are over flights
that HAVE the relevant actual).

OTP definition (v1): a leg is ON-TIME when |actual − scheduled| ≤ threshold
minutes (default 15). Threshold is a plain function argument — NOT wired to
company settings (kept out of the sensitive keys by decision).

Pure computation: takes already-fetched flight dicts, returns a JSON-able
summary. Touches nothing — no writes, no engine, no GD, no hours.
"""
from __future__ import annotations

from datetime import datetime

DEFAULT_OTP_THRESHOLD_MIN = 15


def _dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _delay_min(scheduled, actual) -> float | None:
    s, a = _dt(scheduled), _dt(actual)
    if s is None or a is None:
        return None
    return (a - s).total_seconds() / 60.0


def compute_otp(flights: list[dict],
                threshold_min: int = DEFAULT_OTP_THRESHOLD_MIN) -> dict:
    """OTP summary for a set of flight rows (cancelled flights excluded)."""
    rows = [f for f in flights if (f.get("status") or "").lower() != "cancelled"]
    total = len(rows)

    dep_delays: list[float] = []     # ATD − STD (minutes) where ATD recorded
    arr_delays: list[float] = []     # ATA − STA
    with_atd = with_ata = 0
    dep_on_time = arr_on_time = 0
    reason_counts: dict[str, int] = {}

    for f in rows:
        dd = _delay_min(f.get("departure_time"), f.get("actual_departure_time"))
        if dd is not None:
            with_atd += 1
            dep_delays.append(dd)
            if abs(dd) <= threshold_min:
                dep_on_time += 1
        ad = _delay_min(f.get("arrival_time"), f.get("actual_arrival_time"))
        if ad is not None:
            with_ata += 1
            arr_delays.append(ad)
            if abs(ad) <= threshold_min:
                arr_on_time += 1
        # Delay reason Pareto — over flights that actually departed LATE
        # (positive ATD delay beyond threshold) and carry a reason code.
        if dd is not None and dd > threshold_min:
            code = (f.get("delay_reason_code") or "").strip().lower()
            if code:
                reason_counts[code] = reason_counts.get(code, 0) + 1

    def _pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    def _avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    # Average POSITIVE delay (lateness) — early arrivals don't reduce "delay".
    dep_late = [d for d in dep_delays if d > 0]
    arr_late = [d for d in arr_delays if d > 0]

    pareto = sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "threshold_min": threshold_min,
        "total_flights": total,
        "with_atd": with_atd,
        "with_ata": with_ata,
        "missing_actual": total - max(with_atd, with_ata),
        "departure_on_time": dep_on_time,
        "arrival_on_time": arr_on_time,
        "departure_otp_pct": _pct(dep_on_time, with_atd),
        "arrival_otp_pct": _pct(arr_on_time, with_ata),
        "avg_departure_delay_min": _avg(dep_late),
        "avg_arrival_delay_min": _avg(arr_late),
        "delay_reasons_pareto": [
            {"code": c, "count": n} for c, n in pareto
        ],
    }
