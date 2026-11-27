# Novena Production Backup And Restore Guide

> For remote-control deployments, keep command workers stopped after restore and run the governed-control recovery reset in `docs/governed_remote_control_operations.md`. Restored approvals, outbox rows and commands must never execute.

This guide is for the first low-cost production deployment where Novena runs Postgres/TimescaleDB on the same VPS as the app. For the DigitalOcean trial's encrypted Cloudflare R2 setup, follow `docs/cloudflare_r2_backup_runbook.md`.

## Mental Model

A backup must cover the database, private files (including impact reports and equipment request manuals/nameplates), and Mosquitto Dynamic Security state at `deploy/mosquitto/data/dynamic-security.json`. The broker file holds the live per-Gateway clients and roles; restoring the database alone does not restore those identities. Production environment signing keys and passwords require a separate restricted secret-vault recovery procedure.

Device Template Library drafts, saved revision references and community review status are PostgreSQL rows. Back up the database before `devices.0037`–`0038`; the retirement migration preserves assigned devices but removes unsafe public templates from new selection. A restore to a pre-migration snapshot loses later drafts and saves. See `docs/product/device_template_library.md` for the smoke test after restore.

The important part is the restore. Until we have restored a backup once, we only have a file that we hope is useful.

## What The Scripts Do

- `deploy/backups/backup_postgres.sh` creates a compressed custom-format `pg_dump`.
- `deploy/backups/backup_private_media.sh` creates an atomic archive of all confidential files under `private_media/`.
- `deploy/backups/backup_mqtt_dynsec.sh` validates and atomically saves the live Mosquitto Dynamic Security JSON with root-only permissions, local retention, and optional off-server upload.
- `deploy/backups/restore_postgres.sh` restores one selected backup into an explicitly named, empty target database. It creates the `novena` role if the target cluster lacks it and runs TimescaleDB's pre/post-restore steps.
- `deploy/backups/backup_retention.sh` applies the same daily, weekly, and monthly retention to both backup sets.
- The systemd examples run database/media and broker-state backups nightly as root, using root-owned installed script copies.

**Open pre-customer gate:** install all three backup jobs on the public server, verify encrypted off-server upload, and restore into disposable database, file and broker environments before customers are invited. Do not treat a Droplet snapshot alone as a complete application-level restore plan. Broker backups contain sensitive credential material.

In direct mode, the database backup reads `DATABASE_URL` from `deploy/env/production.env`. The production systemd unit uses Docker Compose mode, so it does not source that file. `BACKUP_RCLONE_REMOTE`, `PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE`, and `MQTT_DYNSEC_BACKUP_RCLONE_REMOTE` must point to an `rclone crypt` remote for off-server uploads; each upload is checked with `rclone cryptcheck`.

## First Setup

On the DigitalOcean trial Droplet, use the R2 runbook for root-only configuration, crypt remote, three backup destinations, manual first run and timer installation. Its jobs write under `/var/backups/novena` and run as root because Docker access and the broker-owned state require it. Other deployments may use the scripts in direct database mode with `DATABASE_URL`, but must still configure an encrypted off-server remote and prove a restore. The database/media and broker timers are close in time, but not an atomic snapshot: choose matching timestamps and reconcile later claims/releases during recovery.

## Restore Drill

Do the first restore into a disposable TimescaleDB container, not production. Follow the R2 runbook for download and use the same PostgreSQL and TimescaleDB versions as the source. The test found that TimescaleDB's `bgw_job.owner` data references the `novena` database role even with `pg_restore --no-owner`. The restore script now creates that role before loading the archive, then runs TimescaleDB's required pre/post-restore calls. Connect as a target-cluster administrator with permission to create roles and the extension; set `RESTORE_DATABASE_URL` explicitly and `RESTORE_CONFIRM=restore-now`. The script refuses a target with existing public tables. Inspect team, Gateway, telemetry and impact-report rows in the restored database.

Restore private files into a disposable directory and compare them with restored `impact_impactreport.private_file_name` and `devices_equipmenttemplaterequest` file rows:

```bash
mkdir -p /tmp/novena-private-media-restore
tar -xzf /root/novena-restore-drill/<backup-file>.tar.gz -C /tmp/novena-private-media-restore
find /tmp/novena-private-media-restore -type f
```

Do not extract an unverified archive over the live directory. For a production recovery, stop web and Celery, restore into a new directory, verify filenames against the restored database, then atomically switch the directory into place. If reports use S3 instead of the local filesystem, enable bucket versioning and private cross-region or cross-account backup rather than this local archive.

For the broker drill, choose a broker backup from the same recovery window as the database backup. Copy it into a root-owned disposable directory, validate its JSON, and start an isolated Mosquitto instance with the production Dynamic Security plugin configuration pointed at that copy. Confirm that a currently claimed test Gateway identity connects, a released Gateway identity is rejected, and the roles/ACLs match the restored database. Never test by replacing the live broker file. Before a real production restore, stop the broker and Hub claim/release workers, preserve the current state, compare all claims/releases since the selected snapshot, and revoke stale credentials before allowing Gateway connections.

Destroy the isolated drill container and plaintext working files only after verification and recording evidence.

## Nightly Timer

Copy the examples:

```bash
sudo install -o root -g root -m 0700 deploy/backups/backup_postgres.sh /usr/local/sbin/novena-backup-postgres
sudo install -o root -g root -m 0700 deploy/backups/backup_private_media.sh /usr/local/sbin/novena-backup-private-media
sudo install -o root -g root -m 0700 deploy/backups/backup_retention.sh /usr/local/sbin/novena-backup-retention
sudo cp deploy/systemd/novena-postgres-backup.service.example /etc/systemd/system/novena-postgres-backup.service
sudo cp deploy/systemd/novena-postgres-backup.timer.example /etc/systemd/system/novena-postgres-backup.timer
sudo install -o root -g root -m 0700 deploy/backups/backup_mqtt_dynsec.sh /usr/local/sbin/novena-backup-mqtt-dynsec
sudo cp deploy/systemd/novena-mqtt-backup.service.example /etc/systemd/system/novena-mqtt-backup.service
sudo cp deploy/systemd/novena-mqtt-backup.timer.example /etc/systemd/system/novena-mqtt-backup.timer
sudo systemctl daemon-reload
```

Run both services manually and complete the isolated off-server restore drill before enabling the timers. After the drill passes, run `sudo systemctl enable --now novena-postgres-backup.timer novena-mqtt-backup.timer`.

Check timer status:

```bash
systemctl list-timers novena-postgres-backup.timer novena-mqtt-backup.timer
journalctl -u novena-postgres-backup.service -n 100 --no-pager
journalctl -u novena-mqtt-backup.service -n 100 --no-pager
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
3. Preserve the old database, create an empty target database, and restore the selected backup with an explicit `RESTORE_DATABASE_URL` and `RESTORE_CONFIRM=restore-now`. The restore script refuses a populated target and never falls back to the live app's `DATABASE_URL`. Confirm the target role and application privileges before starting Hub.
4. Restore the matching private-report archive into a new directory and verify it before switching it live.
5. Restore the matching Dynamic Security state using a rehearsed broker recovery procedure; validate serial-scoped roles before reconnecting Gateways. Do not simply replay an older broker file over a running broker or reinstate credentials for Gateways released after that snapshot.
6. Run migrations only if the code version requires them.
7. Start services, run `python manage.py production_readiness_check` and the full read-only Timescale policy check, then smoke-test one Gateway's MQTT identity, telemetry and release state.
