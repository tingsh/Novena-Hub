# Template system v2 · operator and product contract

## Customer journey

Guided Setup keeps the existing equipment sequence: discover, choose a map, test live readings, confirm, deploy, verify telemetry. A Modbus discovery result is an endpoint, not proof of register meaning. The picker offers Novena maps and private team maps; a customer can build a private draft from an OEM register list or request paid Novena help. The library also accepts help requests before a Gateway exists.

Private team maps are saved only from a confirmed per-device revision. The snapshot contains read-only signal definitions and exact model, firmware, PLC-program and configuration notes, but no IP address, serial interface, connection settings or live samples. A device that selects the snapshot receives a new unconfirmed site map and must pass its own live read. Changing the team map creates a new catalog revision; devices on an older revision remain assigned. PLC maps need an identified program/register assignment. Discovery never suggests a site-defined PLC map from manufacturer or protocol alone.

The evidence labels are distinct: a Novena field-verified map has representative equipment proof; an OEM documented draft has document review but not field proof; a team map is private; and a Novena-assisted map is a paid, private deliverable. Only field-verified maps qualify for the energy-impact source calculations. A customer may separately opt in to share a confirmed private revision for Novena review; paying for assistance does not grant publication consent or a verified mark.

## Paid assistance workflow

Any plan may submit a free assessment. Collect the brand, exact model or nameplate, equipment purpose, desired readings, protocol and firmware/program if known, and optional manual. Novena staff see the request in Django admin. The request is the source of truth when email is delayed or fails.

| State | Staff action |
| --- | --- |
| Submitted | Check scope and equipment identity. |
| More information needed | Enter a customer-visible question; customer replies from the request page. |
| Quote sent | Enter external quote reference and amount; Novena sends the quote outside Hub. |
| In progress | Record external acceptance and payment reference or explicit waiver. |
| Ready for live test | Enter normalized read-only `proposed_signals` in the admin request and run **Deliver saved signal maps**. The action creates a team-private versioned template and links it to the request. |
| Completed | Link a validated Guided Setup item and record the equipment-display/reference comparison. |
| Closed | Record why the case ended, including an existing-map resolution or unsupported equipment. |

The delivered map appears in that team's picker. The customer selects it and runs the current Gateway live test while Novena helps remotely. Requests submitted before installation remain Ready for live test until hardware is available. Staff may link a setup item in admin if the request preceded discovery. Installation, wiring and new protocol work require separate scoping. No Stripe service checkout or subscription-tier entitlement is added in this release.

## Release and privacy procedure

Migration `devices.0035` adds team-map metadata and expands equipment requests; `0036` adds equipment type and configuration notes. Legacy open/reviewing requests become Submitted. A legacy Completed row becomes Closed with an explicit explanation because the old workflow recorded no live-validation evidence. Roll back code without reversing these migrations while requests or team-map revisions exist.

Request manuals and nameplates now use `template_request_documents` private storage. The Nginx example denies the old public `/media/equipment-template-requests/` route. Before exposing the new release, run `python manage.py migrate_template_request_files` and review the legacy public files; then run `python manage.py migrate_template_request_files --apply`, verify it reports zero remaining public manuals on a second dry run, and check the authenticated download on a request. On object storage, also verify the old public objects are gone. The command checks the private copy's SHA-256 digest before deleting the public copy. Keep private media in backup and restore drills.

Use a pilot request with a real meter and another with a programmable device. Capture the OEM register source, raw and decoded live readings, comparison against the device display, staff time, quote outcome, time to first telemetry, and whether the map was reused. No map is promoted to the global verified catalog without the separate review and representative hardware proof described in `customer_template_review.md`.
