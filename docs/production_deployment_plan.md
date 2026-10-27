# Novena Hub Production Deployment Plan

Last reviewed: 2026-10-28 (after factory-energy, cold-room, and facilities/HVAC CM4 replays)

This is the practical plan for putting Novena Hub on a public server. First prove the stack privately, then open a controlled investor showcase with labelled sample data. Invite three customers only as a supervised, read-only pilot after the release and each site's equipment pass the acceptance checks below. Use the same maintainable deployment rather than a throwaway demo environment.

The physical E2E fixes are merged into Hub `main` through PR #30 and Gateway `main` through PR #16. Neither merge installs the public stack or updates the CM4 automatically; choose exact reviewed commits for the deployment branch and verify that pair on the target systems.

The matching repo-backed checklist is `docs/production_readiness_kit.md`.

## CTO Recommendation

Use one low-cost VPS first, then graduate pieces out only when customer risk justifies it.

Recommended first production shape:

- One Ubuntu LTS VPS with Docker Compose.
- Host Nginx handles HTTPS for the public site and app.
- Docker Compose runs Django web, Celery worker, Celery Beat, MQTT consumer, Postgres/TimescaleDB, Redis, and Mosquitto.
- Cloudflare stays as DNS/CDN provider.
- Postgres/TimescaleDB stays self-hosted for the unpaid canary stage.
- Backups go to local disk plus an off-server rclone target such as Cloudflare R2 or Backblaze B2.

Teacher note: a VPS is a rented Linux server. It is cheaper because you operate more of the stack yourself. Managed services cost more because the provider handles pieces like backups, patching, failover, and scaling.

## Target Cost

Recheck current provider prices before ordering. Budget for the VPS, backups and off-server storage, a domain, monitoring, and any enabled SES/WhatsApp/Stripe usage. The older August cost figures are not a purchase quote.

Use at least 4 GB RAM. Prefer 8 GB RAM if the price is still comfortable, because Postgres, Redis, Celery, WeasyPrint PDF generation, and Vite-built static assets all share the same machine.

## Domains

Use the common SaaS split you proposed:

- `novenaplatform.com`: public marketing site.
- `www.novenaplatform.com`: marketing alias.
- `app.novenaplatform.com`: Django app login, onboarding, dashboards, APIs, webhooks.
- `mqtt.novenaplatform.com`: public MQTT endpoint for gateways.

This is a good pattern. It keeps the marketing domain clean while giving the actual SaaS app its own operational hostname.

Cloudflare DNS records:

| Host | Type | Target | Cloudflare proxy |
| --- | --- | --- | --- |
| `novenaplatform.com` | A | VPS public IPv4 | Proxied |
| `www.novenaplatform.com` | CNAME | `novenaplatform.com` | Proxied |
| `app.novenaplatform.com` | A | VPS public IPv4 | Proxied |
| `mqtt.novenaplatform.com` | A | VPS public IPv4 | DNS only |

Keep MQTT as DNS only. Cloudflare's normal orange-cloud proxy is for HTTP/HTTPS traffic, not raw MQTT on port 8883.

## Server Layout

Use this production directory on the VPS:

```bash
/opt/novena/Novena-Hub
```

That path is intentionally separate from the local development checkout. Your current local development checkout is:

```bash
/home/shouheng/Projects/Novena-Platform/Novena-Hub
```

Teacher note: keeping `/opt/novena/Novena-Hub` on the VPS makes commands in systemd timers, backup scripts, and Nginx examples stable even if your laptop workspace moves again.

## Firewall

Allow only the ports customers and operators need:

```text
22/tcp    SSH admin access
80/tcp    HTTP for redirect and certificate challenge
443/tcp   HTTPS web app and WebSockets
8883/tcp  MQTT over TLS for gateways
```

Do not expose Postgres, Redis, Mosquitto `1883`, or Mosquitto Dynamic Security `1884` to the public internet. In the Compose file those are private Docker-network services by default.

## Cloudflare SSL Mode

Set Cloudflare SSL/TLS mode to Full (strict) after the server has certificates.

There are two acceptable certificate paths:

1. Let's Encrypt certificates on the VPS with Certbot.
2. Cloudflare Origin Certificates for the HTTP app, plus a normal certificate chain for MQTT if the Gateway validates public trust roots.

For the first deployment, I recommend Let's Encrypt because it is easier to reason about and works with normal clients.

## Install Server Packages

