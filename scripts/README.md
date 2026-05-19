# Backend Scripts

## `backup_db.sh` — Database backup

Daily backup of the IACM Supabase database (full schema + data).

### Quick start

```bash
cd backend
chmod +x scripts/backup_db.sh
./scripts/backup_db.sh
# → backups/iacm_2026-05-17_0230.sql.gz
```

### Cron — daily backup, retain 30 days

```cron
30 2 * * * cd /opt/iacm/backend && ./scripts/backup_db.sh \
           && find backups/ -name 'iacm_*.sql.gz' -mtime +30 -delete
```

### Restore (DESTRUCTIVE — overwrites the target DB)

```bash
gunzip -c backups/iacm_2026-05-17_0230.sql.gz | psql "$DATABASE_URL_SYNC"
```

### Operational policy

- **Off-server storage**: copy `backups/` to S3 / Backblaze B2 / a laptop
  weekly. A backup on the same box as the DB is no backup.
- **Restore test**: every quarter, run the restore command into a scratch
  Supabase project. You don't find out about corrupted backups until you
  need them.
- **Secrets**: the dump contains the FULL database including hashed
  passwords and tokens. Encrypt at rest if storing externally:
  ```bash
  gpg --symmetric --cipher-algo AES256 iacm_2026-05-17_0230.sql.gz
  ```

### Windows note

The shell script needs Git Bash, WSL, or any bash interpreter. Native
PowerShell users can run the equivalent:

```powershell
$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
pg_dump $env:DATABASE_URL_SYNC `
  --no-owner --no-privileges --clean --if-exists --format=plain `
  | gzip -9 > "backups/iacm_$ts.sql.gz"
```
