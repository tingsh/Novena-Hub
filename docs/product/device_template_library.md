# Device Template Library

The Hub library is a preparation and review area. Customers can search by manufacturer, exact model, model alias, protocol, equipment type, source and evidence. They can inspect signal definitions, save a specific revision for later, create a team-private draft before installing a Gateway, or request paid Novena setup help.

Live testing remains in Guided Setup. The customer must claim an online Gateway and connect the field equipment. Selecting a template for a saved discovery row changes that row to **Template selected**, clears prior validation evidence, and returns to Guided Setup. The Gateway performs an on-demand read there. Confirmation and deployment remain separate; continuous telemetry follows deployment.

## Trust and publication

- **Novena field verified:** representative equipment readings have been compared with its display or independent reference. Energy-impact calculations require this status.
- **OEM documented:** signal definitions passed staff review against an exact-model OEM source, but representative hardware evidence is not recorded.
- **Community contributed:** the private source revision was shared by explicit customer consent and published as a separate anonymous global revision after staff review. Origin and evidence appear separately.
- **Team private / Novena-assisted private:** usable only by the owning team; both need live validation for each device. Paid assistance does not imply field verification.

Staff use the existing Template share submissions queue. The **Publish as OEM documented** action requires exact manufacturer/model, HTTPS OEM source, review and compatibility notes, portability evidence, and read-only signals. **Publish as field verified** additionally requires representative hardware evidence; using that action later creates a new public revision while the documented revision remains pinned on existing devices. The original team revision and its owner remain private. Staff should not include customer site names or addresses in the public compatibility note.

## Release and rollback

Run `devices.0037` and `0038` before exposing the library. Migration `0038` retires public seeds lacking review evidence, exact identity, an HTTPS source, readable signals, or a documented PLC program, and any public template containing write controls. It does not delete rows or reassign existing devices. Review the retired counts and assigned-device impact before inviting customers; qualify replacements separately rather than reactivating a speculative entry.

Back up PostgreSQL before migration. New private drafts and saved revision references are database rows, covered by the normal database backup. They contain template definitions only, not Gateway connection addresses or telemetry. A code rollback should retain `0037`–`0038` and their rows; the older UI simply will not show the new library features. Restoring a pre-migration database snapshot would discard later drafts, bookmarks and community reviews. Do not reverse the retirement without re-reviewing the underlying templates.

Smoke test with one team that has no Gateway and one with a claimed CM4: browse, search by exact SKU and alias, save/remove, draft/publish a private revision, verify another team cannot access it, select a saved discovered item, fail/retry the live read, confirm and deploy, then verify dashboard telemetry. Check mobile navigation and keyboard operation before broad promotion.
