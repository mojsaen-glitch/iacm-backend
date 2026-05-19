#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# IACM Supabase backup — pg_dump wrapper
#
# Usage:
#   $ ./scripts/backup_db.sh                    # → backups/iacm_YYYY-MM-DD_HHMM.sql.gz
#   $ ./scripts/backup_db.sh /path/to/file      # explicit output path
#
# Environment:
#   DATABASE_URL_SYNC   Postgres connection string (preferred — same as Alembic)
#   DATABASE_URL        async URL — converted automatically if SYNC missing
#
# Cron example (daily 02:30 UTC, keep last 30 backups):
#   30 2 * * * cd /opt/iacm/backend && ./scripts/backup_db.sh \
#              && find backups/ -name 'iacm_*.sql.gz' -mtime +30 -delete
#
# Restore (DANGER — overwrites the target DB):
#   $ gunzip -c iacm_2026-05-17_0230.sql.gz | psql "$DATABASE_URL_SYNC"
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BACKEND_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
BACKUP_DIR="${BACKEND_DIR}/backups"
mkdir -p "$BACKUP_DIR"

# Load .env if present (so DATABASE_URL_SYNC resolves outside cron)
if [[ -f "${BACKEND_DIR}/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "${BACKEND_DIR}/.env"; set +a
fi

# Prefer the sync URL (plain postgres://). Fall back by stripping the async driver.
DB_URL="${DATABASE_URL_SYNC:-${DATABASE_URL:-}}"
if [[ -z "$DB_URL" ]]; then
  echo "ERROR: DATABASE_URL_SYNC (or DATABASE_URL) is not set." >&2
  echo "       Export it or add it to backend/.env." >&2
  exit 2
fi
# strip "+asyncpg" / "+psycopg" from SQLAlchemy-style URLs
DB_URL="${DB_URL//+asyncpg/}"
DB_URL="${DB_URL//+psycopg/}"

if ! command -v pg_dump >/dev/null 2>&1; then
  echo "ERROR: pg_dump not installed. Install postgresql-client first." >&2
  echo "       Debian/Ubuntu:  sudo apt install postgresql-client" >&2
  echo "       macOS:          brew install postgresql" >&2
  echo "       Windows:        winget install PostgreSQL.PostgreSQL" >&2
  exit 3
fi

STAMP="$(date -u +'%Y-%m-%d_%H%M')"
OUT="${1:-${BACKUP_DIR}/iacm_${STAMP}.sql.gz}"

echo "→ Dumping IACM database to: $OUT"

# --no-owner so the dump is portable across Supabase projects / locals
# --clean    drops objects before re-creating on restore
# --if-exists pairs with --clean to avoid "object does not exist" noise
pg_dump "$DB_URL" \
  --no-owner --no-privileges \
  --clean --if-exists \
  --format=plain \
  | gzip -9 > "$OUT"

SIZE="$(du -h "$OUT" | cut -f1)"
echo "✓ Backup complete — ${SIZE}"
echo
echo "Keep backups OFF the production server (S3 / B2 / local laptop)."
echo "Test restore quarterly into a scratch DB so you find out about"
echo "corruption BEFORE you need the backup for real."
