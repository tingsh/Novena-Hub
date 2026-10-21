# Round 2 post-onboarding customer audit — 2026-10-21

## Scope and evidence

Customer path: the `pilot-cold-room` account after completing Guided Setup for one CM4 Gateway and one simulated cold-room device. This review used the running in-app browser, the local Hub, live CM4 telemetry already flowing through MQTT, Hub Gateway logs, and focused Django/browser checks. No customer rule, public link, maintenance ticket, automation, or hardware configuration was created or changed for the audit.

This is a first customer walkthrough of the platform after onboarding. It is not production qualification or a sustained field-reliability run.

## What was checked

| Area | Browser result | Customer observation |
| --- | --- | --- |
| Command Center | Opened; manual refresh, customization, hide/restore/cancel, all five KPI destinations, equipment/site cards, Add Hardware and Create Alert Rule exercised | Navigation works after the fix below. Live temperature and freshness are visible. |
| Sites | Directory, cold-room detail, Add Site and Edit Site opened | Site has no address, which is a missing customer data field rather than a broken page. |
| Devices | Directory, equipment detail, Add Device and Edit Device opened; Live/Offline filter tested | The status tabs were inert before the fix below. Device detail has current readings, history and live stream controls. |
| Gateways | Directory, Gateway detail, Technician Logs, Command History and Remote Control Safety opened | Online heartbeat and connected equipment are visible. Recent technical errors need follow-up. |
| Alerts | Triggered Alerts, Monitoring Rules and the cold-room temperature preset form opened | The pilot has **zero active alert rules**. The old empty state falsely implied safe thresholds. |
| Shared Links | Directory and Create Shared Link opened | Creation defaults to active with optional password and expiry; public exposure deserves an explicit review step. |
| Maintenance | Ticket Board, PM Schedules, New Ticket and New PM Schedule opened | Empty states work; copy still assumes factory machinery in a cold-room account. |
| Automations | Directory, Rule History and Create Automation opened | Empty state is clear; trigger key is free text and new automation starts enabled by default. |

## Defects fixed in this audit

1. **Misleading trend mathematics.** Command Center added every temperature sample in an hour. The cold-room chart exceeded 300 °C while the live reading was about 2–3 °C. It now plots one selected device's hourly numeric average, leaves missing periods blank, labels the device and aggregation, and shows site-local times and a readable `°C` unit. A cumulative `energy` key uses the last reading in each hour and is labelled accordingly; consumption deltas remain separate work before energy dashboards can be considered qualified.
2. **Blank page after a dashboard drill-down.** A link inside the HTMX refresh region changed the URL to the equipment page but left the page content blank. Navigation inside that region now uses full-page links; the device/site cards and attention actions load their destinations.
3. **False all-clear language.** No monitoring rule is active for this newly onboarded cold room. Command Center now shows an actionable "Alert monitoring not set up" notice, and Alerts states that monitoring is not configured rather than asserting that all devices are within configured limits. Delayed devices now appear in Needs Attention. The customer still needs to review and enable appropriate thresholds and notification recipients.
4. **Inert device status filters and confusing directory data.** The Devices directory had buttons that did not filter. Live, Delayed, Offline and Alarm are now working status links with a clear empty result. The page no longer describes all equipment as energy assets or shows an irrelevant energy category for the cold-room sensor. Last-reading timestamps now show site-local time and its zone.
5. **Small customization trap.** "Add panels" previously opened an empty drawer when all panels were visible. The action now disables with an explanation until a panel is hidden; hide, restore and cancel were exercised without saving any layout change.

## Open release findings

| Priority | Finding | Why it matters / next acceptance proof |
| --- | --- | --- |
| P1 | **Cold-room monitoring coverage is still absent.** The template offers editable temperature, door and compressor suggestions, but none is saved or active. | Customer and Novena must agree on real thresholds, duration and responders, then trigger and resolve a simulated alert. A dashboard with zero incidents must not be treated as monitored. |
| P1 | **The cold-room simulator path is currently interrupted.** Gateway logs in a ten-minute sample had eight ERROR records, including Modbus decode errors for door/compressor and a later no-response retry. All four signals briefly recovered, then the dashboard showed "Gateway online — device offline" with a sample about three minutes old. Read-only SSH found the CM4 service active, `eth0` up at `10.0.0.10/24`, ARP for Laptop 2 `10.0.0.20` reachable, but TCP `10.0.0.20:502` timed out. Ping also received no reply; this alone could reflect Laptop 2 firewall policy. | Check that Laptop 2's Modbus simulator is listening on its field interface and that port 502 is allowed from `10.0.0.10`; then run a sustained Laptop 2 → CM4 → Hub soak with per-signal freshness, dropout counts and recovery evidence. Diagnose the earlier coil/register response mismatch separately. Do not clear this gate from a brief recovery. |
| P1 | **Manual Add/Edit Device remains a technician-oriented escape path.** The customer-facing form exposes raw connection JSON and a manually editable Online/Offline/Alarm status, separate from the guided discovery/template confirmation flow. | Make the guided path the primary "add equipment" action; label/manual-gate the advanced path and prevent a user-entered status from masquerading as live telemetry. Test reconfiguration and rollback. |
| P1 | **Public Shared Link creation has permissive defaults.** A new link is active, while password and expiry are optional. The page says it is unauthenticated, but a first-time operator could publish more broadly than intended. | Add a review step that states exactly what data is visible, to whom, and for how long; consider a safe initial expiry or inactive draft. Verify revocation and tenant scoping before pilot use. |
| P2 | **One-device dashboard is sparse and repetitive.** Five KPI cards include zero-value Maintenance and Automations; Asset Mix repeats the single cold-room asset; Site & Actions has substantial blank space. | Make the first-use layout adapt to one site/device: prioritize current reading, freshness, trend, and a single next action. Retain expandable fleet panels for larger accounts. |
| P2 | **Device detail repeats the same four signals in top reading cards and again in widget cards.** `degC` also still appears in some widget/table labels despite `°C` in the summary. | Consolidate value, trend and freshness into one signal presentation, then normalize display units across all telemetry surfaces without changing protocol units. |
| P2 | **Automation setup asks for a free-text reading key and starts enabled.** | Offer the chosen device's validated signal list, explain the proposed action, and require a review before activation. Exercise notification and approval paths with a disposable rule. |
| P3 | **Generic maintenance copy still says "industrial machinery" and "factory uptime" in the cold-room pilot.** | Use neutral equipment language or site-profile wording. |

## Validation and limits

- Focused Django tests: 14 passed for dashboard trend/freshness/monitoring copy, team home, and Devices status filtering.
- Ruff, `git diff --check`, Vite production build, Django checks, migrations and local stack health passed. Local Django checks still report the existing Stripe webhook-validation warning and deprecated account settings; those remain production configuration work.
- Browser verified chart values in a plausible cold-room range, full device/site navigation, refresh, customization restore/cancel, Devices filtering and the corrected Alerts empty state.
- `production_readiness_check` still fails against the intentional local development settings: DEBUG, placeholder secrets, HTTP/localhost, permissive hosts and an unset health token. Database, TimescaleDB, Redis, MQTT Dynamic Security and setup checks passed. Production TLS, secrets, endpoints and provider configuration remain separate release gates.
- The review did not submit public links, alerts, maintenance or automation forms, did not perform controlled outage injection, and did not test mobile breakpoints, permissions for every team role, billing or production TLS. These remain separate acceptance checks.
