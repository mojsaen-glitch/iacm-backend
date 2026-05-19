"""Safety Management System (SMS) — ICAO Annex 19 compliance.

Three-tier workflow:
    1. Any logged-in user files a safety_report (low friction)
    2. compliance_officer / ops_manager reviews, adds risk_assessment + actions
    3. admin / super_admin closes it

Crew users see only their own reports unless they're compliance/ops.
Anonymous reports honour `is_anonymous=true` by stripping reporter_id
from API responses for non-safety viewers.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/safety", tags=["Safety"])
log = logging.getLogger(__name__)

# Safety officer / management can read all reports + close
_SAFETY_ROLES = {"super_admin", "admin", "ops_manager", "compliance_officer"}
# Anyone with a login can submit a report (Annex 19: just-culture)
_VALID_TYPES   = {"incident", "hazard", "occurrence", "observation"}
_VALID_STATUS  = {"open", "under_review", "closed", "rejected"}
_VALID_SEVERITY = {"minor", "major", "critical"}


def _is_safety_role(user: dict) -> bool:
    return user.get("role") in _SAFETY_ROLES


def _strip_reporter_for_anon(rows, viewer):
    """If a row is `is_anonymous=true` AND the viewer isn't safety/admin,
    blank out `reporter_id` so the identity can't be inferred."""
    if _is_safety_role(viewer):
        return rows
    out = []
    for r in rows:
        if r.get("is_anonymous"):
            r = {**r, "reporter_id": None}
        out.append(r)
    return out


# ──────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────

@router.get("/reports")
async def list_reports(current_user: CurrentUser, sb: SbClient,
                        status: str | None = Query(None),
                        report_type: str | None = Query(None),
                        mine: bool = Query(False, description="Only my own submissions")):
    company_id = current_user["company_id"]
    q = sb.table("safety_reports") \
        .select("*, reporter:reporter_id(name_ar,name_en,role)") \
        .eq("company_id", company_id)
    if status:      q = q.eq("status", status)
    if report_type: q = q.eq("report_type", report_type)

    # Non-safety users see only their own (unless they explicitly want
    # to filter to 'mine' which is fine for everyone).
    if not _is_safety_role(current_user) or mine:
        q = q.eq("reporter_id", current_user["id"])

    res = q.order("submitted_at", desc=True).execute()
    rows = res.data or []
    return _strip_reporter_for_anon(rows, current_user)


@router.post("/reports", status_code=201)
async def file_report(data: dict, current_user: CurrentUser, sb: SbClient):
    """Anyone with a login can file a safety report. Low-friction by
    design — only title + type are required."""
    report_type = (data.get("report_type") or "occurrence").lower()
    if report_type not in _VALID_TYPES:
        raise HTTPException(status_code=422,
            detail=f"report_type must be one of {sorted(_VALID_TYPES)}")

    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")

    severity = data.get("severity")
    if severity and severity not in _VALID_SEVERITY:
        raise HTTPException(status_code=422,
            detail=f"severity must be one of {sorted(_VALID_SEVERITY)}")

    payload = {
        "id":               str(uuid.uuid4()),
        "company_id":       current_user["company_id"],
        "reporter_id":      current_user["id"],
        "is_anonymous":     bool(data.get("is_anonymous", False)),
        "report_type":      report_type,
        "title":            title,
        "description":      data.get("description"),
        "location":         data.get("location"),
        "occurred_at":      data.get("occurred_at"),
        "flight_id":        data.get("flight_id"),
        "aircraft_id":      data.get("aircraft_id"),
        "severity":         severity,
        "immediate_action": data.get("immediate_action"),
        "status":           "open",
        "submitted_at":     datetime.now(timezone.utc).isoformat(),
    }
    res = sb.table("safety_reports").insert(payload).execute()

    # Notify every safety officer + ops manager so they see new reports
    targets = sb.table("users").select("id") \
        .eq("company_id", current_user["company_id"]) \
        .in_("role", list(_SAFETY_ROLES)).execute()
    now = datetime.now(timezone.utc).isoformat()
    sev_label = severity or "غير محدد"
    notif_rows = [{
        "id":             str(uuid.uuid4()),
        "user_id":        u["id"],
        "type":           "safety_report",
        "title_ar":       f"تقرير سلامة جديد — {report_type}",
        "title_en":       f"New safety report — {report_type}",
        "message_ar":     f"{title} (خطورة: {sev_label})",
        "message_en":     f"{title} (severity: {sev_label})",
        "reference_id":   payload["id"],
        "reference_type": "safety_report",
        "is_read":        False,
        "created_at":     now,
    } for u in (targets.data or []) if u["id"] != current_user["id"]]
    if notif_rows:
        sb.table("notifications").insert(notif_rows).execute()

    return res.data[0] if res.data else payload


