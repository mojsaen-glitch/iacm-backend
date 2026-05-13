"""
Crew Incompatibility (Do-Not-Pair) Endpoints
=============================================
Allows crew members to request not being paired with a specific colleague.
Admins/OpsManagers review and approve/reject these requests.

When approved, the compliance engine will flag an assignment
if it would pair these two crew members on the same flight.

Table: crew_incompatibility
  id            VARCHAR(36) PK
  requestor_id  VARCHAR(36) → crew.id  (who submitted)
  target_id     VARCHAR(36) → crew.id  (who to avoid)
  reason        TEXT
  status        VARCHAR(20): pending | approved | rejected
  reviewed_by   VARCHAR(36) → users.id
  reviewed_at   TIMESTAMPTZ
  company_id    VARCHAR(36)
  created_at    TIMESTAMPTZ
  updated_at    TIMESTAMPTZ
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ForbiddenError

router = APIRouter(prefix="/incompatibility", tags=["Crew Incompatibility"])


# ─────────────────────────────────────────────────────────────
# GET /incompatibility  — list requests (admin) or own (crew)
# ─────────────────────────────────────────────────────────────
@router.get("")
async def list_requests(
    current_user: CurrentUser,
    sb: SbClient,
    status: Optional[str] = Query(None),
):
    role = current_user.get("role", "")
    is_manager = role in ("super_admin", "admin", "ops_manager")

    if is_manager:
        # Admin sees all company requests
        q = sb.table("crew_incompatibility") \
              .select("*") \
              .eq("company_id", current_user["company_id"])
        if status:
            q = q.eq("status", status)
        result = q.order("created_at", desc=True).execute()
    else:
        # Crew sees only their own requests
        crew_id = current_user.get("crew_id")
        if not crew_id:
            return []
        result = sb.table("crew_incompatibility") \
                   .select("*") \
                   .eq("requestor_id", crew_id) \
                   .order("created_at", desc=True).execute()

    rows = result.data or []

    # Enrich with crew names
    all_crew_ids = set()
    for r in rows:
        all_crew_ids.add(r.get("requestor_id"))
        all_crew_ids.add(r.get("target_id"))
    all_crew_ids.discard(None)

    crew_map = {}
    if all_crew_ids:
        crew_res = sb.table("crew") \
            .select("id,full_name_ar,full_name_en,rank,employee_id") \
            .in_("id", list(all_crew_ids)).execute()
        crew_map = {c["id"]: c for c in (crew_res.data or [])}

    for r in rows:
        r["requestor"] = crew_map.get(r.get("requestor_id"), {})
        r["target"]    = crew_map.get(r.get("target_id"), {})

    return rows


# ─────────────────────────────────────────────────────────────
# POST /incompatibility  — crew submits a DNP request
# ─────────────────────────────────────────────────────────────
@router.post("", status_code=201)
async def create_request(data: dict, current_user: CurrentUser, sb: SbClient):
    """
    Crew member submits a Do-Not-Pair request.
    Body: { target_crew_id, reason }
    """
    # Get requester's crew_id
    requestor_crew_id = current_user.get("crew_id")

    # Admins can submit on behalf of crew (provide requestor_crew_id)
    role = current_user.get("role", "")
    if role in ("admin", "super_admin", "ops_manager"):
        requestor_crew_id = data.get("requestor_crew_id", requestor_crew_id)

    if not requestor_crew_id:
        raise ForbiddenError("هذا الحساب غير مرتبط بسجل طاقم")

    target_crew_id = data.get("target_crew_id") or data.get("target_id")
    if not target_crew_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="target_crew_id مطلوب")

    if requestor_crew_id == target_crew_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="لا يمكنك تقديم طلب ضد نفسك")

    # Check for existing active request
    existing = sb.table("crew_incompatibility") \
        .select("id") \
        .eq("requestor_id", requestor_crew_id) \
        .eq("target_id", target_crew_id) \
        .in_("status", ["pending", "approved"]) \
        .execute()
    if existing.data:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="طلب مشابه موجود بالفعل")

    record = {
        "id":           str(uuid.uuid4()),
        "requestor_id": requestor_crew_id,
        "target_id":    target_crew_id,
        "reason":       data.get("reason", ""),
        "status":       "pending",
        "company_id":   current_user["company_id"],
        "created_at":   datetime.now(timezone.utc).isoformat(),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }

    result = sb.table("crew_incompatibility").insert(record).execute()

    # Notify admin/ops_manager
    try:
        managers = sb.table("users").select("id,role") \
            .eq("company_id", current_user["company_id"]) \
            .eq("is_active", True).execute()

        # Get crew names
        crew_info = sb.table("crew").select("full_name_ar").eq("id", requestor_crew_id).execute()
        name_ar   = crew_info.data[0]["full_name_ar"] if crew_info.data else "عضو طاقم"

        notifs = []
        for u in (managers.data or []):
            if u["role"] in ("admin", "super_admin", "ops_manager"):
                notifs.append({
                    "id":           str(uuid.uuid4()),
                    "user_id":      u["id"],
                    "type":         "dnp_request",
                    "title_ar":     "طلب عدم تطيير جديد",
                    "title_en":     "New Do-Not-Pair Request",
                    "message_ar":   f"{name_ar} يطلب عدم التطيير مع زميل",
                    "message_en":   f"{name_ar} submitted a Do-Not-Pair request",
                    "reference_id": record["id"],
                    "reference_type": "dnp",
                    "is_read":      False,
                    "created_at":   datetime.now(timezone.utc).isoformat(),
                })
        if notifs:
            sb.table("notifications").insert(notifs).execute()
    except Exception:
        pass

    return result.data[0] if result.data else record


# ─────────────────────────────────────────────────────────────
# PATCH /incompatibility/{id}/review  — admin approves/rejects
# ─────────────────────────────────────────────────────────────
@router.patch("/{request_id}/review")
async def review_request(request_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """
    Admin/OpsManager approves or rejects a DNP request.
    Body: { action: "approve" | "reject", notes: "..." }
    """
    role = current_user.get("role", "")
    if role not in ("super_admin", "admin", "ops_manager"):
        raise ForbiddenError("يتطلب صلاحية مدير")

    existing = sb.table("crew_incompatibility").select("*").eq("id", request_id).execute()
    if not existing.data:
        raise NotFoundError("DNP Request", request_id)

    action = data.get("action", "").lower()
    if action not in ("approve", "reject"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="action يجب أن يكون approve أو reject")

    new_status = "approved" if action == "approve" else "rejected"

    updated = sb.table("crew_incompatibility").update({
        "status":      new_status,
        "reviewed_by": current_user["id"],
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "notes":       data.get("notes", ""),
        "updated_at":  datetime.now(timezone.utc).isoformat(),
    }).eq("id", request_id).execute()

    # Notify requestor
    try:
        req = existing.data[0]
        crew_user = sb.table("users").select("id").eq("crew_id", req["requestor_id"]).execute()
        if crew_user.data:
            result_ar = "تمت الموافقة على" if new_status == "approved" else "تم رفض"
            sb.table("notifications").insert({
                "id":           str(uuid.uuid4()),
                "user_id":      crew_user.data[0]["id"],
                "type":         "dnp_reviewed",
                "title_ar":     f"{result_ar} طلب عدم التطيير",
                "title_en":     f"DNP Request {new_status}",
                "message_ar":   f"{result_ar} طلب عدم التطيير الخاص بك",
                "message_en":   f"Your Do-Not-Pair request has been {new_status}",
                "reference_id": request_id,
                "reference_type": "dnp",
                "is_read":      False,
                "created_at":   datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception:
        pass

    return updated.data[0] if updated.data else {}


# ─────────────────────────────────────────────────────────────
# DELETE /incompatibility/{id}  — cancel own request
# ─────────────────────────────────────────────────────────────
@router.delete("/{request_id}")
async def cancel_request(request_id: str, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("crew_incompatibility").select("*").eq("id", request_id).execute()
    if not existing.data:
        raise NotFoundError("DNP Request", request_id)

    req  = existing.data[0]
    role = current_user.get("role", "")

    # Only requestor or admin can cancel
    crew_id = current_user.get("crew_id")
    if req["requestor_id"] != crew_id and role not in ("super_admin", "admin", "ops_manager"):
        raise ForbiddenError("لا يمكنك إلغاء هذا الطلب")

    sb.table("crew_incompatibility").delete().eq("id", request_id).execute()
    return {"message": "تم إلغاء الطلب", "success": True}


# ─────────────────────────────────────────────────────────────
# Helper used by compliance engine
# ─────────────────────────────────────────────────────────────
def get_approved_dnp_pairs(sb, company_id: str) -> list[tuple]:
    """Returns list of (crew_id_a, crew_id_b) that must not fly together."""
    result = sb.table("crew_incompatibility") \
        .select("requestor_id,target_id") \
        .eq("company_id", company_id) \
        .eq("status", "approved").execute()
    pairs = []
    for r in (result.data or []):
        pairs.append((r["requestor_id"], r["target_id"]))
    return pairs
