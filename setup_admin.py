# -*- coding: utf-8 -*-
"""
Quick setup: creates company + super admin without interactive prompts.
Edit the values below, then run: python setup_admin.py
"""
import os, re, sys, uuid
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client
import bcrypt

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
now = datetime.now(timezone.utc).isoformat()

# ── Configure these values ────────────────────────────────
COMPANY_NAME_EN  = os.environ.get("ADMIN_COMPANY_NAME_EN", "Iraqi Airways")
COMPANY_NAME_AR  = os.environ.get("ADMIN_COMPANY_NAME_AR", "الخطوط الجوية العراقية")
COMPANY_CODE     = os.environ.get("ADMIN_COMPANY_CODE", "IA")
COMPANY_ICAO     = os.environ.get("ADMIN_COMPANY_ICAO", "IAW")
COMPANY_COUNTRY  = os.environ.get("ADMIN_COMPANY_COUNTRY", "Iraq")

ADMIN_EMAIL      = os.environ.get("ADMIN_EMAIL", "admin@iacm.iq")
ADMIN_NAME_EN    = os.environ.get("ADMIN_NAME_EN", "System Admin")
ADMIN_NAME_AR    = os.environ.get("ADMIN_NAME_AR", "مدير النظام")
ADMIN_PASSWORD   = os.environ.get("ADMIN_PASSWORD")
# ─────────────────────────────────────────────────────────

if not ADMIN_PASSWORD:
    sys.exit(
        "ERROR: ADMIN_PASSWORD env var is required.\n"
        "Set it before running, e.g.:\n"
        "  $env:ADMIN_PASSWORD = 'YourStrongP@ssw0rd!'   # PowerShell\n"
        "  export ADMIN_PASSWORD='YourStrongP@ssw0rd!'   # bash"
    )

# Enforce a minimum password policy: 12+ chars, upper + lower + digit + symbol
_pw_ok = (
    len(ADMIN_PASSWORD) >= 12
    and re.search(r"[A-Z]", ADMIN_PASSWORD)
    and re.search(r"[a-z]", ADMIN_PASSWORD)
    and re.search(r"\d", ADMIN_PASSWORD)
    and re.search(r"[^A-Za-z0-9]", ADMIN_PASSWORD)
)
if not _pw_ok:
    sys.exit(
        "ERROR: ADMIN_PASSWORD must be at least 12 characters and contain "
        "upper-case, lower-case, digit, and symbol."
    )

def hash_pw(p):
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

company_id = str(uuid.uuid4())
user_id    = str(uuid.uuid4())

print("Creating company...")
sb.table("companies").insert({
    "id": company_id, "name_en": COMPANY_NAME_EN, "name_ar": COMPANY_NAME_AR,
    "code": COMPANY_CODE, "icao_code": COMPANY_ICAO, "iata_code": COMPANY_CODE,
    "country": COMPANY_COUNTRY, "primary_color": "#1B5E20",
    "is_active": True, "created_at": now, "updated_at": now,
}).execute()
print(f"  [OK] Company: {COMPANY_NAME_EN}  (ID: {company_id})")

print("Creating admin user...")
sb.table("users").insert({
    "id": user_id, "email": ADMIN_EMAIL,
    "hashed_password": hash_pw(ADMIN_PASSWORD),
    "name_en": ADMIN_NAME_EN, "name_ar": ADMIN_NAME_AR,
    "role": "super_admin", "company_id": company_id,
    "is_active": True, "is_superuser": True,
    "created_at": now, "updated_at": now,
}).execute()
print(f"  [OK] Admin: {ADMIN_EMAIL}")

print()
print("=" * 50)
print("  System ready!")
print("=" * 50)
print(f"  API Docs : http://localhost:8000/api/docs")
print(f"  Email    : {ADMIN_EMAIL}")
print(f"  Password : (not printed for security; use the value from ADMIN_PASSWORD env var)")
print("=" * 50)
