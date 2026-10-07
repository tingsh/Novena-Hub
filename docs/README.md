# Novena Hub Documentation Authority

This index separates current source-of-truth material from historical records. Humans and coding models should start here instead of selecting a document by filename or modification date.

## Current project direction

- [Current project status](agent_context/skills/novena-project-status/references/project_status.md) — current phase, sprint goal, release gates and recent work.
- [CTO product progress review](cto_product_progress_review_2026-07-18.md) — latest cross-repository assessment.
- [Business plan](business_plan_2026-07-18.md) — current commercial and product strategy.
- [Physical Operations Platform roadmap](physical_operations_platform_roadmap_2026-08-11.md) — staged CTO/CPO roadmap from current narrow wedges to mature APJC physical operations platform.
- [Pilot launch checklist](20_customer_pilot_launch_checklist_2026-07-31.md) — controlled pilot gates and evidence requirements.

## Architecture and protocol

The current architecture and MQTT contract are maintained as repo-local skills because agents must load them before changing those areas:

- `novena-hub-architecture`
- `novena-telemetry-gateway-protocol`
- `novena-ui-product-design`
- `novena-product-strategy`

The Gateway repository's `ARCHITECTURE.md` is the current edge-runtime guide. Historical architecture brainstorms and the old bidirectional MQTT implementation specification are archived.

## Operations and deployment

- [Production deployment plan](production_deployment_plan.md)
- [Production readiness kit](production_readiness_kit.md)
- [Backup and restore](production_backup_restore.md)
- [Governed remote-control operations](governed_remote_control_operations.md)
- [Business Impact and ROI operations](business_impact_roi_operations.md)
- [Automated first-customer journey](automated_first_customer_journey.md)
- [Telemetry retention policy](telemetry_retention_policy.md)
- [Pilot readiness audit](pilot_readiness_audit.md) and [scorecard](pilot_readiness_scorecard.md)
- [Hardware replay runbook](hardware_replay_runbook_2026-07-09.md)
- [Factory-energy CM4 replay result (2026-10-08)](factory_energy_cm4_replay_2026-10-08.md) — observed end-to-end behavior, fixes and remaining gates.
- [Local development machine notes](local_development_machine_notes.md)

## External integrations

- [Amazon SES setup](amazon_ses_setup.md)
- [Email sender strategy](email_sender_strategy.md)
- [WhatsApp integration](whatsapp_integration_setup.md)
- [Stripe production checklist](stripe_production_checklist.md)

## Historical material

Everything under `archive/` is retained for institutional memory and Git history. It is not current implementation guidance. Archived files carry a warning and link back to this index.
