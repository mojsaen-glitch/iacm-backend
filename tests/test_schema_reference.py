"""create_tables.sql is the schema REFERENCE for new environments — it must
keep carrying every column the backend actually reads/writes on the CORE
tables. This guard fails whenever a feature adds a column without updating
the reference (the drift that hid passport_number / gd_status for weeks).

Run:  py -m pytest tests/test_schema_reference.py -q
"""
import re
from pathlib import Path

import pytest

SQL = (Path(__file__).resolve().parents[1] / "create_tables.sql").read_text(
    encoding="utf-8")


def _table_block(name: str) -> str:
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {name}\s*\((.*?)\n\);", SQL, re.S)
    assert m, f"table `{name}` missing from create_tables.sql"
    return m.group(1)


# (table, column) pairs the backend code depends on — curated, not exhaustive.
CRITICAL = {
    "users": ["crew_id", "role", "company_id", "is_active",
              "totp_secret", "totp_enabled"],
    "crew": ["passport_number", "operator_company_id", "roster_name",
             "aircraft_qualifications", "max_monthly_hours", "status",
             "block_reason", "rank", "base"],
    "flights": ["publish_status", "aircraft_registration", "duration_hours",
                "estimated_departure_time", "delay_reason_code",
                "delay_updated_by", "cancellation_reason",
                "roster_finalized_status", "roster_finalized_at",
                "roster_finalized_by", "gd_status", "gd_version",
                "operator_company_id",
                "actual_departure_time", "actual_arrival_time",
                "actual_times_updated_by"],
    "assignments": ["assigned_role", "duty_type", "operator_company_id",
                    "acknowledged", "acknowledged_at",
                    "declined", "decline_reason", "declined_at",
                    "admin_confirmed", "admin_confirmed_by",
                    "admin_confirmed_at", "admin_confirm_reason",
                    "is_override", "override_reason"],
    "notifications": ["user_id", "target_user_id", "message_ar", "message_en",
                      "reference_id", "reference_type",
                      "related_flight_id", "related_crew_id",
                      "is_read", "read_at"],
    "audit_log": ["user_id", "user_name", "action", "entity_type",
                  "entity_id", "before_data", "after_data",
                  "is_override", "override_reason", "company_id"],
}


@pytest.mark.parametrize("table", sorted(CRITICAL))
def test_core_table_columns_documented(table):
    block = _table_block(table)
    missing = [c for c in CRITICAL[table]
               if not re.search(rf"^\s*{c}\s", block, re.M)]
    assert not missing, (
        f"create_tables.sql: table `{table}` is missing documented "
        f"column(s): {missing} — update the reference with the migration")


def test_reference_mentions_migrations_dir():
    """New environments must know feature tables live in migrations/."""
    assert "migrations/" in SQL
