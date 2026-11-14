# First customer equipment qualification worksheet

Use one copy per exact meter model, firmware, wiring configuration and Gateway release. The seven imported energy maps remain **OEM-documented drafts** until this worksheet contains field evidence. The first qualification candidate is the Schneider PowerLogic PM5340 Modbus TCP map; confirm that the first customer actually owns this exact model before booking a field visit. Do not substitute another PM5000 model based on family name.

| Item | Evidence to record |
| --- | --- |
| Customer/site, consent and owner | Site owner approval for read-only test and publication decision; do not copy credentials into this worksheet. |
| Nameplate and exact model | Photo, model suffix, serial (private record), hardware revision. |
| Firmware and protocol | Firmware version, Modbus TCP/RTU transport, unit ID, supported function codes. |
| Electrical installation | Single/three phase, wiring mode, CT/PT ratios and polarity, nominal voltage/current, import/export direction. |
| Register source | OEM document URL, revision, applicable model/firmware page and address convention. |
| Hub/Gateway pair | Hub commit, Gateway release, imported template revision and connector checksum. |
| Baseline comparison | Timestamped side-by-side display/read-only register/Gateway/Hub values for volts, amps, total active power and cumulative import energy, including units and scale. |
| Counter behavior | Observe midnight boundary, reconnect, rollover/reset handling and import direction. Reject negative jumps as consumption until documented. |
| Reliability | At least one hour of live 10-second samples, gap count, freshness state and recovery after a read-only disconnect. |
| Decision | Field-qualified for this exact configuration, needs map revision, or not supported; reviewer, date and customer confirmation. |

Keep nameplate photos, serials and customer documents in the private request workflow. Publish only a consented, reviewed template revision with explicit model/firmware applicability and known CT/PT, wiring and direction limits. Do not describe draft maps as plug-and-play.
