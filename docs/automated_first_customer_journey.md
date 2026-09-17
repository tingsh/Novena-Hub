# Automated First-Customer Journey

This runbook covers the first useful automated pilot-customer canary for Novena Hub. The canary creates one isolated
customer hierarchy, simulates a claimed Gateway and temperature/humidity device, sends ten Gateway-shaped readings,
checks the customer-visible Hub state, and removes every successful-run record.

The test is deliberately small. It exercises current Hub behavior without pretending that simulated edge software is
physical hardware.

## Safety boundary

The command fails closed unless `NOVENA_DEPLOYMENT_MODE` is one of `local`, `dev`, `development`, `test`, or `staging`.
Production, an empty value, and unknown values are refused for both run and cleanup actions.

Every run receives an identifier shaped like:

```text
fcj_20260818T052628Z_5ff68f
```

`Site.metadata`, `Device.metadata`, and `GatewayInventory.notes` contain all three ownership fields:

```text
test_run_id=<exact run ID>
created_by=automated_first_customer_journey_test
expires_at=<UTC timestamp 24 hours after setup>
```

The user, team, template, Gateway, and device also use the exact `FCJ Test <test_run_id>` prefix because those models
do not all offer general-purpose metadata. The generated user uses the reserved `@automated-fcj.invalid` domain.
Cleanup starts from record-level ownership markers and only follows their exact relationships and generated names.

## Prerequisites

Run from the Novena Hub repository with the normal Hub virtual environment and a non-production `.env`:

```bash
cd /home/shouheng/Projects/Novena-Platform/Novena-Hub
export NOVENA_DEPLOYMENT_MODE=local
```

The default MQTT journey requires:

- PostgreSQL/TimescaleDB configured by `DATABASE_URL`.
- Redis configured by `REDIS_URL`.
- Mosquitto or another development broker configured by `MQTT_BROKER_HOST` and `MQTT_BROKER_PORT`.
- `python manage.py mqtt_consumer` subscribed to the broker.
- a Celery worker with Beat running `apps.telemetry.tasks.flush_telemetry_buffer_task`.

On the supported Ubuntu development machine, start and verify the existing stack:

```bash
.agents/skills/novena-local-dev/scripts/start-novena-local-dev.sh
.agents/skills/novena-local-dev/scripts/health-check.sh
```

No production credentials are needed or permitted. The MQTT mode uses the internal development broker settings, not
the public browser/Gateway endpoint.

## Run the journey

The default command publishes one serial-scoped heartbeat and ten QoS-1 telemetry messages. It waits up to 45 seconds
for the existing MQTT consumer, Redis queue, and Celery flush task to persist the readings:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py first_customer_journey run
```

The deterministic service mode is intended for CI and focused local testing. It sends the same Gateway-shaped payloads
through `ingest_telemetry_data`, but bypasses the broker, MQTT consumer, Redis queue, and Celery:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py first_customer_journey run \
  --ingestion-mode service
```

The sample count may be changed within the intentionally small 10–100 range. The persistence wait is also bounded:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py first_customer_journey run \
  --sample-count 20 \
  --timeout-seconds 60
```

Successful output is a compact JSON result containing the run ID, ingestion mode, assertions, counts, cleanup result,
and duration. A successful run always deletes its data before returning success.

## What is created and asserted

Each run creates one user, team, site, team-owned device template, claimed `GatewayInventory`, `Gateway`, and device. The
device template generates one dashboard and temperature/humidity widgets. Ten samples create 20 telemetry points.

The canary then verifies:

- the inventory resolves through `current_claimed_gateway`;
- the Gateway and device freshness states are live;
- exactly the expected temperature/humidity timestamps and values were persisted;
- the authenticated telemetry samples endpoint returns the expected grouped rows and columns;
- the generated device dashboard exposes both metrics; and
- Command Center reports the correct fleet counts and a latest device reading.

Successful cleanup removes telemetry first, followed by widgets/dashboard, device, Gateway logs/configuration,
inventory, Gateway, site, template, team, and user. It clears the run's Command Center cache and removes only Redis queue
messages whose exact Gateway serial belongs to the run. Cleanup is idempotent, so repeating it reports zero counts
instead of failing.

## Failure retention and manual cleanup

Preflight and transactional setup failures leave no partial hierarchy. By default, a later ingestion or assertion
failure retains the marked run for up to 24 hours so it can be inspected. The error prints the exact cleanup command:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py first_customer_journey cleanup \
  --test-run-id fcj_20260818T052628Z_5ff68f
```

Use `--cleanup-on-failure` when retaining diagnostic state is not useful:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py first_customer_journey run \
  --cleanup-on-failure
```

To remove all expired abandoned runs owned by this canary:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py first_customer_journey cleanup \
  --created-by automated_first_customer_journey_test
```

Created-by cleanup skips unexpired runs by default to avoid disrupting a concurrent or recently failed canary. Use the
following only after confirming that no FCJ run is active:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py first_customer_journey cleanup \
  --created-by automated_first_customer_journey_test \
  --include-unexpired
```

Selectors are strict: cleanup accepts either one syntactically valid exact `test_run_id` or the exact `created_by`
constant. Broad selectors such as `test` are rejected.

## Coverage boundary

Covered today: fresh team/customer setup, site/device registration, simulated claimed Gateway identity, Hub MQTT → Redis
→ Celery ingestion in the default mode, persistence, freshness, authenticated samples API readback, generated device
dashboard, Command Center readback, and cleanup.

Simulated today: the Novena Gateway process and the physical field device.

Not yet covered: signup browser UX, sticker claim/activation and Dynamic Security provisioning, Gateway connector
polling/offline buffering, physical CM4 hardware, public-cloud TLS/networking, WebSocket/browser rendering, and real
equipment. The founder's hardware/software integration and public-cloud deployment P0 work must be available before
those paths can become true end-to-end canary coverage.
