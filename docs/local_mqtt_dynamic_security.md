# Ubuntu local MQTT credential lifecycle

Dynamic Security controls broker identities and ACLs. TLS controls transport encryption.
This run uses plain MQTT on a trusted private LAN. Production TLS remains unchanged.
See the [Mosquitto Dynamic Security documentation](https://mosquitto.org/documentation/dynamic-security/)
for the broker control protocol and ACL model.

## What was missing

The installed `/etc/mosquitto/conf.d/novena-local-replay.conf` allowed anonymous
connections on `0.0.0.0:1883`. No plugin or admin listener was configured.
The Hub `.env` omitted `MQTT_PROVISIONING_REQUIRED`, whose default is false.
The September migration restored files but did not activate their broker security.
The October factory replay therefore proved release/reclaim workflow only, not revocation.

`apps/devices/activation.py` provisions only when that setting is true. Failures
keep the durable claim for retry. `gateway_release.py` completes release only
after verified revocation when enabled. False plus DEBUG permits an intentional
local bypass; false outside DEBUG leaves the release quarantined. Never disable
provisioning to clear a failed secured release.

The preserved repository/bundle JSON has legacy `roleName` fields, nested password
objects, a passwordless cloud client and broad cloud/admin permissions. It is not
an installable Mosquitto 2.0 state. It remains untouched. New local state has a fresh
private administrator credential, deny-by-default ACLs and an empty shared Gateway
role. Compatible future states can be staged with `--source`, `--admin-user` and
`--admin-password-file`; incompatible schemas, groups and unsafe shared/scoped ACLs
are refused rather than silently converted. Do not copy the historical JSON into
`/var/lib/mosquitto` or restore the migration database over the current database.

## Listener layout

| Listener | Binding | Authentication |
| --- | --- | --- |
| Gateway 1883 | Specific private LAN IPv4 address | Dynamic Security; anonymous rejected |
| Admin 1884 | 127.0.0.1 | Dynamic Security administrator |
| Hub 1885 | 127.0.0.1 | Anonymous local Hub consumer/publisher |

Both authenticated listeners reference one writable state file under
`/var/lib/mosquitto/novena-dynsec`. The installed-broker activation probe must prove
that admin changes affect the Gateway listener before changing Hub `.env`.
Keep all ports unforwarded on the router; do not add internet-facing firewall rules.
The LAN is trusted for this unencrypted test, including credentials in transit.
An admin credential remains privileged on any listener using that security state;
the separate loopback admin port does not restrict the credential itself to localhost.

## Prepare and validate without disturbing the current broker

From the Hub checkout:

```bash
cd ~/Projects/Novena-Platform/Novena-Hub
source ~/.venvs/novena/bin/activate
sudo bash scripts/hardware-test/install_local_dynsec.sh --prepare-tests
NOVENA_LIVE_MQTT_TESTS=1 NOVENA_MQTT_TEST_ROOT=/var/lib/mosquitto/novena-tests \
  DJANGO_SETTINGS_MODULE=novena_hub.settings python -m pytest \
  apps/devices/tests/test_local_dynsec_live.py -q --tb=short --disable-warnings
```

The sudo step backs up the local AppArmor override and permits only the disposable
test directory. It neither stops the system broker nor disables AppArmor. Live tests
use free loopback ports, their own state, a Django test database and the sibling
Gateway checkout. Failures must be investigated; a skipped test is not acceptance.

A private stage has been prepared at `backups/local-dynsec-staging` for
`192.168.0.16`. Recheck the desktop address before using it. To prepare a *new*
stage on another machine/address (do not overwrite an existing stage):

```bash
python scripts/hardware-test/local_dynsec.py stage \
  --lan-ip 192.168.0.16 --directory backups/local-dynsec-staging-new
```

Secret files are mode 0600 inside a mode 0700 directory. Do not `cat` them, attach
them to a PR or pass passwords to `mosquitto_pub -P`. The helper never prints secrets.

## Install during a hardware maintenance window

The next command restarts the broker and temporarily interrupts the existing
factory Gateway. Its claim and data are preserved. New state cannot know the
Gateway's current operational password; use **Retry activation** on its existing
Hub detail page after activation below. This creates fresh operational credentials
and the bootstrap identity without releasing the factory claim. Existing claim and
signing secrets are preserved. Do not run the pilot fixture preparation again.

Review the tracked installer and staged listener configuration, then run:

```bash
sudo bash scripts/hardware-test/install_local_dynsec.sh \
  "$PWD/backups/local-dynsec-staging"
python scripts/hardware-test/local_dynsec.py activate \
  --directory backups/local-dynsec-staging
bash scripts/start_wsl_dev_stack.sh
python scripts/hardware-test/check_local_mqtt.py
```

Installer behavior: refuse conflicting broker snippets/main-file auth directives
and existing initialized state; stop the broker; back up `/etc/mosquitto`, broker
data and the local AppArmor override under `/var/backups/novena-mqtt-*`; install
root-owned config and mosquitto-owned private state; reload the narrowly scoped
profile; start and inspect listeners. Failures after installation begins restore
the original config/profile. It never changes firewall rules.

Activation verifies anonymous rejection, internal connectivity, operational and
bootstrap provisioning on 1884, acceptance on 1883, revocation and explicit rejected
CONNACKs. It uses unique disposable broker identities, not customer claims, and
cleans them up. Only after passing does it back up and atomically update `.env`.
Restart Django, Celery and the MQTT consumer together to avoid mixed flag values.
The hardware helper preserves the 1885 internal port when provisioning is enabled;
the launcher fails rather than selecting the anonymous broker fallback.

For an already initialized broker, do not rerun the initial installer or copy the
staged JSON back over live state. Back up first, edit only the listener snippet if
needed, and retain the live security JSON. Reuse the private stage for activation
only if its administrator still matches the live broker.

## Dedicated physical CM4 acceptance

Use `NOV-DYNSEC-HW-TEST`, never release `NOV-AUDIT-FACTORY-HW` for this test.
Create the dedicated factory inventory entry in Django admin, claim it through the
normal Hub onboarding flow in a test team/site, and obtain its claim code into a
private file using `compute_claim_code` (do not print it into logs). Transfer the
file privately to the Pi and `chmod 600` it. Existing factory history stays attached
to the factory claim.

Run a second isolated Gateway process on the CM4, leaving the factory service and
its configuration unchanged. Create a private config directory with `mktemp -d`
and a runtime directory owned by the SSH account. On this machine:

```bash
sudo install -d -o shouheng -g shouheng -m 0700 /var/lib/novena-gateway/dynsec-test-20261010
sudo setfacl -m u:shouheng:--x /var/lib/novena-gateway
```

The temporary ACL permits traversal, not listing the factory runtime directory.
After stopping the isolated process, remove this temporary grant with
`sudo setfacl -x u:shouheng /var/lib/novena-gateway`.
The Gateway validates that every runtime path remains beneath `/var/lib/novena-gateway`.
Use the existing replay renderer with these additional arguments and the current
Hub signing public key and simulator host:

```text
--serial NOV-DYNSEC-HW-TEST
--runtime-dir /var/lib/novena-gateway/dynsec-test-20261010
--mqtt-password-file /path/to/private-claim-code
```

Keep `--mqtt-host` at the private LAN address and port 1883. The renderer backs up
existing config and isolates SQLite, configuration journals/backups, OTA and command
state. TLS stays off. Disable field connectors and network watchdog actions in the
isolated configuration. Run the installed Gateway Python with `umask 077` and the
private configuration path, let bootstrap receive the provisioned
credential, and require an operational reconnect plus fresh Hub attributes.
Privately save the provisioned config before release for the old-credential probe.

Release only the dedicated serial through the Hub. Require all of:

1. Release remains pending until broker revocation succeeds; inventory remains
   claimed on an injected admin-authentication failure (covered by the live test).
2. Successful release completes and both operational/bootstrap connections are
   refused with authentication CONNACK 134 or 135. A timeout or refused TCP socket
   is inconclusive. Use Paho reading the privately saved config; never print or put
   its password on a command line. The probe has no bootstrap fallback. On the Pi run:

   ```bash
   python install/hardware-test/probe_mqtt_auth.py --config /path/to/private-saved-config.json --expect rejected
   python install/hardware-test/probe_mqtt_auth.py --config /path/to/private-saved-config.json --bootstrap --expect rejected
   ```
3. Reclaim creates a fresh Gateway row and password; new credentials connect and
   the old operational password is still rejected. Bootstrap is intentionally
   recreated on reclaim, so check its rejection before reclaim.
4. Stop only the isolated test process and remove its temporary traversal ACL.
   Verify the factory claim remains intact; use Retry activation on its existing
   claim if needed after broker migration. Verify its heartbeat and telemetry resume.

Record timestamp, serial, claim/release IDs, activation generation, software commits,
listener addresses and CONNACK results. Store credential-bearing files privately.
Do not mark the hardware gate passed from mocked tests or from an anonymous listener.

## Rollback

Record the backup directory printed by the installer. Stop Hub processes and the
broker. Preserve the failed/new state for diagnosis before restoring anything.
Restore the previous listener snippet from `<backup>/etc-mosquitto/conf.d/`, move
`/var/lib/mosquitto/novena-dynsec` aside, restore the AppArmor local file from
`<backup>/apparmor-local` (or remove it if none existed), reload AppArmor, and restart
Mosquitto. Restore `.env` from the activation backup under `backups/mqtt-dynsec-*`,
then restart Hub processes. Do not overwrite current PostgreSQL data or broker
persistence with historical migration dumps. The old anonymous setup is a temporary
rollback and does not satisfy credential lifecycle acceptance.

## Evidence — 2026-10-08

- Focused Hub lifecycle/configuration tests: 54 passed and 10 subtests passed, including real-broker lifecycle, six partial
  revocation scenarios and response correlation/PUBACK-only rejection.
- Gateway suite: 171 passed, including private file input, dedicated serial,
  configuration backup and isolated state rendering.
- Django check and migration drift check passed with existing warnings. At that
  point production readiness still reported local settings/secrets and disabled provisioning.
- Disposable live tests passed after the operator installed narrow AppArmor file
  and TERM/KILL receive permissions. Both authenticated listeners share provisioned
  state; operational/bootstrap credentials are rejected after release, ACL isolation
  holds, real Gateway reconnect succeeds, and admin-authentication failure blocks release.
- The operator subsequently installed the broker configuration and activated Hub
  settings. On 2026-10-10, the installed-broker probe passed provisioning across
  listeners, revocation, and explicit operational/bootstrap authentication rejection.
  The actual bindings are `192.168.0.16:1883`, `127.0.0.1:1884` and `127.0.0.1:1885`.
  Provisioning is enabled; production readiness still reports expected local
  DEBUG, URL and secret settings.

## Physical acceptance — 2026-10-11 Singapore time

**Passed** at 01:30 Singapore time (2026-10-10 17:30 UTC) on Ubuntu
`192.168.0.16` and CM4 `192.168.0.20`, using dedicated serial `NOV-DYNSEC-HW-TEST`.
The CM4 used its installed Gateway runtime (`cd34b2a`) with a second private
configuration and isolated state; the factory service was never stopped or reconfigured.

- Gateway row 4 received its provisioned credential, connected, and sent a fresh
  operational heartbeat. A process restart proved persisted-credential reconnect.
- Invalid administrator authentication left release 2 in `retry`, inventory
  `claimed` and the Gateway `release_pending`. Restoring administration completed it.
- Both old operational and bootstrap credentials returned explicit CONNACK 135
  from the reachable LAN broker after release.
- Reclaim created row 5. Its fresh credential connected (CONNACK 0), while the
  saved old operational credential still returned 135. Release 3 completed and
  the new operational credential then returned 135.
- The isolated runtime stopped; dedicated inventory is released. Factory row 3
  remains claimed and resumed online heartbeats with successful credential activation.
  Factory telemetry replay was not part of this credential-only acceptance.

The physical run exposed a provisioning retry defect: repeated `addClientRole`
returned Mosquitto's `Internal error`. Hub now sets the managed operational role
list through verified `modifyClient`, preserving scoped permissions and repeatability.
The disposable regression failed before this fix and passes after it. Release
verification and fail-closed behavior are unchanged.

Final validation: **55 Hub tests + 10 subtests**, **171 Gateway tests**, Django
check (existing warnings), no migration drift, Ruff, shell syntax, Git whitespace
checks and production Compose configuration passed. Production readiness correctly
fails for local DEBUG/settings, default local secrets, activation-key fallback,
HTTP URLs, allowed hosts/CSRF and missing health token; MQTT provisioning checks pass.
No new schema, environment variable or production dependency is required.

Private evidence is in `backups/local-dynsec-hardware-20261010/evidence.json`;
credential-bearing files stay excluded from Git. CM4 evidence is retained privately
under `/tmp/novena-dynsec-hw-a848zokd` and isolated runtime state under
`/var/lib/novena-gateway/dynsec-test-20261010`. Remove the temporary traversal ACL
using the cleanup command above after the test. Do not rerun the initial broker installer.
