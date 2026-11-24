# Gateway support recovery

The customer dashboard's Delete Gateway action uses the existing signed release protocol. Hub quarantines the claim, the Gateway stops field connectors and erases its buffered/site state, and Hub requires an exact reset receipt plus verified broker revocation before freeing inventory. Release now invalidates the old sticker claim code. A new credential must be issued and installed on the physical Gateway before another claim.

## Online support release

An active Novena superuser with an enrolled TOTP authenticator can open **Django Admin → Gateways → Release / recovery**. Enter the exact serial, support case reference, and ownership-verification reason. This never requires the customer's login. The normal signed Gateway reset and broker revocation then run. Do not reissue a claim credential until release status is `completed` and inventory status is `released`.

## Offline physical reset

1. Open a support release in Admin first. This immediately quarantines the Hub claim. Record the case and verified physical custody of the serial-numbered CM4.
2. Stop the Gateway service on the physical CM4. An engineer with root access runs the versioned release's `novena_gateway.engineer_reset` command, passing the exact serial, case reference, and `--confirm ERASE-LOCAL-CUSTOMER-DATA`. The command refuses a running service or mismatched serial. It rewrites `/etc/novena-gateway/config.json` into an unstartable quarantine state, clears other files in that config directory except the OTA public key, and removes `/var/lib/novena-gateway` and `/var/log/novena-gateway` state. It does not free Hub ownership or revoke broker credentials.
3. A **different** TOTP-enrolled Novena superuser records the physical reset evidence in the same Admin page. Hub then attempts retained-policy clearing and verified revocation of both MQTT identities. Broker failure keeps the claim and inventory quarantined for retry.
4. Only after release completes, use the **Gateway inventory → Reissue claim credentials** Admin action. Transfer the new credential through a secure channel to the commissioning engineer and new owner. Install it in a fresh physical Gateway configuration before restarting the service. The old sticker code is invalid after release.

The local reset is intentionally destructive: queued readings, tenant configuration, runtime journals, local logs, and credential snapshots are erased. It preserves the serial, installed software, hardware settings, and manufacturer OTA public key. Keep any legally required customer records in the appropriate approved backup system before initiating a release; do not keep an unencrypted copy on the gateway.

## Verification and boundaries

- Confirm the CM4 service is stopped before the local command; after the command, `python -m novena_gateway.main --config /etc/novena-gateway/config.json --validate-only` must reject the quarantined config.
- Confirm Hub release status `completed`, inventory `released`, and both broker identities absent before issuing the new credential.
- Reclaim with the new code only; the old code must fail. A new operational password and claim ID are created at onboarding.
- Never paste claim codes or MQTT passwords in tickets, shell history, chat, or logs.
- Admin currently checks superuser status and TOTP enrollment; a fresh MFA challenge and a customer-facing same-owner reconfiguration flow remain launch follow-ups.
