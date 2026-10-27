# Novena Platform: product, market and next-stage review

**Date:** 24 October 2026
**Decision:** Prepare a **supervised, read-only 3–5 customer design-partner canary**. Do not yet invite customers to an unrestricted public trial or call the product self-service ready. Release the first customer only after the deployment and field acceptance gates below pass for that customer's exact equipment.

## Executive assessment

Novena has crossed an important threshold: a physical CM4 has been claimed through Hub, configured with a signed revision, read a Modbus simulator, delivered live telemetry to the dashboard, and exercised a local alert lifecycle. Gateway buffering and replay have also been seen on physical hardware. This is materially stronger than a dashboard demo. The Hub now includes onboarding, fleet status, history, mapped alerts, automations, maintenance, private equipment maps and a support-oriented Gateway workflow. [Current project status](agent_context/skills/novena-project-status/references/project_status.md), [factory energy replay](factory_energy_cm4_replay_2026-10-08.md), [Round 2 audit](round2_post_onboarding_platform_audit_2026-10-21.md).

The remaining uncertainty is concentrated in **field repeatability, public operations and willingness to pay**, rather than basic feature coverage. The recent cold-room run included a 91-second gap after a simulator-process restart and an earlier 11-minute gap. A 24-hour soak and actual host/cable and CM4 uplink interruptions are still open. Email was proved with a local backend, not a real recipient. The seven new exact-model energy maps are documented drafts, not field-verified products. The public DigitalOcean environment has not passed its production preflight or a restore drill. These are release gates, not reasons to build another broad feature set.

**Confidence labels used here:** built = present in code; locally tested = automated or developer workflow; hardware replayed = current CM4 plus simulated field device; customer proven = a real customer's site and independent operator. Novena has substantial evidence in the first three categories and almost none yet in the fourth.

## 1. Product readiness

| Capability | Evidence today | Verdict for canary |
| --- | --- | --- |
| Customer onboarding and first dashboard | Physical CM4 claim, discovery, signed config and first telemetry completed in local Hub; several post-onboarding defects were fixed | Strong enough for **assisted** onboarding after a clean public-deployment replay |
| Telemetry and freshness | MQTT-to-Hub history and live display passed; Hub distinguishes live Gateway from stale equipment; offline buffer/replay passed in bounded tests | Require sustained soak, real outage matrix and per-site data-quality checks |
| Alerts and response | Local cold-room threshold triggered, was acknowledged and resolved; mapped-signal alert forms improved | Require customer-approved threshold/recipient, real provider receipt and a responder walkthrough |
| Device compatibility | Modbus-first commissioning; seven OEM-backed energy map drafts; team-owned map workflow | Qualify each actual make/model/firmware/CT ratio and compare decoded values with meter or independent reference; do not market a draft as verified |
| Remote control and automation | Governed command framework and local automation tests exist | **Disable write-back** for the initial canary; use alerts and human action. Test automation only with a disposable rule after notification delivery works |
| Fleet/support operations | Gateway diagnostics, signed config, rollback, preflight and customer-safe status exist | Freeze a reproducible Hub/Gateway release, burn in each kit and provide a named support owner and spare |
| Public SaaS operations | Compose, Nginx, Mosquitto, backup scripts and readiness command exist; Compose parses | Prove actual TLS, tenant/broker isolation, secrets, monitoring, backup **and restore**, rollback and notification delivery on DigitalOcean |
| Commercial proof | S$99/S$299 public price hypotheses and business plan; no customer cohort results in this review | No evidence yet of conversion, low support cost, retention or expansion |

The older July blockers for serial-scoped MQTT identity, activation retry, tenant scoping and signed setup have code-level closure in subsequent work, but that is not a substitute for a fresh two-tenant test on the release candidate and deployed broker. [July review](cto_product_progress_review_2026-07-18.md), [current status](agent_context/skills/novena-project-status/references/project_status.md).

### Release-management issue

At review time Hub is on local `feat/template-system-v2`, **38 first-parent commits ahead of local `main`**; Gateway is on `fix/read-device-modbus-rpc`, four commits ahead of `main`. The branches are clean locally, but the production release must name an exact compatible Hub/Gateway pair, migrations and image/checksums. Building the DigitalOcean server from `main` would omit much of the tested onboarding, alert and template work. Deploying an arbitrary moving development branch would make rollback hard. Freeze and review a release candidate, record its SHAs, test it, and deploy that artifact.

### Minimum gates before the first external site

