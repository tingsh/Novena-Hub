# Novena Production Readiness Kit

Last reviewed: 2026-08-17

This guide documents the repo-backed production assets and checks for Novena Hub. Use it together with `docs/production_deployment_plan.md`.

The deployment plan answers: "How do I set up the VPS and public cloud/DNS environment?"

This readiness kit answers: "What files, checks, env vars, backups, and smoke tests prove the repo is ready to run there?"

## Current CTO Verdict

The July readiness kit is still relevant, but the August codebase has raised the bar. The platform now includes managed Gateway hardening, serial-scoped MQTT identity, signed Guided Setup config delivery, governed remote-control flows, Business Impact private reports, customizable Command Center assets, and stricter Stripe webhook expectations.

So the core deployment model stays the same:

```text
single VPS + Docker Compose + host Nginx + Cloudflare DNS
```

But the go-live checks are stricter than the original July version.

## Current Architecture Summary

Novena Platform has two runtime parts:

- Novena Hub: Django SaaS app for onboarding, dashboards, telemetry, alerts, automations, maintenance, billing, AI, SES email, WhatsApp, Business Impact reports, and governed control.
- Novena Gateway: Python edge runtime on Raspberry Pi CM4-class hardware. It connects field devices and sends MQTT telemetry to Hub.

The first production target remains a lean VPS deployment:

- Host Nginx for HTTPS and WebSocket reverse proxy.
- Django ASGI web app through Gunicorn/Uvicorn.
- PostgreSQL with TimescaleDB for relational and time-series data.
- Redis for cache, Celery, and Channels.
- Celery worker for background jobs.
- Celery Beat for scheduled dispatch/recovery jobs.
- MQTT consumer for Gateway telemetry/logs/attributes/RPC responses.
- Mosquitto with Dynamic Security for per-Gateway credentials.
- Local filesystem media for the first VPS deployment, including private Business Impact reports.

Teacher note: ASGI is the Python web interface that supports both normal HTTP requests and WebSockets. Novena uses WebSockets on device detail pages, so production must run the ASGI app through an ASGI-capable worker.

Telemetry retention has two layers. Subscription plans cap customer-visible chart/API/export history, while TimescaleDB enforces the global physical cleanup window. The current global physical telemetry retention window is 90 days. This is intentional: upgrades can reveal previously stored telemetry inside the upgraded plan window, and downgrades shrink access immediately without requiring an immediate row purge.

## Active Repo Paths

Current local development checkout:

```bash
/home/shouheng/Novena-Platform/Novena-Hub
```

Recommended production server checkout:

```bash
/opt/novena/Novena-Hub
```

Do not treat old `/home/shouheng/projects/Novena-Hub` references as current unless you intentionally restore that path.

## Deployment Asset Map

Production assets in this repo:

| Asset | Purpose |
| --- | --- |
| `Dockerfile.prod` | Production app image. Builds Vite assets, installs Python prod dependencies, runs as non-root `django`. |
| `docker-compose.prod.yml` | Production service graph for web, worker, Beat, MQTT consumer, Postgres/TimescaleDB, Redis, and Mosquitto. |
| `deploy/env/production.env.example` | Production-safe env template with placeholders only. |
| `deploy/nginx/novena.conf.example` | Host Nginx HTTPS reverse proxy with WebSocket headers. |
| `deploy/mosquitto/mosquitto.prod.conf` | Mosquitto listeners for internal Hub traffic, private Dynamic Security control, and public TLS MQTT. |
| `deploy/mosquitto/dynamic-security.example.json` | Safe seed/example for Dynamic Security roles. Real credentials must be generated on the server. |
| `deploy/backups/backup_postgres.sh` | Compressed Postgres/TimescaleDB backup. |
| `deploy/backups/backup_private_media.sh` | Backup for confidential Business Impact report files. |
| `deploy/backups/restore_postgres.sh` | Guarded database restore tool. |
| `deploy/backups/backup_retention.sh` | Local daily/weekly/monthly retention cleanup. |
| `deploy/systemd/*.example` | Nightly backup timer examples. |
| `apps/web/management/commands/production_readiness_check.py` | App-level production preflight. |
| `docs/production_deployment_plan.md` | VPS/cloud setup guide. |
| `docs/production_backup_restore.md` | Backup and restore guide. |

The frontend production build includes the locked `gridstack` package used by the customizable Command Center. `npm run build` must pass before deploying a new image.

## DNS Records

Use Cloudflare DNS:

