# Novena Hub Architecture Reference

## System Shape
Novena Platform has two main parts:

- Novena Hub: Django SaaS control plane, dashboards, telemetry ingestion, billing, AI, alerts, automations, and maintenance workflows.
- Novena Gateway: Python edge runtime on Raspberry Pi CM4-class hardware that connects field devices and sends MQTT telemetry to Hub.

## Hub Codebase Map
- apps/devices: Sites, gateways, devices, templates, commissioning, config generation, freshness, RPC command audit.
- apps/telemetry: MQTT parsing, ingestion, TimescaleDB data, WebSocket consumer, telemetry APIs, samples/history/export.
- apps/dashboard: dashboards, widgets, public links, dashboard services.
- apps/alerts: alert rules, triggered alerts, notification dispatch, maintenance escalation.
- apps/automations: Cloud PLC-style automation rules and execution engine.
- apps/maintenance: preventive/reactive tickets, contractor links, WhatsApp/email maintenance loop.
- apps/subscriptions: Stripe metadata, plan limits, feature gating, latency enforcement.
- apps/teams: Pegasus team tenancy, RBAC, signup/team workflow.
- apps/content and templates/content: Wagtail marketing content.
- novena_hub: settings, ASGI/WSGI, URL configuration, Celery setup.
- templates: Django templates and app UI surfaces.
- scripts: local simulators, Ubuntu dev stack helper, seed/config utilities.

## Data Hierarchy
Use this tenancy chain as the default mental model:

Team -> Site -> Gateway -> Device -> TelemetryData

Rules:

- Tenant-aware domain models should extend apps.teams.models.BaseTeamModel where appropriate.
- Querysets exposed to users must be scoped to the current team.
- Gateway health and field-device telemetry freshness are separate concepts.
- Browser stream state is a UI delivery mode, not hardware health.

## Important Runtime Flows
- Edge telemetry enters Hub through MQTT topics consumed by apps/telemetry/management/commands/mqtt_consumer.py.
- Events are parsed by apps/telemetry/mqtt_parser.py.
- Ingestion and buffering are handled by apps/telemetry/tasks.py and apps/telemetry/services.py.
- Telemetry is stored in TimescaleDB-backed TelemetryData.
- Device pages consume live data through apps/telemetry/consumers.py WebSockets with polling fallback.
- Celery Beat flushes telemetry/log queues and checks device/gateway heartbeat freshness.

## Service-Layer Guidance
- Use services for multi-step workflows, external effects, calculations, config generation, and cross-app coordination.
- Keep models to fields, choices, relationships, and lightweight helper properties.
- Keep forms/templates/views aligned with existing app patterns before introducing new helper layers.

## Validation Defaults
- Django syntax: ~/.venvs/novena/bin/python -m py_compile <files>.
- Django check: DJANGO_SETTINGS_MODULE=novena_hub.settings ~/.venvs/novena/bin/python manage.py check.
- Migrations: DJANGO_SETTINGS_MODULE=novena_hub.settings ~/.venvs/novena/bin/python manage.py makemigrations --check --dry-run.
- Focused tests: DJANGO_SETTINGS_MODULE=novena_hub.settings ~/.venvs/novena/bin/python -m pytest <test files> -q.
