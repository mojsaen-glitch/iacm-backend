"""AI-assisted crew scheduling.

A new "smart engine" that delegates the *recommendation* step to an external
LLM, while keeping a human-in-the-loop: the model proposes crew↔flight
assignments with a fairness/efficiency score and an Arabic reason, and the
scheduler reviews + applies them on the page.

Security: the API key NEVER leaves the server. It is read from the
`AI_API_KEY` environment variable (set in backend/.env locally and in the
Vercel project env). The Flutter client only ever talks to this endpoint.

Provider-agnostic:
  • AI_PROVIDER = "openai" (default) | "anthropic"
  • AI_API_KEY  = secret key (required to enable the feature)
  • AI_MODEL    = model id   (sensible default per provider)
  • AI_BASE_URL = override for OpenAI-compatible gateways (OpenRouter, Groq,
                  Azure, local). Ignored for the anthropic provider.

If no key is configured the endpoint returns 503 with a clear Arabic message
so the UI can tell the operator to configure it.
"""

import json
import logging
import os
import re
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError

router = APIRouter(prefix="/ai-scheduling", tags=["AI Scheduling"])
log = logging.getLogger(__name__)

# Who may run the AI engine — same population that may assign crew.
_ALLOWED_ROLES = {
    "super_admin", "admin", "ops_manager", "scheduler", "scheduler_admin",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}

# Keep payloads (and cost) bounded — cap how much context we forward.
_MAX_CREW = 80
_MAX_FLIGHTS = 50

_SYSTEM_PROMPT = (
    "You are an expert airline crew-scheduling engine for Iraqi Airways. "
    "Assign crew members to flights using a strict, tiered algorithm:\n"
    "1) COMPLIANCE GATE (hard) — exclude a crew member ONLY for a fact that is "
    "EXPLICITLY present in the data:\n"
    "   • has_alert == true (operational block), OR\n"
    "   • a time conflict / insufficient rest with another assignment in this "
    "same batch, OR\n"
    "   • aircraft mismatch ONLY when the crew 'qualifications' list is NON-EMPTY "
    "and does not include the flight's aircraft_type.\n"
    "   IMPORTANT: Missing or empty fields mean 'unknown' and MUST NOT "
    "disqualify anyone. Do NOT invent expired licences, wrong ranks, or rest "
    "violations that are not in the data. 'base' is a PREFERENCE, never a hard "
    "gate.\n"
    "2) FAIRNESS: among eligible crew, prefer the LEAST-loaded (fewest "
    "monthly_hours).\n"
    "3) TIE-BREAK: base matching the flight origin, then aircraft-qualified.\n"
    "You MUST assign exactly ONE crew member to every flight UNLESS every single "
    "crew member is excluded by rule 1 — in that case only, leave crew_id null "
    "and explain which rule excluded them. Never leave a flight unassigned "
    "merely because data is incomplete.\n"
    "Return ONLY valid minified JSON, no markdown, with this exact shape:\n"
    '{"decisions":[{"flight_id":"..","crew_id":".. or null","score":0-100,'
    '"reason_ar":"سبب موجز بالعربية"}],"summary_ar":"ملخص موجز"}'
)


def _build_user_prompt(flights: list, crew: list, ftl: dict) -> str:
    return json.dumps(
        {
            "ftl_limits": ftl,
            "flights": flights[:_MAX_FLIGHTS],
            "crew": crew[:_MAX_CREW],
            "instruction": (
                "Produce the best compliant, fair assignment for every flight."
            ),
        },
        ensure_ascii=False,
    )


async def _call_openai(system: str, user: str, api_key: str) -> str:
    base = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("AI_MODEL", "gpt-4o-mini")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502,
                            detail=f"AI provider error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def _call_anthropic(system: str, user: str, api_key: str) -> str:
    model = os.getenv("AI_MODEL", "claude-3-5-sonnet-latest")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502,
                            detail=f"AI provider error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return "".join(block.get("text", "") for block in data.get("content", []))


def _parse_decisions(raw: str) -> dict:
    """LLMs sometimes wrap JSON in prose/markdown — extract the object."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise HTTPException(status_code=502, detail="تعذّر تحليل رد الذكاء الاصطناعي")
        try:
            return json.loads(m.group(0))
        except Exception:
            raise HTTPException(status_code=502, detail="رد الذكاء الاصطناعي ليس JSON صالحاً")


@router.post("/suggest")
async def ai_suggest(data: dict, current_user: CurrentUser, sb: SbClient):
    if current_user.get("role") not in _ALLOWED_ROLES and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بتشغيل محرّك الجدولة الذكي")

    api_key = os.getenv("AI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="لم يُهيّأ مفتاح الذكاء الاصطناعي (AI_API_KEY) على الخادم",
        )

    flights = data.get("flights") or []
    crew = data.get("crew") or []
    ftl = data.get("ftl") or {}
    if not flights or not crew:
        raise HTTPException(status_code=422, detail="يجب إرسال قائمة الرحلات والطاقم")

    valid_flight_ids = {str(f.get("id")) for f in flights}
    valid_crew_ids = {str(c.get("id")) for c in crew}

    system = _SYSTEM_PROMPT
    user = _build_user_prompt(flights, crew, ftl)
    provider = os.getenv("AI_PROVIDER", "openai").lower()

    try:
        if provider == "anthropic":
            raw = await _call_anthropic(system, user, api_key)
        else:
            raw = await _call_openai(system, user, api_key)
    except HTTPException:
        raise
    except Exception as e:  # network/timeout
        log.exception("AI scheduling call failed")
        raise HTTPException(status_code=502, detail=f"تعذّر الاتصال بالذكاء الاصطناعي: {str(e)[:160]}")

    parsed = _parse_decisions(raw)

    # Validate: drop any decision referencing unknown ids, so a hallucinated
    # crew/flight id can never reach the assignment step.
    clean = []
    for d in (parsed.get("decisions") or []):
        fid = str(d.get("flight_id"))
        cid = d.get("crew_id")
        cid = str(cid) if cid not in (None, "", "null") else None
        if fid not in valid_flight_ids:
            continue
        if cid is not None and cid not in valid_crew_ids:
            cid = None
        score = d.get("score")
        try:
            score = max(0, min(100, int(score)))
        except Exception:
            score = None
        clean.append({
            "flight_id": fid,
            "crew_id": cid,
            "score": score,
            "reason_ar": str(d.get("reason_ar") or ""),
        })

    return {
        "provider": provider,
        "model": os.getenv("AI_MODEL", ""),
        "summary_ar": str(parsed.get("summary_ar") or ""),
        "decisions": clean,
    }
