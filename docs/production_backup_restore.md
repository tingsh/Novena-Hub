# Novena Production Backup And Restore Guide

> For remote-control deployments, keep command workers stopped after restore and run the governed-control recovery reset in `docs/governed_remote_control_operations.md`. Restored approvals, outbox rows and commands must never execute.

This guide is for the first low-cost production deployment where Novena runs Postgres/TimescaleDB on the same VPS as the app.

## Mental Model

A backup is a copy of the database and private files, including impact reports and equipment request manuals/nameplates, at a point in time. A restore is the act of loading those copies again.

Device Template Library drafts, saved revision references and community review status are PostgreSQL rows. Back up the database before `devices.0037`–`0038`; the retirement migration preserves assigned devices but removes unsafe public templates from new selection. A restore to a pre-migration snapshot loses later drafts and saves. See `docs/product/device_template_library.md` for the smoke test after restore.

The important part is the restore. Until we have restored a backup once, we only have a file that we hope is useful.

## What The Scripts Do

- `deploy/backups/backup_postgres.sh` creates a compressed custom-format `pg_dump`.
- `deploy/backups/backup_private_media.sh` creates an atomic archive of all confidential files under `private_media/`.
- `deploy/backups/restore_postgres.sh` restores one selected backup into a target database.
- `deploy/backups/backup_retention.sh` applies the same daily, weekly, and monthly retention to both backup sets.
- The systemd examples run the backup nightly.

The database backup reads `DATABASE_URL` from `deploy/env/production.env` by default. `BACKUP_RCLONE_REMOTE` uploads database backups. `PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE` separately uploads confidential report archives; use a private bucket and restricted credentials.

## First Setup

On the VPS:

```bash
sudo apt-get update
sudo apt-get install -y postgresql-client gzip rclone
cd /opt/novena/Novena-Hub
chmod +x deploy/backups/*.sh
```

Create the first backup manually:

```bash
NOVENA_ROOT=/opt/novena/Novena-Hub ./deploy/backups/backup_postgres.sh
NOVENA_ROOT=/opt/novena/Novena-Hub ./deploy/backups/backup_private_media.sh
```

If using Cloudflare R2 or Backblaze B2, configure `rclone` and add this to `deploy/env/production.env`:

```env
BACKUP_RCLONE_REMOTE=novena-r2:novena-production-backups/postgres
PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE=novena-r2:novena-production-backups/private-media
```

## Restore Drill

Do the first restore into a disposable database, not production.

```bash
createdb novena_restore_test
RESTORE_DATABASE_URL=postgresql://novena:<password>@localhost:5432/novena_restore_test \
BACKUP_FILE=/opt/novena/Novena-Hub/backups/postgres/daily/<backup-file>.dump.gz \
RESTORE_CONFIRM=restore-now \
./deploy/backups/restore_postgres.sh
```

Then inspect the restored database:

```bash
psql postgresql://novena:<password>@localhost:5432/novena_restore_test
```

Check that important tables exist:

```sql
\dt
select count(*) from teams_team;
select count(*) from devices_gateway;
select count(*) from telemetry_telemetrydata;
select count(*) from impact_impactreport;
```

Restore private files into a disposable directory and compare them with restored `impact_impactreport.private_file_name` and `devices_equipmenttemplaterequest` file rows:

```bash
mkdir -p /tmp/novena-private-media-restore
tar -xzf /opt/novena/Novena-Hub/backups/private_media/daily/<backup-file>.tar.gz \
  -C /tmp/novena-private-media-restore
find /tmp/novena-private-media-restore -type f -name '*.pdf'
```

Do not extract an unverified archive over the live directory. For a production recovery, stop web and Celery, restore into a new directory, verify filenames against the restored database, then atomically switch the directory into place. If reports use S3 instead of the local filesystem, enable bucket versioning and private cross-region or cross-account backup rather than this local archive.

Drop the disposable database only after the restore is verified:

```bash
dropdb novena_restore_test
```

## Nightly Timer

Copy the examples:

```bash
sudo cp deploy/systemd/novena-postgres-backup.service.example /etc/systemd/system/novena-postgres-backup.service
sudo cp deploy/systemd/novena-postgres-backup.timer.example /etc/systemd/system/novena-postgres-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now novena-postgres-backup.timer
```

Check timer status:

```bash
systemctl list-timers novena-postgres-backup.timer
journalctl -u novena-postgres-backup.service -n 100 --no-pager
```

## If Backup Fails

1. Check disk space: `df -h`.
2. Check database connectivity from the server.
3. Run the backup script manually and read the first error.
4. Check `rclone` remote credentials if upload failed.
5. Do not invite more pilot customers until backup is healthy again.

## Restore Into Production

Only restore production when data is corrupted or a migration caused irreversible damage. A restore rewinds the database to the backup time, so newer customer data may be lost.

Before production restore:

1. Stop `web`, `celery-worker`, `celery-beat`, and `mqtt-consumer`.
2. Take one final backup of the current broken state.
3. Restore the selected database backup with `RESTORE_CONFIRM=restore-now`.
4. Restore the matching private-report archive into a new directory and verify it before switching it live.
5. Run migrations only if the code version requires them.
6. Start services and run `python manage.py production_readiness_check`.
