"""
Clear all data from the IACM Supabase database.
Run from the backend/ directory:
    python clear_data.py
"""
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

TABLES = [
    "assignments", "notifications", "messages", "audit_log",
    "leave_requests", "documents", "flights", "crew",
    "users", "aircraft", "routes", "settings", "companies",
]

print("=" * 50)
print("WARNING: This will delete ALL data from Supabase.")
print("=" * 50)
confirm = input("Type YES to confirm: ").strip()
if confirm != "YES":
    print("Aborted.")
    exit(0)

print()
for table in TABLES:
    try:
        result = sb.table(table).delete().gte("created_at", "1900-01-01").execute()
        count = len(result.data) if result.data else 0
        print(f"[OK] {table:<20} — {count} rows deleted")
    except Exception as e:
        err = str(e)
        if "Could not find the table" in err:
            print(f"[--] {table:<20} — table not found (skipped)")
        else:
            print(f"[!!] {table:<20} — {err[:80]}")

print()
print("Database cleared. Run setup_admin.py to create the first admin user.")
