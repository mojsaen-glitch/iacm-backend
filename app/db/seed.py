"""
Seed script - runs once to populate the database with initial data.
Usage: python -m app.db.seed
"""
import asyncio
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


async def seed(db: AsyncSession):
    print("Seeding database...")

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
        User(id=str(uuid.uuid4()), email="admin@iraqiairways.iq", hashed_password=get_password_hash("admin123"),
             name_ar="مدير النظام", name_en="System Admin", role="super_admin", company_id=cid, is_superuser=True),
        User(id=str(uuid.uuid4()), email="supervisor@iraqiairways.iq", hashed_password=get_password_hash("super123"),
             name_ar="مدير العمليات", name_en="Operations Manager", role="ops_manager", company_id=cid),
        User(id=str(uuid.uuid4()), email="scheduler@iraqiairways.iq", hashed_password=get_password_hash("sched123"),
             name_ar="مسؤول الجدولة", name_en="Scheduler", role="scheduler", company_id=cid),
        User(id=str(uuid.uuid4()), email="compliance@iraqiairways.iq", hashed_password=get_password_hash("comp123"),
             name_ar="ضابط الامتثال", name_en="Compliance Officer", role="compliance_officer", company_id=cid),
        User(id=str(uuid.uuid4()), email="flights@iraqiairways.iq", hashed_password=get_password_hash("flights123"),
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
    print("\nLogin credentials:")
    print("  admin@iraqiairways.iq / admin123")
    print("  supervisor@iraqiairways.iq / super123")
    print("  scheduler@iraqiairways.iq / sched123")
    print("  crew1@iraqiairways.iq / crew123")


async def main():
    async with AsyncSessionLocal() as db:
        await seed(db)


if __name__ == "__main__":
    asyncio.run(main())
