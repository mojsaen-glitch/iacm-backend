"""
IACM Compliance Engine
======================
Checks crew compliance against ICAO rules and company policy.

Severity levels:
  INFO     — informational only
  WARNING  — issue exists but not blocking
  CRITICAL — serious issue, requires manager attention
  BLOCKING — crew CANNOT be assigned to any flight

Overall status:
  GREEN   — all checks pass
  YELLOW  — has warnings only
  RED     — has critical issues
  BLOCKED — has any blocking violation
"""

from __future__ import annotations
import json
import logging
import re
from datetime import datetime, date, time, timezone, timedelta
from dataclasses import dataclass, field, replace as dc_replace
from typing import Optional

from app.core.config import settings

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Constants  (can be moved to DB operational_rules table later)
# ──────────────────────────────────────────────────────────────
class Severity:
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"
    BLOCKING = "BLOCKING"


class ComplianceStatus:
    GREEN   = "GREEN"    # All clear — assignable
    YELLOW  = "YELLOW"   # Warnings only — assignable with caution
    RED     = "RED"      # Critical — needs review before assigning
    BLOCKED = "BLOCKED"  # Blocking violations — cannot assign


WARN_DAYS_BEFORE_EXPIRY = 30                              # days before expiry to start warning
MAX_MONTHLY_HOURS       = settings.MAX_MONTHLY_HOURS      # default crew max monthly hours (single source of truth)
WARN_MONTHLY_HOURS      = MAX_MONTHLY_HOURS * 0.83        # warn at ~83% of monthly limit
MAX_28DAY_HOURS         = 100.0                           # ICAO 28-day rolling limit
WARN_28DAY_HOURS        = MAX_28DAY_HOURS * 0.9           # warn at 90% of 28-day limit
MAX_YEARLY_HOURS        = settings.MAX_YEARLY_HOURS       # ICAO yearly absolute limit
WARN_YEARLY_HOURS       = MAX_YEARLY_HOURS * 0.89         # warn at ~89% of yearly limit
MIN_REST_DOMESTIC       = settings.MIN_REST_HOURS         # ICAO minimum rest hours — domestic
MIN_REST_INTERNATIONAL  = settings.MIN_REST_HOURS + 2.0   # international rest = domestic + 2h
REST_WARN_BUFFER        = 2.0                             # warn when rest is within 2h of minimum
# A short ground stop at the same station between two sectors is an intra-duty
# TURNAROUND (sit time), NOT inter-duty rest. The minimum-rest rule must not
# fire across it — legal rest begins only after the LAST sector of the duty.
MAX_TURNAROUND_HOURS    = settings.MAX_TURNAROUND_HOURS   # ground-stop ceiling to still count as one duty

IRAQI_AIRPORTS = {"BGW", "NJF", "BSR", "EBL", "OSM", "ISU", "RUM", "TQD"}

# ── FDP (Flight Duty Period) constants ─────────────────────────
FDP_REPORTING_LEAD_MIN = 60     # report 1h before STD (counts toward FDP)
FDP_POST_FLIGHT_MIN    = 30     # 30 min post-flight duty after last landing
FDP_NIGHT_FROM         = time(2, 0)    # WOCL window start (local)
FDP_NIGHT_TO           = time(4, 59)   # WOCL window end (local)
FDP_NIGHT_MAX_SECTORS  = 4       # night-duty hard sector cap
FDP_WARN_RATIO         = 0.90    # warn at 90% of allowed FDP
BAGHDAD_OFFSET         = timedelta(hours=3)  # local = UTC+3 (band lookup)
# A gap >= this many hours between two flights splits them into separate
# duties (so sectors/FDP are counted per duty, not across a rest).
DUTY_SPLIT_REST_HOURS  = MIN_REST_DOMESTIC

DOCUMENT_LABELS = {
    "passport":               ("جواز السفر",              "Passport"),
    "medical":                ("الشهادة الطبية",           "Medical Certificate"),
    "medical_certificate":    ("الفحص الطبي",              "Medical Certificate"),
    "license":                ("رخصة الطيار",              "Pilot License"),
    "crew_license":           ("رخصة الطاقم",              "Crew License"),
    "cabin_crew_certificate": ("شهادة طاقم الضيافة",        "Cabin Crew Certificate"),
    "crew_id":                ("بطاقة الطاقم",             "Crew ID"),
    "safety":                 ("شهادة السلامة",             "Safety Certificate"),
    "safety_training":        ("تدريب السلامة",            "Safety Training"),
    "emergency":              ("شهادة الطوارئ",             "Emergency Certificate"),
    "emergency_training":     ("تدريب الطوارئ",            "Emergency Training"),
    "first_aid":              ("الإسعافات الأولية",         "First Aid"),
    "crm":                    ("شهادة CRM",                 "CRM Certificate"),
    "dangerous_goods":        ("البضائع الخطرة",            "Dangerous Goods"),
    "visa":                   ("التأشيرة",                  "Visa"),
    "airport_pass":           ("تصريح المطار",             "Airport Pass"),
    "other":                  ("وثيقة أخرى",               "Other document"),
}

TRAINING_LABELS = {
    "safety":          ("تدريب السلامة",             "Safety Training"),
    "emergency":       ("إجراءات الطوارئ",           "Emergency Procedures"),
    "crm":             ("إدارة الموارد CRM",          "Crew Resource Management"),
    "dangerous_goods": ("البضائع الخطرة",            "Dangerous Goods"),
    "first_aid":       ("الإسعافات الأولية",         "First Aid"),
    "security":        ("الأمن والسلامة",             "Security Training"),
    "aircraft_type":   ("تأهيل نوع الطائرة",         "Aircraft Type Rating"),
    "recurrent":       ("التدريب الدوري",             "Recurrent Training"),
    "line_check":      ("فحص الخط",                  "Line Check"),
}


# ──────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────
@dataclass
class ComplianceIssue:
    rule:       str
    severity:   str
    message_ar: str
    message_en: str
    detail:     dict = field(default_factory=dict)
    # OM clause number this issue is governed by, e.g. "OM-C 8.1". Set by the
    # OM binding layer (_apply_om) so every violation traces back to a manual
    # clause. None when no OM rule binds to this check.
    om_ref:     Optional[str] = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == Severity.BLOCKING

    def to_dict(self) -> dict:
        return {
            "rule":       self.rule,
            "severity":   self.severity,
            "message_ar": self.message_ar,
            "message_en": self.message_en,
            "is_blocking": self.is_blocking,
            "detail":     self.detail,
            "om_ref":     self.om_ref,
        }


