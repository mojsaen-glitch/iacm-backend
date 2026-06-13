"""Standby report — PURE, READ-ONLY aggregation (R6.1).

Turns `standby_assignments` rows into per-crew monthly counts. It NEVER writes,
never touches the DB, and is completely separate from the flight-hours engine:
the `window_hours` it reports are INFORMATIONAL only and are NOT flight hours,
duty hours, or anything fed into FTL/FDP or monthly_hours.

The state-derivation here mirrors the frozen states of
`app.api.v1.endpoints.standby._standby_state` (R5) but only the counts the
report needs — direct field checks plus the no-response timeout.
"""
from datetime import datetime, timedelta


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _blank(crew: dict) -> dict:
    return {
        "crew_id":          crew.get("id"),
        "crew_name_ar":     crew.get("full_name_ar", ""),
        "crew_name_en":     crew.get("full_name_en", ""),
        "rank":             crew.get("rank", ""),
        "base":             crew.get("base", ""),
        "shifts":           0,
        "window_hours":     0.0,   # informational ONLY — never flight/duty hours
        "callouts":         0,
        "accepted":         0,
        "rejected":         0,
        "no_response":      0,
        "expired":          0,
        "assignments_made": 0,
        "last_callout_at":  None,
    }


def compute_standby_report(rows: list, crew_by_id: dict, now: datetime) -> dict:
    """Aggregate standby rows per crew. `now` drives only the no-response
    timeout classification. Returns {"crew": [...], "totals": {...}}.
    An empty `rows` yields empty crew + zeroed totals (never an error)."""
    per: dict[str, dict] = {}

    def agg(cid):
        if cid not in per:
            per[cid] = _blank(crew_by_id.get(cid, {"id": cid}))
        return per[cid]

    for r in rows:
        cid = r.get("crew_id")
        if not cid:
            continue
        a = agg(cid)
        a["shifts"] += 1

        s, e = _parse_dt(r.get("start_time")), _parse_dt(r.get("end_time"))
        if s and e and e > s:
            a["window_hours"] += (e - s).total_seconds() / 3600.0

        called = bool(r.get("called_out"))
        if called:
            a["callouts"] += 1
        resp = r.get("response_status")
        if resp == "ACCEPTED":
            a["accepted"] += 1
        elif resp == "REJECTED":
            a["rejected"] += 1
        elif resp is None and called:
            co = _parse_dt(r.get("called_out_at"))
            if co is not None:
                deadline = co + timedelta(minutes=int(r.get("response_minutes") or 60))
                if now > deadline:
                    a["no_response"] += 1
        if r.get("status") == "EXPIRED":
            a["expired"] += 1
        if r.get("assignment_id"):
            a["assignments_made"] += 1

        co_raw = r.get("called_out_at")
        if co_raw and (a["last_callout_at"] is None
                       or str(co_raw) > str(a["last_callout_at"])):
            a["last_callout_at"] = co_raw

    crew_list = []
    totals = {k: 0 for k in ("shifts", "callouts", "accepted", "rejected",
                             "no_response", "expired", "assignments_made")}
    totals["window_hours"] = 0.0
    for a in per.values():
        a["window_hours"] = round(a["window_hours"], 2)
        crew_list.append(a)
        for k in totals:
            totals[k] += a[k]
    totals["window_hours"] = round(totals["window_hours"], 2)
    totals["crew_count"] = len(crew_list)

    crew_list.sort(key=lambda x: (-x["shifts"],
                                  str(x.get("crew_name_ar") or x.get("crew_id"))))
    return {"crew": crew_list, "totals": totals}