On a fresh Ubuntu LTS VPS:

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl git nginx certbot python3-certbot-nginx rclone postgresql-client gzip tar
```

Install Docker Engine and the Compose plugin using the official Docker instructions for Ubuntu. Then verify:

```bash
docker --version
docker compose version
```

Use a modern Docker Compose version. The production Compose file uses `env_file.required`, which needs a recent Compose v2 release. If your server Compose is old, upgrade Docker Compose before continuing.

## Freeze the release before cloning

Record one reviewed Hub Git SHA, one compatible Gateway Git SHA, their migrations, production image digests, and a rollback pair. Do not deploy a moving development branch or rely on the latest local checkout. As of this review, the E2E baseline is on Hub `main` at `86f8e20` (PR #30) and Gateway `main` at `f2f7f84` (PR #16). Branch from those reviewed commits, freeze the resulting image and CM4 build, and verify the pair on the target systems before inviting anyone. Keep write-back and OTA disabled for the initial read-only pilot.

Before publishing the app, prove two disposable tenants cannot see each other's devices, telemetry, templates or MQTT topics. Verify a released Gateway loses its old operational credentials and site configuration and can be claimed by the new tenant; an offline release must remain pending. Do not seed customer accounts or manufacturing claim codes into a public demo.

## Clone And Prepare The Repo

```bash
sudo mkdir -p /opt/novena
sudo chown -R "$USER:$USER" /opt/novena
cd /opt/novena
git clone <your-github-repo-url> Novena-Hub
cd /opt/novena/Novena-Hub
git fetch origin
git checkout --detach <reviewed-hub-release-sha>
```

Create runtime directories:

```bash
mkdir -p deploy/env deploy/mosquitto/data media private_media static_root backups/postgres backups/private_media
chmod +x deploy/backups/*.sh
```

Copy the production env template:

```bash
cp deploy/env/production.env.example deploy/env/production.env
chmod 600 deploy/env/production.env
```

Then edit it:

```bash
nano deploy/env/production.env
```

## Required Production Environment

Fill the real values in `deploy/env/production.env`. The most important groups are:

- Django security: `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `APP_BASE_URL`, `CSRF_TRUSTED_ORIGINS`, `HEALTH_CHECK_TOKENS`.
- Database: `POSTGRES_PASSWORD`, `DATABASE_URL`.
- Redis: `REDIS_URL`.
- MQTT: `MQTT_DYNSEC_ADMIN_USER`, `MQTT_DYNSEC_ADMIN_PASS`, `MQTT_PROVISIONING_REQUIRED=True`, `MQTT_ACCEPT_LEGACY_SHARED_INBOUND=False`.
- Gateway identity and activation: `GATEWAY_CLAIM_SECRET`, `GATEWAY_ACTIVATION_ENCRYPTION_KEY`.
- Gateway release: `GATEWAY_RELEASE_LEASE_SECONDS`, `GATEWAY_RELEASE_RETRY_MAX_SECONDS`, a paired Gateway build advertising `secure_release_v1`, and the signed config/command key trusted by that Gateway. Apply `devices.0039_gateway_release_edge_reset_at` with the release candidate. Verify its physical reset/credential scrub before reassigning hardware.
- Public gateway instructions: `PUBLIC_MQTT_BROKER_SCHEME=mqtts`, `PUBLIC_MQTT_BROKER_HOST=mqtt.novenaplatform.com`, `PUBLIC_MQTT_BROKER_PORT=8883`.
- Guided Setup and governed control: `REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID`, `REMOTE_CONTROL_SIGNING_KEYS`, `GATEWAY_CONFIG_*`, `GUIDED_SETUP_FIRST_TELEMETRY_TIMEOUT_SECONDS`.
- Email: AWS SES keys, sender addresses, optional Anymail webhook secret.
- WhatsApp: Meta phone ID, access token, verify token, approved alert template.
- Stripe: test mode is acceptable for unpaid canaries; live keys and webhook secret are required before paid launch.
- Business Impact reports: leave `USE_S3_MEDIA=False` for the first VPS deployment unless you intentionally move media to object storage.
- OTA: leave `NOVENA_OTA_SIGNING_PRIVATE_KEY` blank if Gateway software updates are operationally disabled for pilots; configure it before using signed OTA.

Generate secrets with commands like:

```bash
openssl rand -base64 48
```

For `GATEWAY_ACTIVATION_ENCRYPTION_KEY`, use a Fernet key. One safe way on your dev machine or server is:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

For `REMOTE_CONTROL_SIGNING_KEYS`, use an Ed25519 seed in JSON form. Example shape only:

```env
REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID=prod-2026-08
REMOTE_CONTROL_SIGNING_KEYS={"prod-2026-08":"replace-with-base64-ed25519-seed"}
```

Teacher note: signing keys prove that Hub created a command/config envelope. The Gateway stores the matching public key and rejects unsigned or wrong-key instructions.

## Certificates

Issue HTTPS certificates for the web domains:

```bash
sudo certbot --nginx -d novenaplatform.com -d www.novenaplatform.com -d app.novenaplatform.com
```

Issue a certificate for MQTT:

```bash
sudo certbot certonly --standalone -d mqtt.novenaplatform.com
```

If Certbot cannot bind port 80 because Nginx is running, temporarily use the Nginx plugin or stop Nginx during the MQTT certificate challenge.

The Compose file expects MQTT certificate files here:

```text
/etc/letsencrypt/live/mqtt.novenaplatform.com/fullchain.pem
/etc/letsencrypt/live/mqtt.novenaplatform.com/privkey.pem
```

## Mosquitto Dynamic Security

The production Mosquitto config expects:

```bash
deploy/mosquitto/data/dynamic-security.json
```

Do not use development broker credentials.

Create a real Dynamic Security file from `deploy/mosquitto/dynamic-security.example.json`, then add:

- one admin client matching `MQTT_DYNSEC_ADMIN_USER` and `MQTT_DYNSEC_ADMIN_PASS`;
- a `gateway` baseline role with no broad publish ACLs;
- optional cloud-internal credentials if you later require authentication on the internal listener.

Current Hub provisioning creates per-gateway operational and bootstrap roles dynamically. That means you should not manually grant gateways broad publish access to shared topics such as `v1/gateway/telemetry`.

Teacher note: Dynamic Security is Mosquitto's runtime user/role system. Hub uses it to create one MQTT identity per Gateway, then scopes that Gateway to its own serial-number topics.

## Nginx

Copy the example and adapt paths if needed:

```bash
sudo cp deploy/nginx/novena.conf.example /etc/nginx/sites-available/novena.conf
sudo ln -s /etc/nginx/sites-available/novena.conf /etc/nginx/sites-enabled/novena.conf
sudo nginx -t
sudo systemctl reload nginx
```

The Nginx config must preserve WebSocket headers. Novena uses WebSockets for live device detail pages; if those headers are missing, normal pages may load but live telemetry will degrade to polling.

## First Build And Database Setup

Validate the production service graph:

```bash
docker compose -f docker-compose.prod.yml config
```

Build the app image:

```bash
docker compose -f docker-compose.prod.yml build
```

Start the infrastructure services:

```bash
docker compose -f docker-compose.prod.yml up -d postgres redis mosquitto
```

Run migrations and Timescale verification:

```bash
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml run --rm web python manage.py verify_timescale
docker compose -f docker-compose.prod.yml run --rm web python manage.py collectstatic --noinput
```

`verify_timescale` checks basic hypertable presence only; it does not fail on missing Community policies. Run the stronger, read-only policy check before bringing up customer traffic:

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U novena -d novena_hub \
  < scripts/database/verify_timescale_policies.sql
```

It must print `PASS` for Community license, compression, the hourly continuous aggregate, refresh, and both 90-day retention jobs. If it fails, stop; do not mark fallback migrations as healthy or accept telemetry until the schema and policies are repaired and rechecked. Pin the PostgreSQL/TimescaleDB image version or digest for the release rather than silently advancing the mutable `latest-pg17` tag.

Start the full stack:

```bash
docker compose -f docker-compose.prod.yml up -d
```

Check services:

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 web
docker compose -f docker-compose.prod.yml logs --tail=100 celery-worker
docker compose -f docker-compose.prod.yml logs --tail=100 mqtt-consumer
```

## Production Readiness Check

Run the app-level preflight:

```bash
docker compose -f docker-compose.prod.yml run --rm web python manage.py production_readiness_check
```

Run JSON output if you want a machine-readable record:

```bash
docker compose -f docker-compose.prod.yml run --rm web python manage.py production_readiness_check --json
```

Do not invite canary users while any `FAIL` remains. `WARN` can be acceptable for unpaid pilots when intentional, for example Stripe test mode.

## Backups

Set up local backup plus off-server upload. The current system needs both:

- Postgres/TimescaleDB backup for relational data and telemetry.
- Private media backup for confidential Business Impact PDF reports.

It also needs a protected backup of `deploy/mosquitto/data/dynamic-security.json`, which contains the broker's live per-Gateway client and role state. The broker backup script and separate root timer are provided; install them, configure restricted off-server upload, and pass a disposable broker-state restore drill before inviting customers. A PostgreSQL restore alone cannot recreate an unchanged broker identity state. Store production environment secrets separately in a restricted secret vault.

Subscription telemetry limits are access windows, not backup or database deletion schedules. PostgreSQL/TimescaleDB may still contain telemetry outside a team's current visible plan window until the global physical retention policy, currently 90 days, drops it.

Manual first backup:

```bash
NOVENA_ROOT=/opt/novena/Novena-Hub ./deploy/backups/backup_postgres.sh
NOVENA_ROOT=/opt/novena/Novena-Hub ./deploy/backups/backup_private_media.sh
NOVENA_ROOT=/opt/novena/Novena-Hub ./deploy/backups/backup_retention.sh
sudo install -o root -g root -m 0700 deploy/backups/backup_mqtt_dynsec.sh /usr/local/sbin/novena-backup-mqtt-dynsec
sudo /usr/local/sbin/novena-backup-mqtt-dynsec
```

Install the timer:

```bash
sudo cp deploy/systemd/novena-postgres-backup.service.example /etc/systemd/system/novena-postgres-backup.service
sudo cp deploy/systemd/novena-postgres-backup.timer.example /etc/systemd/system/novena-postgres-backup.timer
sudo cp deploy/systemd/novena-mqtt-backup.service.example /etc/systemd/system/novena-mqtt-backup.service
sudo cp deploy/systemd/novena-mqtt-backup.timer.example /etc/systemd/system/novena-mqtt-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now novena-postgres-backup.timer novena-mqtt-backup.timer
```

Then perform one restore drill before inviting customers. A backup that has never been restored is only a hope, not a recovery plan.

## Go-Live Smoke Test

For the controlled investor showcase, complete items 1–6, the production preflight, tenant/access isolation, and restore/rollback checks with labelled sample data. Do not imply that the sample equipment is live customer hardware. Before inviting the first three technical canary customers, complete the full list on the frozen release:

1. `https://novenaplatform.com` loads.
2. `https://app.novenaplatform.com` loads signup/login.
3. `https://app.novenaplatform.com/health/?token=<token>` returns healthy.
4. `production_readiness_check` has no `FAIL`.
5. Nginx WebSocket proxy works on a device detail page.
6. Mosquitto accepts a TLS MQTT connection on `mqtt.novenaplatform.com:8883`.
7. A test Gateway claims successfully and receives managed MQTT credentials.
8. Serial-scoped telemetry arrives on `v1/gateway/{serial}/telemetry` and lands under the correct tenant.
9. Guided Setup publishes signed config and receives matching acknowledgement evidence.
10. First telemetry appears on the customer dashboard.
11. A customer-approved alert triggers, is acknowledged and resolves; at least one approved external notification channel reaches its designated recipient. Test WhatsApp separately before enabling it for customers.
12. Maintenance ticket flow works.
13. Database, private-media and Mosquitto Dynamic Security backups and restore drills passed; application and Gateway rollback were rehearsed.
14. Stripe webhook signature validation is configured before any paid user is charged.
15. Two-tenant HTTP/MQTT isolation, Gateway release/reclaim, signed config acknowledgement, offline buffer/replay and correct customer dashboard assignment pass.
16. Each customer's exact model, firmware, wiring and CT/PT scaling are documented and displayed readings are compared against the device or an independent reference. A simulator template is not field verification.

## Hardware Replay Handoff

The production server is not considered customer-ready until the physical replay proves:

```text
field device -> Novena Gateway -> mqtt.novenaplatform.com:8883 -> Mosquitto -> mqtt_consumer -> TimescaleDB -> dashboard -> alert -> email/WhatsApp -> maintenance ticket
```

The local CM4 has now replayed factory energy, cold room and facilities/HVAC with Laptop 2 simulators, plus actual field cable/host and CM4 Wi-Fi outages. This establishes the workflow, not compatibility with a customer's equipment or the public server. The intermittent field port-502 timeout still needs diagnosis or a measured support/recovery boundary. Automatic route failover was not tested because that CM4 has only one uplink. The owner deferred the local 24-hour cold-room soak; do not describe it as passed. Keep governed write-back, autonomous automation and OTA disabled for the first read-only pilot. Test payment and inbound WhatsApp security before enabling those paid or messaging functions.

## Deployment Update Routine

For normal code updates after the first deployment:

```bash
cd /opt/novena/Novena-Hub
git fetch origin
git checkout <release-sha-or-branch>
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml run --rm web python manage.py collectstatic --noinput
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml run --rm web python manage.py production_readiness_check
```

Deploy web, worker, Beat, and MQTT consumer together. They share lifecycle states, outbox semantics, and acknowledgement fields.

## Rollback

For a bad UI or code deploy:

1. Revert to the previous known-good Git SHA.
2. Rebuild the image.
3. Restart web, worker, Beat, and MQTT consumer.
4. Run smoke tests again.

For a bad data migration:

1. Stop web, worker, Beat, and MQTT consumer.
2. Take a backup of the broken current state.
3. Restore the last known-good backup only if data is corrupted or unrecoverable.
4. Deploy the matching code version.
5. Run migrations and readiness checks.

Do not restore the database for ordinary UI bugs. Database restore rewinds customer data.

## When To Move Beyond One VPS

Stay on the single VPS through unpaid canaries if it is stable and backups are proven.

Move Postgres/TimescaleDB to managed infrastructure around 20 paying customers, or earlier if customers depend on Novena for daily operations. The trigger is not just traffic; it is responsibility. Once paying customers rely on the data, database backup, restore, patching, and monitoring become business-critical.
