"""
Seed script - runs once to populate the database with initial data.

DEVELOPMENT / STAGING USE ONLY. This script is gated behind the
`ALLOW_SEED=1` environment variable so it cannot run in production by
accident. Seed-user passwords are also read from env vars so this file
contains no static credentials.

Usage:
    # local dev:
    export ALLOW_SEED=1
    export SEED_ADMIN_PASSWORD='...'        # required, no default
    export SEED_OPSMGR_PASSWORD='...'       # required
    export SEED_SCHED_PASSWORD='...'        # required
    export SEED_COMP_PASSWORD='...'         # required
    export SEED_FLIGHTOPS_PASSWORD='...'    # required
    python -m app.db.seed
"""
import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import AsyncSessionLocal
from app.models.company import Company
from app.models.user import User
from app.models.crew import Crew
from app.models.document import CrewDocument
from app.models.flight import Flight
from app.models.aircraft import Aircraft
from app.core.security import get_password_hash


def _required_env(name: str) -> str:
    """Return env var or abort. Centralised so the error message is identical
    everywhere and we never silently fall back to a baked-in default."""
    val = os.environ.get(name)
    if not val:
        print(
            f"ERROR: env var {name} is required to seed. Refusing to use a "
            f"hard-coded default. See seed.py docstring for the full list.",
            file=sys.stderr,
        )
        sys.exit(2)
    return val


