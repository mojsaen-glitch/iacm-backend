"""Seed Supabase database with initial IACM data.

DEVELOPMENT / STAGING USE ONLY. Gated behind ALLOW_SEED=1 so it can never run
in production by accident. The Supabase URL + service-role key and every seed
password are read from the environment — this file contains NO static
credentials. Mirror of `app.db.seed` for the Supabase REST client path.

Usage:
    export ALLOW_SEED=1
    export SUPABASE_URL='https://YOUR_PROJECT_ID.supabase.co'
    export SUPABASE_SERVICE_KEY='...'           # service_role key
    export SEED_ADMIN_PASSWORD='...'            # required
    export SEED_OPSMGR_PASSWORD='...'           # required
    export SEED_SCHED_PASSWORD='...'            # required
    export SEED_COMP_PASSWORD='...'             # required
    export SEED_FLIGHTOPS_PASSWORD='...'        # required
    export SEED_CREW1_PASSWORD='...'            # optional (skipped if unset)
    export SEED_CREW2_PASSWORD='...'            # optional (skipped if unset)
    python seed_supabase.py
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone, timedelta
from supabase import create_client
import bcrypt as _bcrypt


def _required_env(name: str) -> str:
    """Return env var or abort — never fall back to a baked-in default."""
    val = os.environ.get(name)
    if not val:
        print(
            f"ERROR: env var {name} is required to seed. Refusing to use a "
            f"hard-coded default. See this script's docstring for the full list.",
            file=sys.stderr,
        )
        sys.exit(2)
    return val


if os.environ.get("ALLOW_SEED") != "1":
    print(
        "ERROR: seeding is disabled. Set ALLOW_SEED=1 to confirm this is NOT a "
        "production environment, then re-run.",
        file=sys.stderr,
    )
    sys.exit(2)

SUPABASE_URL = _required_env("SUPABASE_URL")
SERVICE_KEY = _required_env("SUPABASE_SERVICE_KEY")

sb = create_client(SUPABASE_URL, SERVICE_KEY)
def hash_pw(p): return _bcrypt.hashpw(p.encode(), _bcrypt.gensalt()).decode()
now = datetime.now(timezone.utc).isoformat()


def uid(): return str(uuid.uuid4())


print("Seeding IACM database...")

# ── Company ──────────────────────────────────────────────────────────────
company_id = uid()
sb.table("companies").insert({
    "id": company_id, "name_ar": "الخطوط الجوية العراقية",
    "name_en": "Iraqi Airways", "code": "IA",
    "icao_code": "IAW", "iata_code": "IA", "country": "Iraq",
    "primary_color": "#1B5E20", "is_active": True,
    "created_at": now, "updated_at": now,
}).execute()
print(f"[OK] Company: Iraqi Airways ({company_id})")

# ── Aircraft ─────────────────────────────────────────────────────────────
ac1, ac2, ac3 = uid(), uid(), uid()
sb.table("aircraft").insert([
    {"id": ac1, "company_id": company_id, "aircraft_type": "A320", "registration": "YI-AQY", "name": "Baghdad",   "min_crew": 4, "max_crew": 8,  "is_active": True, "created_at": now, "updated_at": now},
    {"id": ac2, "company_id": company_id, "aircraft_type": "B737", "registration": "YI-AGE", "name": "Basra",    "min_crew": 4, "max_crew": 6,  "is_active": True, "created_at": now, "updated_at": now},
    {"id": ac3, "company_id": company_id, "aircraft_type": "B787", "registration": "YI-ARS", "name": "Erbil",    "min_crew": 8, "max_crew": 14, "is_active": True, "created_at": now, "updated_at": now},
]).execute()
print("[OK] Aircraft: 3 registered")

# ── Crew ─────────────────────────────────────────────────────────────────
cr1, cr2, cr3, cr4, cr5 = uid(), uid(), uid(), uid(), uid()
crew_rows = [
    {"id": cr1, "employee_id": "IA-001", "full_name_ar": "سارة أحمد الجابري",   "full_name_en": "Sara Ahmed Al-Jaberi",    "rank": "purser",       "base": "BGW", "operation_type": "long_haul",  "status": "active",   "monthly_flight_hours": 42.5, "yearly_flight_hours": 380.0,  "total_flight_hours": 3240.0, "last_28day_hours": 42.5, "max_monthly_hours": 100, "gender": "female", "nationality": "Iraqi", "join_date": "2018-03-15", "aircraft_qualifications": '["A320","B737","B787"]'},
    {"id": cr2, "employee_id": "IA-002", "full_name_ar": "محمد علي الخزاعي",    "full_name_en": "Mohammed Ali Al-Khazaei", "rank": "senior_crew",  "base": "BGW", "operation_type": "short_haul", "status": "in_flight","monthly_flight_hours": 68.0, "yearly_flight_hours": 620.0,  "total_flight_hours": 5120.5, "last_28day_hours": 68.0, "max_monthly_hours": 100, "gender": "male",   "nationality": "Iraqi", "join_date": "2015-06-01", "aircraft_qualifications": '["A320","B737"]'},
    {"id": cr3, "employee_id": "IA-003", "full_name_ar": "نور حسين المنصور",    "full_name_en": "Noor Hussein Al-Mansour", "rank": "cabin_crew",   "base": "BGW", "operation_type": "short_haul", "status": "standby",  "monthly_flight_hours": 15.0, "yearly_flight_hours": 140.0,  "total_flight_hours": 890.0,  "last_28day_hours": 15.0, "max_monthly_hours": 100, "gender": "female", "nationality": "Iraqi", "join_date": "2021-09-10", "aircraft_qualifications": '["A320"]'},
    {"id": cr4, "employee_id": "IA-004", "full_name_ar": "كريم صالح العبيدي",   "full_name_en": "Kareem Saleh Al-Ubaidi",  "rank": "chief_purser", "base": "BGW", "operation_type": "both",       "status": "blocked",  "monthly_flight_hours": 0.0,  "yearly_flight_hours": 0.0,    "total_flight_hours": 8950.0, "last_28day_hours": 0.0,  "max_monthly_hours": 100, "gender": "male",   "nationality": "Iraqi", "join_date": "2010-01-20", "aircraft_qualifications": '["A320","B737","B787"]', "block_reason": "Expired medical certificate"},
    {"id": cr5, "employee_id": "IA-005", "full_name_ar": "رنا عبدالله الراوي",  "full_name_en": "Rana Abdullah Al-Rawi",   "rank": "cabin_crew",   "base": "BGW", "operation_type": "short_haul", "status": "on_leave", "monthly_flight_hours": 0.0,  "yearly_flight_hours": 0.0,    "total_flight_hours": 1230.0, "last_28day_hours": 0.0,  "max_monthly_hours": 100, "gender": "female", "nationality": "Iraqi", "join_date": "2019-11-05", "aircraft_qualifications": '["A320","B737"]'},
]
for c in crew_rows:
    c["company_id"] = company_id
    c["contract_type"] = "full_time"
    c["rest_hours_due"] = 0
    c["created_at"] = now
    c["updated_at"] = now
sb.table("crew").insert(crew_rows).execute()
print("[OK] Crew: 5 members added")

# ── Users ────────────────────────────────────────────────────────────────
# Staff passwords are required from env (no static defaults — these are real
# logins). Crew login accounts are optional and skipped unless their password
# env var is provided.
user_rows = [
    {"id": uid(), "email": "admin@iraqiairways.iq",      "name_ar": "مدير النظام",      "name_en": "System Admin",        "role": "super_admin",       "is_superuser": True,  "hashed_password": hash_pw(_required_env("SEED_ADMIN_PASSWORD"))},
    {"id": uid(), "email": "supervisor@iraqiairways.iq", "name_ar": "مدير العمليات",    "name_en": "Operations Manager",  "role": "ops_manager",       "is_superuser": False, "hashed_password": hash_pw(_required_env("SEED_OPSMGR_PASSWORD"))},
    {"id": uid(), "email": "scheduler@iraqiairways.iq",  "name_ar": "مسؤول الجدولة",   "name_en": "Scheduler",           "role": "scheduler",         "is_superuser": False, "hashed_password": hash_pw(_required_env("SEED_SCHED_PASSWORD"))},
    {"id": uid(), "email": "compliance@iraqiairways.iq", "name_ar": "ضابط الامتثال",   "name_en": "Compliance Officer",  "role": "compliance_officer","is_superuser": False, "hashed_password": hash_pw(_required_env("SEED_COMP_PASSWORD"))},
    {"id": uid(), "email": "flights@iraqiairways.iq",    "name_ar": "عمليات الطيران",  "name_en": "Flight Operations",   "role": "flight_ops",        "is_superuser": False, "hashed_password": hash_pw(_required_env("SEED_FLIGHTOPS_PASSWORD"))},
]
_crew1_pw = os.environ.get("SEED_CREW1_PASSWORD")
if _crew1_pw:
    user_rows.append({"id": uid(), "email": "crew1@iraqiairways.iq", "name_ar": "سارة أحمد الجابري", "name_en": "Sara Ahmed Al-Jaberi", "role": "crew", "is_superuser": False, "hashed_password": hash_pw(_crew1_pw), "crew_id": cr1})
_crew2_pw = os.environ.get("SEED_CREW2_PASSWORD")
if _crew2_pw:
    user_rows.append({"id": uid(), "email": "crew2@iraqiairways.iq", "name_ar": "محمد علي الخزاعي", "name_en": "Mohammed Ali Al-Khazaei", "role": "crew", "is_superuser": False, "hashed_password": hash_pw(_crew2_pw), "crew_id": cr2})
for u in user_rows:
    u["company_id"] = company_id
    u["is_active"] = True
    u["created_at"] = now
    u["updated_at"] = now
sb.table("users").insert(user_rows).execute()
print(f"[OK] Users: {len(user_rows)} accounts created")

# ── Documents ────────────────────────────────────────────────────────────
today = date.today()
sb.table("documents").insert([
    {"id": uid(), "crew_id": cr1, "document_type": "passport",            "document_number": "A12345678", "expiry_date": "2027-06-15", "issued_by": "Iraqi Ministry of Interior", "is_verified": True,  "created_at": now, "updated_at": now},
    {"id": uid(), "crew_id": cr1, "document_type": "medical_certificate", "document_number": "MED-2024-001", "expiry_date": (today + timedelta(days=8)).isoformat(),  "issued_by": "ICAA", "is_verified": True, "created_at": now, "updated_at": now},
    {"id": uid(), "crew_id": cr1, "document_type": "pilot_license",       "document_number": "LIC-001",   "expiry_date": "2026-12-31", "issued_by": "ICAA",                      "is_verified": True,  "created_at": now, "updated_at": now},
    {"id": uid(), "crew_id": cr4, "document_type": "medical_certificate", "document_number": "MED-2023-045", "expiry_date": (today - timedelta(days=5)).isoformat(),   "issued_by": "ICAA", "is_verified": False,"created_at": now, "updated_at": now},
    {"id": uid(), "crew_id": cr2, "document_type": "passport",            "document_number": "B98765432", "expiry_date": "2026-03-20", "issued_by": "Iraqi Ministry of Interior", "is_verified": True,  "created_at": now, "updated_at": now},
]).execute()
print("[OK] Documents: 5 records added")

# ── Flights ──────────────────────────────────────────────────────────────
base = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
fl1, fl2, fl3 = uid(), uid(), uid()
sb.table("flights").insert([
    {"id": fl1, "flight_number": "IA-210", "company_id": company_id, "aircraft_id": ac1, "origin_code": "BGW", "destination_code": "DXB", "departure_time": base.isoformat(), "arrival_time": (base + timedelta(hours=3)).isoformat(), "duration_hours": 3.0, "crew_required": 4, "status": "in_air",    "publish_status": "published", "delay_minutes": 0, "created_at": now, "updated_at": now},
    {"id": fl2, "flight_number": "IA-315", "company_id": company_id, "aircraft_id": ac2, "origin_code": "BGW", "destination_code": "AMM", "departure_time": (base + timedelta(hours=6)).isoformat(), "arrival_time": (base + timedelta(hours=8, minutes=30)).isoformat(), "duration_hours": 2.5, "crew_required": 4, "status": "scheduled","publish_status": "published", "delay_minutes": 0, "created_at": now, "updated_at": now},
    {"id": fl3, "flight_number": "IA-420", "company_id": company_id, "aircraft_id": ac3, "origin_code": "BGW", "destination_code": "LHR", "departure_time": (base + timedelta(hours=14)).isoformat(), "arrival_time": (base + timedelta(hours=21, minutes=30)).isoformat(), "duration_hours": 7.5, "crew_required": 8, "status": "scheduled","publish_status": "draft",      "delay_minutes": 0, "created_at": now, "updated_at": now},
]).execute()
print("[OK] Flights: 3 flights created")

print()
print("=" * 50)
print("Seeding complete! Accounts were created with the passwords you supplied")
print("via the SEED_* environment variables — they are NOT printed here.")
print("Log in with the emails above and the password you set for each role.")
print()
print("API Docs: http://localhost:8000/api/docs")
print("=" * 50)