@router.get("/reports/{report_id}")
async def get_report(report_id: str, current_user: CurrentUser, sb: SbClient):
    res = sb.table("safety_reports") \
        .select("*, reporter:reporter_id(name_ar,name_en,role)") \
        .eq("id", report_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("Safety report", report_id)
    row = res.data[0]
    # Non-safety can only fetch their own
    if not _is_safety_role(current_user) and row.get("reporter_id") != current_user["id"]:
        raise ForbiddenError("Not your report")
    return _strip_reporter_for_anon([row], current_user)[0]


@router.patch("/reports/{report_id}")
async def update_report(report_id: str, data: dict,
                         current_user: CurrentUser, sb: SbClient):
    """Safety officer / admin updates status + severity + closure notes."""
    if not _is_safety_role(current_user):
        raise ForbiddenError("Only safety / management can edit reports")

    existing = sb.table("safety_reports").select("*") \
        .eq("id", report_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Safety report", report_id)

    update = {}
    if "status" in data:
        s = (data["status"] or "").lower()
        if s not in _VALID_STATUS:
            raise HTTPException(status_code=422, detail="invalid status")
        update["status"] = s
        now = datetime.now(timezone.utc).isoformat()
        if s == "under_review":
            update["reviewed_by"] = current_user["id"]
            update["reviewed_at"] = now
        elif s in {"closed", "rejected"}:
            update["closed_by"]     = current_user["id"]
            update["closed_at"]     = now
            update["closure_notes"] = data.get("closure_notes")

    if "severity" in data:
        if data["severity"] and data["severity"] not in _VALID_SEVERITY:
            raise HTTPException(status_code=422, detail="invalid severity")
        update["severity"] = data["severity"]

    if not update:
        return existing.data[0]

    res = sb.table("safety_reports").update(update).eq("id", report_id).execute()
    return res.data[0] if res.data else existing.data[0]


# ──────────────────────────────────────────────────────────────────────
# Risk assessments
# ──────────────────────────────────────────────────────────────────────

_LIKELIHOODS = set("ABCDE")
_SEVERITIES_5 = set("12345")

@router.post("/reports/{report_id}/risk", status_code=201)
async def add_risk_assessment(report_id: str, data: dict,
                                current_user: CurrentUser, sb: SbClient):
    if not _is_safety_role(current_user):
        raise ForbiddenError("Only safety can record risk assessments")

    likelihood = (data.get("likelihood") or "").upper()
    severity   = str(data.get("severity") or "")
    if likelihood not in _LIKELIHOODS:
        raise HTTPException(status_code=422, detail="likelihood must be A..E")
    if severity not in _SEVERITIES_5:
        raise HTTPException(status_code=422, detail="severity must be 1..5")

    # Verify report scope
    rep = sb.table("safety_reports").select("id").eq("id", report_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not rep.data:
        raise NotFoundError("Safety report", report_id)

    payload = {
        "id":          str(uuid.uuid4()),
        "report_id":   report_id,
        "likelihood":  likelihood,
        "severity":    severity,
        "risk_score":  f"{likelihood}{severity}",   # e.g. 'B2'
        "rationale":   data.get("rationale"),
        "assessed_by": current_user["id"],
        "assessed_at": datetime.now(timezone.utc).isoformat(),
    }
    res = sb.table("risk_assessments").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.get("/reports/{report_id}/risk")
async def list_risk_assessments(report_id: str,
                                  current_user: CurrentUser, sb: SbClient):
    # Reader can see if they can see the report
    rep = sb.table("safety_reports").select("reporter_id") \
        .eq("id", report_id).eq("company_id", current_user["company_id"]).execute()
    if not rep.data:
        raise NotFoundError("Safety report", report_id)
    if not _is_safety_role(current_user) \
        and rep.data[0].get("reporter_id") != current_user["id"]:
        raise ForbiddenError("Not your report")

    res = sb.table("risk_assessments").select("*") \
        .eq("report_id", report_id).order("assessed_at", desc=True).execute()
    return res.data or []


# ──────────────────────────────────────────────────────────────────────
# Safety actions
# ──────────────────────────────────────────────────────────────────────

@router.get("/reports/{report_id}/actions")
async def list_actions(report_id: str, current_user: CurrentUser, sb: SbClient):
    rep = sb.table("safety_reports").select("reporter_id") \
        .eq("id", report_id).eq("company_id", current_user["company_id"]).execute()
    if not rep.data:
        raise NotFoundError("Safety report", report_id)
    if not _is_safety_role(current_user) \
        and rep.data[0].get("reporter_id") != current_user["id"]:
        raise ForbiddenError("Not your report")

    res = sb.table("safety_actions").select(
        "*, assignee:assigned_to(name_ar,name_en)") \
        .eq("report_id", report_id) \
        .order("due_date", desc=False).execute()
    return res.data or []


@router.post("/reports/{report_id}/actions", status_code=201)
async def create_action(report_id: str, data: dict,
                         current_user: CurrentUser, sb: SbClient):
    if not _is_safety_role(current_user):
        raise ForbiddenError("Only safety can create actions")

    desc = (data.get("description") or "").strip()
    if not desc:
        raise HTTPException(status_code=422, detail="description is required")

    action_type = (data.get("action_type") or "corrective").lower()
    if action_type not in {"corrective", "preventive", "mitigating"}:
        raise HTTPException(status_code=422,
            detail="action_type must be corrective | preventive | mitigating")

    payload = {
        "id":          str(uuid.uuid4()),
        "report_id":   report_id,
        "action_type": action_type,
        "description": desc,
        "assigned_to": data.get("assigned_to"),
        "due_date":    data.get("due_date"),
        "status":      "open",
        "created_by":  current_user["id"],
    }
    res = sb.table("safety_actions").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.patch("/actions/{action_id}")
async def update_action(action_id: str, data: dict,
                         current_user: CurrentUser, sb: SbClient):
    if not _is_safety_role(current_user):
        raise ForbiddenError("Only safety can update actions")

    update = {k: v for k, v in data.items()
              if k in {"description", "assigned_to", "due_date", "status", "notes", "action_type"}}
    if update.get("status") == "done":
        update["completed_at"] = datetime.now(timezone.utc).isoformat()
        update["completed_by"] = current_user["id"]

    res = sb.table("safety_actions").update(update).eq("id", action_id).execute()
    if not res.data:
        raise NotFoundError("Safety action", action_id)
    return res.data[0]


# ──────────────────────────────────────────────────────────────────────
# Stats — for the safety dashboard widget
# ──────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def safety_stats(current_user: CurrentUser, sb: SbClient):
    if not _is_safety_role(current_user):
        raise ForbiddenError("Only safety can see stats")
    company_id = current_user["company_id"]

    total = sb.table("safety_reports").select("id", count="exact") \
        .eq("company_id", company_id).execute()
    by_status = {}
    for s in _VALID_STATUS:
        r = sb.table("safety_reports").select("id", count="exact") \
            .eq("company_id", company_id).eq("status", s).execute()
        by_status[s] = r.count or 0
    by_type = {}
    for t in _VALID_TYPES:
        r = sb.table("safety_reports").select("id", count="exact") \
            .eq("company_id", company_id).eq("report_type", t).execute()
        by_type[t] = r.count or 0

    return {
        "total":     total.count or 0,
        "by_status": by_status,
        "by_type":   by_type,
    }