# ──────────────────────────────────────────────────────────────
# Main Engine
# ──────────────────────────────────────────────────────────────
class ComplianceEngine:

    def __init__(self, sb):
        self.sb = sb

    # ── Public API ─────────────────────────────────────────────

    def check_crew(
        self,
        crew_id: str,
        flight_id:         Optional[str]      = None,
        flight_departure:  Optional[datetime]  = None,
        flight_arrival:    Optional[datetime]  = None,
        is_international:  bool               = False,
        flight_aircraft_type: Optional[str]   = None,
    ) -> dict:
        """
        Full compliance check for a crew member.
        If flight details are provided, also checks:
          - Assignment conflicts (overlapping flights)
          - Rest period between last flight and this one
        """
        issues: list[ComplianceIssue] = []

        # Load crew record
        crew_res = self.sb.table("crew").select("*").eq("id", crew_id).execute()
        if not crew_res.data:
            return {"error": f"Crew member {crew_id} not found", "status": "UNKNOWN"}
        crew = crew_res.data[0]

        # Run all rule groups
        issues += self._check_crew_status(crew)
        issues += self._check_documents(crew_id)
        issues += self._check_training(crew_id)
        # Project the flight being assigned into the FTL totals (current + new).
        _proj = [(flight_departure, flight_arrival)] if (flight_departure and flight_arrival) else None
        issues += self._check_flight_hours(crew_id, crew, projected_segs=_proj)
        issues += self._check_aircraft_qualification(crew, flight_aircraft_type)

        if flight_departure and flight_arrival:
            issues += self._check_conflict(crew_id, flight_id, flight_departure, flight_arrival)
            issues += self._check_rest(crew_id, flight_departure, is_international, flight_id)
            issues += self._check_fdp(crew_id, flight_id, flight_departure, flight_arrival)

        # OM binding layer — let active Operations-Manual clauses govern the
        # hardcoded checks (enable/disable, block-vs-warn, stamp clause number).
        issues = self._apply_om(issues)

        # Aggregate
        status        = self._overall_status(issues)
        blocking      = [i for i in issues if i.severity == Severity.BLOCKING]
        critical      = [i for i in issues if i.severity == Severity.CRITICAL]
        warnings      = [i for i in issues if i.severity == Severity.WARNING]
        info_list     = [i for i in issues if i.severity == Severity.INFO]

        return {
            "crew_id":          crew_id,
            "crew_name_ar":     crew.get("full_name_ar", ""),
            "crew_name_en":     crew.get("full_name_en", ""),
            "employee_id":      crew.get("employee_id", ""),
            "rank":             crew.get("rank", ""),
            "status":           status,
            "issues":           [i.to_dict() for i in issues],
            "blocking_count":   len(blocking),
            "critical_count":   len(critical),
            "warning_count":    len(warnings),
            "info_count":       len(info_list),
            "blocking_reasons": [i.message_ar for i in blocking],
            "checked_at":       datetime.now(timezone.utc).isoformat(),
        }

    # ── Connected duty (multi-sector, one duty) ────────────────
    def check_connected_duty(self, crew_id: str, flight_ids: list[str]) -> dict:
        """Compliance for assigning ONE crew member to a set of flights flown as
        a single connected duty (same-day rotation / turnaround chain).

        The gaps between the duty's sectors are turnarounds (never rest); FDP is
        evaluated over the WHOLE duty; rest is enforced only against the crew's
        OTHER duties. Per-crew checks (status/docs/training/hours/qualification)
        run once. OM governance is applied. Returns the same shape as check_crew
        plus a `duty` summary.
        """
        issues: list[ComplianceIssue] = []
        crew_res = self.sb.table("crew").select("*").eq("id", crew_id).execute()
        if not crew_res.data:
            return {"error": f"Crew member {crew_id} not found", "status": "UNKNOWN"}
        crew = crew_res.data[0]

        rows = self.sb.table("flights").select("*").in_("id", list(flight_ids)).execute().data or []
        segs = []
        for f in rows:
            try:
                dep = datetime.fromisoformat(f["departure_time"].replace("Z", "+00:00"))
                arr = datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            segs.append({
                "id": f["id"], "dep": dep, "arr": arr,
                "origin": (f.get("origin_code") or "").upper(),
                "dest": (f.get("destination_code") or "").upper(),
                "flight_number": f.get("flight_number", ""),
                "aircraft_type": (f.get("aircraft_type") or "").strip(),
                "status": f.get("status"),
            })
        segs.sort(key=lambda s: s["dep"])

        if len(segs) < 2:
            issues.append(ComplianceIssue(
                rule="connected_duty_invalid", severity=Severity.BLOCKING,
                message_ar="الواجب المتصل يتطلب رحلتين فأكثر صالحتين",
                message_en="A connected duty needs at least two valid flights"))

        # ── Validate duty shape: ordered, station-contiguous, short turnaround ──
        # Turnaround ceiling comes from the bound OM clause when present.
        _tp = self._om_params("turnaround")
        max_turn = self._pnum(_tp, "max_turnaround_hours", lo=0, hi=12)
        max_turn = max_turn if max_turn is not None else MAX_TURNAROUND_HOURS
        for prev, nxt in zip(segs, segs[1:]):
            gap_h = (nxt["dep"] - prev["arr"]).total_seconds() / 3600.0
            if nxt["dep"] < prev["arr"]:
                issues.append(ComplianceIssue(
                    rule="connected_duty_overlap", severity=Severity.BLOCKING,
                    message_ar=f"تعارض زمني بين {prev['flight_number']} و{nxt['flight_number']}",
                    message_en=f"Time overlap between {prev['flight_number']} and {nxt['flight_number']}"))
            elif nxt["origin"] != prev["dest"]:
                issues.append(ComplianceIssue(
                    rule="connected_duty_not_contiguous", severity=Severity.BLOCKING,
                    message_ar=f"القطاعات غير متّصلة: وصول {prev['dest']} ثم إقلاع {nxt['origin']}",
                    message_en=f"Sectors not contiguous: arrive {prev['dest']} then depart {nxt['origin']}"))
            elif gap_h > max_turn:
                issues.append(ComplianceIssue(
                    rule="connected_duty_gap_too_long", severity=Severity.BLOCKING,
                    message_ar=f"الفاصل {gap_h:.1f} ساعة يتجاوز حد الدوران ({max_turn:.0f} ساعة) — هذا واجب منفصل",
                    message_en=f"Gap {gap_h:.1f}h exceeds turnaround limit ({max_turn:.0f}h) — that is a separate duty",
                    detail={"gap_hours": round(gap_h, 1), "max": max_turn}))

        # ── Per-crew checks (once) ──
        issues += self._check_crew_status(crew)
        issues += self._check_documents(crew_id)
        issues += self._check_training(crew_id)
        # Project the WHOLE duty's sectors into the FTL totals.
        issues += self._check_flight_hours(
            crew_id, crew, projected_segs=[(s["dep"], s["arr"]) for s in segs])
        for ac in {s["aircraft_type"] for s in segs if s["aircraft_type"]}:
            issues += self._check_aircraft_qualification(crew, ac)

        if segs:
            is_intl = any(s["origin"] not in IRAQI_AIRPORTS or s["dest"] not in IRAQI_AIRPORTS
                          for s in segs)
            # Rest against OTHER duties only — measured at the duty's first
            # departure; intra-duty gaps are turnarounds and are never checked.
            issues += self._check_rest(crew_id, segs[0]["dep"], is_intl, segs[0]["id"])
            # FDP over the entire duty.
            issues += self._evaluate_fdp([(s["dep"], s["arr"]) for s in segs])

        issues = self._apply_om(issues)
        status = self._overall_status(issues)

        total_flight_min = sum((s["arr"] - s["dep"]).total_seconds() / 60.0 for s in segs)
        duty_summary = {
            "sectors": len(segs),
            "first_departure_utc": segs[0]["dep"].isoformat() if segs else None,
            "last_arrival_utc": segs[-1]["arr"].isoformat() if segs else None,
            "total_flight_minutes": int(round(total_flight_min)),
            "stations": [segs[0]["origin"]] + [s["dest"] for s in segs] if segs else [],
            "turnarounds": [
                {"station": prev["dest"],
                 "minutes": int(round((nxt["dep"] - prev["arr"]).total_seconds() / 60.0))}
                for prev, nxt in zip(segs, segs[1:])
            ],
        }
        return {
            "crew_id": crew_id,
            "crew_name_ar": crew.get("full_name_ar", ""),
            "crew_name_en": crew.get("full_name_en", ""),
            "status": status,
            "issues": [i.to_dict() for i in issues],
            "blocking_reasons": [i.message_ar for i in issues if i.severity == Severity.BLOCKING],
            "requires_approval": any((i.detail or {}).get("requires_approval") for i in issues),
            "duty": duty_summary,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Connected duty for MANY crew (batched, no N× full engine) ──────────
    def batch_connected_duty(self, crew_ids: list[str], flight_ids: list[str]) -> list[dict]:
        """Compliance for assigning MANY crew to ONE connected duty, with all the
        shared data PRELOADED in a handful of bulk queries instead of running the
        full engine once per crew (which timed out on multi-crew duties).

        Same rules + same per-crew return shape as `check_connected_duty`. The
        duty SHAPE (overlap/contiguity/turnaround) and the FDP limit are
        duty-level — identical for every crew — so they're computed ONCE and
        copied per crew. Only status/documents/training/flight-hours/
        qualification/rest are per-crew, and they use the preloaded rows.
        """
        # ── Duty flights (ONCE, not per crew) ──
        rows = self.sb.table("flights").select("*").in_("id", list(flight_ids)).execute().data or []
        segs = []
        for f in rows:
            try:
                dep = datetime.fromisoformat(f["departure_time"].replace("Z", "+00:00"))
                arr = datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            segs.append({"id": f["id"], "dep": dep, "arr": arr,
                         "origin": (f.get("origin_code") or "").upper(),
                         "dest": (f.get("destination_code") or "").upper(),
                         "flight_number": f.get("flight_number", ""),
                         "aircraft_type": (f.get("aircraft_type") or "").strip(),
                         "status": f.get("status")})
        segs.sort(key=lambda s: s["dep"])

        # ── Duty-level issues (shape + FDP) — identical for every crew ──
        duty_issues: list[ComplianceIssue] = []
        if len(segs) < 2:
            duty_issues.append(ComplianceIssue(
                rule="connected_duty_invalid", severity=Severity.BLOCKING,
                message_ar="الواجب المتصل يتطلب رحلتين فأكثر صالحتين",
                message_en="A connected duty needs at least two valid flights"))
        _tp = self._om_params("turnaround")
        max_turn = self._pnum(_tp, "max_turnaround_hours", lo=0, hi=12)
        max_turn = max_turn if max_turn is not None else MAX_TURNAROUND_HOURS
        for prev, nxt in zip(segs, segs[1:]):
            gap_h = (nxt["dep"] - prev["arr"]).total_seconds() / 3600.0
            if nxt["dep"] < prev["arr"]:
                duty_issues.append(ComplianceIssue(
                    rule="connected_duty_overlap", severity=Severity.BLOCKING,
                    message_ar=f"تعارض زمني بين {prev['flight_number']} و{nxt['flight_number']}",
                    message_en=f"Time overlap between {prev['flight_number']} and {nxt['flight_number']}"))
            elif nxt["origin"] != prev["dest"]:
                duty_issues.append(ComplianceIssue(
                    rule="connected_duty_not_contiguous", severity=Severity.BLOCKING,
                    message_ar=f"القطاعات غير متّصلة: وصول {prev['dest']} ثم إقلاع {nxt['origin']}",
                    message_en=f"Sectors not contiguous: arrive {prev['dest']} then depart {nxt['origin']}"))
            elif gap_h > max_turn:
                duty_issues.append(ComplianceIssue(
                    rule="connected_duty_gap_too_long", severity=Severity.BLOCKING,
                    message_ar=f"الفاصل {gap_h:.1f} ساعة يتجاوز حد الدوران ({max_turn:.0f} ساعة) — هذا واجب منفصل",
                    message_en=f"Gap {gap_h:.1f}h exceeds turnaround limit ({max_turn:.0f}h) — that is a separate duty",
                    detail={"gap_hours": round(gap_h, 1), "max": max_turn}))

        is_intl = bool(segs) and any(
            s["origin"] not in IRAQI_AIRPORTS or s["dest"] not in IRAQI_AIRPORTS for s in segs)
        if segs:
            duty_issues += self._evaluate_fdp([(s["dep"], s["arr"]) for s in segs])

        total_flight_min = sum((s["arr"] - s["dep"]).total_seconds() / 60.0 for s in segs)
        duty_summary = {
            "sectors": len(segs),
            "first_departure_utc": segs[0]["dep"].isoformat() if segs else None,
            "last_arrival_utc": segs[-1]["arr"].isoformat() if segs else None,
            "total_flight_minutes": int(round(total_flight_min)),
            "stations": [segs[0]["origin"]] + [s["dest"] for s in segs] if segs else [],
            "turnarounds": [
                {"station": prev["dest"],
                 "minutes": int(round((nxt["dep"] - prev["arr"]).total_seconds() / 60.0))}
                for prev, nxt in zip(segs, segs[1:])
            ],
        }

        # ── Bulk preload per-crew data (a handful of queries, never N×) ──
        def _in(table, cols, ids, col="id"):
            out = []
            for i in range(0, len(ids), 500):
                out.extend(self.sb.table(table).select(cols)
                           .in_(col, ids[i:i + 500]).execute().data or [])
            return out

        crew_map = {c["id"]: c for c in _in("crew", "*", crew_ids)}
        docs_by: dict[str, list] = {}
        for d in _in("documents", "*", crew_ids, col="crew_id"):
            docs_by.setdefault(d.get("crew_id"), []).append(d)
        train_by: dict[str, list] = {}
        for t in _in("training_records", "*", crew_ids, col="crew_id"):
            train_by.setdefault(t.get("crew_id"), []).append(t)
        asg_rows = _in("assignments", "crew_id,flight_id", crew_ids, col="crew_id")
        fids = list({a["flight_id"] for a in asg_rows if a.get("flight_id")})
        fmap = {f["id"]: f for f in _in(
            "flights", "id,departure_time,arrival_time,duration_hours,status,destination_code", fids)}
        flights_by_crew: dict[str, list] = {}
        for a in asg_rows:
            f = fmap.get(a.get("flight_id"))
            if f:
                flights_by_crew.setdefault(a["crew_id"], []).append(f)

        first_origin = segs[0]["origin"] if segs else None
        first_id = segs[0]["id"] if segs else None
        first_dep = segs[0]["dep"] if segs else None
        proj = [(s["dep"], s["arr"]) for s in segs]
        ac_types = {s["aircraft_type"] for s in segs if s["aircraft_type"]}

        # ── Per crew: reuse the SAME rule logic with preloaded data ──
        out: list[dict] = []
        for cid in crew_ids:
            crew = crew_map.get(cid)
            if crew is None:
                out.append({"error": f"Crew member {cid} not found", "status": "UNKNOWN",
                            "crew_id": cid, "issues": [], "blocking_reasons": []})
                continue
            cflights = flights_by_crew.get(cid, [])
            # Copy duty-level issues so per-crew _apply_om can't mutate shared ones.
            issues: list[ComplianceIssue] = [dc_replace(i) for i in duty_issues]
            issues += self._check_crew_status(crew)
            issues += self._check_documents(cid, docs=docs_by.get(cid, []))
            issues += self._check_training(cid, records=train_by.get(cid, []))
            issues += self._check_flight_hours(cid, crew, projected_segs=proj, crew_flights=cflights)
            for ac in ac_types:
                issues += self._check_aircraft_qualification(crew, ac)
            if segs:
                issues += self._check_rest(cid, first_dep, is_intl, first_id,
                                           crew_flights=cflights, next_origin=first_origin)
            issues = self._apply_om(issues)
            status = self._overall_status(issues)
            out.append({
                "crew_id": cid,
                "crew_name_ar": crew.get("full_name_ar", ""),
                "crew_name_en": crew.get("full_name_en", ""),
                "status": status,
                "issues": [i.to_dict() for i in issues],
                "blocking_reasons": [i.message_ar for i in issues if i.severity == Severity.BLOCKING],
                "requires_approval": any((i.detail or {}).get("requires_approval") for i in issues),
                "duty": duty_summary,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        return out

    # ── Live legality snapshot (OCC countdown) ─────────────────
    @staticmethod
    def _aware(dt: Optional[datetime]) -> Optional[datetime]:
        """Force a datetime to be UTC-aware (assume UTC for naive values)."""
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    # Fallback daily FDP cap (minutes) when fdp_rules has no matching band —
    # a conservative single-day maximum so the countdown still works.
    DEFAULT_MAX_FDP_MIN = 13 * 60

    def crew_legality(
        self,
        crew_id:         str,
        reference_time:  Optional[datetime] = None,
        window_start:    Optional[datetime] = None,
        window_end:      Optional[datetime] = None,
        flight_aircraft_type: Optional[str] = None,
    ) -> dict:
        """Live legality snapshot for a crew member — drives the OCC "legality
        countdown" card. Read-only: nothing is persisted.

        Computes, relative to ``reference_time`` (default = now, UTC):
          - status               GREEN / YELLOW / RED / BLOCKED
          - remaining FDP / duty / flight-time minutes
          - minimum rest required + next legal report time
          - current duty start + the absolute time the crew stays legal until
          - blocking_reasons[] / warnings[] / fatigue_score

        Reuses the same rule helpers as :meth:`check_crew` (status, documents,
        training, flight-hours, aircraft type-rating) so the verdict is
        consistent. Absolute ISO-8601 (UTC) timestamps are returned so the
        client ticks the countdown locally without re-polling."""
        now = self._aware(reference_time) or datetime.now(timezone.utc)

        crew_res = self.sb.table("crew").select("*").eq("id", crew_id).execute()
        if not crew_res.data:
            return {"error": f"Crew member {crew_id} not found",
                    "status": "UNKNOWN", "crew_id": crew_id}
        crew = crew_res.data[0]

        # ── Absolute (non-temporal) compliance issues — reuse existing rules ─
        issues: list[ComplianceIssue] = []
        issues += self._check_crew_status(crew)
        issues += self._check_documents(crew_id)
        issues += self._check_training(crew_id)
        issues += self._check_flight_hours(crew_id, crew)
        if flight_aircraft_type:
            issues += self._check_aircraft_qualification(crew, flight_aircraft_type)

        # ── Load this crew's flights (FTL accumulation + duty detection) ─────
        today = now.date()
        month_start = today.replace(day=1)
        day28_start = today - timedelta(days=28)
        year_start  = today.replace(month=1, day=1)

        flown_monthly = flown_28 = flown_year = 0.0
        segs: list[tuple[datetime, datetime, bool]] = []  # (dep, arr, is_intl)
        try:
            asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
            ids = [r["flight_id"] for r in asgn if r.get("flight_id")]
            rows = []
            if ids:
                rows = self.sb.table("flights").select(
                    "departure_time,arrival_time,duration_hours,status,origin_code,destination_code"
                ).in_("id", ids).execute().data or []
            for f in rows:
                if f.get("status") == "cancelled":
                    continue
                try:
                    d = self._aware(datetime.fromisoformat(str(f["departure_time"]).replace("Z", "+00:00")))
                    a = self._aware(datetime.fromisoformat(str(f["arrival_time"]).replace("Z", "+00:00")))
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
                dur = float(f.get("duration_hours") or ((a - d).total_seconds() / 3600.0))
                dd = d.date()
                if dd >= month_start: flown_monthly += dur
                if dd >= day28_start: flown_28      += dur
                if dd >= year_start:  flown_year    += dur
                intl = (str(f.get("origin_code", "")).upper()      not in IRAQI_AIRPORTS or
                        str(f.get("destination_code", "")).upper() not in IRAQI_AIRPORTS)
                segs.append((d, a, intl))
        except Exception:
            _log.exception("crew_legality flight lookup failed for %s", crew_id)

        # Optional analysis window — restrict duty detection to overlapping flights.
        ws, we = self._aware(window_start), self._aware(window_end)
        if ws or we:
            lo = ws or datetime.min.replace(tzinfo=timezone.utc)
            hi = we or datetime.max.replace(tzinfo=timezone.utc)
            segs = [s for s in segs if s[1] >= lo and s[0] <= hi]
        segs.sort(key=lambda x: x[0])

        # ── Split flights into duties wherever the gap >= a rest break ───────
        rest_break = timedelta(hours=DUTY_SPLIT_REST_HOURS)
        duties: list[list[tuple[datetime, datetime, bool]]] = []
        if segs:
            cur = [segs[0]]
            for prev, nxt in zip(segs, segs[1:]):
                if (nxt[0] - prev[1]) >= rest_break:
                    duties.append(cur); cur = [nxt]
                else:
                    cur.append(nxt)
            duties.append(cur)

        def duty_window(duty):
            first_dep = duty[0][0]
            last_arr  = duty[-1][1]
            fdp_start = first_dep - timedelta(minutes=FDP_REPORTING_LEAD_MIN)
            fdp_end   = last_arr + timedelta(minutes=FDP_POST_FLIGHT_MIN)
            return first_dep, last_arr, fdp_start, fdp_end

        current_duty = None
        last_completed = None
        for duty in duties:
            _, _, fdp_start, fdp_end = duty_window(duty)
            if fdp_start <= now <= fdp_end:
                current_duty = duty
                break
            if fdp_end < now and (last_completed is None
                                  or duty_window(last_completed)[3] < fdp_end):
                last_completed = duty

        warnings: list[str] = []
        blocking_reasons: list[str] = []

        remaining_fdp_minutes  = int(self.DEFAULT_MAX_FDP_MIN)
        remaining_duty_minutes = int(self.DEFAULT_MAX_FDP_MIN)
        current_duty_start_utc: Optional[str] = None
        legal_until_utc:        Optional[str] = None
        next_legal_report_time_utc: Optional[str] = None
        minimum_rest_required_minutes = int(MIN_REST_DOMESTIC * 60)

        if current_duty is not None:
            # ── On duty now — count down the active FDP ──
            first_dep, last_arr, fdp_start, fdp_end = duty_window(current_duty)
            sectors = len(current_duty)
            is_intl = any(s[2] for s in current_duty)
            local_start = (fdp_start + BAGHDAD_OFFSET).time()
            rule = self._lookup_fdp_rule("acclimated", local_start, sectors)
            max_fdp_min = float(rule["max_fdp_minutes"]) if rule else float(self.DEFAULT_MAX_FDP_MIN)
            elapsed_min = (now - fdp_start).total_seconds() / 60.0
            remaining_fdp_minutes  = int(round(max_fdp_min - elapsed_min))
            remaining_duty_minutes = remaining_fdp_minutes
            current_duty_start_utc = fdp_start.isoformat()
            legal_until_utc        = (fdp_start + timedelta(minutes=max_fdp_min)).isoformat()
            min_rest_after = MIN_REST_INTERNATIONAL if is_intl else MIN_REST_DOMESTIC
            minimum_rest_required_minutes = int(min_rest_after * 60)
            next_legal_report_time_utc = (fdp_end + timedelta(hours=min_rest_after)).isoformat()
            if remaining_fdp_minutes <= 0:
                blocking_reasons.append(
                    "تجاوز فترة العمل الجوي (FDP) — الواجب الحالي تخطّى الحد المسموح")
            elif elapsed_min >= max_fdp_min * FDP_WARN_RATIO:
                warnings.append(f"اقتراب من حد FDP — يتبقّى {remaining_fdp_minutes} دقيقة")
        elif last_completed is not None:
            # ── Resting after the most recent duty ──
            _, _, _, fdp_end = duty_window(last_completed)
            is_intl = any(s[2] for s in last_completed)
            min_rest_after = MIN_REST_INTERNATIONAL if is_intl else MIN_REST_DOMESTIC
            minimum_rest_required_minutes = int(min_rest_after * 60)
            report_ready = fdp_end + timedelta(hours=min_rest_after)
            next_legal_report_time_utc = report_ready.isoformat()
            if report_ready > now:
                rest_left = int((report_ready - now).total_seconds() / 60.0)
                warnings.append(f"الطاقم في فترة راحة — قانوني للتقرير بعد {rest_left} دقيقة")
        else:
            # ── No duty history — fully rested, legal to report now ──
            next_legal_report_time_utc = now.isoformat()

        # ── Remaining flight time (FTL) — tightest of monthly / 28-day / yearly
        max_monthly = float(crew.get("max_monthly_hours") or MAX_MONTHLY_HOURS)
        rem_monthly = max(0.0, max_monthly - flown_monthly)
        rem_28      = max(0.0, MAX_28DAY_HOURS - flown_28)
        rem_year    = max(0.0, MAX_YEARLY_HOURS - flown_year)
        remaining_flight_minutes = int(round(min(rem_monthly, rem_28, rem_year) * 60))

        # ── Fold absolute issues into the verdict ──
        for i in issues:
            if i.severity == Severity.BLOCKING:
                blocking_reasons.append(i.message_ar)
            elif i.severity in (Severity.WARNING, Severity.CRITICAL):
                warnings.append(i.message_ar)

        if remaining_flight_minutes <= 0 and not any(
                "ساعات الطيران" in r or "الحد" in r for r in blocking_reasons):
            blocking_reasons.append("استُنفدت ساعات الطيران المسموح بها")

        if blocking_reasons:
            status = ComplianceStatus.BLOCKED
        elif any(i.severity == Severity.CRITICAL for i in issues):
            status = ComplianceStatus.RED
        elif warnings:
            status = ComplianceStatus.YELLOW
        else:
            status = ComplianceStatus.GREEN

        # ── Fatigue score (0 = fresh, 100 = exhausted) ──
        fdp_ratio = 0.0
        if current_duty is not None and current_duty_start_utc:
            used  = max(0.0, (now - self._aware(
                datetime.fromisoformat(current_duty_start_utc))).total_seconds() / 60.0)
            total = used + max(0, remaining_fdp_minutes)
            fdp_ratio = (used / total) if total > 0 else 0.0
        monthly_ratio = (flown_monthly / max_monthly) if max_monthly else 0.0
        d28_ratio     = flown_28 / MAX_28DAY_HOURS if MAX_28DAY_HOURS else 0.0
        fatigue_score = int(round(min(1.0, max(fdp_ratio, monthly_ratio, d28_ratio)) * 100))

        return {
            "crew_id":                       crew_id,
            "crew_name_ar":                  crew.get("full_name_ar", ""),
            "crew_name_en":                  crew.get("full_name_en", ""),
            "reference_time_utc":            now.isoformat(),
            "status":                        status,
            "on_duty":                       current_duty is not None,
            "remaining_fdp_minutes":         remaining_fdp_minutes,
            "remaining_duty_minutes":        remaining_duty_minutes,
            "remaining_flight_minutes":      remaining_flight_minutes,
            "minimum_rest_required_minutes": minimum_rest_required_minutes,
            "next_legal_report_time_utc":    next_legal_report_time_utc,
            "current_duty_start_utc":        current_duty_start_utc,
            "legal_until_utc":               legal_until_utc,
            "blocking_reasons":              blocking_reasons,
            "warnings":                      warnings,
            "fatigue_score":                 fatigue_score,
            # extra context for the card
            "flown_monthly_hours":           round(flown_monthly, 1),
            "max_monthly_hours":             max_monthly,
            "flown_28day_hours":             round(flown_28, 1),
            "max_28day_hours":               MAX_28DAY_HOURS,
            "flown_yearly_hours":            round(flown_year, 1),
            "max_yearly_hours":              MAX_YEARLY_HOURS,
        }

    # ── FDP Monitor — schedule-linked duty snapshot for one crew ───────────
    def fdp_monitor(self, crew_id: str, on_date: Optional[date] = None,
                    reference_time: Optional[datetime] = None) -> dict:
        """Operational FDP snapshot for the FDP-Monitor page. Pulls the crew's
        flights for the TARGET DAY (their duty), then computes report time,
        sectors, final arrival, FDP used/max/remaining, previous rest and the
        compliance verdict — all in UTC (the UI renders Baghdad = UTC+3).

        Reuses the same FDP table (`_lookup_fdp_rule`), rest minimums and the
        absolute rule checks (status/docs/training/hours) as the scheduler, so
        the verdict matches what assignment enforces."""
        now = self._aware(reference_time) or datetime.now(timezone.utc)

        crew_res = self.sb.table("crew").select("*").eq("id", crew_id).execute()
        if not crew_res.data:
            return {"error": f"Crew member {crew_id} not found",
                    "status": "UNKNOWN", "crew_id": crew_id, "flights": []}
        crew = crew_res.data[0]

        # ── Load this crew's flights ──
        rows = []
        try:
            asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
            ids = [r["flight_id"] for r in asgn if r.get("flight_id")]
            if ids:
                rows = self.sb.table("flights").select(
                    "id,flight_number,departure_time,arrival_time,status,"
                    "origin_code,destination_code,aircraft_type"
                ).in_("id", ids).execute().data or []
        except Exception:
            _log.exception("fdp_monitor flight lookup failed for %s", crew_id)

        warnings: list[str] = []
        reasons: list[str] = []
        missing_times = False
        segs = []  # (dep, arr, intl, row)
        for f in rows:
            if f.get("status") == "cancelled":
                continue
            try:
                d = self._aware(datetime.fromisoformat(str(f["departure_time"]).replace("Z", "+00:00")))
                a = self._aware(datetime.fromisoformat(str(f["arrival_time"]).replace("Z", "+00:00")))
            except (KeyError, ValueError, TypeError, AttributeError):
                missing_times = True
                continue
            intl = (str(f.get("origin_code", "")).upper() not in IRAQI_AIRPORTS or
                    str(f.get("destination_code", "")).upper() not in IRAQI_AIRPORTS)
            segs.append((d, a, intl, f))
        segs.sort(key=lambda x: x[0])

        # ── Split into duties on rest-sized gaps ──
        duties: list[list] = []
        if segs:
            cur = [segs[0]]
            for prev, nxt in zip(segs, segs[1:]):
                if (nxt[0] - prev[1]) >= timedelta(hours=DUTY_SPLIT_REST_HOURS):
                    duties.append(cur); cur = [nxt]
                else:
                    cur.append(nxt)
            duties.append(cur)

        # ── Choose the target duty ──
        # local Baghdad date of a duty = local date of its first departure.
        def _local_date(dt): return (dt + BAGHDAD_OFFSET).date()
        target_idx = None
        if on_date is not None:
            for i, dty in enumerate(duties):
                if _local_date(dty[0][0]) == on_date:
                    target_idx = i; break
        if target_idx is None and duties:
            # duty active now → else next upcoming → else most recent.
            for i, dty in enumerate(duties):
                rep = dty[0][0] - timedelta(minutes=FDP_REPORTING_LEAD_MIN)
                end = dty[-1][1] + timedelta(minutes=FDP_POST_FLIGHT_MIN)
                if rep <= now <= end:
                    target_idx = i; break
            if target_idx is None:
                upcoming = [i for i, dty in enumerate(duties) if dty[0][0] >= now]
                target_idx = upcoming[0] if upcoming else len(duties) - 1

        # ── Absolute (non-temporal) checks — reuse scheduler rules ──
        for iss in (self._check_crew_status(crew) + self._check_documents(crew_id)
                    + self._check_training(crew_id) + self._check_flight_hours(crew_id, crew)):
            (reasons if iss.severity == Severity.BLOCKING else warnings).append(iss.message_ar)

        out = {
            "crew_id": crew_id,
            "crew_name_ar": crew.get("full_name_ar", ""),
            "crew_name_en": crew.get("full_name_en", ""),
            "rank": crew.get("rank", ""),
            "reference_time_utc": now.isoformat(),
            "report_time_utc": None, "final_arrival_utc": None, "duty_end_utc": None,
            "sectors": 0, "on_duty": False,
            "fdp_used_minutes": 0, "fdp_max_minutes": int(self.DEFAULT_MAX_FDP_MIN),
            "fdp_remaining_minutes": int(self.DEFAULT_MAX_FDP_MIN),
            "previous_rest_minutes": None, "previous_rest_ok": None,
            "minimum_rest_minutes": int(MIN_REST_DOMESTIC * 60),
            "flights": [],
        }

        if target_idx is None:
            out["status"] = ComplianceStatus.GREEN if not reasons else ComplianceStatus.BLOCKED
            out["blocking_reasons"] = reasons
            out["warnings"] = warnings + (["لا توجد رحلات مجدولة لهذا الطاقم"] if not segs else [])
            out["note"] = "no_flights"
            return out

        duty = duties[target_idx]
        first_dep = duty[0][0]
        last_arr  = duty[-1][1]
        report    = first_dep - timedelta(minutes=FDP_REPORTING_LEAD_MIN)
        fdp_end   = last_arr + timedelta(minutes=FDP_POST_FLIGHT_MIN)
        sectors   = len(duty)
        is_intl   = any(s[2] for s in duty)

        local_start = (report + BAGHDAD_OFFSET).time()
        rule = self._lookup_fdp_rule("acclimated", local_start, sectors)
        max_fdp_min = float(rule["max_fdp_minutes"]) if rule else float(self.DEFAULT_MAX_FDP_MIN)

        if now < report:
            used_min = 0
        elif now > fdp_end:
            used_min = (fdp_end - report).total_seconds() / 60.0
        else:
            used_min = (now - report).total_seconds() / 60.0
        used_min = int(round(max(0.0, used_min)))
        remaining_min = int(round(max_fdp_min)) - used_min

        # ── Previous rest: gap from the prior duty's end to this report. ──
        prev_rest_min = None
        min_rest = MIN_REST_INTERNATIONAL if is_intl else MIN_REST_DOMESTIC
        if target_idx > 0:
            prev_end = duties[target_idx - 1][-1][1] + timedelta(minutes=FDP_POST_FLIGHT_MIN)
            prev_rest_min = int(round((report - prev_end).total_seconds() / 60.0))
        prev_rest_ok = prev_rest_min is None or prev_rest_min >= int(min_rest * 60)

        # ── Verdict ──
        if remaining_min <= 0:
            reasons.append("تجاوز حد FDP المسموح للواجب")
        elif used_min >= max_fdp_min * FDP_WARN_RATIO:
            warnings.append("الطاقم تجاوز 80% من حد FDP المسموح")
        if prev_rest_min is not None and prev_rest_min < int(min_rest * 60):
            reasons.append(f"الراحة السابقة غير كافية ({prev_rest_min // 60}س — الحد {int(min_rest)}س)")
        if missing_times:
            warnings.append("بعض الرحلات تنقصها أوقات إقلاع/وصول — تحقّق قبل الاعتماد")

        status = (ComplianceStatus.BLOCKED if reasons
                  else ComplianceStatus.YELLOW if warnings
                  else ComplianceStatus.GREEN)

        out.update({
            "report_time_utc": report.isoformat(),
            "final_arrival_utc": last_arr.isoformat(),
            "duty_end_utc": fdp_end.isoformat(),
            "sectors": sectors,
            "on_duty": report <= now <= fdp_end,
            "is_international": is_intl,
            "fdp_used_minutes": used_min,
            "fdp_max_minutes": int(round(max_fdp_min)),
            "fdp_remaining_minutes": remaining_min,
            "previous_rest_minutes": prev_rest_min,
            "previous_rest_ok": prev_rest_ok,
            "minimum_rest_minutes": int(min_rest * 60),
            "status": status,
            "blocking_reasons": reasons,
            "warnings": warnings,
            "flights": [{
                "flight_number": f.get("flight_number", ""),
                "origin": (f.get("origin_code") or "").upper(),
                "destination": (f.get("destination_code") or "").upper(),
                "departure_utc": d.isoformat(),
                "arrival_utc": a.isoformat(),
                "status": f.get("status", ""),
                "aircraft_type": f.get("aircraft_type", ""),
            } for (d, a, _i, f) in duty],
        })
        return out

    def fdp_monitor_today(self, company_id: str, on_date: Optional[date] = None) -> list:
        """Roster-wide FDP board: every crew SCHEDULED on the target Baghdad day,
        each with a compact verdict (sectors, FDP used/remaining, previous rest,
        status). Reuses `fdp_monitor` per crew (manual action, bounded count)."""
        on_date = on_date or (datetime.now(timezone.utc) + BAGHDAD_OFFSET).date()

        # Company flights whose LOCAL (Baghdad) departure date == on_date.
        frows = self.sb.table("flights").select("id,departure_time,company_id") \
            .eq("company_id", company_id).execute().data or []
        today_ids = []
        for f in frows:
            try:
                d = self._aware(datetime.fromisoformat(str(f["departure_time"]).replace("Z", "+00:00")))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            if (d + BAGHDAD_OFFSET).date() == on_date:
                today_ids.append(f["id"])
        if not today_ids:
            return []

        asg = self.sb.table("assignments").select("crew_id,flight_id") \
            .in_("flight_id", today_ids).execute().data or []
        crew_ids = list({a["crew_id"] for a in asg if a.get("crew_id")})

        out = []
        for cid in crew_ids:
            m = self.fdp_monitor(cid, on_date=on_date)
            reasons = m.get("blocking_reasons") or []
            warns = m.get("warnings") or []
            out.append({
                "crew_id": cid,
                "crew_name_ar": m.get("crew_name_ar", ""),
                "crew_name_en": m.get("crew_name_en", ""),
                "rank": m.get("rank", ""),
                "sectors": m.get("sectors", 0),
                "fdp_used_minutes": m.get("fdp_used_minutes", 0),
                "fdp_max_minutes": m.get("fdp_max_minutes", 0),
                "fdp_remaining_minutes": m.get("fdp_remaining_minutes", 0),
                "previous_rest_minutes": m.get("previous_rest_minutes"),
                "previous_rest_ok": m.get("previous_rest_ok"),
                "status": m.get("status", ComplianceStatus.GREEN),
                "reason": reasons[0] if reasons else (warns[0] if warns else ""),
            })
        # Worst first: BLOCKED → RED → YELLOW → GREEN, then by name.
        order = {ComplianceStatus.BLOCKED: 0, ComplianceStatus.RED: 1,
                 ComplianceStatus.YELLOW: 2, ComplianceStatus.GREEN: 3}
        out.sort(key=lambda r: (order.get(r["status"], 4),
                                r.get("crew_name_ar") or r.get("crew_name_en") or ""))
        return out

    # ── Rule: Crew Status ──────────────────────────────────────

    def _check_crew_status(self, crew: dict) -> list[ComplianceIssue]:
        issues = []
        status = crew.get("status", "active")

        if status in ("blocked", "suspended"):
            issues.append(ComplianceIssue(
                rule="crew_status_blocked",
                severity=Severity.BLOCKING,
                message_ar=f"حالة الطاقم '{status}' — ممنوع من الجدولة",
                message_en=f"Crew status is '{status}' — blocked from scheduling",
                detail={"status": status},
            ))
        elif status == "on_leave":
            issues.append(ComplianceIssue(
                rule="crew_on_leave",
                severity=Severity.WARNING,
                message_ar="الطاقم في إجازة — تحقق قبل التكليف",
                message_en="Crew is on leave — verify before assignment",
                detail={"status": status},
            ))
        return issues

    # ── Rule: Documents ────────────────────────────────────────

    def _check_documents(self, crew_id: str, *, docs: Optional[list] = None) -> list[ComplianceIssue]:
        issues = []
        today = date.today()
        # OM overrides: warning window + whether an expired doc hard-blocks.
        dpar = self._om_params("documents")
        warn_days = self._pnum(dpar, "warning_before_days", lo=1, hi=365)
        warn_days = int(warn_days) if warn_days is not None else WARN_DAYS_BEFORE_EXPIRY
        block_docs = self._pbool(dpar, "block_if_expired", True)

        # `docs` may be PRELOADED in bulk by a batch caller (connected-duty) to
        # avoid one query per crew; otherwise fetch this crew's docs.
        if docs is None:
            try:
                docs = self.sb.table("documents").select("*").eq("crew_id", crew_id).execute().data or []
            except Exception as e:
                _log.exception("documents lookup failed for crew_id=%s", crew_id)
                return [ComplianceIssue(
                    rule="check_documents_unavailable",
                    severity=Severity.CRITICAL,
                    message_ar="تعذّر التحقق من الوثائق — راجع المسؤول قبل التكليف",
                    message_en="Could not verify documents — review before assignment",
                    detail={"error": str(e)[:200]},
                )]

        for doc in docs:
            doc_type = doc.get("document_type", "document")
            label_ar, label_en = DOCUMENT_LABELS.get(doc_type, (doc_type, doc_type))
            expiry_str = doc.get("expiry_date")

            # A malformed / missing expiry is NEVER silently ignored — it surfaces
            # as a REVIEW (warning) so a human checks the record before assigning.
            expiry = None
            if expiry_str:
                try:
                    expiry = date.fromisoformat(str(expiry_str)[:10])
                except (ValueError, TypeError):
                    expiry = None
            if expiry is None:
                issues.append(ComplianceIssue(
                    rule=f"doc_invalid_date_{doc_type}",
                    severity=Severity.WARNING,
                    message_ar=f"تاريخ انتهاء غير صالح أو ناقص: {label_ar} — يحتاج مراجعة",
                    message_en=f"Invalid/missing expiry date: {label_en} — needs review",
                    detail={"type": doc_type, "expiry_raw": expiry_str, "review": True},
                ))
                continue

            days_diff = (expiry - today).days
            if expiry < today:
                issues.append(ComplianceIssue(
                    rule=f"doc_expired_{doc_type}",
                    severity=Severity.BLOCKING if block_docs else Severity.WARNING,
                    message_ar=f"وثيقة منتهية الصلاحية: {label_ar} (انتهت {expiry})",
                    message_en=f"Expired document: {label_en} (expired {expiry})",
                    detail={"type": doc_type, "expiry": str(expiry), "days_overdue": abs(days_diff)},
                ))
            elif days_diff <= warn_days:
                issues.append(ComplianceIssue(
                    rule=f"doc_expiring_{doc_type}",
                    severity=Severity.WARNING,
                    message_ar=f"وثيقة على وشك الانتهاء: {label_ar} (تبقى {days_diff} يوم)",
                    message_en=f"Document expiring soon: {label_en} ({days_diff} days left)",
                    detail={"type": doc_type, "expiry": str(expiry), "days_left": days_diff},
                ))

            # Unverified documents are REVIEW (non-blocking for now), never treated
            # as silently valid. Can be escalated to BLOCKING after data cleanup.
            if not doc.get("is_verified", False):
                issues.append(ComplianceIssue(
                    rule=f"doc_unverified_{doc_type}",
                    severity=Severity.WARNING,
                    message_ar=f"وثيقة غير موثّقة: {label_ar} — تحتاج توثيق",
                    message_en=f"Unverified document: {label_en} — needs verification",
                    detail={"type": doc_type, "review": True, "unverified": True},
                ))

        return issues

    # ── Rule: Training Records ─────────────────────────────────

    def _check_training(self, crew_id: str, *, records: Optional[list] = None) -> list[ComplianceIssue]:
        issues = []
        today = date.today()
        # OM override: block_if_expired=false downgrades an expired record to a
        # warning instead of a hard block.
        block_training = self._pbool(self._om_params("training"), "block_if_expired", True)

        # `records` may be PRELOADED in bulk by a batch caller (connected-duty).
        if records is None:
            try:
                records = self.sb.table("training_records").select("*").eq("crew_id", crew_id).execute().data or []
            except Exception as e:
                _log.exception("training lookup failed for crew_id=%s", crew_id)
                return [ComplianceIssue(
                    rule="check_training_unavailable",
                    severity=Severity.CRITICAL,
                    message_ar="تعذّر التحقق من سجلات التدريب — راجع المسؤول قبل التكليف",
                    message_en="Could not verify training records — review before assignment",
                    detail={"error": str(e)[:200]},
                )]

        for rec in records:
            expiry_str = rec.get("expiry_date")
            if not expiry_str:
                continue
            try:
                expiry = date.fromisoformat(str(expiry_str)[:10])
            except (ValueError, TypeError):
                continue

            t_type = rec.get("training_type", "training")
            label_ar, label_en = TRAINING_LABELS.get(t_type, (t_type, t_type))
            days_diff = (expiry - today).days

            if expiry < today:
                issues.append(ComplianceIssue(
                    rule=f"training_expired_{t_type}",
                    severity=Severity.BLOCKING if block_training else Severity.WARNING,
                    message_ar=f"تدريب منتهٍ: {label_ar} (انتهى {expiry})",
                    message_en=f"Expired training: {label_en} (expired {expiry})",
                    detail={"type": t_type, "expiry": str(expiry), "days_overdue": abs(days_diff)},
                ))
            elif days_diff <= WARN_DAYS_BEFORE_EXPIRY:
                issues.append(ComplianceIssue(
                    rule=f"training_expiring_{t_type}",
                    severity=Severity.WARNING,
                    message_ar=f"تدريب على وشك الانتهاء: {label_ar} (تبقى {days_diff} يوم)",
                    message_en=f"Training expiring soon: {label_en} ({days_diff} days left)",
                    detail={"type": t_type, "expiry": str(expiry), "days_left": days_diff},
                ))

        return issues

    # ── Rule: Flight Hours (FTL) ───────────────────────────────

    def _check_flight_hours(self, crew_id: str, crew: dict,
                            projected_segs: Optional[list] = None,
                            *, crew_flights: Optional[list] = None) -> list[ComplianceIssue]:
        """FTL check. Counts the crew's CURRENT assigned hours PLUS the hours of
        the flight(s) being assigned now (`projected_segs`: list of (dep, arr)),
        so the decision is made on the PROJECTED total — e.g. 97h current + 5h
        new = 102h is caught BEFORE the assignment, not after.

        `crew_flights` (rows with departure_time/duration_hours/status) may be
        PRELOADED in bulk by a batch caller to avoid the per-crew queries."""
        issues = []
        today = date.today()
        month_start  = today.replace(day=1)
        day28_start  = today - timedelta(days=28)
        year_start   = today.replace(month=1, day=1)

        if crew_flights is not None:
            flights = crew_flights
        else:
            try:
                # Get all assignment flight_ids for this crew
                asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
                flight_ids = [r["flight_id"] for r in asgn if r.get("flight_id")]
                flights = []
                if flight_ids:
                    flights = self.sb.table("flights") \
                        .select("departure_time,duration_hours,status") \
                        .in_("id", flight_ids) \
                        .execute().data or []
            except Exception as e:
                _log.exception("flight-hours lookup failed for crew_id=%s", crew_id)
                return [ComplianceIssue(
                    rule="check_flight_hours_unavailable",
                    severity=Severity.CRITICAL,
                    message_ar="تعذّر التحقق من ساعات الطيران (FTL) — راجع المسؤول",
                    message_en="Could not verify flight-time limits (FTL) — review before assignment",
                    detail={"error": str(e)[:200]},
                )]

        monthly_h = 0.0
        h28d      = 0.0
        yearly_h  = 0.0

        def _accrue(dep_date: date, duration: float):
            nonlocal monthly_h, h28d, yearly_h
            if dep_date >= month_start:
                monthly_h += duration
            if dep_date >= day28_start:
                h28d += duration
            if dep_date >= year_start:
                yearly_h += duration

        for f in flights:
            if f.get("status") == "cancelled":
                continue
            dep_str = f.get("departure_time", "")
            if not dep_str:
                continue
            try:
                dep_date = date.fromisoformat(dep_str[:10])
            except (ValueError, TypeError):
                continue
            _accrue(dep_date, float(f.get("duration_hours") or 0))

        # ── Project the flight(s) being assigned NOW into the totals. ──
        projected_total = 0.0
        for dep_dt, arr_dt in (projected_segs or []):
            if not dep_dt or not arr_dt:
                continue
            dur = (self._aware(arr_dt) - self._aware(dep_dt)).total_seconds() / 3600.0
            if dur <= 0:
                continue
            projected_total += dur
            _accrue(dep_dt.date(), dur)

        if not flights and not projected_total:
            return issues  # nothing to evaluate

        max_monthly = float(crew.get("max_monthly_hours") or MAX_MONTHLY_HOURS)

        # ── OM parameter overrides (fallback to config / crew when absent or
        # invalid). max_hours / warning_threshold_percent come from the clause
        # bound to flight_hours_{yearly,28day,monthly}. ──
        yp = self._om_params("flight_hours_yearly")
        max_year = self._pnum(yp, "max_hours", lo=1) or MAX_YEARLY_HOURS
        _wy = self._pnum(yp, "warning_threshold_percent", lo=1, hi=100)
        warn_year = max_year * _wy / 100.0 if _wy is not None else WARN_YEARLY_HOURS

        hp = self._om_params("flight_hours_28day")
        max_28 = self._pnum(hp, "max_hours", lo=1) or MAX_28DAY_HOURS
        _w28 = self._pnum(hp, "warning_threshold_percent", lo=1, hi=100)
        warn_28 = max_28 * _w28 / 100.0 if _w28 is not None else WARN_28DAY_HOURS

        mp = self._om_params("flight_hours_monthly")
        _mh = self._pnum(mp, "max_hours", lo=1)
        if _mh is not None:
            max_monthly = _mh
        _wm = self._pnum(mp, "warning_threshold_percent", lo=1, hi=100)
        warn_monthly = max_monthly * _wm / 100.0 if _wm is not None else WARN_MONTHLY_HOURS

        # ─ Yearly check (ICAO absolute) ─
        if yearly_h >= max_year:
            issues.append(ComplianceIssue(
                rule="hours_yearly_exceeded",
                severity=Severity.BLOCKING,
                message_ar=f"تجاوز الحد السنوي ICAO: {yearly_h:.1f} / {max_year:.0f} ساعة",
                message_en=f"ICAO yearly limit exceeded: {yearly_h:.1f} / {max_year:.0f}h",
                detail={"yearly_hours": round(yearly_h, 1), "limit": max_year},
            ))
        elif yearly_h >= warn_year:
            issues.append(ComplianceIssue(
                rule="hours_yearly_warning",
                severity=Severity.WARNING,
                message_ar=f"اقتراب من الحد السنوي: {yearly_h:.1f} / {max_year:.0f} ساعة",
                message_en=f"Approaching yearly limit: {yearly_h:.1f} / {max_year:.0f}h",
                detail={"yearly_hours": round(yearly_h, 1), "limit": max_year},
            ))

        # ─ 28-day rolling check ─
        if h28d >= max_28:
            issues.append(ComplianceIssue(
                rule="hours_28day_exceeded",
                severity=Severity.CRITICAL,
                message_ar=f"تجاوز حد الـ 28 يوم: {h28d:.1f} / {max_28:.0f} ساعة",
                message_en=f"28-day rolling limit exceeded: {h28d:.1f} / {max_28:.0f}h",
                detail={"hours_28d": round(h28d, 1), "limit": max_28},
            ))
        elif h28d >= warn_28:
            issues.append(ComplianceIssue(
                rule="hours_28day_warning",
                severity=Severity.WARNING,
                message_ar=f"اقتراب من حد الـ 28 يوم: {h28d:.1f} / {max_28:.0f} ساعة",
                message_en=f"Approaching 28-day limit: {h28d:.1f} / {max_28:.0f}h",
                detail={"hours_28d": round(h28d, 1), "limit": max_28},
            ))

        # ─ Monthly check ─
        if monthly_h >= max_monthly:
            issues.append(ComplianceIssue(
                rule="hours_monthly_exceeded",
                severity=Severity.BLOCKING,
                message_ar=f"تجاوز الحد الشهري: {monthly_h:.1f} / {max_monthly:.0f} ساعة",
                message_en=f"Monthly limit exceeded: {monthly_h:.1f} / {max_monthly:.0f}h",
                detail={"monthly_hours": round(monthly_h, 1), "limit": max_monthly},
            ))
        elif monthly_h >= warn_monthly:
            issues.append(ComplianceIssue(
                rule="hours_monthly_warning",
                severity=Severity.WARNING,
                message_ar=f"اقتراب من الحد الشهري: {monthly_h:.1f} / {max_monthly:.0f} ساعة",
                message_en=f"Approaching monthly limit: {monthly_h:.1f} / {max_monthly:.0f}h",
                detail={"monthly_hours": round(monthly_h, 1), "limit": max_monthly},
            ))

        return issues

    # ── Rule: Aircraft Type Rating ─────────────────────────────

    @staticmethod
    def _norm_aircraft_types(value) -> set[str]:
        """Normalise an aircraft-type value (list, JSON array string, or a
        delimited string like 'B738,737') into a set of comparable codes —
        both the full token (B738) and its digit form (738) — so 'B738', '738'
        and '73M' families can be matched tolerantly."""
        tokens: list[str] = []
        if isinstance(value, list):
            tokens = [str(x) for x in value]
        elif isinstance(value, str):
            s = value.strip()
            if s.startswith("["):
                try:
                    tokens = [str(x) for x in json.loads(s)]
                except Exception:
                    tokens = re.split(r"[,/;\s]+", s)
            elif s:
                tokens = re.split(r"[,/;\s]+", s)
        out: set[str] = set()
        for t in tokens:
            t = t.strip().upper()
            if not t:
                continue
            out.add(t)
            digits = re.sub(r"\D", "", t)
            if digits:
                out.add(digits)
        return out

    def _check_aircraft_qualification(
        self, crew: dict, flight_aircraft_type: Optional[str],
    ) -> list[ComplianceIssue]:
        """Block crew who aren't type-rated for the flight's aircraft. Degrades
        gracefully: if the flight's type is unknown, or the crew has no
        recorded qualifications, no issue is raised (avoids false blocks on
        incomplete data)."""
        flight_set = self._norm_aircraft_types(flight_aircraft_type)
        if not flight_set:
            return []
        crew_set = self._norm_aircraft_types(crew.get("aircraft_qualifications"))
        crew_set |= self._norm_aircraft_types(crew.get("aircraft_type"))
        if not crew_set:
            return []  # unknown qualifications — don't block
        if flight_set & crew_set:
            return []  # qualified
        return [ComplianceIssue(
            rule="aircraft_not_type_rated",
            severity=Severity.BLOCKING,
            message_ar=f"غير مؤهّل لنوع الطائرة {flight_aircraft_type} (لا يملك التأهيل)",
            message_en=f"Not type-rated for aircraft {flight_aircraft_type}",
            detail={"flight_type": flight_aircraft_type,
                    "crew_qualified": sorted(crew_set)},
        )]

    # ── Fail-closed safety errors ──────────────────────────────
    @staticmethod
    def _engine_error(check: str, exc: Exception) -> ComplianceIssue:
        """Fail-CLOSED guard: when a SAFETY check itself errors (DB/parse/etc.),
        the assignment is BLOCKED, never allowed. A technical failure must never
        be read as "crew is legal". Used by rest / conflict / FDP checks."""
        return ComplianceIssue(
            rule="compliance_engine_error",
            severity=Severity.BLOCKING,
            message_ar="تعذّر التحقق من سلامة الطاقم بأمان — مُنع التكليف مؤقتاً (COMPLIANCE_ENGINE_ERROR)",
            message_en="Unable to verify crew legality safely — assignment blocked (COMPLIANCE_ENGINE_ERROR)",
            detail={"check": check, "error": str(exc)[:200]},
        )

    # ── Rule: Assignment Conflicts ─────────────────────────────

    def _check_conflict(
        self,
        crew_id:    str,
        flight_id:  Optional[str],
        fl_start:   datetime,
        fl_end:     datetime,
    ) -> list[ComplianceIssue]:
        issues = []
        try:
            # Normalise to UTC-aware so a naive/aware mix can never raise mid-compare
            # (and silently skip a real conflict).
            fl_start = self._aware(fl_start)
            fl_end   = self._aware(fl_end)
            asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
            existing_ids = [
                r["flight_id"] for r in asgn
                if r.get("flight_id") and r["flight_id"] != flight_id
            ]
            if not existing_ids:
                return issues

            flights = self.sb.table("flights") \
                .select("id,flight_number,departure_time,arrival_time") \
                .in_("id", existing_ids) \
                .execute().data or []

            for f in flights:
                try:
                    dep = self._aware(datetime.fromisoformat(f["departure_time"].replace("Z", "+00:00")))
                    arr = self._aware(datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00")))
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue

                # Overlap: A starts before B ends AND B starts before A ends
                if fl_start < arr and dep < fl_end:
                    issues.append(ComplianceIssue(
                        rule="assignment_conflict",
                        severity=Severity.BLOCKING,
                        message_ar=f"تعارض مع رحلة {f.get('flight_number', '')} "
                                   f"({dep.strftime('%Y-%m-%d %H:%M')} UTC)",
                        message_en=f"Conflict with flight {f.get('flight_number', '')} "
                                   f"({dep.strftime('%Y-%m-%d %H:%M')} UTC)",
                        detail={
                            "conflicting_flight": f.get("flight_number"),
                            "departure": f.get("departure_time"),
                            "arrival":   f.get("arrival_time"),
                        },
                    ))
        except Exception as e:
            # FAIL-CLOSED: a conflict check that errors must BLOCK, not pass.
            _log.exception("conflict check failed for crew_id=%s", crew_id)
            issues.append(self._engine_error("conflict", e))
        return issues

    # ── Rule: Rest Period ──────────────────────────────────────

    @staticmethod
    def _is_turnaround(
        prev_dest:   Optional[str],
        next_origin: Optional[str],
        gap_hours:   float,
        max_turn:    float = MAX_TURNAROUND_HOURS,
        require_same_station: bool = True,
    ) -> bool:
        """Tell an intra-duty TURNAROUND apart from inter-duty REST.

        Two sectors belong to ONE duty (a turnaround / sit, e.g. a same-day
        BGW→JED→BGW rotation) when the crew lands at the very station the next
        sector departs from AND the ground stop is short. There is no rest
        between them — legal rest begins only after the LAST sector of the duty.

        • Same duty  → ground stop ≤ MAX_TURNAROUND_HOURS at the SAME station
                       ⇒ turnaround, the minimum-rest rule does NOT apply
                       (duty length is still bounded by the FDP check).
        • New duty   → anything else ⇒ MIN_REST_HOURS applies.

        Bounded by MAX_TURNAROUND_HOURS so a long sit is still treated as — and
        must satisfy — rest, never silently bypassed.
        """
        if gap_hours < 0 or gap_hours > max_turn:
            return False
        if require_same_station:
            return (prev_dest is not None and next_origin is not None
                    and prev_dest == next_origin)
        return True

    def _check_rest(
        self,
        crew_id:        str,
        next_dep:       datetime,
        is_international: bool,
        flight_id:      Optional[str] = None,
        *,
        crew_flights:   Optional[list] = None,
        next_origin:    Optional[str] = None,
    ) -> list[ComplianceIssue]:
        issues = []
        # Live values from the bound OM clause (fallback to config when absent).
        rp = self._om_params("rest")
        dom  = self._pnum(rp, "domestic_min_rest_hours", lo=1, hi=48)
        intl = self._pnum(rp, "international_min_rest_hours", lo=1, hi=48)
        min_rest = (intl if intl is not None else MIN_REST_INTERNATIONAL) if is_international \
            else (dom if dom is not None else MIN_REST_DOMESTIC)
        tp = self._om_params("turnaround")
        max_turn = self._pnum(tp, "max_turnaround_hours", lo=0, hi=12)
        max_turn = max_turn if max_turn is not None else MAX_TURNAROUND_HOURS
        same_station = self._pbool(tp, "same_station_required", True)

        try:
            next_dep = self._aware(next_dep)
            # `crew_flights` (arrival_time/destination_code) + `next_origin` may be
            # PRELOADED in bulk by a batch caller; otherwise fetch per crew.
            if crew_flights is not None:
                flights = crew_flights
            else:
                asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
                flight_ids = [r["flight_id"] for r in asgn if r.get("flight_id")]
                if not flight_ids:
                    return issues
                flights = self.sb.table("flights") \
                    .select("arrival_time,destination_code") \
                    .in_("id", flight_ids) \
                    .execute().data or []
                # Departure station of the flight being assigned — needed to tell a
                # turnaround (land + take off again from the SAME station) apart
                # from a true rest gap.
                if flight_id:
                    fr = self.sb.table("flights").select("origin_code").eq("id", flight_id).execute().data
                    if fr:
                        next_origin = (fr[0].get("origin_code") or "").upper() or None
            if not flights:
                return issues

            # Find the most recent arrival BEFORE the next departure (and where it
            # landed) — that flight is the candidate previous sector.
            last_arrival: Optional[datetime] = None
            last_dest: Optional[str] = None
            for f in flights:
                arr_str = f.get("arrival_time", "")
                if not arr_str:
                    continue
                try:
                    arr = self._aware(datetime.fromisoformat(arr_str.replace("Z", "+00:00")))
                except (ValueError, TypeError, AttributeError):
                    continue
                if arr < next_dep:
                    if last_arrival is None or arr > last_arrival:
                        last_arrival = arr
                        last_dest = (f.get("destination_code") or "").upper() or None

            if last_arrival is None:
                return issues

            rest_h = (next_dep - last_arrival).total_seconds() / 3600.0

            # Intra-duty turnaround (same station, short stop) is NOT rest —
            # skip the minimum-rest rule; the FDP check bounds duty length.
            if self._is_turnaround(last_dest, next_origin, rest_h,
                                   max_turn, same_station):
                return issues

            if rest_h < min_rest:
                issues.append(ComplianceIssue(
                    rule="rest_insufficient",
                    severity=Severity.BLOCKING,
                    message_ar=f"فترة الراحة غير كافية: {rest_h:.1f} ساعة "
                               f"(الحد الأدنى {min_rest:.0f} ساعة — ICAO)",
                    message_en=f"Insufficient rest: {rest_h:.1f}h "
                               f"(minimum {min_rest:.0f}h required by ICAO)",
                    detail={
                        "rest_hours": round(rest_h, 1),
                        "required":   min_rest,
                        "last_arrival": last_arrival.isoformat(),
                        "is_international": is_international,
                    },
                ))
            elif rest_h < (min_rest + REST_WARN_BUFFER):
                issues.append(ComplianceIssue(
                    rule="rest_near_limit",
                    severity=Severity.WARNING,
                    message_ar=f"فترة الراحة قريبة من الحد الأدنى: {rest_h:.1f} ساعة",
                    message_en=f"Rest period near minimum: {rest_h:.1f}h",
                    detail={"rest_hours": round(rest_h, 1), "required": min_rest},
                ))
        except Exception as e:
            # FAIL-CLOSED: a rest check that errors must BLOCK, not pass.
            _log.exception("rest check failed for crew_id=%s", crew_id)
            issues.append(self._engine_error("rest", e))
        return issues

    # ── Rule: Flight Duty Period (FDP) ─────────────────────────
    def _check_fdp(
        self,
        crew_id:   str,
        flight_id: Optional[str],
        fl_dep:    datetime,
        fl_arr:    datetime,
        acclimatisation: str = "acclimated",
    ) -> list[ComplianceIssue]:
        """Enforce the FDP table: report 1h before STD, +30 min post-flight,
        max FDP read from `fdp_rules` by (local start band × sectors ×
        acclimatisation), plus the night-duty 4-sector hard cap."""
        issues: list[ComplianceIssue] = []
        try:
            # Gather this crew's other (non-cancelled) flights as (dep, arr).
            asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
            ids = [r["flight_id"] for r in asgn if r.get("flight_id") and r["flight_id"] != flight_id]
            segs: list[tuple[datetime, datetime]] = []
            if ids:
                rows = self.sb.table("flights").select("departure_time,arrival_time,status").in_("id", ids).execute().data or []
                for f in rows:
                    if f.get("status") == "cancelled":
                        continue
                    try:
                        d = datetime.fromisoformat(f["departure_time"].replace("Z", "+00:00"))
                        a = datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00"))
                        segs.append((d, a))
                    except (KeyError, ValueError, TypeError, AttributeError):
                        continue
            segs.append((fl_dep, fl_arr))
            segs.sort(key=lambda x: x[0])

            # Split into duties wherever the gap >= a rest break, then take the
            # duty that contains the flight being assigned.
            rest_break = timedelta(hours=DUTY_SPLIT_REST_HOURS)
            chains: list[list[tuple[datetime, datetime]]] = []
            cur = [segs[0]]
            for prev, nxt in zip(segs, segs[1:]):
                if (nxt[0] - prev[1]) >= rest_break:
                    chains.append(cur)
                    cur = [nxt]
                else:
                    cur.append(nxt)
            chains.append(cur)
            duty = next((c for c in chains if (fl_dep, fl_arr) in c), [(fl_dep, fl_arr)])
            issues += self._evaluate_fdp(duty, acclimatisation)
        except Exception as e:
            # FAIL-CLOSED: an FDP check that errors must BLOCK, not pass silently.
            _log.exception("FDP check failed for crew_id=%s", crew_id)
            issues.append(self._engine_error("fdp", e))
        return issues

    def _evaluate_fdp(self, duty: list[tuple[datetime, datetime]],
                      acclimatisation: str = "acclimated") -> list[ComplianceIssue]:
        """Evaluate one duty (a list of (dep, arr) sectors) against the FDP table.
        Shared by the per-flight check and the connected-duty check so both use
        identical limits. The gaps BETWEEN the given sectors are treated as
        turnarounds (intra-duty), never rest."""
        issues: list[ComplianceIssue] = []
        if not duty:
            return issues
        sectors   = len(duty)
        first_dep = duty[0][0]
        last_arr  = duty[-1][1]
        fdp_start = first_dep - timedelta(minutes=FDP_REPORTING_LEAD_MIN)
        fdp_end   = last_arr + timedelta(minutes=FDP_POST_FLIGHT_MIN)
        actual_min = (fdp_end - fdp_start).total_seconds() / 60.0
        local_start = (fdp_start + BAGHDAD_OFFSET).time()

        def hhmm(total_min: float) -> str:
            m = int(round(total_min))
            return f"{m // 60}:{m % 60:02d}"

        # ── Night-duty hard sector cap (WOCL 02:00–04:59 local) ──
        if FDP_NIGHT_FROM <= local_start <= FDP_NIGHT_TO and sectors > FDP_NIGHT_MAX_SECTORS:
            issues.append(ComplianceIssue(
                rule="fdp_night_sector_limit",
                severity=Severity.BLOCKING,
                message_ar=f"الواجب الليلي (02:00–04:59) يسمح بـ {FDP_NIGHT_MAX_SECTORS} قطاعات فقط — المطلوب {sectors}",
                message_en=f"Night duty (02:00–04:59) allows only {FDP_NIGHT_MAX_SECTORS} sectors — {sectors} requested",
                detail={"sectors": sectors, "max": FDP_NIGHT_MAX_SECTORS,
                        "local_start": local_start.strftime("%H:%M")},
            ))

        rule = self._lookup_fdp_rule(acclimatisation, local_start, sectors)
        if rule is None:
            issues.append(ComplianceIssue(
                rule="fdp_table_unavailable",
                severity=Severity.WARNING,
                message_ar="تعذّر تحديد حد FDP — تأكد من تشغيل جدول fdp_rules",
                message_en="Could not determine FDP limit — ensure fdp_rules is seeded",
                detail={"sectors": sectors, "acclimatisation": acclimatisation},
            ))
            return issues

        max_min = float(rule["max_fdp_minutes"])
        detail = {
            "sectors": sectors,
            "actual_fdp_min": int(round(actual_min)),
            "max_fdp_min": int(max_min),
            "fdp_start_local": local_start.strftime("%H:%M"),
            "acclimatisation": acclimatisation,
            "rule_source": "FRM" if rule.get("is_frm") else "FTL",
        }
        if actual_min > max_min:
            issues.append(ComplianceIssue(
                rule="fdp_exceeded",
                severity=Severity.BLOCKING,
                message_ar=f"تجاوز فترة العمل الجوي (FDP): {hhmm(actual_min)} > الحد {hhmm(max_min)} "
                           f"({sectors} قطاعات، بداية {local_start.strftime('%H:%M')})",
                message_en=f"FDP exceeded: {hhmm(actual_min)} > limit {hhmm(max_min)} "
                           f"({sectors} sectors, start {local_start.strftime('%H:%M')})",
                detail=detail,
            ))
        elif actual_min >= max_min * FDP_WARN_RATIO:
            issues.append(ComplianceIssue(
                rule="fdp_near_limit",
                severity=Severity.WARNING,
                message_ar=f"اقتراب من حد FDP: {hhmm(actual_min)} / {hhmm(max_min)}",
                message_en=f"Approaching FDP limit: {hhmm(actual_min)} / {hhmm(max_min)}",
                detail=detail,
            ))
        return issues

    def _lookup_fdp_rule(self, state: str, local_start: time, sectors: int) -> Optional[dict]:
        """Find the fdp_rules row matching state + start-time band + sector bucket.
        TIME columns come back as 'HH:MM:SS' strings; non-wrapping bands compare
        correctly as strings."""
        try:
            rows = self.sb.table("fdp_rules").select("*").eq("acclimatisation_state", state).execute().data or []
        except Exception:
            return None
        hhmmss = local_start.strftime("%H:%M:%S")
        for r in rows:
            frm = str(r.get("start_band_from", ""))[:8]
            to  = str(r.get("start_band_to", ""))[:8]
            if frm <= hhmmss <= to and int(r["sectors_from"]) <= sectors <= int(r["sectors_to"]):
                return r
        return None

    # ── Helper ─────────────────────────────────────────────────

    @staticmethod
    def _overall_status(issues: list[ComplianceIssue]) -> str:
        if any(i.severity == Severity.BLOCKING for i in issues):
            return ComplianceStatus.BLOCKED
        if any(i.severity == Severity.CRITICAL for i in issues):
            return ComplianceStatus.RED
        if any(i.severity == Severity.WARNING for i in issues):
            return ComplianceStatus.YELLOW
        return ComplianceStatus.GREEN

    # ══════════════════════════════════════════════════════════════
    #  Crew Readiness Engine (Phase A)
    #  ───────────────────────────────────────────────────────────
    #  An ADVISORY 0–100 score + status derived from the SAME compliance
    #  issues the engine already produces. It NEVER changes the assignment
    #  decision — assign_crew still gates on the issues themselves.
    # ══════════════════════════════════════════════════════════════
    READINESS_WEIGHTS = {
        "rest":                   25,
        "hours":                  25,
        "fdp":                    20,
        "documents_training":     20,
        "qualification_conflict": 10,
    }
    # Worst-severity → fraction of a category's weight retained.
    _READINESS_FACTOR = {
        Severity.BLOCKING: 0.0,
        Severity.CRITICAL: 0.2,
        Severity.WARNING:  0.5,
        Severity.INFO:     1.0,
    }
    _READINESS_COLOR = {
        "READY": "green", "LIMITED": "amber", "FATIGUED": "orange", "BLOCKED": "red",
    }

    @staticmethod
    def _readiness_category(rule: str) -> Optional[str]:
        """Map an engine rule to one of the five readiness scoring buckets.
        Rules with no bucket (crew_status, engine_error) don't dock the score —
        but a BLOCKING severity still forces BLOCKED below."""
        if rule.startswith("rest_"):
            return "rest"
        if rule.startswith("hours_"):
            return "hours"
        if rule.startswith("fdp_"):
            return "fdp"
        if (rule.startswith("doc_") or rule.startswith("training_")
                or rule in ("check_documents_unavailable", "check_training_unavailable")):
            return "documents_training"
        if rule in ("aircraft_not_type_rated", "assignment_conflict"):
            return "qualification_conflict"
        return None

    def _readiness_from_result(self, result: dict) -> dict:
        """Compute readiness {score,status,reasons,color} from a check_crew result.
        Reuses the already-computed issues — no extra DB work."""
        issues = result.get("issues", [])
        factors = {cat: 1.0 for cat in self.READINESS_WEIGHTS}
        reasons: list[str] = []
        hard_blocked = False
        for i in issues:
            sev = i.get("severity")
            if i.get("is_blocking"):
                hard_blocked = True
            cat = self._readiness_category(i.get("rule", ""))
            if cat:
                f = self._READINESS_FACTOR.get(sev, 1.0)
                if f < factors[cat]:
                    factors[cat] = f
            if sev != Severity.INFO and i.get("message_ar"):
                reasons.append(i["message_ar"])

        score = round(sum(self.READINESS_WEIGHTS[c] * factors[c]
                          for c in self.READINESS_WEIGHTS))

        # Any real BLOCKING issue (conflict, expired doc/training, missing type
        # rating, manual block, monthly/yearly over-limit, rest/FDP block, engine
        # error) forces BLOCKED regardless of the numeric score.
        if hard_blocked:
            status = "BLOCKED"
        elif score >= 90:
            status = "READY"
        elif score >= 70:
            status = "LIMITED"
        elif score >= 50:
            status = "FATIGUED"
        else:
            status = "BLOCKED"

        return {
            "readiness_score":  score,
            "readiness_status": status,
            "readiness_reasons": reasons[:6],
            "readiness_color":  self._READINESS_COLOR[status],
        }

    def crew_readiness(self, crew_id: str, **flight_kwargs) -> dict:
        """Public: advisory readiness for a crew member (optionally vs a flight).
        Accepts the same flight_* kwargs as check_crew."""
        result = self.check_crew(crew_id, **flight_kwargs)
        if result.get("status") == "UNKNOWN":
            return {"readiness_score": 0, "readiness_status": "BLOCKED",
                    "readiness_reasons": [result.get("error", "unknown crew")],
                    "readiness_color": "red"}
        return self._readiness_from_result(result)

    def batch_readiness(self, company_id: str, crew_rows: Optional[list] = None) -> dict:
        """BATCHED roster-readiness board (advisory). Loads crew + assignments +
        flights + documents + training in a HANDFUL of bulk queries (never N×),
        then computes per-crew cumulative-load readiness in memory.

        This is the standby/availability view (no specific flight): it scores
        accumulated hours + document/training validity + crew status. The full
        per-flight picture (FDP, rest-vs-next, conflict, qualification) comes
        from the per-(crew,flight) projection path. Returns {crew_id: {...}}."""
        today = date.today()
        month_start = today.replace(day=1)
        day28_start = today - timedelta(days=28)
        year_start  = today.replace(month=1, day=1)
        WARN = WARN_DAYS_BEFORE_EXPIRY

        if crew_rows is None:
            try:
                crew_rows = self.sb.table("crew").select(
                    "id,status,rank,max_monthly_hours"
                ).eq("company_id", company_id).execute().data or []
            except Exception:
                _log.exception("batch_readiness: crew load failed")
                return {}
        crew_ids = [c["id"] for c in crew_rows if c.get("id")]
        if not crew_ids:
            return {}

        # ── Bulk loads (a few queries total) ──
        asgs = self.sb.table("assignments").select("crew_id,flight_id") \
            .in_("crew_id", crew_ids).execute().data or []
        fids = list({a["flight_id"] for a in asgs if a.get("flight_id")})
        fmap = {}
        if fids:
            for f in (self.sb.table("flights").select(
                    "id,departure_time,arrival_time,duration_hours,status"
            ).in_("id", fids).execute().data or []):
                fmap[f["id"]] = f
        by_crew: dict[str, list] = {}
        for a in asgs:
            f = fmap.get(a.get("flight_id"))
            if f:
                by_crew.setdefault(a["crew_id"], []).append(f)

        docs_by_crew: dict[str, list] = {}
        train_by_crew: dict[str, list] = {}
        try:
            for d in (self.sb.table("documents").select("crew_id,document_type,expiry_date")
                      .in_("crew_id", crew_ids).execute().data or []):
                docs_by_crew.setdefault(d.get("crew_id"), []).append(d)
        except Exception:
            pass
        try:
            for t in (self.sb.table("training_records").select("crew_id,training_type,expiry_date")
                      .in_("crew_id", crew_ids).execute().data or []):
                train_by_crew.setdefault(t.get("crew_id"), []).append(t)
        except Exception:
            pass

        def _exp_issues(rows, type_key, rule_prefix):
            out = []
            for r in rows or []:
                es = r.get("expiry_date")
                if not es:
                    continue
                try:
                    exp = date.fromisoformat(str(es)[:10])
                except (ValueError, TypeError):
                    continue
                tt = r.get(type_key, "item")
                if exp < today:
                    out.append({"rule": f"{rule_prefix}_expired_{tt}", "severity": Severity.BLOCKING,
                                "is_blocking": True,
                                "message_ar": f"منتهٍ: {tt} ({exp})", "message_en": f"Expired: {tt}"})
                elif (exp - today).days <= WARN:
                    out.append({"rule": f"{rule_prefix}_expiring_{tt}", "severity": Severity.WARNING,
                                "is_blocking": False,
                                "message_ar": f"قرب الانتهاء: {tt}", "message_en": f"Expiring: {tt}"})
            return out

        out: dict[str, dict] = {}
        for c in crew_rows:
            cid = c["id"]
            issues: list[dict] = []

            # Crew status
            st = c.get("status", "active")
            if st in ("blocked", "suspended"):
                issues.append({"rule": "crew_status_blocked", "severity": Severity.BLOCKING,
                               "is_blocking": True, "message_ar": f"حالة الطاقم {st}",
                               "message_en": f"Crew status {st}"})
            elif st == "on_leave":
                issues.append({"rule": "crew_on_leave", "severity": Severity.WARNING,
                               "is_blocking": False, "message_ar": "في إجازة",
                               "message_en": "On leave"})

            # Hours (cumulative)
            monthly = h28 = yearly = 0.0
            last_arr: Optional[datetime] = None
            for f in by_crew.get(cid, []):
                if f.get("status") == "cancelled":
                    continue
                ds = f.get("departure_time")
                try:
                    dd = date.fromisoformat(str(ds)[:10]) if ds else None
                except (ValueError, TypeError):
                    dd = None
                dur = float(f.get("duration_hours") or 0)
                if dd:
                    if dd >= month_start: monthly += dur
                    if dd >= day28_start: h28 += dur
                    if dd >= year_start:  yearly += dur
                a_s = f.get("arrival_time")
                if a_s:
                    try:
                        arr = self._aware(datetime.fromisoformat(str(a_s).replace("Z", "+00:00")))
                        if last_arr is None or arr > last_arr:
                            last_arr = arr
                    except (ValueError, TypeError):
                        pass

            max_monthly = float(c.get("max_monthly_hours") or MAX_MONTHLY_HOURS)
            if monthly >= max_monthly:
                issues.append({"rule": "hours_monthly_exceeded", "severity": Severity.BLOCKING,
                               "is_blocking": True, "message_ar": f"تجاوز الحد الشهري {monthly:.0f}/{max_monthly:.0f}",
                               "message_en": "Monthly limit exceeded"})
            elif monthly >= max_monthly * 0.83:
                issues.append({"rule": "hours_monthly_warning", "severity": Severity.WARNING,
                               "is_blocking": False, "message_ar": f"اقتراب من الحد الشهري {monthly:.0f}/{max_monthly:.0f}",
                               "message_en": "Approaching monthly limit"})
            if h28 >= MAX_28DAY_HOURS:
                issues.append({"rule": "hours_28day_exceeded", "severity": Severity.CRITICAL,
                               "is_blocking": False, "message_ar": f"تجاوز حد 28 يوم {h28:.0f}/{MAX_28DAY_HOURS:.0f}",
                               "message_en": "28-day limit exceeded"})
            elif h28 >= WARN_28DAY_HOURS:
                issues.append({"rule": "hours_28day_warning", "severity": Severity.WARNING,
                               "is_blocking": False, "message_ar": f"اقتراب من حد 28 يوم {h28:.0f}/{MAX_28DAY_HOURS:.0f}",
                               "message_en": "Approaching 28-day limit"})
            if yearly >= MAX_YEARLY_HOURS:
                issues.append({"rule": "hours_yearly_exceeded", "severity": Severity.BLOCKING,
                               "is_blocking": True, "message_ar": f"تجاوز الحد السنوي {yearly:.0f}/{MAX_YEARLY_HOURS:.0f}",
                               "message_en": "Yearly limit exceeded"})

            # Documents + training validity
            issues += _exp_issues(docs_by_crew.get(cid), "document_type", "doc")
            issues += _exp_issues(train_by_crew.get(cid), "training_type", "training")

            # Rest status (advisory): rested once MIN_REST_DOMESTIC has elapsed
            # since the last arrival.
            rest_status = "rested"
            next_available_at = None
            if last_arr is not None:
                now = datetime.now(timezone.utc)
                ready_at = last_arr + timedelta(hours=MIN_REST_DOMESTIC)
                if ready_at > now:
                    rest_status = "resting"
                    next_available_at = ready_at.isoformat()

            readiness = self._readiness_from_result({"issues": issues})
            # Coarse GREEN/YELLOW/RED/BLOCKED + the hard-stop reasons, so callers
            # that need a simple status (e.g. the suggest sheet) don't have to run
            # the per-crew engine. Additive — board consumers ignore extra keys.
            blocking_reasons = [i["message_ar"] for i in issues if i.get("is_blocking")]
            if blocking_reasons:
                comp_status = "BLOCKED"
            elif any(i.get("severity") == Severity.CRITICAL for i in issues):
                comp_status = "RED"
            elif any(i.get("severity") == Severity.WARNING for i in issues):
                comp_status = "YELLOW"
            else:
                comp_status = "GREEN"
            out[cid] = {
                "crew_id": cid,
                "monthly_flight_hours": round(monthly, 1),
                "last_28day_hours": round(h28, 1),
                "yearly_hours": round(yearly, 1),
                "max_monthly_hours": round(max_monthly, 1),
                "rest_status": rest_status,
                "next_available_at": next_available_at,
                "compliance_status": comp_status,
                "blocking_reasons": blocking_reasons,
                **readiness,
            }
        return out

    # ══════════════════════════════════════════════════════════════
    #  OM (Operations Manual) binding layer
    #  ───────────────────────────────────────────────────────────
    #  Each hardcoded check emits a ComplianceIssue with a `rule` key. An OM
    #  article (om_articles row) can BIND to a family of those keys via
    #  `bound_check_key` and then govern them: enable/disable, set whether the
    #  family BLOCKS or only WARNS, and stamp the OM clause number onto every
    #  message. The check logic stays in code; OM is the control plane over it.
    # ══════════════════════════════════════════════════════════════

    # System-level infra errors (a lookup failed) are never governed by OM.
    @staticmethod
    def _binding_key(rule: str) -> Optional[str]:
        """Map a concrete engine rule key to its OM binding family."""
        if rule.endswith("_unavailable"):
            return None
        if rule.startswith("doc_"):      return "documents"
        if rule.startswith("training_"): return "training"
        # Hours split per window so each OM clause governs its own limit.
        if rule.startswith("hours_28day"):   return "flight_hours_28day"
        if rule.startswith("hours_yearly"):  return "flight_hours_yearly"
        if rule.startswith("hours_monthly"): return "flight_hours_monthly"
        if rule.startswith("hours_"):    return "flight_hours"
        if rule.startswith("rest_"):     return "rest"
        if rule.startswith("fdp_"):      return "fdp"
        if rule.startswith("crew_status") or rule == "crew_on_leave":
            return "crew_status"
        if rule == "aircraft_not_type_rated": return "aircraft_qualification"
        if rule == "assignment_conflict":     return "assignment_conflict"
        return None

    def _load_om_rules(self) -> dict:
        """Active OM articles that affect compliance, indexed by bound_check_key.
        Degrades to {} (no governance — pure hardcoded behavior) if the columns
        aren't migrated yet or the table is unreachable, so the engine is never
        less safe than before."""
        try:
            rows = self.sb.table("om_articles").select(
                "id,bound_check_key,rule_type,is_active,affects_compliance,parameters"
            ).eq("affects_compliance", True).execute().data or []
        except Exception:
            return {}
        out: dict = {}
        for r in rows:
            k = r.get("bound_check_key")
            if k:
                out[k] = r
        return out

    # ── OM parameters → live operational values (Phase B) ──────────
    def _om_param_rules(self) -> dict:
        """Cached map bound_check_key → active affects-compliance OM rule."""
        if not hasattr(self, "_om_cache"):
            self._om_cache = self._load_om_rules()
        return self._om_cache

    def _om_params(self, check_key: str) -> dict:
        """parameters of the ACTIVE clause governing [check_key], else {}. An
        inactive / unbound clause returns {} so the engine falls back to config
        — an inactive clause never changes the law."""
        r = self._om_param_rules().get(check_key)
        if not r or not r.get("is_active", True) or not r.get("affects_compliance"):
            return {}
        p = r.get("parameters")
        return p if isinstance(p, dict) else {}

    @staticmethod
    def _pnum(params: dict, key: str, *, lo: Optional[float] = None,
              hi: Optional[float] = None) -> Optional[float]:
        """Read a numeric parameter with sanity bounds. Returns None when the
        value is missing, the wrong type, or out of [lo, hi] — the caller then
        falls back to the safe config default. Rejects nonsense like 0 / negative."""
        v = params.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        v = float(v)
        if lo is not None and v < lo:
            return None
        if hi is not None and v > hi:
            return None
        return v

    @staticmethod
    def _pbool(params: dict, key: str, default: bool) -> bool:
        v = params.get(key)
        return bool(v) if isinstance(v, bool) else default

    # rule_type → severity (used to DOWNGRADE/annotate only; never silently
    # upgrade a warning into a block — that needs a deliberate per-condition rule).
    _OM_SEVERITY = {
        "blocking":          Severity.BLOCKING,
        "approval_required": Severity.BLOCKING,
        "warning":           Severity.WARNING,
        "informational":     Severity.INFO,
    }

    def _apply_om(self, issues: list[ComplianceIssue]) -> list[ComplianceIssue]:
        om = self._load_om_rules()
        if not om:
            return issues
        out: list[ComplianceIssue] = []
        for i in issues:
            key = self._binding_key(i.rule)
            rule = om.get(key) if key else None
            if rule is None:
                out.append(i)
                continue
            # Disabled OM clause → the whole family stops firing.
            if not rule.get("is_active", True):
                continue
            rt = rule.get("rule_type")
            # Stamp the clause number onto the message (always, when bound).
            num = rule.get("id")
            if num and not i.om_ref:
                i.om_ref = num
                # `num` already reads like "OM-C 8.1" — don't double the "OM".
                i.message_ar = f"{num}: {i.message_ar}"
                i.message_en = f"{num}: {i.message_en}"
            # Adjust severity DOWNWARD only (informational < warning < blocking),
            # or annotate approval. Hard conditions the engine already blocks
            # are never weakened beyond what the OM clause type dictates.
            target = self._OM_SEVERITY.get(rt)
            if rt == "informational" and target:
                i.severity = target                       # display-only
            elif rt == "warning" and i.severity == Severity.BLOCKING:
                i.severity = Severity.WARNING             # block → advisory
            elif rt == "approval_required" and i.severity == Severity.BLOCKING:
                i.detail = {**(i.detail or {}), "requires_approval": True}
            out.append(i)
        return out
