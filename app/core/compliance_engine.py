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
from datetime import datetime, date, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional


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


WARN_DAYS_BEFORE_EXPIRY = 30    # days before expiry to start warning
MAX_MONTHLY_HOURS       = 90.0  # default crew max monthly hours
WARN_MONTHLY_HOURS      = 75.0  # warn when approaching monthly limit
MAX_28DAY_HOURS         = 100.0 # ICAO 28-day rolling limit
WARN_28DAY_HOURS        = 90.0  # warn when approaching 28-day limit
MAX_YEARLY_HOURS        = 900.0 # ICAO yearly absolute limit
WARN_YEARLY_HOURS       = 800.0 # warn when approaching yearly limit
MIN_REST_DOMESTIC       = 10.0  # ICAO minimum rest hours — domestic
MIN_REST_INTERNATIONAL  = 12.0  # ICAO minimum rest hours — international
REST_WARN_BUFFER        = 2.0   # warn when rest is within 2h of minimum

IRAQI_AIRPORTS = {"BGW", "NJF", "BSR", "EBL", "OSM", "ISU", "RUM", "TQD"}

DOCUMENT_LABELS = {
    "passport":        ("جواز السفر",              "Passport"),
    "medical":         ("الشهادة الطبية",           "Medical Certificate"),
    "license":         ("رخصة الطيار",              "Pilot License"),
    "crew_id":         ("بطاقة الطاقم",             "Crew ID"),
    "safety":          ("شهادة السلامة",             "Safety Certificate"),
    "emergency":       ("شهادة الطوارئ",             "Emergency Certificate"),
    "first_aid":       ("الإسعافات الأولية",         "First Aid"),
    "crm":             ("شهادة CRM",                 "CRM Certificate"),
    "dangerous_goods": ("البضائع الخطرة",            "Dangerous Goods"),
    "visa":            ("التأشيرة",                  "Visa"),
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
        issues += self._check_flight_hours(crew_id, crew)

        if flight_departure and flight_arrival:
            issues += self._check_conflict(crew_id, flight_id, flight_departure, flight_arrival)
            issues += self._check_rest(crew_id, flight_departure, is_international)

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

    def _check_documents(self, crew_id: str) -> list[ComplianceIssue]:
        issues = []
        today = date.today()

        try:
            docs = self.sb.table("documents").select("*").eq("crew_id", crew_id).execute().data or []
        except Exception:
            return issues

        for doc in docs:
            expiry_str = doc.get("expiry_date")
            if not expiry_str:
                continue
            try:
                expiry = date.fromisoformat(str(expiry_str)[:10])
            except (ValueError, TypeError):
                continue

            doc_type = doc.get("document_type", "document")
            label_ar, label_en = DOCUMENT_LABELS.get(doc_type, (doc_type, doc_type))
            days_diff = (expiry - today).days

            if expiry < today:
                issues.append(ComplianceIssue(
                    rule=f"doc_expired_{doc_type}",
                    severity=Severity.BLOCKING,
                    message_ar=f"وثيقة منتهية الصلاحية: {label_ar} (انتهت {expiry})",
                    message_en=f"Expired document: {label_en} (expired {expiry})",
                    detail={"type": doc_type, "expiry": str(expiry), "days_overdue": abs(days_diff)},
                ))
            elif days_diff <= WARN_DAYS_BEFORE_EXPIRY:
                issues.append(ComplianceIssue(
                    rule=f"doc_expiring_{doc_type}",
                    severity=Severity.WARNING,
                    message_ar=f"وثيقة على وشك الانتهاء: {label_ar} (تبقى {days_diff} يوم)",
                    message_en=f"Document expiring soon: {label_en} ({days_diff} days left)",
                    detail={"type": doc_type, "expiry": str(expiry), "days_left": days_diff},
                ))

        return issues

    # ── Rule: Training Records ─────────────────────────────────

    def _check_training(self, crew_id: str) -> list[ComplianceIssue]:
        issues = []
        today = date.today()

        try:
            records = self.sb.table("training").select("*").eq("crew_id", crew_id).execute().data or []
        except Exception:
            return issues

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
                    severity=Severity.BLOCKING,
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

    def _check_flight_hours(self, crew_id: str, crew: dict) -> list[ComplianceIssue]:
        issues = []
        today = date.today()
        month_start  = today.replace(day=1)
        day28_start  = today - timedelta(days=28)
        year_start   = today.replace(month=1, day=1)

        try:
            # Get all assignment flight_ids for this crew
            asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
            flight_ids = [r["flight_id"] for r in asgn if r.get("flight_id")]
            if not flight_ids:
                return issues

            flights = self.sb.table("flights") \
                .select("departure_time,duration_hours,status") \
                .in_("id", flight_ids) \
                .execute().data or []
        except Exception:
            return issues

        monthly_h = 0.0
        h28d      = 0.0
        yearly_h  = 0.0

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
            duration = float(f.get("duration_hours") or 0)

            if dep_date >= month_start:
                monthly_h += duration
            if dep_date >= day28_start:
                h28d += duration
            if dep_date >= year_start:
                yearly_h += duration

        max_monthly = float(crew.get("max_monthly_hours") or MAX_MONTHLY_HOURS)

        # ─ Yearly check (ICAO absolute) ─
        if yearly_h >= MAX_YEARLY_HOURS:
            issues.append(ComplianceIssue(
                rule="hours_yearly_exceeded",
                severity=Severity.BLOCKING,
                message_ar=f"تجاوز الحد السنوي ICAO: {yearly_h:.1f} / {MAX_YEARLY_HOURS:.0f} ساعة",
                message_en=f"ICAO yearly limit exceeded: {yearly_h:.1f} / {MAX_YEARLY_HOURS:.0f}h",
                detail={"yearly_hours": round(yearly_h, 1), "limit": MAX_YEARLY_HOURS},
            ))
        elif yearly_h >= WARN_YEARLY_HOURS:
            issues.append(ComplianceIssue(
                rule="hours_yearly_warning",
                severity=Severity.WARNING,
                message_ar=f"اقتراب من الحد السنوي: {yearly_h:.1f} / {MAX_YEARLY_HOURS:.0f} ساعة",
                message_en=f"Approaching yearly limit: {yearly_h:.1f} / {MAX_YEARLY_HOURS:.0f}h",
                detail={"yearly_hours": round(yearly_h, 1), "limit": MAX_YEARLY_HOURS},
            ))

        # ─ 28-day rolling check ─
        if h28d >= MAX_28DAY_HOURS:
            issues.append(ComplianceIssue(
                rule="hours_28day_exceeded",
                severity=Severity.CRITICAL,
                message_ar=f"تجاوز حد الـ 28 يوم: {h28d:.1f} / {MAX_28DAY_HOURS:.0f} ساعة",
                message_en=f"28-day rolling limit exceeded: {h28d:.1f} / {MAX_28DAY_HOURS:.0f}h",
                detail={"hours_28d": round(h28d, 1), "limit": MAX_28DAY_HOURS},
            ))
        elif h28d >= WARN_28DAY_HOURS:
            issues.append(ComplianceIssue(
                rule="hours_28day_warning",
                severity=Severity.WARNING,
                message_ar=f"اقتراب من حد الـ 28 يوم: {h28d:.1f} / {MAX_28DAY_HOURS:.0f} ساعة",
                message_en=f"Approaching 28-day limit: {h28d:.1f} / {MAX_28DAY_HOURS:.0f}h",
                detail={"hours_28d": round(h28d, 1), "limit": MAX_28DAY_HOURS},
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
        elif monthly_h >= WARN_MONTHLY_HOURS:
            issues.append(ComplianceIssue(
                rule="hours_monthly_warning",
                severity=Severity.WARNING,
                message_ar=f"اقتراب من الحد الشهري: {monthly_h:.1f} / {max_monthly:.0f} ساعة",
                message_en=f"Approaching monthly limit: {monthly_h:.1f} / {max_monthly:.0f}h",
                detail={"monthly_hours": round(monthly_h, 1), "limit": max_monthly},
            ))

        return issues

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
                    dep = datetime.fromisoformat(f["departure_time"].replace("Z", "+00:00"))
                    arr = datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00"))
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
        except Exception:
            pass
        return issues

    # ── Rule: Rest Period ──────────────────────────────────────

    def _check_rest(
        self,
        crew_id:        str,
        next_dep:       datetime,
        is_international: bool,
    ) -> list[ComplianceIssue]:
        issues = []
        min_rest = MIN_REST_INTERNATIONAL if is_international else MIN_REST_DOMESTIC

        try:
            asgn = self.sb.table("assignments").select("flight_id").eq("crew_id", crew_id).execute().data or []
            flight_ids = [r["flight_id"] for r in asgn if r.get("flight_id")]
            if not flight_ids:
                return issues

            flights = self.sb.table("flights") \
                .select("arrival_time") \
                .in_("id", flight_ids) \
                .execute().data or []

            # Find the most recent arrival BEFORE the next departure
            last_arrival: Optional[datetime] = None
            for f in flights:
                arr_str = f.get("arrival_time", "")
                if not arr_str:
                    continue
                try:
                    arr = datetime.fromisoformat(arr_str.replace("Z", "+00:00"))
                except (ValueError, TypeError, AttributeError):
                    continue
                if arr < next_dep:
                    if last_arrival is None or arr > last_arrival:
                        last_arrival = arr

            if last_arrival is None:
                return issues

            rest_h = (next_dep - last_arrival).total_seconds() / 3600.0

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
        except Exception:
            pass
        return issues

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