1. **Release and isolation:** Freeze Hub/Gateway SHAs and migrations. Replay account creation, claim while Gateway is offline, credential activation/revocation, serial-scoped MQTT ACLs and web tenant isolation with two disposable tenants. Keep remote writes and OTA disabled unless separately qualified.
2. **Field reliability:** On the release build, run a timestamped 24-hour CM4-to-Hub soak with sample counts, gaps, clock state, queue depth and restart logs. Test field-device process, actual host/cable loss, Gateway uplink loss, CM4 power cycle and recovery; define acceptable maximum gap and replay loss before starting. Record any residual pymodbus late-response exception.
3. **Exact equipment:** For each customer, record model, firmware, wiring, CT/PT ratio and source document; compare every displayed metric and unit to the equipment display or independent reading. Mark unsupported signals clearly. A simulator map qualifies the workflow, not a real meter.
4. **DigitalOcean:** Run `production_readiness_check` against production settings with no `FAIL`; test HTTPS/WebSocket, MQTT TLS/identity, worker/Beat/consumer, migrations, real email delivery, alert acknowledgement, off-server backup and a timed restore on a disposable environment. Rehearse application and Gateway rollback. Put basic uptime, disk, certificate-expiry, queue and database monitoring in place.
5. **Customer safety and operations:** Confirm read-only access, qualified installation, named site owner and responder, approved alert thresholds, pilot terms, support channel, incident response and device replacement. Check mobile access on the customer's actual device. Disable or restrict public shared links until password/expiry/revocation and audience are accepted.

Stripe live billing and inbound WhatsApp can be deferred **only if** the canary is contracted and invoiced manually and those product paths are disabled. They must pass their production checks before self-service paid rollout. Governed write-back is not a prerequisite for a read-only monitoring pilot, but is a prerequisite before selling control outcomes.

### The canary offer and experiment

Run a 6–8 week design-partner program: one site, one Gateway, one primary asset or identical equipment group, one measurable outcome, assisted onboarding, weekly outcome review, written support hours and an end-date conversion decision. Loan the Gateway or charge a transparent hardware/assurance fee; charge or quote qualified installation separately. A commitment fee/deposit is useful if buyers will accept it. Avoid custom code for individual sites. The first 3–5 sites should preferably share one or two equipment families; that tests repeatability. Include cold chain or HVAC only where exact hardware and alerts pass their own gates.

Measure per customer: time from physical installation to first **trusted** value, field and support hours, expected-vs-observed telemetry coverage, incident-to-action use, weekly active operator, buyer's quantified baseline and outcome, and willingness to pay the full recurring plus hardware/service price. Ask the buyer before the trial what decision would justify paying. A polite “interesting” comment is not a conversion signal.

**Suggested scale gate, to agree before recruitment:** 4/5 sites achieve trusted first data within one working day after approved physical installation; all have correct tenant attribution and no unsafe write; at least 90% of expected pilot days have fresh data excluding logged site outages; 3/5 buyers accept paid continuation or sign a dated purchase commitment; median ongoing support is under 30 minutes/customer/week after week one. Five is a tiny sample, so these are operational tripwires, not statistical proof. If any site requires bespoke code or persistent founder intervention, narrow the equipment offer before expanding to 20.

## 2. Business potential in Singapore and ASEAN

### Why the opportunity is real

Singapore reported **371,000 enterprises in 2025, of which 369,500 were SMEs**. That is a broad pool, not Novena's addressable market: most firms do not have accessible industrial assets or an urgent telemetry budget. [Singapore Department of Statistics, enterprise landscape](https://www.singstat.gov.sg/find-data/explore-data-themes/industry/enterprises-and-corporate-sector/latest-news-data).

