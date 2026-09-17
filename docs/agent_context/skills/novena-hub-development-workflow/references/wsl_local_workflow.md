# Ubuntu Local Development Workflow

This file kept its historical name so existing skill references keep working. The current default development host is Ubuntu desktop.

## Default Path

cd /home/shouheng/Projects/Novena-Platform/Novena-Hub
source ~/.venvs/novena/bin/activate

Use the native Ubuntu Python environment `~/.venvs/novena` for Hub work.
Docker remains useful for deployment validation, but it is not the default local test runner on this machine.

## Local Services
Production-like local development should use native Ubuntu services:

- PostgreSQL with TimescaleDB-compatible telemetry storage.
- Redis for queues/cache/Channels.
- Mosquitto for MQTT.
- Django dev server.
- Celery worker.
- Celery Beat.
- MQTT consumer management command.
- Vite dev server.

## Common Checks
- make local-check
- make local-manage ARGS="makemigrations --check --dry-run"
- make local-manage ARGS="migrate --check --noinput"
- npm run type-check -- --pretty false

Focused tests are preferred while developing:

```bash
make local-test ARGS="apps.devices.tests.test_onboarding --keepdb"
make local-pytest ARGS="apps/devices/tests/test_infrastructure.py -q"
```

If Make is not convenient, use the venv interpreter directly instead of bare `python3`:

```bash
DJANGO_SETTINGS_MODULE=novena_hub.settings /home/shouheng/.venvs/novena/bin/python manage.py check
DJANGO_SETTINGS_MODULE=novena_hub.settings /home/shouheng/.venvs/novena/bin/python manage.py test apps.devices.tests.test_onboarding --keepdb
DJANGO_SETTINGS_MODULE=novena_hub.settings /home/shouheng/.venvs/novena/bin/python -m pytest apps/devices/tests/test_infrastructure.py -q
```

## Service Smoke Tests
- Django: check http://127.0.0.1:8000/admin/login/ returns a redirect or page.
- Vite: check http://127.0.0.1:5173/static/@vite/client.
- Redis: redis-cli ping.
- Mosquitto: publish/subscribe locally on 127.0.0.1:1883.
- Celery: inspect ping should show an online worker.

## Hardware Test Notes
- Local hardware testing has used plain MQTT on port 1883.
- Production target remains MQTT over TLS on 8883.
- Keep browser stream status separate from gateway/device health.
- If machine-specific networking, Redis, or process-launch quirks appear, prefer documenting them as local caveats instead of designing production behavior around them.

## GitHub PR Workflow

Use local `git` for working-tree operations: branch creation, status, diff review, staging, and commits.

Use the connected GitHub plugin for PR-related operations whenever tools are available: PR creation, PR metadata, CI/check status, review comments, marking a PR ready, merge actions, and issue/PR summaries. Use `gh` only as a fallback when the plugin cannot perform the needed operation.

The local machine may have GitHub CLI credentials configured for this repository. It is acceptable to use the stored credential through `gh` or normal Git credential helpers when the user has approved that workflow, but do not print tokens, embed tokens in remote URLs, or document token values in repo files. Prefer SSH or the normal credential helper for `git push`; if push authentication is not configured, pause and explain the setup instead of using a token-in-command workaround.
