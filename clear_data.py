"""
Clear all data from the IACM Supabase database.
Run from the backend/ directory:
    python clear_data.py --dry-run        # show what WOULD be deleted
    python clear_data.py --confirm DELETE-ALL-DATA   # actually delete

Refuses to run when SUPABASE_URL points at a production-looking host unless
the env var ALLOW_PROD_WIPE=1 is also set.
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in the environment.")

TABLES = [
    "assignments", "notifications", "messages", "audit_log",
    "leave_requests", "documents", "flights", "crew",
    "users", "aircraft", "routes", "settings", "companies",
]

CONFIRM_PHRASE = "DELETE-ALL-DATA"

parser = argparse.ArgumentParser(description="Wipe all IACM tables.")
parser.add_argument("--dry-run", action="store_true",
                    help="Count rows but do not delete anything.")
parser.add_argument("--confirm", default="",
                    help=f"Pass {CONFIRM_PHRASE!r} to actually delete.")
args = parser.parse_args()

# Refuse silently in CI / non-interactive runs unless explicitly confirmed
if not args.dry_run and args.confirm != CONFIRM_PHRASE:
    sys.exit(f"Refusing to delete. Re-run with --dry-run or --confirm {CONFIRM_PHRASE}")

# Production guard — supabase.co is the managed-prod hostname
is_prod_host = "supabase.co" in SUPABASE_URL.lower()
if is_prod_host and not args.dry_run and os.environ.get("ALLOW_PROD_WIPE") != "1":
    sys.exit(
        f"REFUSED: {SUPABASE_URL} looks like production.\n"
        "Set ALLOW_PROD_WIPE=1 in the environment if you really mean it."
    )

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

print("=" * 60)
print(f"  Target  : {SUPABASE_URL}")
print(f"  Mode    : {'DRY RUN' if args.dry_run else 'DELETE'}")
print(f"  Time    : {datetime.now(timezone.utc).isoformat()}")
print("=" * 60)
print()

total = 0
for table in TABLES:
    try:
        if args.dry_run:
            res = sb.table(table).select("id", count="exact").limit(1).execute()
            n = getattr(res, "count", None) or len(res.data or [])
            print(f"[DRY] {table:<20} — {n} rows would be deleted")
        else:
            result = sb.table(table).delete().gte("created_at", "1900-01-01").execute()
            n = len(result.data) if result.data else 0
            print(f"[OK]  {table:<20} — {n} rows deleted")
        total += n
    except Exception as e:
        err = str(e)
        if "Could not find the table" in err:
            print(f"[--]  {table:<20} — table not found (skipped)")
        else:
            print(f"[!!]  {table:<20} — {err[:80]}")

print()
print(f"Total rows {'simulated' if args.dry_run else 'deleted'}: {total}")
if not args.dry_run:
    print("Run setup_admin.py to create the first admin user.")
