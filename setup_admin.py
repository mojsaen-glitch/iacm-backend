# -*- coding: utf-8 -*-
"""
Quick setup: creates company + super admin without interactive prompts.
Edit the values below, then run: python setup_admin.py
"""
import os, uuid
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client
import bcrypt

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
now = datetime.now(timezone.utc).isoformat()

# ── Configure these values ────────────────────────────────
COMPANY_NAME_EN  = "Iraqi Airways"
COMPANY_NAME_AR  = "الخطوط الجوية العراقية"
COMPANY_CODE     = "IA"
COMPANY_ICAO     = "IAW"
COMPANY_COUNTRY  = "Iraq"

ADMIN_EMAIL      = "admin@iacm.iq"
ADMIN_NAME_EN    = "System Admin"
ADMIN_NAME_AR    = "مدير النظام"
ADMIN_PASSWORD   = "Admin@2024"
# ─────────────────────────────────────────────────────────

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
print(f"  Password : {ADMIN_PASSWORD}")
print("=" * 50)