async def seed(db: AsyncSession):
    if os.environ.get("ALLOW_SEED") != "1":
        print(
            "ERROR: seeding is disabled. Set ALLOW_SEED=1 to confirm this is "
            "NOT a production environment, then re-run.",
            file=sys.stderr,
        )
        sys.exit(2)

    print("Seeding database...")

    # Pull seed passwords from env. No defaults — if anyone forgets to set
    # one, refusing is safer than minting a known-weak credential.
    pw_admin     = _required_env("SEED_ADMIN_PASSWORD")
    pw_opsmgr    = _required_env("SEED_OPSMGR_PASSWORD")
    pw_sched     = _required_env("SEED_SCHED_PASSWORD")
    pw_comp      = _required_env("SEED_COMP_PASSWORD")
    pw_flightops = _required_env("SEED_FLIGHTOPS_PASSWORD")

    # Company
    company = Company(
        id=str(uuid.uuid4()),
        name_ar="الخطوط الجوية العراقية",
        name_en="Iraqi Airways",
        code="IA",
        icao_code="IAW",
        iata_code="IA",
        country="Iraq",
        primary_color="#1B5E20",
        is_active=True,
    )
    db.add(company)
    await db.flush()
    cid = company.id

    # Users
    users = [
        User(id=str(uuid.uuid4()), email="admin@iraqiairways.iq", hashed_password=get_password_hash(pw_admin),
             name_ar="مدير النظام", name_en="System Admin", role="super_admin", company_id=cid, is_superuser=True),
        User(id=str(uuid.uuid4()), email="supervisor@iraqiairways.iq", hashed_password=get_password_hash(pw_opsmgr),
             name_ar="مدير العمليات", name_en="Operations Manager", role="ops_manager", company_id=cid),
        User(id=str(uuid.uuid4()), email="scheduler@iraqiairways.iq", hashed_password=get_password_hash(pw_sched),
             name_ar="مسؤول الجدولة", name_en="Scheduler", role="scheduler", company_id=cid),
        User(id=str(uuid.uuid4()), email="compliance@iraqiairways.iq", hashed_password=get_password_hash(pw_comp),
             name_ar="ضابط الامتثال", name_en="Compliance Officer", role="compliance_officer", company_id=cid),
        User(id=str(uuid.uuid4()), email="flights@iraqiairways.iq", hashed_password=get_password_hash(pw_flightops),
             name_ar="عمليات الطيران", name_en="Flight Operations", role="flight_ops", company_id=cid),
    ]
    for u in users:
        db.add(u)
    await db.flush()

    # Aircraft
    aircraft_list = [
        Aircraft(id=str(uuid.uuid4()), company_id=cid, aircraft_type="A320", registration="YI-AQY", name="Baghdad", min_crew=4, max_crew=8),
        Aircraft(id=str(uuid.uuid4()), company_id=cid, aircraft_type="B737", registration="YI-AGE", name="Basra", min_crew=4, max_crew=6),
        Aircraft(id=str(uuid.uuid4()), company_id=cid, aircraft_type="B787", registration="YI-ARS", name="Erbil", min_crew=8, max_crew=14),
    ]
    for a in aircraft_list:
        db.add(a)
    await db.flush()

    # Crew members
    crew_data = [
        {"employee_id": "IA-001", "full_name_ar": "سارة أحمد الجابري", "full_name_en": "Sara Ahmed Al-Jaberi",
         "rank": "purser", "base": "BGW", "operation_type": "long_haul", "status": "active",
         "monthly_flight_hours": 42.5, "total_flight_hours": 3240.0},
        {"employee_id": "IA-002", "full_name_ar": "محمد علي الخزاعي", "full_name_en": "Mohammed Ali Al-Khazaei",
         "rank": "senior_crew", "base": "BGW", "operation_type": "short_haul", "status": "in_flight",
         "monthly_flight_hours": 68.0, "total_flight_hours": 5120.5},
        {"employee_id": "IA-003", "full_name_ar": "نور حسين المنصور", "full_name_en": "Noor Hussein Al-Mansour",
         "rank": "cabin_crew", "base": "BGW", "operation_type": "short_haul", "status": "standby",
         "monthly_flight_hours": 15.0, "total_flight_hours": 890.0},
        {"employee_id": "IA-004", "full_name_ar": "كريم صالح العبيدي", "full_name_en": "Kareem Saleh Al-Ubaidi",
         "rank": "chief_purser", "base": "BGW", "operation_type": "both", "status": "blocked",
         "block_reason": "Expired medical certificate", "monthly_flight_hours": 0.0, "total_flight_hours": 8950.0},
        {"employee_id": "IA-005", "full_name_ar": "رنا عبدالله الراوي", "full_name_en": "Rana Abdullah Al-Rawi",
         "rank": "cabin_crew", "base": "BGW", "operation_type": "short_haul", "status": "on_leave",
         "monthly_flight_hours": 0.0, "total_flight_hours": 1230.0},
    ]
    crew_ids = []
    for cd in crew_data:
        crew = Crew(id=str(uuid.uuid4()), company_id=cid, join_date=date(2018, 3, 15),
                    gender="female" if cd["full_name_en"].startswith(("Sara", "Noor", "Rana")) else "male",
                    nationality="Iraqi", aircraft_qualifications='["A320","B737"]', **cd)
        db.add(crew)
        crew_ids.append(crew.id)
    await db.flush()

    # Create crew users for first 2 crew members
    for i, (crew_id, email, password) in enumerate(zip(crew_ids[:2], ["crew1@iraqiairways.iq", "crew2@iraqiairways.iq"], ["crew123", "crew456"])):
        cu = User(id=str(uuid.uuid4()), email=email, hashed_password=get_password_hash(password),
                  name_ar=crew_data[i]["full_name_ar"], name_en=crew_data[i]["full_name_en"],
                  role="crew", company_id=cid, crew_id=crew_id)
        db.add(cu)

    # Documents
    now = datetime.now(timezone.utc).date()
    docs = [
        CrewDocument(id=str(uuid.uuid4()), crew_id=crew_ids[0], document_type="passport",
                     document_number="A12345678", expiry_date=date(2027, 6, 15), issued_by="Iraqi Ministry of Interior"),
        CrewDocument(id=str(uuid.uuid4()), crew_id=crew_ids[0], document_type="medical_certificate",
                     document_number="MED-2024-001", expiry_date=now + timedelta(days=8), issued_by="ICAA"),
        CrewDocument(id=str(uuid.uuid4()), crew_id=crew_ids[3], document_type="medical_certificate",
                     document_number="MED-2023-045", expiry_date=now - timedelta(days=5), issued_by="ICAA"),
    ]
    for d in docs:
        db.add(d)

    # Flights
    base_time = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    flights = [
        Flight(id=str(uuid.uuid4()), flight_number="IA-210", company_id=cid, aircraft_id=aircraft_list[0].id,
               origin_code="BGW", destination_code="DXB", departure_time=base_time,
               arrival_time=base_time + timedelta(hours=3), duration_hours=3.0,
               status="in_air", publish_status="published", crew_required=4),
        Flight(id=str(uuid.uuid4()), flight_number="IA-315", company_id=cid, aircraft_id=aircraft_list[1].id,
               origin_code="BGW", destination_code="AMM", departure_time=base_time + timedelta(hours=6),
               arrival_time=base_time + timedelta(hours=8, minutes=30), duration_hours=2.5,
               status="scheduled", publish_status="published", crew_required=4),
        Flight(id=str(uuid.uuid4()), flight_number="IA-420", company_id=cid, aircraft_id=aircraft_list[2].id,
               origin_code="BGW", destination_code="LHR", departure_time=base_time + timedelta(hours=14),
               arrival_time=base_time + timedelta(hours=21, minutes=30), duration_hours=7.5,
               status="scheduled", publish_status="draft", crew_required=8),
    ]
    for f in flights:
        db.add(f)

    await db.commit()
    print(f"✓ Company: Iraqi Airways (ID: {cid})")
    print(f"✓ Users: {len(users) + 2} created")
    print(f"✓ Crew: {len(crew_data)} members")
    print(f"✓ Aircraft: {len(aircraft_list)} registered")
    print(f"✓ Flights: {len(flights)} created")
    print(f"✓ Documents: {len(docs)} created")
    print("\nSeed completed. Login credentials are defined in this script's source — review it directly.")


async def main():
    async with AsyncSessionLocal() as db:
        await seed(db)


if __name__ == "__main__":
    asyncio.run(main())
