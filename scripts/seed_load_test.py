"""Load-test seed generator — Crew Management System.

Generates a realistic large dataset for capacity testing:
  • 3000 crew members (configurable)         • ~100 flights/day for 12 months
  • 6 assignments/flight (operating + DH + standby mix)
  • a slice of documents / standby / blocked crew / audit rows (best-effort)

⚠ SAFETY — this WRITES a lot of data. It refuses to run unless ALL of:
    ALLOW_SEED=1
    SUPABASE_URL / SUPABASE_SERVICE_KEY   (point these at a STAGING project!)
    SEED_COMPANY_ID                       (an existing company row to attach data to)
    SEED_ASSIGNED_BY                      (an existing users.id — assignments.assigned_by FK)
  NEVER run against the production database. All seeded rows are tagged with
  employee_id / flight_number prefix "LT-" so they can be deleted afterwards:
      DELETE FROM assignments WHERE crew_id IN (SELECT id FROM crew WHERE employee_id LIKE 'LT-%');
      DELETE FROM flights WHERE flight_number LIKE 'LT-%';
      DELETE FROM crew   WHERE employee_id  LIKE 'LT-%';

Usage:
    ALLOW_SEED=1 SUPABASE_URL=... SUPABASE_SERVICE_KEY=... \
    SEED_COMPANY_ID=... SEED_ASSIGNED_BY=... \
    venv/Scripts/python scripts/seed_load_test.py
Env knobs: CREW=3000  DAYS=365  FLIGHTS_PER_DAY=100  ASSIGN_PER_FLIGHT=6
"""
import os
import sys
import random
from datetime import datetime, timedelta, timezone

random.seed(42)

CREW = int(os.getenv("CREW", "3000"))
DAYS = int(os.getenv("DAYS", "365"))
FLIGHTS_PER_DAY = int(os.getenv("FLIGHTS_PER_DAY", "100"))
ASSIGN_PER_FLIGHT = int(os.getenv("ASSIGN_PER_FLIGHT", "6"))
BATCH = 500

RANKS = ["captain", "first_officer", "senior_cabin_crew", "cabin_crew",
         "cabin_crew", "cabin_crew", "in_flight_security_officer", "load_sheet_officer"]
BASES = ["BGW", "BSR", "EBL", "NJF"]
AC = ["B737-800", "A320", "A321", "B777-200"]
STATIONS = ["BGW", "BSR", "EBL", "NJF", "DXB", "IST", "AMM", "CAI", "BEY", "MED", "DOH"]


def _need(name):
    v = os.getenv(name)
    if not v:
        sys.exit(f"❌ missing required env {name}")
    return v


def main():
    if os.getenv("ALLOW_SEED") != "1":
        sys.exit("❌ refusing to seed: set ALLOW_SEED=1 (point at STAGING, never prod).")
    url = _need("SUPABASE_URL")
    key = _need("SUPABASE_SERVICE_KEY")
    company_id = _need("SEED_COMPANY_ID")
    assigned_by = _need("SEED_ASSIGNED_BY")
    host = url.split("//")[-1].split(".")[0]
    print(f"→ target Supabase project: {host}   company={company_id}")
    print(f"→ generating {CREW} crew · {DAYS}d × {FLIGHTS_PER_DAY} flights · "
          f"{ASSIGN_PER_FLIGHT} assignments/flight")

    from supabase import create_client
    sb = create_client(url, key)

    def insert(table, rows):
        for i in range(0, len(rows), BATCH):
            sb.table(table).insert(rows[i:i + BATCH]).execute()

    def best_effort(table, rows, label):
        try:
            insert(table, rows)
            print(f"   ✓ {label}: {len(rows)}")
        except Exception as e:  # schema differs → skip, don't abort the core seed
            print(f"   ⚠ {label} skipped ({str(e)[:80]})")

    now = datetime.now(timezone.utc)

    # ── Crew ────────────────────────────────────────────────────────────────
    crew_ids = []
    crew_rows = []
    for i in range(CREW):
        cid = f"LT-crew-{i:05d}"
        crew_ids.append(cid)
        blocked = (i % 20 == 0)  # ~5% grounded
        crew_rows.append({
            "id": cid,
            "employee_id": f"LT-{i:05d}",
            "full_name_en": f"LoadTest Crew {i}",
            "full_name_ar": f"طاقم اختبار {i}",
            "roster_name": f"LT{i}",
            "company_id": company_id,
            "base": random.choice(BASES),
            "rank": random.choice(RANKS),
            "status": "blocked" if blocked else "active",
            "block_reason": "load-test medical hold" if blocked else None,
            "blocked_on": now.isoformat() if blocked else None,
            "aircraft_qualifications": random.choice(AC),
            "date_of_birth": f"{random.randint(1965, 1998)}-0{random.randint(1,9)}-15",
            "max_monthly_hours": 100,
        })
    insert("crew", crew_rows)
    print(f"   ✓ crew: {len(crew_rows)}")

    # ── Flights + assignments (per day) ──────────────────────────────────────
    total_flights = total_assign = 0
    for d in range(DAYS):
        day = now - timedelta(days=DAYS - d)
        flight_rows, fids = [], []
        for f in range(FLIGHTS_PER_DAY):
            fid = f"LT-flt-{d:03d}-{f:03d}"
            fids.append(fid)
            o, dest = random.sample(STATIONS, 2)
            dep = day.replace(hour=random.randint(0, 22), minute=0, second=0, microsecond=0)
            dur = round(random.uniform(1.0, 6.0), 1)
            flight_rows.append({
                "id": fid,
                "flight_number": f"LT-{d:03d}{f:03d}",
                "company_id": company_id,
                "origin_code": o, "destination_code": dest,
                "departure_time": dep.isoformat(),
                "arrival_time": (dep + timedelta(hours=dur)).isoformat(),
                "duration_hours": dur,
                "aircraft_type": random.choice(AC),
                "status": "scheduled",
                "publish_status": "published",
            })
        insert("flights", flight_rows)
        total_flights += len(flight_rows)

        asg_rows = []
        for fid in fids:
            for c in random.sample(crew_ids, ASSIGN_PER_FLIGHT):
                r = random.random()
                duty = "operating" if r < 0.8 else ("deadhead" if r < 0.92 else "standby")
                asg_rows.append({
                    "flight_id": fid, "crew_id": c, "assigned_by": assigned_by,
                    "duty_type": duty, "assignment_type": "regular",
                })
        insert("assignments", asg_rows)
        total_assign += len(asg_rows)
        if d % 30 == 0:
            print(f"   … day {d}/{DAYS}  flights={total_flights} assignments={total_assign}")
    print(f"   ✓ flights: {total_flights}   assignments: {total_assign}")

    # ── Best-effort extras (schema-tolerant) ─────────────────────────────────
    docs = [{"crew_id": c, "doc_type": "license",
             "expiry_date": (now + timedelta(days=random.randint(-30, 365))).date().isoformat()}
            for c in crew_ids[:1000]]
    best_effort("documents", docs, "documents")

    standby = [{"company_id": company_id, "crew_id": c, "status": "available",
                "start_time": now.isoformat(), "end_time": (now + timedelta(hours=8)).isoformat()}
               for c in crew_ids[:300]]
    best_effort("standby_assignments", standby, "standby")

    audit = [{"actor_id": assigned_by, "action": "load_test_seed",
              "entity_type": "crew", "entity_id": c} for c in crew_ids[:500]]
    best_effort("audit_log", audit, "audit_log")

    print("✅ seed complete.")


if __name__ == "__main__":
    main()
