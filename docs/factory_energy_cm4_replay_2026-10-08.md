# Factory-energy CM4 replay — 2026-10-08

## Result and test topology

**Core factory replay passed** on the local pre-launch setup: Ubuntu desktop Hub/Mosquitto (`192.168.0.16:1883`), Raspberry Pi CM4 Gateway (`NOV-AUDIT-FACTORY-HW`, LAN `192.168.0.20`, field Ethernet `10.0.0.10/24`), and Laptop 2 Modbus simulator (`10.0.0.20:502`). This is physical integration evidence, not a production-site qualification. The [operator runbook](hardware_replay_runbook_2026-07-09.md) remains the procedure for the next replay.

Using the in-app browser as a customer would, we released the serial from `pilot-cold-room`, reclaimed it under `pilot-factory-energy`, created the factory site, passed all seven Gateway readiness checks, discovered one Modbus TCP endpoint, selected the verified PM-100 template, and validated a read-only register. Signed configuration revision 2 became active after a retry. Guided Setup reached **Completed** with one equipment device and live data received. The equipment dashboard showed fresh voltage, current, active power, frequency and energy samples over the WebSocket stream and in history.

Laptop 2's older simulator then cycled from normal to incident and recovery. Active power rose above 1,700 W, the reviewed 1,200 W Power Spike rule opened after its 60-second hold, and the alert resolved when power returned below 1,200 W. The normal dashboard later showed approximately 760 W. These states were observed in the browser; screenshots and a timestamped evidence bundle were **not** saved, so the runbook's evidence-capture gate remains open.

## Findings and fixes

| Finding during replay | Cause and correction | Source |
| --- | --- | --- |
| Factory serial was still claimed by the cold-room pilot | Used the normal release and reclaim flow with the user's approval. Local release initially tried to revoke MQTT credentials through Dynamic Security even though provisioning was disabled. Hub now skips that call only in local debug mode; production release still requires revocation. | Hub `84da8be` |
| Earlier onboarding waited for operational/clock readiness | The Gateway heartbeat worker deadlocked during Ethernet failover while holding the network-watchdog lock. Gateway heartbeat handling and Hub's distinction between connectivity diagnostics and full operational heartbeats were fixed before this replay. The CM4 then reported clock readiness and Guided Setup capability. | Gateway `416f13d`; Hub `dd320fb` |
| First signed connector configuration rolled back | The connector tried to write its log in the read-only release directory. The systemd service now points connector logs at `/var/log/novena-gateway`. | Gateway `b90389a` |
| Rollback restored the obsolete Hub MQTT address | The persisted last-known-good file predated the local broker address and credential rotation. Connector rollback now restores the immediately previous active configuration. The CM4 was restored to `192.168.0.16` and reinstalled with the fix. | Gateway `cd34b2a` |
| A successful retry still appeared failed in Guided Setup | The Hub retained the first revision's rolled-back setup item after revision 2 became active. Reconciliation now recovers the item and confirms already-arrived telemetry in one refresh. The progress checklist waits for actual telemetry before claiming equipment communication. | Hub `2664b09`, `e916cff` |
| Two Power Spike alerts fired for the same threshold | Device creation activated a template rule before customer alert review; the later approved profile created a second rule. Guided Setup now defers template presets, and profile review disables equivalent legacy automatic rules while preserving their alert history. The legacy rule in this local pilot was disabled through the UI. | Hub `0742040` |
| Disabling that legacy rule was blocked by recipient validation | The form required a notification recipient even for an inactive rule. Inactive rules can now be saved without one. | Hub `f740652` |
| Laptop 2 rejected `--mode incident` | Its simulator file predates fixed-mode support. That older script cycles normal → incident → recovery every 90 seconds and was sufficient to complete this replay. Update Laptop 2's Hub checkout from current `main` before the next deterministic replay. | Current Hub `scripts/modbus_simulator.py` |

The Pi initially could not reach Laptop 2, although Windows showed `0.0.0.0:502` listening. Connectivity later recovered, and the CM4 successfully read a Modbus register. No Gateway or simulator protocol change was needed for that network interruption. The Gateway watchdog also logged `unknown connection 'wlan0'/'wwan0'` while trying to change route metrics; MQTT, Modbus polling and the core replay continued. Check NetworkManager connection-profile naming before relying on automatic failover at a customer site.

## Validation and source state

- Browser: all Guided Setup deployment checks completed; Go Live showed Gateway settings **Active** and live data **Received**; live values, incident alert and resolution were observed.
- Gateway: complete 170-test suite passed for the connector/rollback changes; the CM4 service stayed active with the corrected log path and broker address.
- Hub: relevant commissioning/onboarding tests and new regressions passed; Ruff and Django checks passed. The local stack health check passed for Django, Celery/Beat, MQTT consumer, Vite, Mosquitto, Redis, PostgreSQL and TimescaleDB policies.
- Both repositories' fixes were fast-forwarded and pushed to `main`: Hub `f740652`, Gateway `cd34b2a`. The CM4 checkout tracks Gateway `main` at `cd34b2a`.
- The production-readiness command still fails on this intentionally local configuration (debug/default secrets, local HTTP, no production MQTT provisioning or health token). Its database, TimescaleDB, Redis and MQTT checks passed. No customer deployment was tested.

## Next test order

1. **Update Laptop 2's Hub checkout** to current `main` after checking for local changes. Confirm `python scripts/modbus_simulator.py --help` lists `--mode`, then use fixed `normal`/`incident` phases and save the runbook screenshots/logs. The cycling script proved the behavior but is less repeatable.
2. **Close the physical CM4 offline-buffer gate** using runbook Step 6 while this factory topology is connected. Interrupt only Mosquitto, keep Modbus polling, restart the Gateway while offline, restore the broker, and prove timestamped buffered samples reached the Hub database/UI in order. Reconnection alone is insufficient.
3. Run separate **cold-chain and facilities/HVAC** physical replays, each with its correct site/team, template and simulator scenario. Release/reclaim the factory serial through the normal flow if reusing it; do not mix scenarios under one claim.
4. Test **governed write-back and failure recovery** on representative equipment, then plan-aware history and production-site preflight before a customer canary. Do not treat this local MQTT setup as customer deployment evidence.
