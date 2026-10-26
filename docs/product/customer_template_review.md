# Customer mapping and Novena review

## First-time setup without a map

Discovery identifies a Modbus endpoint, not the meaning of its registers. Guided Setup should offer a matching Novena map first, then a confirmed map from the same team. If neither exists, a customer without a register list can use **Get Novena setup help** in the equipment review row. The form records manufacturer, exact model, protocol, optional manual URL/PDF and discovery evidence. The customer can save the equipment draft while Novena investigates. A technician who has the register list can create a private map, live test it, compare decoded readings with the equipment, and confirm it.

Do not present a blank register editor as a self-service default for an operator who cannot identify addresses, data types and scaling. Probe results alone cannot establish the signal meaning.

## Sharing and publication

Customer-created templates remain team-private. On a confirmed site map, the customer may explicitly check consent and send the confirmed revision to Novena. The submission copies signal definitions and manufacturer/model/protocol metadata, with a source equipment reference for audit. The shared map excludes Gateway connection settings, IP addresses and live telemetry samples. The original mapping and template remain private and keep their current deployment binding. A missing checkbox, unconfirmed mapping, stale checksum or duplicate revision submission is rejected.

Novena staff use **Template share submissions** in Django admin as the internal queue. Review the submitted register map against the exact-model OEM document, firmware and address convention. Compare readings from representative hardware against the equipment display or an independent reference. Enter internal review notes, customer-facing compatibility notes, cross-equipment applicability evidence, and field verification evidence before choosing **Publish selected maps as field verified**. The publication action rejects missing evidence, write-capable signals and an existing current Novena map for the same manufacturer/model/protocol. It creates a separate, globally selectable, read-only `DeviceTemplate` with a catalog key and revision; it strips site-specific alert and safety bands and never changes the customer's assigned template. For a programmable PLC map, site program addresses are typically not portable: reject it with notes or keep it for that team unless the review establishes an exact reusable program/firmware scope.

The reviewer can mark a submission reviewing or reject it with notes. This queue is currently an internal admin workflow; it does not send notifications, offer customer withdrawal, or provide a customer-facing review conversation. A submitted map is not automatically verified. Revisions already assigned to devices stay pinned.

## Release and rollback

Migration `devices.0034_templatesharesubmission` adds the submission table. Apply it with the matching Hub release before allowing customer shares. Include submission rows in normal PostgreSQL backup/restore validation. On rollback to older Hub code, keep the table and submitted data; the share UI and admin queue will be unavailable until the new code returns. Do not reverse this migration while submissions exist unless they have been exported and retention is explicitly decided.
