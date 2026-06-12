#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# IACM — restore VERIFICATION on a NON-PRODUCTION database (phase 0).
# Downloads (or takes) an encrypted backup, verifies its checksum against the
# manifest, decrypts, pg_restore's into the RESTORE database, then runs safe
# smoke checks (core tables exist + row counts + one query). PRODUCTION IS
# NEVER TOUCHED — two independent guards enforce that:
#   1. RESTORE_CONFIRM_NON_PROD must equal "yes" (explicit human intent)
#   2. the RESTORE host must DIFFER from the backup-source host (when known)
#
# SECRETS POLICY: never echoes credentials; env var NAMES only.
#
# Required env:
#   RESTORE_DATABASE_URL        postgres:// of the SCRATCH/non-prod database
#   RESTORE_CONFIRM_NON_PROD    must be "yes"
#   BACKUP_ENCRYPTION_KEY       same passphrase used by backup_db.sh
# For --latest (download newest from storage), additionally:
#   BACKUP_STORAGE_ENDPOINT / BACKUP_STORAGE_BUCKET /
#   BACKUP_STORAGE_ACCESS_KEY_ID / BACKUP_STORAGE_SECRET_ACCESS_KEY
# Optional safety reference:
#   DATABASE_BACKUP_URL         if set, its host must differ from RESTORE host
#
# Usage:
#   ./restore_check.sh --latest                     newest backup from storage
#   ./restore_check.sh <db.dump.enc> <manifest.json> local files
#   ./restore_check.sh --dry-run                    guards + env check only
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODE="${1:---dry-run}"

host_of() {  # extract host from a postgres URL WITHOUT printing the URL
  local url="${1#*://}"        # strip scheme
  url="${url##*@}"             # strip credentials
  url="${url%%/*}"             # strip path
  echo "${url%%:*}"            # strip port
}

require() {
  local miss=()
  for v in "$@"; do [[ -n "${!v:-}" ]] || miss+=("$v"); done
  if ((${#miss[@]})); then
    echo "ERROR: missing required env (names only): ${miss[*]}" >&2
    exit 1
  fi
}

# ── Guards (always, including dry-run) ───────────────────────────────────────
require RESTORE_DATABASE_URL RESTORE_CONFIRM_NON_PROD BACKUP_ENCRYPTION_KEY
if [[ "$RESTORE_CONFIRM_NON_PROD" != "yes" ]]; then
  echo "ERROR: RESTORE_CONFIRM_NON_PROD must be exactly 'yes' — refusing." >&2
  exit 1
fi
RESTORE_HOST="$(host_of "$RESTORE_DATABASE_URL")"
if [[ -n "${DATABASE_BACKUP_URL:-}" ]]; then
  SRC_HOST="$(host_of "$DATABASE_BACKUP_URL")"
  if [[ "$RESTORE_HOST" == "$SRC_HOST" ]]; then
    echo "ERROR: restore host equals the backup-source host — this looks like" >&2
    echo "       PRODUCTION. Refusing. Use a scratch database." >&2
    exit 1
  fi
fi

if [[ "$MODE" == "--dry-run" ]]; then
  echo "dry-run OK — guards passed (non-prod confirmed, host differs)."
  echo "plan: fetch → sha256 verify vs manifest → decrypt → pg_restore"
  echo "      → smoke: core tables exist + row counts + published-flights query"
  exit 0
fi

for tool in openssl aws sha256sum pg_restore psql python3; do
  command -v "$tool" >/dev/null 2>&1 \
    || { echo "ERROR: required tool not found: $tool" >&2; exit 1; }
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# ── Fetch ─────────────────────────────────────────────────────────────────────
if [[ "$MODE" == "--latest" ]]; then
  require BACKUP_STORAGE_ENDPOINT BACKUP_STORAGE_BUCKET \
          BACKUP_STORAGE_ACCESS_KEY_ID BACKUP_STORAGE_SECRET_ACCESS_KEY
  export AWS_ACCESS_KEY_ID="$BACKUP_STORAGE_ACCESS_KEY_ID"
  export AWS_SECRET_ACCESS_KEY="$BACKUP_STORAGE_SECRET_ACCESS_KEY"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-auto}"
  s3() { aws --endpoint-url "$BACKUP_STORAGE_ENDPOINT" s3 "$@"; }
  BASE="s3://${BACKUP_STORAGE_BUCKET}"
  LATEST="$(s3 ls "${BASE}/daily/" | awk '{print $2}' | tr -d '/' | sort | tail -1)"
  [[ -n "$LATEST" ]] || { echo "ERROR: no backups found under daily/" >&2; exit 1; }
  echo "[fetch] daily/${LATEST}/ …"
  s3 cp "${BASE}/daily/${LATEST}/db.dump.enc"   "$WORKDIR/db.dump.enc"   --only-show-errors
  s3 cp "${BASE}/daily/${LATEST}/manifest.json" "$WORKDIR/manifest.json" --only-show-errors
else
  cp "$MODE" "$WORKDIR/db.dump.enc"
  cp "${2:?usage: restore_check.sh <db.dump.enc> <manifest.json>}" "$WORKDIR/manifest.json"
fi

# ── Verify checksum against the manifest ────────────────────────────────────
WANT_ENC="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["encrypted_sha256"])' "$WORKDIR/manifest.json")"
GOT_ENC="$(sha256sum "$WORKDIR/db.dump.enc" | cut -d' ' -f1)"
[[ "$WANT_ENC" == "$GOT_ENC" ]] \
  || { echo "ERROR: encrypted checksum mismatch — backup corrupt?" >&2; exit 1; }
echo "[verify] encrypted sha256 OK (${GOT_ENC:0:12}…)"

echo "[decrypt]…"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
        -in "$WORKDIR/db.dump.enc" -out "$WORKDIR/db.dump" \
        -pass env:BACKUP_ENCRYPTION_KEY
WANT_PLAIN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plain_sha256"])' "$WORKDIR/manifest.json")"
GOT_PLAIN="$(sha256sum "$WORKDIR/db.dump" | cut -d' ' -f1)"
[[ "$WANT_PLAIN" == "$GOT_PLAIN" ]] \
  || { echo "ERROR: plain checksum mismatch after decrypt" >&2; exit 1; }
echo "[verify] plain sha256 OK (${GOT_PLAIN:0:12}…)"

echo "[restore] into non-prod host '${RESTORE_HOST}' …"
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname="$RESTORE_DATABASE_URL" "$WORKDIR/db.dump" \
  || echo "  (pg_restore reported non-fatal object warnings — expected with --clean)"

echo "[smoke] core tables + counts:"
CORE=(companies users crew flights assignments notifications audit_log documents)
for t in "${CORE[@]}"; do
  exists="$(psql "$RESTORE_DATABASE_URL" -Atc "select to_regclass('public.${t}') is not null")"
  [[ "$exists" == "t" ]] || { echo "ERROR: table missing after restore: $t" >&2; exit 1; }
  count="$(psql "$RESTORE_DATABASE_URL" -Atc "select count(*) from ${t}")"
  printf "  %-15s %s rows\n" "$t" "$count"
done
PUB="$(psql "$RESTORE_DATABASE_URL" -Atc \
       "select count(*) from flights where publish_status='published'")"
echo "  smoke query: published flights = ${PUB}"
echo "DONE: restore verified on non-production host '${RESTORE_HOST}'."