| Host | Type | Target | Cloudflare mode |
| --- | --- | --- | --- |
| `novenaplatform.com` | A | VPS public IP | Proxied |
| `www.novenaplatform.com` | CNAME | `novenaplatform.com` | Proxied |
| `app.novenaplatform.com` | A | VPS public IP | Proxied |
| `mqtt.novenaplatform.com` | A | VPS public IP | DNS only |

MQTT must be DNS only because standard Cloudflare proxying is HTTP(S), not raw MQTT.

## Required Environment Variables

Start from:

```bash
cp deploy/env/production.env.example deploy/env/production.env
```

Then fill real values. Important groups:

### Django and URLs

- `DJANGO_SETTINGS_MODULE=novena_hub.settings_production`
- `NOVENA_DEPLOYMENT_MODE=production`
- `DEBUG=False`
- `SECRET_KEY`
- `ALLOWED_HOSTS=novenaplatform.com,www.novenaplatform.com,app.novenaplatform.com`
- `APP_DOMAIN=app.novenaplatform.com`
- `SESSION_COOKIE_DOMAIN=.novenaplatform.com`
- `APP_BASE_URL=https://app.novenaplatform.com`
- `FRONTEND_ADDRESS=https://app.novenaplatform.com`
- `CORS_ALLOWED_ORIGINS`
- `CSRF_TRUSTED_ORIGINS`
- `USE_HTTPS_IN_ABSOLUTE_URLS=True`
- `HEALTH_CHECK_TOKENS`
- `ACCOUNT_EMAIL_VERIFICATION=optional` for canaries, consider `mandatory` before broad self-serve signup.

### Database and Redis

- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `REDIS_URL`

### MQTT and Gateway Activation

- `MQTT_BROKER_HOST=mosquitto`
- `MQTT_BROKER_PORT=1883`
- `MQTT_CONSUMER_CLIENT_ID`
- `MQTT_PUBLISHER_CLIENT_ID`
- `MQTT_DYNSEC_PORT=1884`
- `MQTT_DYNSEC_ADMIN_USER`
- `MQTT_DYNSEC_ADMIN_PASS`
- `MQTT_PROVISIONING_REQUIRED=True`
- `MQTT_ACCEPT_LEGACY_SHARED_INBOUND=False`
- `GATEWAY_CLAIM_SECRET`
- `GATEWAY_ACTIVATION_ENCRYPTION_KEY`
- `PUBLIC_MQTT_BROKER_SCHEME=mqtts`
- `PUBLIC_MQTT_BROKER_HOST=mqtt.novenaplatform.com`
- `PUBLIC_MQTT_BROKER_PORT=8883`

Teacher note: `GATEWAY_CLAIM_SECRET` protects the sticker claim code flow. `GATEWAY_ACTIVATION_ENCRYPTION_KEY` protects the activation credential escrow used when a Gateway is offline during claim and comes online later.

### Signed Guided Setup and Governed Control

- `REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID`
- `REMOTE_CONTROL_SIGNING_KEYS`
- `REMOTE_CONTROL_AUDIT_RETENTION_DAYS=2555`
- `REMOTE_CONTROL_OUTBOX_*`
- `GATEWAY_CONFIG_ENVELOPE_TTL_SECONDS`
- `GATEWAY_CONFIG_OUTBOX_*`
- `GATEWAY_CONFIG_INTENT_TTL_SECONDS`
- `GATEWAY_CONFIG_APPLY_TIMEOUT_SECONDS`
- `GUIDED_SETUP_FIRST_TELEMETRY_TIMEOUT_SECONDS`

Guided Setup uses the same Ed25519 signing authority as governed remote commands. The Gateway must have the matching public key installed before it advertises `guided_setup_v1`.

Do not copy a Hub private signing key to a Gateway.

### Notifications and Billing

- SES: `EMAIL_BACKEND`, `AWS_SES_REGION_NAME`, `AWS_SES_ACCESS_KEY_ID`, `AWS_SES_SECRET_ACCESS_KEY`, sender emails.
- WhatsApp: `WHATSAPP_PROVIDER=meta`, Meta phone number ID, access token, verify token, approved alert template.
- Stripe: `STRIPE_LIVE_MODE=False` is acceptable for unpaid pilots; live keys and `DJSTRIPE_WEBHOOK_SECRET` are required before paid rollout.
- Sentry: strongly recommended before technical canaries.

### Business Impact Reports and Media

