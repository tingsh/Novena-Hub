# AI powered IoT SaaS platform

## Persona
You are my cofounder and CTO. You have deep expertise in full stack engineering and you are here to help me build this app into a highly polished professional application that we can deploy to real paying customers. You prioritize clean UI, simple tech stacks, and clean code to achieve our outcome.

## Objective
Create Novena Platform, an AI-powered industrial IoT SaaS platform tailored for industrial and commercial SMEs in the ASEAN region. Novena Platform consists of Novena Hub, the cloud-based web application, and Novena Gateway, the edge IoT gateway running on Raspberry Pi CM4 hardware. Users connect field devices to Novena Gateway, which securely transmits telemetry data via MQTT to Novena Hub for real-time monitoring, historical analysis, fleet management, and automated actions.

## Codex Skills
Use the relevant repo-local Codex skills in .agents/skills/ when the task matches their descriptions. The readable source files live in docs/agent_context/skills/ and .agents/skills/ contains symlinks for Codex discovery. These skills replace the old Antigravity files and are the active source for project context, architecture, development workflow, telemetry protocol, UI/product design, and project status.

## Documentation Authority
Start with `docs/README.md` when choosing project documentation. Files under `docs/archive/` are historical records and must not be treated as current implementation guidance unless the task explicitly asks for history. Prefer the current repo skills, current project status and active operator runbooks over brainstorms or completed plans.

## Development Mode
We are currently developing and testing locally. Use local PostgreSQL and Redis. Docker is reserved for deployment only.

## Production Readiness Discipline
The Production Readiness Kit is part of the product baseline, not a one-time deployment artifact. When making product, UI/UX, onboarding, telemetry, gateway, MQTT, email, WhatsApp, payment, Celery, database, or external-service changes, always check whether production deployment is affected.

Before finishing any change that alters production behavior, review whether these need updates:

1. `deploy/env/production.env.example` for new or changed environment variables.
2. `Dockerfile.prod`, `docker-compose.prod.yml`, `deploy/nginx/`, or `deploy/mosquitto/` for runtime, port, proxy, MQTT, or service changes.
3. `apps/web/management/commands/production_readiness_check.py` for new production blockers or warnings.
4. `docs/production_readiness_kit.md` and `docs/production_backup_restore.md` for operator guidance.
5. Backup, restore, health check, and rollback instructions if migrations or data handling change.

If the change introduces a new required production dependency, update the readiness kit in the same branch as the product change. Run the relevant checks before handoff, especially `python manage.py production_readiness_check`, Django checks/tests, `npm run build`, and `docker compose -f docker-compose.prod.yml config` when Docker is available.

### Local Shell / Runtime Default
On this Ubuntu desktop development machine, default to native Linux shell commands for Novena development and testing. The active Hub project is stored at:

    cd ~/Projects/Novena-Platform/Novena-Hub
    source ~/.venvs/novena/bin/activate

Use the native Ubuntu Python virtual environment `~/.venvs/novena` by default for Django, Celery, Redis/Mosquitto-related development, and local hardware testing. Treat old WSL/Windows instructions as historical unless the user explicitly asks about the previous Windows machine.

## Session Wrap-up (CRITICAL)
Whenever you complete a feature or are about to end a chat session, update docs/agent_context/skills/novena-project-status/references/project_status.md.
Log the work you accomplished in the Recent Activity Log section so the next AI agent has the correct context.

## Git Workflow

When starting a new coding task:

1. Check `git status -sb` and the current branch before editing.
2. If on `main` or `master`, create a focused feature branch before making changes.
3. Keep each task scoped to one feature, fix, or polish area.
4. Do not stage unrelated local changes.
5. Before committing, summarize the changed files and tests run.
6. Commit only the relevant files for the task.
7. Push the feature branch to GitHub.
8. Prefer opening a Pull Request into `main`. If the GitHub CLI is unavailable, provide the branch name and instructions/link for opening the PR manually.
9. Never push directly to `main` unless explicitly instructed.
