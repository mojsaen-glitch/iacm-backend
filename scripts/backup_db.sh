#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# IACM — daily EXTERNAL database backup (phase 0).
# Replaces the old local-gzip wrapper: runs on GitHub Actions (never inside the
# serving environment), pg_dump → AES-256 encrypt → manifest+checksums →
# S3-compatible storage. Retention: daily/ kept 30 days, monthly/ kept 12
# months (the 1st-of-month backup is promoted to monthly/).
#
# SECRETS POLICY: this script NEVER echoes credentials. Sensitive values arrive
# via environment variables (names below) and are referenced only.
#
# Required env (NAMES only — values live in GitHub Actions Secrets):
#   DATABASE_BACKUP_URL                postgres:// connection (read-only role
#                                      preferred; pg_dump needs SELECT only)
#   BACKUP_STORAGE_ENDPOINT            S3-compatible endpoint URL (R2/B2/S3)
#   BACKUP_STORAGE_BUCKET              bucket name (private, least-privilege)
#   BACKUP_STORAGE_ACCESS_KEY_ID
#   BACKUP_STORAGE_SECRET_ACCESS_KEY
#   BACKUP_ENCRYPTION_KEY              passphrase for AES-256 (openssl pbkdf2)
#
# Usage:
#   ./backup_db.sh             real backup (needs all env + pg_dump/openssl/aws)
#   ./backup_db.sh --dry-run   validate env presence + print plan only
#                              (CI-safe: touches nothing, needs no tools)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

REQUIRED=(DATABASE_BACKUP_URL BACKUP_STORAGE_ENDPOINT BACKUP_STORAGE_BUCKET
          BACKUP_STORAGE_ACCESS_KEY_ID BACKUP_STORAGE_SECRET_ACCESS_KEY
          BACKUP_ENCRYPTION_KEY)
missing=()
for v in "${REQUIRED[@]}"; do
  [[ -n "${!v:-}" ]] || missing+=("$v")
done
if ((${#missing[@]})); then
  echo "ERROR: missing required env (names only): ${missing[*]}" >&2
  exit 1
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DAY="$(date -u +%d)"
DAILY_KEEP_DAYS=30
MONTHLY_KEEP_DAYS=365

if $DRY_RUN; then
  echo "dry-run OK — required env present (values not shown)."
  echo "plan: pg_dump -Fc → sha256 → openssl aes-256-cbc(pbkdf2 200k) → manifest"
  echo "      → s3://<bucket>/daily/${TS}/ (+ monthly/ promotion on day 01)"
  echo "      → prune daily >${DAILY_KEEP_DAYS}d, monthly >${MONTHLY_KEEP_DAYS}d"
  exit 0
fi

for tool in pg_dump openssl aws sha256sum; do
  command -v "$tool" >/dev/null 2>&1 \
    || { echo "ERROR: required tool not found: $tool" >&2; exit 1; }
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[1/5] pg_dump (custom format)…"
pg_dump "$DATABASE_BACKUP_URL" --format=custom --no-owner --no-privileges \
        --file="$WORKDIR/db.dump"
PLAIN_SHA="$(sha256sum "$WORKDIR/db.dump" | cut -d' ' -f1)"
PLAIN_SIZE="$(stat -c%s "$WORKDIR/db.dump" 2>/dev/null \
              || stat -f%z "$WORKDIR/db.dump")"

echo "[2/5] encrypt (AES-256-CBC, PBKDF2 200k iterations)…"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -in "$WORKDIR/db.dump" -out "$WORKDIR/db.dump.enc" \
        -pass env:BACKUP_ENCRYPTION_KEY
ENC_SHA="$(sha256sum "$WORKDIR/db.dump.enc" | cut -d' ' -f1)"

echo "[3/5] manifest…"
cat > "$WORKDIR/manifest.json" <<EOF
{
  "timestamp_utc": "${TS}",
  "format": "pg_dump_custom + aes-256-cbc(pbkdf2,200000)",
  "pg_dump_version": "$(pg_dump --version | tr -d '\n')",
  "plain_size_bytes": ${PLAIN_SIZE},
  "plain_sha256": "${PLAIN_SHA}",
  "encrypted_sha256": "${ENC_SHA}"
}
EOF

export AWS_ACCESS_KEY_ID="$BACKUP_STORAGE_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$BACKUP_STORAGE_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-auto}"
s3() { aws --endpoint-url "$BACKUP_STORAGE_ENDPOINT" s3 "$@"; }
BASE="s3://${BACKUP_STORAGE_BUCKET}"

echo "[4/5] upload → daily/${TS}/ …"
s3 cp "$WORKDIR/db.dump.enc"   "${BASE}/daily/${TS}/db.dump.enc"   --only-show-errors
s3 cp "$WORKDIR/manifest.json" "${BASE}/daily/${TS}/manifest.json" --only-show-errors
if [[ "$DAY" == "01" ]]; then
  echo "      promoting to monthly/${TS}/ …"
  s3 cp "$WORKDIR/db.dump.enc"   "${BASE}/monthly/${TS}/db.dump.enc"   --only-show-errors
  s3 cp "$WORKDIR/manifest.json" "${BASE}/monthly/${TS}/manifest.json" --only-show-errors
fi

echo "[5/5] retention prune…"
prune() {  # prune <prefix> <keep_days>
  local prefix="$1" keep="$2" cutoff ts_dir
  cutoff="$(date -u -d "-${keep} days" +%Y%m%dT%H%M%SZ 2>/dev/null \
            || date -u -v "-${keep}d" +%Y%m%dT%H%M%SZ)"
  s3 ls "${BASE}/${prefix}/" 2>/dev/null | awk '{print $2}' | tr -d '/' | \
  while read -r ts_dir; do
    [[ -n "$ts_dir" && "$ts_dir" < "$cutoff" ]] || continue
    echo "      pruning ${prefix}/${ts_dir}/"
    s3 rm "${BASE}/${prefix}/${ts_dir}/" --recursive --only-show-errors
  done
}
prune daily   "$DAILY_KEEP_DAYS"
prune monthly "$MONTHLY_KEEP_DAYS"

echo "DONE: backup ${TS} uploaded (plain_sha256=${PLAIN_SHA:0:12}…, ${PLAIN_SIZE} bytes)."