- Use local filesystem media first: `USE_S3_MEDIA=False`.
- Ensure Compose mounts both `./media` and `./private_media`.
- Run both database and private-media backups.
- If you later enable S3-compatible media, configure `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_STORAGE_BUCKET_NAME`, then revise backup/restore expectations.

### Gateway OTA

OTA settings are present but optional for unpaid canaries:

- `NOVENA_OTA_SIGNING_PRIVATE_KEY`
- `NOVENA_OTA_SIGNING_KEY_ID`
- `NOVENA_OTA_MANIFEST_TTL_DAYS`

If the signing key is blank, do not use the OTA update flow. For pilots, it is acceptable to disable OTA operationally and update Gateway software manually through a controlled field procedure. Before paid rollout, signed OTA should be tested on representative CM4 hardware.

## Required Celery Beat Tasks

`production_readiness_check` requires these scheduled tasks to exist:

- `apps.devices.tasks.dispatch_due_gateway_config_outboxes`
- `apps.devices.tasks.dispatch_due_gateway_releases`
- `apps.devices.tasks.dispatch_due_plan_reconciliations`
- `apps.devices.tasks.dispatch_due_remote_command_outboxes`
- `apps.devices.tasks.expire_and_retry_gateway_activations`
- `apps.devices.tasks.expire_control_activations`
- `apps.devices.tasks.check_device_heartbeats`
- `apps.devices.tasks.check_gateway_heartbeats`
- `apps.telemetry.tasks.flush_telemetry_buffer_task`
- `apps.telemetry.tasks.flush_logs_buffer_task`
- `apps.maintenance.tasks.generate_preventive_tickets`
- `apps.impact.tasks.dispatch_impact_refreshes`
- `apps.impact.tasks.dispatch_daily_impact_finalization`
- `apps.impact.tasks.dispatch_monthly_impact_reports`
- `apps.impact.tasks.cleanup_impact_history`

Teacher note: Celery Beat is the scheduler. It is what keeps background recovery work happening even if a web request or worker crashes halfway through a Gateway operation.

## First Server Build

From `/opt/novena/Novena-Hub` on the VPS:

```bash
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d postgres redis mosquitto
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml run --rm web python manage.py verify_timescale
docker compose -f docker-compose.prod.yml run --rm web python manage.py collectstatic --noinput
docker compose -f docker-compose.prod.yml up -d
```

Deploy migrations before bringing the final app stack up. Web, worker, Beat, and MQTT consumer should run the same Git SHA.

## Mosquitto Dynamic Security

Production Mosquitto expects:

```text
deploy/mosquitto/data/dynamic-security.json
```

Do not use the development file from a local broker.

Current Hub behavior:

- Hub uses Dynamic Security admin credentials on internal port `1884`.
- Hub creates per-Gateway operational clients and per-Gateway bootstrap clients during claim/provisioning.
- Each Gateway should publish only to serial-scoped topics such as `v1/gateway/{serial}/telemetry`.
- `MQTT_ACCEPT_LEGACY_SHARED_INBOUND=False` must stay false for production customer traffic.

The readiness command checks Dynamic Security JSON files for unsafe shared publish ACLs such as `v1/gateway/telemetry` on non-admin roles.

## Production Readiness Command

Run:

```bash
docker compose -f docker-compose.prod.yml run --rm web python manage.py production_readiness_check
```

For machine-readable output:

```bash
docker compose -f docker-compose.prod.yml run --rm web python manage.py production_readiness_check --json
```

Current blocker checks include:

- production settings module;
- `DEBUG=False`;
- custom `SECRET_KEY`;
- custom `GATEWAY_CLAIM_SECRET`;
- configured `GATEWAY_ACTIVATION_ENCRYPTION_KEY`;
- no wildcard `ALLOWED_HOSTS`;
- production domains present;
- HTTPS app base URL;
- HTTPS absolute URLs;
- production CSRF trusted origins;
- required Celery Beat tasks;
- non-default Dynamic Security credentials;
- required Gateway MQTT provisioning enabled;
- legacy shared inbound MQTT disabled;
- no unsafe shared Gateway publish ACLs in Dynamic Security files;
- SES configured;
- WhatsApp configured when provider is `meta`;
- health check token configured;
- database connection;
- TimescaleDB extension and telemetry hypertable;
- Redis connectivity;
- MQTT broker connectivity;
- managed command signing key configured;
- remote-control audit retention at least seven years;
- controlled teams have acknowledged policy bundles;
- Guided Setup TTL/retry bounds;
- private Business Impact report storage separated from public media;
- WeasyPrint import works for PDF rendering.

