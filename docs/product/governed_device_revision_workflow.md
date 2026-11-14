# Governed post-setup device changes

## Scope for first implementation

Start with a customer-requested correction to a read-only Modbus signal map on an already installed device. Defer connection credential, topology and write-capable changes until the first customer cases and Gateway rollback behavior are agreed. Rename remains a separate immediate edit.

## Proposed state flow

1. **Draft revision:** Clone the active map and pin its parent revision. Record requester, reason, exact device/Gateway and proposed signal changes. The active connector remains unchanged.
2. **Read-only validation:** Validate address, type, unit, scale, invalid values and duplicate keys; perform bounded live reads against the draft. Store raw/decoded results, sample timestamps and Gateway identity. A failed or stale test cannot advance.
3. **Customer review:** Present a diff, current vs proposed readings, units, expected data discontinuity, rollback target and any alert/automation references. Require an authorized site manager or owner to confirm the specific revision and evidence.
4. **Apply:** Generate a signed config with an idempotency key. Mark the revision pending until the Gateway acknowledges the exact revision/checksum and fresh Hub telemetry matches the new map. Broker publish alone is insufficient.
5. **Verify or roll back:** If acknowledgement or fresh telemetry fails within the agreed window, restore the pinned active config and preserve both attempts in an audit trail. Tell the customer which revision is live and whether a local technician is needed.

## Acceptance gate

Use a test device and one agreed first-customer map correction. Prove failed read validation leaves the active connector untouched; rejected review cannot publish; a successful signed apply changes the active revision only after acknowledgement and fresh readings; a missing acknowledgement rolls back to the recorded parent. Verify the revision and actor in the activity log and direct unauthorized POST denial. This workflow is a design gate and has **not** been implemented or field validated in this branch.
