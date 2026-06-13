"""Monthly standby roster — DRAFT PREVIEW generator (R6.3).

PURE + READ-ONLY. Produces a *proposed* standby roster for a month and returns
it; it persists NOTHING, creates NO standby row, NO flight assignment, NO
callout. Activating a draft is a separate, explicitly-approved step (not here).

Eligibility (status / documents / training / time-conflict / rest) is delegated
to an injected `is_eligible(crew_id, start_iso, end_iso) -> (hard_reasons,
warnings)` — the endpoint wires R4's `_standby_eligibility` so the SAME gate
runs (no parallel compliance logic). Fairness reuses the per-crew load (R6.2):
the least-loaded eligible candidate is picked, and one reserve is proposed per
person per day, so nobody is over-selected.
"""
from datetime import date, timedelta

DEFAULT_START_HOUR = 6
DEFAULT_END_HOUR = 18
# Backstop on eligibility evaluations so a pathological spec can't run away.
MAX_EVALUATIONS = 5000


def _month_days(year: int, month: int) -> list:
    d = date(year, month, 1)
    out = []
    while d.month == month:
        out.append(d)
        d += timedelta(days=1)
    return out


def generate_standby_roster_draft(*, year, month, requirements, crew_pool,
                                  base_load, is_eligible,
                                  max_eval=MAX_EVALUATIONS):
    """Greedy, fairness-ordered DRAFT generator. Returns
    {year, month, slots, uncovered, summary}. Saves nothing.

    requirements: [{base, rank, standby_type?, per_day?, start_hour?, end_hour?}]
    crew_pool:    [{id, base, rank, name_ar?, name_en?}]
    base_load:    {crew_id: existing_shift_count}  (from the R6.2 aggregation)
    is_eligible:  (crew_id, start_iso, end_iso) -> (hard_reasons:list, warnings:list)
    """
    draft_load: dict[str, int] = {}
    slots, uncovered = [], []
    evals = 0
    capped = False

    for day in _month_days(year, month):
        iso = day.isoformat()
        day_assigned: set[str] = set()       # one standby per person per day
        for req in requirements:
            base = req.get("base")
            rank = req.get("rank")
            stype = (req.get("standby_type") or "AIRPORT_STANDBY")
            per_day = max(1, int(req.get("per_day") or 1))
            sh = int(req.get("start_hour", DEFAULT_START_HOUR))
            eh = int(req.get("end_hour", DEFAULT_END_HOUR))
            start = f"{iso}T{sh:02d}:00:00+03:00"
            end = f"{iso}T{eh:02d}:00:00+03:00"

            pool = [c for c in crew_pool
                    if c.get("base") == base and c.get("rank") == rank]

            for _slot in range(per_day):
                if not pool:
                    uncovered.append({
                        "date": iso, "base": base, "rank": rank,
                        "standby_type": stype,
                        "reason_category": "no_crew_in_base_rank",
                        "reasons": [],
                    })
                    continue

                ordered = sorted(
                    pool,
                    key=lambda c: (base_load.get(c["id"], 0) + draft_load.get(c["id"], 0),
                                   str(c.get("name_ar") or c.get("name_en") or c["id"])))
                picked = None
                reasons_seen: list = []
                for c in ordered:
                    if c["id"] in day_assigned:
                        continue
                    if evals >= max_eval:
                        capped = True
                        break
                    hard, warns = is_eligible(c["id"], start, end)
                    evals += 1
                    if hard:
                        for r in hard:
                            if r not in reasons_seen:
                                reasons_seen.append(r)
                        continue
                    picked = (c, warns)
                    break

                if picked:
                    c, warns = picked
                    draft_load[c["id"]] = draft_load.get(c["id"], 0) + 1
                    day_assigned.add(c["id"])
                    slots.append({
                        "date": iso, "base": base, "rank": rank,
                        "standby_type": stype,
                        "crew_id": c["id"],
                        "crew_name_ar": c.get("name_ar", ""),
                        "crew_name_en": c.get("name_en", ""),
                        "reason": "least-loaded eligible candidate",
                        "fairness_load": base_load.get(c["id"], 0) + draft_load[c["id"]],
                        "warnings": warns,
                        "status": "DRAFT",
                    })
                else:
                    uncovered.append({
                        "date": iso, "base": base, "rank": rank,
                        "standby_type": stype,
                        "reason_category": ("evaluation_cap_reached" if capped
                                            else "no_eligible_candidate"),
                        "reasons": reasons_seen,
                    })

    return {
        "year": year, "month": month,
        "slots": slots, "uncovered": uncovered,
        "summary": {
            "slots_filled": len(slots),
            "uncovered": len(uncovered),
            "evaluations": evals,
            "evaluation_capped": capped,
        },
    }