Warnings include:

- Stripe test mode, which is acceptable for unpaid pilots.
- Missing Stripe webhook secret, acceptable only before paid launch.
- Missing Sentry, not ideal but not a hard blocker.

## Backup And Restore

Read `docs/production_backup_restore.md` before inviting pilots.

Minimum routine:

```bash
./deploy/backups/backup_postgres.sh
./deploy/backups/backup_private_media.sh
./deploy/backups/backup_retention.sh
```

First restore drill:

```bash
RESTORE_DATABASE_URL=postgresql://novena:<password>@localhost:5432/novena_restore_test BACKUP_FILE=/opt/novena/Novena-Hub/backups/postgres/daily/<backup>.dump.gz RESTORE_CONFIRM=restore-now ./deploy/backups/restore_postgres.sh
```

Also restore private media into a disposable directory and confirm Business Impact report files match restored database rows.

Teacher note: a database restore recovers rows. It does not automatically recover files stored on disk. That is why private report backup is separate.

## Go-Live Smoke Test

Before inviting customers:

1. `https://novenaplatform.com` loads the public site.
2. `https://app.novenaplatform.com` loads login/signup.
3. `/health/?token=<token>` returns healthy.
4. `production_readiness_check` has no `FAIL` results.
5. WebSocket live telemetry works on a device detail page.
6. Gateway connects to `mqtts://mqtt.novenaplatform.com:8883`.
7. Gateway claim creates managed MQTT credentials.
8. Serial-scoped MQTT accepts correct Gateway topics and rejects cross-Gateway/shared-topic misuse.
   Verify that a claimed Gateway publishes an operational attribute heartbeat with
   `status=online`, software version, and clock readiness. Connectivity diagnostics
   alone must leave Guided Setup blocked with a clear missing-heartbeat reason.
9. Guided Setup sends signed config and receives matching acknowledgement evidence.
10. First telemetry reaches TimescaleDB and the correct customer dashboard.
11. Alert, email, approved WhatsApp template, and maintenance ticket flow works end to end.
12. Business Impact private report generation and authorized download works.
13. Backup runs and restore drill has passed.
14. Stripe webhook signature validation is configured before paid rollout.

## Hardware Replay Handoff

Hardware replay should prove this path:

```text
field device -> Novena Gateway -> mqtt.novenaplatform.com:8883 -> Mosquitto -> mqtt_consumer -> TimescaleDB -> dashboard -> alert -> email/WhatsApp -> maintenance ticket
```

Do not use `pilot_readiness_audit keepalive` during physical replay. Physical Gateways must prove heartbeat, discovery, telemetry, alerting, buffering, restart recovery, and configuration acknowledgement themselves.
Stop the Gateway attribute heartbeat worker during a replay and confirm that the
service reports failure and restarts, then resumes full heartbeats before setup advances.

## Current Release Gates

As of 2026-08-17, do not widen beyond a 3-5 customer technical canary until these are proven:

1. Offline buffering across process restart and reconnect on physical CM4 hardware.
2. Factory-energy, cold-chain, and facilities/HVAC CM4 hardware replays with representative equipment.
3. Governed write-back on representative devices, including power loss, key rotation, restore, and emergency-disable recovery.
4. Production Stripe webhooks/live billing and inbound WhatsApp security before paid rollout.
5. A 3-5 customer technical canary before a 20-customer cohort.

## Rollback Basics

For a normal bad deploy:

1. Revert to the previous Git SHA.
2. Rebuild the production image.
3. Restart web, worker, Beat, and MQTT consumer.
4. Run readiness and smoke tests.

For a bad database migration:

1. Stop web, worker, Beat, and MQTT consumer.
2. Take a backup of the broken current state.
3. Restore the last known-good backup only if data is corrupted.
4. Deploy the matching code version.
5. Run migrations and readiness checks.

For governed remote-control deployments, keep command workers stopped after restore and run the governed-control recovery reset from `docs/governed_remote_control_operations.md`. Restored command/outbox rows must not execute accidentally.

## Upgrade Path

Stay lean for unpaid canaries. Around 20 paying customers, or sooner if customers rely on Novena operationally, plan to:

- migrate Postgres/TimescaleDB to a managed database if TimescaleDB extension support fits;
- move media/private reports to object storage with versioning;
- add GitHub Actions deployment with manual approval;
- add external uptime monitoring;
- consider a separate MQTT VPS if gateway traffic grows;
- add centralized log retention and alerting.