Digital purchasing behavior is improving: IMDA reports that 95.1% of SMEs adopted at least one measured digital area in 2024, and 97% of SMEs in surveyed Industry Digital Plan sectors adopted at least one sector-specific solution. These figures support a route to market but say nothing about willingness to buy Novena. [IMDA Singapore Digital Economy Report 2025](https://www.imda.gov.sg/resources/press-releases-factsheets-and-speeches/press-releases/2025/singapore-digital-economy).

Energy is a credible opening problem. Singapore's electricity consumption and industrial/commercial breakdown are published by EMA, while government programs encourage efficiency. The Energy Efficiency Grant funds qualifying **energy-efficient equipment**; a Novena monitor is not automatically eligible. IMDA's SMEs Go Digital offers pre-approved solutions with potential grant support, but Novena has no such approval established here. Sell measured operational value and treat grant eligibility as a separate verification. [EMA energy statistics](https://www.ema.gov.sg/resources/singapore-energy-statistics/chapter3), [Enterprise Singapore EEG](https://www.enterprisesg.gov.sg/EEG), [IMDA SMEs Go Digital](https://www.imda.gov.sg/how-we-can-help/smes-go-digital/ctoaas).

ASEAN is an expansion opportunity, not a single homogeneous market. Device models, electrical practice, installer coverage, connectivity, language, currency, service expectations and data terms vary by country. An ASEAN-wide digital-economy forecast is too broad to count as Novena demand. Start with Singapore proof and choose the first neighboring market from signed buyer/installer pull; Malaysia is a practical **hypothesis**, not a committed next launch. [ASEAN digital sector](https://asean.org/our-communities/economic-community/asean-digital-sector), [ASEAN statistics](https://www.aseanstats.org/).

### Competitive reality and possible advantage

Novena faces industrial software and hardware vendors, existing BMS/SCADA installations, low-cost gateways and local integrators. Schneider offers power monitoring, reporting and alarms; EMQX Neuron offers broad industrial connectivity. A generic “we have Modbus, MQTT, dashboards and AI” claim will be easy to match. [Schneider Power Monitoring Expert](https://www.se.com/sg/en/product-range/65404-ecostruxure-power-monitoring-expert/), [EMQX Neuron documentation](https://docs.emqx.com/en/neuronex/latest/).

Novena's plausible edge is the **whole installed outcome for an underserved SME**: managed Gateway, exact-device verified map, safe local installation, plain-language exception, remote support and evidence that a person acted. A verified compatibility library and an installer/support network can compound over time. That moat remains a hypothesis until deployments become faster and cheaper with each repetition. Avoid competing on protocol count against specialist gateways or on enterprise BMS depth against incumbents.

### Can this become a multimillion-dollar company?

**Yes, plausibly; it is not yet demonstrated.** At the current public pricing hypothesis, annual recurring revenue requires meaningful distribution:

| Realized average recurring revenue per customer/month | Customers for ~S$1m ARR | Customers for ~S$3m ARR |
| ---: | ---: | ---: |
| S$99 | 842 | 2,526 |
| S$200 | 417 | 1,250 |
| S$299 | 279 | 837 |
| S$500 | 167 | 500 |

These are rounded arithmetic scenarios, **not forecasts**, and exclude one-time hardware/installation revenue. The key strategic implication: low-price one-site plans alone need a large installed and supported fleet. Multi-site expansion, paid Gateway assurance, outcome packs and partner distribution matter. At S$299/month, even S$100 of combined support, cloud, messaging, replacement allowance and channel cost leaves S$199 before sales and fixed overhead; actual pilot measurements must replace this illustration. Track gross and contribution margin separately, plus CAC payback and churn. Do not count hardware pass-through as high-quality software ARR.

The path to several million in annual revenue requires (a) one repeatable wedge with strong customer ROI; (b) standardized installation and support that scale beyond founders; (c) retention and site expansion; (d) a channel with trusted access to industrial sites; and (e) controls on support cost and quality. Valuation is a separate question and cannot be inferred from a revenue target or a few trials.

## 3. Recommended sequence

| Stage | Primary work | Exit evidence |
| --- | --- | --- |
| **Now: 2–4 weeks** | Freeze release pair, close the five gates above, qualify first exact devices and sites, deploy DigitalOcean privately | Production restore and public smoke pass; 24-hour field evidence; first kit/site signed off |
| **Canary: 6–8 weeks** | 3–5 supervised read-only design partners in Singapore, preferably one equipment family first | Trusted data, weekly operator action, real paid conversion decision, measured per-site economics |
| **Then: 10–20 sites** | Double down on the wedge with best conversion and lowest support load; train one installer/support partner; make templates field verified | Repeatable install/support playbook, retention, positive contribution after field costs, referenceable outcomes |
| **Regional entry** | Follow customer or channel pull into one ASEAN market; localize terms/support and test a partner-led cohort | Same data quality, install time, conversion and economics without founder travel for every site |
| **Platform expansion** | Add adjacent assets, multi-site benchmarking, maintenance and carefully governed control where customers already act on Novena data | Expansion revenue and workflow retention, not only more connected tags |

The next product sprint should favor **release and recovery**, exact-device qualification, alert delivery and first-use clarity. Defer broad protocol expansion, generic AI features and autonomous control until customer evidence identifies a repeatable use case. The immediate company milestone is not “IoT platform of ASEAN”; it is **five real sites that trust the readings, use the outcome and choose to pay at a supportable price**.

## Review method and limits

Reviewed the current Hub and Gateway source trees, current status/audit documents, release branches, deployment files and official market sources. `docker compose -f docker-compose.prod.yml config --quiet` passed and Django's production-settings system check passed with three allauth deprecation warnings. The production-readiness command could not complete in this shell because PostgreSQL was unavailable; it did **not** produce a passing production result. Gateway's full suite on its project virtual environment ran 182 tests with one failure: the loopback Modbus integration test could not bind a local socket under this sandbox. The first run with system Python failed import checks because it lacked project dependencies; that run is not product evidence. No DigitalOcean server, physical customer device, real notification receipt or paying customer was directly tested in this review. Product and revenue conclusions above are judgments bounded by that evidence.
