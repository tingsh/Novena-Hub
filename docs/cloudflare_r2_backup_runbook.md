# Cloudflare R2 backup and recovery: first customer trial

The off-server copy is in the private R2 bucket `novenaplatform-backup`. This is an application-level recovery set, separate from an optional DigitalOcean Droplet snapshot. A set contains PostgreSQL/TimescaleDB, `private_media`, and Mosquitto Dynamic Security state. Environment secrets and the rclone encryption password must be escrowed outside R2 in a restricted password vault. The trial target is a nightly backup with up to 24 hours of data loss after a Droplet loss; measure actual restore time during the first drill before promising an RTO.

## Cloudflare setup

1. Confirm Public Development URL is disabled and no public domain is attached to `novenaplatform-backup`. Use Standard storage.
2. Create an **Account API token** with **Object Read & Write**, scoped to this bucket only. Save the Access Key ID, Secret Access Key, account ID and S3 endpoint privately. Do not paste them in chat, a shell command, Git, or a ticket. Keep bucket-configuration/admin rights off the Droplet token. [Cloudflare token instructions](https://developers.cloudflare.com/r2/api/tokens/).
3. After the first successful upload and restore drill, add a bucket lock for the backup prefix for **35 days** and a lifecycle expiration after **180 days**. The lock prevents object overwrite and deletion during its retention window; the lifecycle bounds storage growth. Retain an account-admin recovery route separately because bucket configuration can be changed by account administrators. [Bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/) and [lifecycle rules](https://developers.cloudflare.com/r2/buckets/object-lifecycles/).

## Droplet setup

Install `rclone` from the Ubuntu repository or a pinned official release. Create `/etc/novena` as `root:root` mode `0700`. Create `/etc/novena/rclone.conf` and `/etc/novena/backup.env` as `root:root` mode `0600`. Use `sudo rclone config --config /etc/novena/rclone.conf` interactively so credentials never appear in shell history. Create two remotes:

- `novena_r2_raw`: type `s3`, provider `Cloudflare`, the bucket-scoped access and secret keys, and the exact S3 endpoint for this Cloudflare account/jurisdiction. Do not use a public R2 URL.
- `novena_r2_crypt`: type `crypt`, remote `novena_r2_raw:novenaplatform-backup/novena-hub`, standard filename encryption and directory-name encryption. Generate a long random encryption password and salt in the interactive wizard. Escrow both in a separate restricted vault. Losing them makes the backups unreadable. `rclone.conf` obfuscation is not encryption of the config file; protect and escrow that file as a secret. [rclone crypt](https://rclone.org/crypt/).

The root-only environment file contains no access keys, only these destinations:

```ini
BACKUP_RCLONE_REMOTE=novena_r2_crypt:postgres
PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE=novena_r2_crypt:private-media
MQTT_DYNSEC_BACKUP_RCLONE_REMOTE=novena_r2_crypt:mqtt-dynsec
```

The example systemd units set `RCLONE_CONFIG=/etc/novena/rclone.conf` and `NOVENA_OFFSITE_REQUIRED=1`, run as root for Docker and the broker-owned JSON, and put local backups under `/var/backups/novena`. A missing off-server destination makes the unit fail instead of reporting a local-only success. Create `/var/backups/novena` as `root:root` mode `0700`. Install root-owned script copies into `/usr/local/sbin` as shown in `docs/production_backup_restore.md`; refresh those copies after each reviewed backup-script update. The database job uses `docker compose exec -T postgres` because the production database hostname is inside Docker; a host-side `pg_dump` cannot use that address. Do not put the backup config, dumps, private archives or broker JSON in the web-served tree.

First run the two jobs manually with `sudo systemctl start novena-postgres-backup.service` and `sudo systemctl start novena-mqtt-backup.service`. Confirm both are `Result=success`, inspect their journal, and list the three encrypted remote prefixes through `sudo rclone --config /etc/novena/rclone.conf lsf novena_r2_crypt:`. The scripts check local archive structure, upload through the crypt remote, then run `rclone cryptcheck`; any failure makes the service fail. Enable the timers only after this first run succeeds. Review the unit results and remote object dates daily during the guided trial; add an independent alert for missed/failed backups before broader pilots.

Local retention is daily 7 days, weekly 35 days and monthly 100 days. The R2 lifecycle retains all off-server copies for 180 days, including individual nightly files. These are not atomic cross-service snapshots: record UTC timestamps and reconcile Gateway claims, releases and MQTT revocations after restore.

## Off-server restore drill

Use a disposable TimescaleDB PostgreSQL container and an isolated, unexposed Docker network. Download one database dump from `novena_r2_crypt:postgres`, one private-media archive, and one Dynamic Security JSON from the same time window into a root-only temporary directory. Verify the dump with `gzip -t` and `pg_restore --list`, the archive with `tar -tzf`, and the JSON shape with the broker backup validator. Restore the database into the disposable instance using the matching PostgreSQL and TimescaleDB versions. Prepare the empty target with `CREATE EXTENSION timescaledb`, call `timescaledb_pre_restore()`, run `pg_restore` without parallel jobs, then call `timescaledb_post_restore()`; Timescale documents this sequence in its [logical backup guide](https://github.com/timescale/Tiger-Data-Docs/blob/main/src/content/docs/deploy/self-hosted/backup-and-restore/logical-backup.mdx).

Check team, Gateway, device, telemetry, and alert row counts; sample one recent device/history query. Extract the private archive into a separate directory and verify referenced report/request files exist. Start a disposable Mosquitto broker with the restored Dynamic Security JSON on a network inaccessible to live Gateways. Check that one currently claimed trial identity has only its serial-scoped ACLs and a released identity is denied. Never point a disposable broker at production MQTT port 8883.

Keep command workers stopped throughout the drill. A restored database may contain pending approvals, commands and outbox rows; follow `docs/governed_remote_control_operations.md` before any worker is started. Verify restore duration and write down the observed recovery point, missing data interval, and any manual corrections. Destroy the disposable containers, network and downloaded plaintext after evidence is recorded. A successful upload or `pg_restore --list` alone does not pass this gate.

For an actual incident, stop Hub writers and Gateway claim/release handling, preserve the broken state, choose one recovery window, and review claims/releases since that window before restoring broker identities. Do not let a snapshot resurrect revoked Gateway credentials. Use the full production procedure in `docs/production_backup_restore.md`.
