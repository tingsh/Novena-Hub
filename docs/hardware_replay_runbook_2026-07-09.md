# Novena Hardware Replay Guide - Round 2

This guide walks through the second-round live hardware/software integration test.

```text
Laptop 2 simulated Modbus device
  -> Pi CM4 Novena Gateway
  -> MQTT broker on Laptop 1 port 1883
  -> local Novena Hub on Laptop 1
```

Compatibility verdict: use this revised guide. The telemetry protocol and register
map remain compatible, but the previous 2026-08-18 instructions omitted required
CM4 Ethernet setup and current onboarding/alert steps.

Reviewed on 2026-09-01 against Hub `a8a5427` and Gateway `f224406` (or newer
descendants on `main`).

## Quick Reference

Use these values unless your network has changed.

```text
Hub URL:                    http://localhost:8000/
Onboarding entry:          http://localhost:8000/a/pilot-factory-energy/onboarding/
Hub login:                  pilot.audit@novena.local / PilotReady123!
Gateway serial:             NOV-AUDIT-FACTORY-HW
Gateway claim/password:     use GATEWAY_CLAIM_CODE printed by the Laptop 1 helper
Laptop 1 MQTT example:      192.168.100.7:1883
Pi wired test address:      10.0.0.10/24 (no wired gateway)
Laptop 2 Modbus:            10.0.0.20:502
Gateway branch:             main
```

Teacher note: `localhost` means "this same machine." Hub can use `localhost` because Django, the MQTT consumer, and Mosquitto run on Laptop 1. The Pi must use Laptop 1's LAN IP, not `localhost`, because `localhost` on the Pi means the Pi itself.

## Network Layout

```text
Laptop 1
  Role:      Novena Hub + Mosquitto MQTT broker
  LAN IP:    192.168.100.7
  MQTT:      0.0.0.0:1883

Pi CM4 Gateway
  Role:      Novena Gateway
  Wi-Fi:     same LAN as Laptop 1
  Ethernet:  same wired test network as Laptop 2

Laptop 2
  Role:      Modbus TCP simulator
  Ethernet:  10.0.0.20/24
  Modbus:    10.0.0.20:502
```

Keep three terminals open: Laptop 1 WSL, Laptop 2, and Pi CM4 SSH/terminal.

# Step 1 - Laptop 1: Prepare Hub And MQTT

## 1.1 Confirm Laptop 1 IP

Run this in **Windows PowerShell** on Laptop 1:

```powershell
ipconfig
```

Use the IPv4 address of the Wi-Fi adapter that shares the Pi's wireless LAN. The
previous working address was:

```text
192.168.100.7
```

Use your actual reachable IP in the next command. A WSL `hostname -I` address is
often a private WSL virtual address and is not automatically reachable from the Pi;
the Pi TCP check in Step 3.2 is the final authority.

## 1.2 Run The Hub Hardware-Test Helper

```bash
cd /home/shouheng/Novena-Platform/Novena-Hub
bash scripts/hardware-test/prepare_laptop1_hub.sh --mqtt-host 192.168.100.7
```

What this does:

- Sets Hub's internal MQTT connection to `localhost:1883`.
- Sets the Gateway-facing MQTT address to `192.168.100.7:1883`.
- Generates or reuses the Guided Setup signing key.
- Prints the Gateway-facing public key values.
- Starts local Hub services and runs the local health check.
- Prepares the pilot audit user and the replay Gateway inventory.

Expected success signals:

```text
Gateway-facing replay values:
GATEWAY_SERIAL=NOV-AUDIT-FACTORY-HW
GATEWAY_CLAIM_CODE=<claim-derived-from-this-Hub-environment>
GATEWAY_CONFIG_KEY_ID=local-replay-2026-08
GATEWAY_CONFIG_PUBLIC_KEY_B64=<base64-public-key>

OK: MQTT is listening on 0.0.0.0:1883 for the Pi.
Laptop 1 is prepared for hardware replay.
```

Keep all four values. The claim code can change when `GATEWAY_CLAIM_SECRET`
changes, so the helper output is authoritative; do not rely on an older copied
claim code. You will paste the claim and signing-key values into Step 3.3.

## 1.3 Verify Laptop 1 Services

```bash
bash .agents/skills/novena-local-dev/scripts/health-check.sh
ss -ltnp | grep ':1883'
```

Expected: each service prints an `[ok]` line, and Mosquitto is bound to all WSL
interfaces:

```text
[ok] mqtt-consumer-wsl running as pid ...
[ok] Django responds at http://127.0.0.1:8000/
LISTEN ... 0.0.0.0:1883 ... mosquitto
```

If MQTT shows only `127.0.0.1:1883`, the Pi will not be able to reach the broker.

## 1.4 Open A Serial-Scoped MQTT Evidence Terminal

In a second Laptop 1 WSL terminal, keep this running:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 \
  -t 'v1/gateway/NOV-AUDIT-FACTORY-HW/#' -v
```

This proves which serial-scoped attributes, commands, configuration,
acknowledgements, logs, and telemetry actually cross the broker. It is evidence,
not a replacement for verifying persistence and UI behavior in Hub.

# Step 2 - Laptop 2: Run The Modbus Simulator

## 2.1 Set Laptop 2 Ethernet IP

Configure the Ethernet adapter connected to the Pi test network:

```text
IP address: 10.0.0.20
Netmask:    255.255.255.0
Gateway:    blank
```

## 2.2 Start The Simulator

Copy `scripts/modbus_simulator.py` from the Hub repo to Laptop 2, or run it from a cloned Hub checkout.

Install the dependency:

```bash
python -m pip install "pymodbus==3.8.0"
python -c "import pymodbus; print(pymodbus.__version__)"
```

Expected version: `3.8.0`. Keep the simulator on the same qualified release as
the Gateway. The simulator contains limited compatibility fallbacks for newer
Pymodbus releases, but newer releases are not part of this hardware replay.

Start the factory power-meter simulator in a fixed normal state. Fixed mode keeps
the alert incident from happening before onboarding is complete:

```bash
python scripts/modbus_simulator.py \
  --host 10.0.0.20 \
  --port 502 \
  --scenario factory \
  --mode normal
```

Expected output:

```text
[modbus-sim] power=740.0 W current=3.20 A voltage=231.5 V mode=normal
```

Leave this running. Port `502` may require an elevated terminal on some operating systems.

# Step 3 - Pi CM4: Install Gateway Config And Service

## 3.1 Clone Or Update Gateway Main

New Pi checkout:

```bash
cd ~
git clone git@github.com:tingsh/Novena-Gateway.git
cd Novena-Gateway
```

Existing Pi checkout:

```bash
cd ~/Novena-Gateway
git fetch origin
git checkout main
git pull --ff-only origin main
```

Verify:

```bash
git status --short --branch
git log -1 --oneline
```

Expected:

```text
## main...origin/main
```

## 3.2 Configure The Pi Wired Test Address And Confirm Reachability

The Wi-Fi interface keeps the default route to Laptop 1. The direct Ethernet link
to Laptop 2 uses a separate subnet with **no gateway**. First confirm the wired
interface name; it is normally `eth0` on the CM4:

```bash
ip -brief link
ip -brief address
```

Assign a temporary address to the Pi wired interface:

```bash
PI_WIRED_IF=eth0
sudo ip link set "$PI_WIRED_IF" up
sudo ip address replace 10.0.0.10/24 dev "$PI_WIRED_IF"
ip -4 address show dev "$PI_WIRED_IF"
```

Expected:

```text
inet 10.0.0.10/24 ...
```

Teacher note: no default gateway is added on Ethernet. That prevents the test
cable from stealing the Wi-Fi route used to reach Laptop 1. This `ip address`
setting is temporary and must be repeated after a Pi reboot.

Now test both paths from the Pi, replacing the Laptop 1 address if needed:

```bash
ping -c 3 192.168.100.7
ping -c 3 10.0.0.20
timeout 3 bash -c '</dev/tcp/192.168.100.7/1883' && echo 'Laptop 1 MQTT reachable'
timeout 3 bash -c '</dev/tcp/10.0.0.20/502' && echo 'Laptop 2 Modbus reachable'
```

Expected:

```text
Laptop 1 MQTT reachable
Laptop 2 Modbus reachable
```

## 3.3 Render And Install The Local Gateway Config

Use the public key values printed by Laptop 1 in Step 1.2.

```bash
GATEWAY_CLAIM_CODE='PASTE_GATEWAY_CLAIM_CODE_FROM_LAPTOP_1'
GATEWAY_CONFIG_KEY_ID='PASTE_GATEWAY_CONFIG_KEY_ID_FROM_LAPTOP_1'
GATEWAY_CONFIG_PUBLIC_KEY_B64='PASTE_GATEWAY_CONFIG_PUBLIC_KEY_B64_FROM_LAPTOP_1'

sudo python3 install/hardware-test/render_local_replay_config.py \
  --mqtt-host 192.168.100.7 \
  --mqtt-password "$GATEWAY_CLAIM_CODE" \
  --public-key-id "$GATEWAY_CONFIG_KEY_ID" \
  --public-key-b64 "$GATEWAY_CONFIG_PUBLIC_KEY_B64" \
  --modbus-host 10.0.0.20
```

Expected:

```text
Wrote Gateway config: /etc/novena-gateway/config.json
MQTT target: 192.168.100.7:1883
Guided Setup key id: <the key id printed on Laptop 1>
Manual fallback Modbus target: 10.0.0.20:502
```

Teacher note: this helper keeps the hardening requirement intact. Hub keeps the private signing key; the Pi only receives the public key and will reject Guided Setup/config commands that are not signed by the matching private key.

## 3.4 Install Or Refresh Gateway Service

```bash
sudo NOVENA_DEPLOYMENT_MODE=local bash install.sh
```

If the installer says hardware overlays changed, reboot once:

```bash
sudo reboot
```

After reboot:

```bash
cd ~/Novena-Gateway
PI_WIRED_IF=eth0
sudo ip link set "$PI_WIRED_IF" up
sudo ip address replace 10.0.0.10/24 dev "$PI_WIRED_IF"
```

Repeat both `/dev/tcp` checks from Step 3.2 after the reboot.

## 3.5 Validate Gateway Before Starting The Test

Confirm that the service virtual environment contains the qualified Pymodbus
release. This catches an older Pi installation whose virtual environment was not
refreshed by Step 3.4:

```bash
/opt/novena-gateway/venv/bin/python -c "import pymodbus; print(pymodbus.__version__)"
```

Expected version: `3.8.0`.

```bash
/opt/novena-gateway/venv/bin/python -m novena_gateway.main \
  --config /etc/novena-gateway/config.json \
  --validate-only
```

Expected:

```text
Configuration is VALID.
```

Run preflight:

```bash
/opt/novena-gateway/venv/bin/python -m novena_gateway.main \
  --config /etc/novena-gateway/config.json \
  --preflight
```

Expected: JSON output showing Gateway hardware and network checks. Save this output as evidence.

## 3.6 Start Gateway And Watch Logs

```bash
sudo systemctl restart novena-gateway
sudo journalctl -u novena-gateway -f
```

Expected log signals:

```text
Connected to MQTT broker at 192.168.100.7:1883
Remote config handler started, listening on: v1/gateway/NOV-AUDIT-FACTORY-HW/config
Discovery is ready for signed on-demand scans; background scanning is disabled.
```

After at least one successful Modbus poll, check that the removed decoder API is
not producing warnings or a traceback:

```bash
sudo journalctl -u novena-gateway --since "10 minutes ago" --no-pager \
  | grep -E 'BinaryPayloadDecoder|DeprecationWarning|Traceback'
```

Expected: no output. A non-zero `grep` exit status is normal when no matching log
line exists.

No discovery result is expected during startup. Discovery now begins only after the
operator clicks **Scan for devices** in Hub. Gateway health reporting and configured
device telemetry polling remain active independently of discovery.

# Step 4 - Laptop 1 Browser: Claim And Complete Guided Setup

Open the onboarding entry URL:

```text
http://localhost:8000/a/pilot-factory-energy/onboarding/
```

Login:

```text
pilot.audit@novena.local / PilotReady123!
```

The screen intentionally differs based on earlier progress:

- Fresh audit team: begin onboarding, select **Factory Energy Monitoring**, then
  create the Location below.
- Earlier partial replay: click **Begin Setup**, select the existing replay Site,
  choose **Novena Gateway**, and continue to pairing. Reusing the Site also lets
  Hub resume the durable setup run instead of creating duplicate replay Sites.
- Deliberate clean-site rerun: choose **New Site**, then select the Factory Energy
  profile.

For a new Location, use:

```text
Profile:         Factory Energy Monitoring
Site name:       Tuas Assembly Line Hardware Replay
Setup goals:     Track energy usage; Get alerts for abnormal readings
Operating hours: 24/7
Address:          optional
```

Then pair the Gateway using the claim code printed by the helper:

```text
Gateway name: Factory Energy Gateway
Serial:       NOV-AUDIT-FACTORY-HW
Claim code:   <GATEWAY_CLAIM_CODE from Step 1.2>
```

Expected flow:

```text
1. Hub accepts the serial and claim code.
2. Gateway connects and sends heartbeat attributes.
3. Hub shows the Gateway online and the readiness Continue button becomes available.
4. Hub sees gateway_capabilities: ["guided_setup_v1"].
5. Continue to Equipment. Hub shows Ready to scan; click Scan for devices.
6. Hub shows Scanning connected devices and target progress.
7. Hub shows Found 1 device for 10.0.0.20:502. If it shows No devices found or Scan failed, use the manual fallback below.
8. Select the suggested Novena Power Meter PM-100 template and validate it.
9. Wait for the live validation result; deployment stays blocked until validation succeeds.
10. Click Deploy validated configuration.
11. Gateway acknowledges the signed config and starts polling Modbus.
12. Hub moves to Verify deployment and review alerts.
```

The scan button creates a new scan ID and sends a signed, serial-scoped command to
the Gateway. Retry creates a different scan ID, so a late result from the earlier
attempt cannot complete the new scan.

## 4.1 Manual Fallback If The Scan Finds Nothing

Select **Add device manually**, then enter:

```text
Equipment name:  Factory Replay Power Meter
Protocol:        Modbus TCP
Manufacturer:    Novena
Model:           NPM-100
Device type:     Power meter
Host:            10.0.0.20
Port:            502
Slave ID:        1
Timeout:         3 seconds
Byte order:      BIG
Word order:      BIG
```

Use these read-only holding-register signals from the factory simulator. Each value
is a big-endian 32-bit float occupying two registers:

```text
Key            Display label  Address  Register table   Type     Scale  Unit
current        Current        3000     Holding register float32  1      A
voltage        Voltage        3028     Holding register float32  1      V
active_power   Active Power   3060     Holding register float32  1      W
frequency      Frequency      3100     Holding register float32  1      Hz
energy         Energy         3200     Holding register float32  1      kWh
```

Save the manual equipment, run its read-only validation, confirm the decoded values,
then deploy. Manual entry bypasses device finding only; it does not bypass signed
commands, live validation, or configuration trust checks.

Expected MQTT topics:

```text
v1/gateway/NOV-AUDIT-FACTORY-HW/attributes
v1/gateway/NOV-AUDIT-FACTORY-HW/telemetry
v1/gateway/NOV-AUDIT-FACTORY-HW/config
```

# Step 5 - Verify Go-Live, Alert, And Recovery

## 5.1 Finish The Customer Onboarding Journey

On **Verify deployment and review alerts**, wait for:

```text
Gateway settings: Applied
Live data: Received
```

The first packet can take one effective polling interval. Then click **Apply
Recommendations**. If Hub says it is still waiting for the Gateway or first live
data, leave the simulator and Gateway running, wait for the status panel to update,
and click **Apply Recommendations** again.

The Go live page must show:

```text
Your live dashboard is ready
Gateway settings: Active
Live data: Received
```

Open the equipment dashboard and confirm changing `voltage`, `current`,
`active_power`, `frequency`, and `energy` values. This verifies more than MQTT
delivery: it proves broker consumption, Redis/Celery processing, database
persistence, device identity matching, and customer UI readback.

## 5.2 Trigger A Deterministic Power Incident

On Laptop 2, stop the normal simulator with `Ctrl+C`, then start fixed incident
mode:

```bash
python scripts/modbus_simulator.py \
  --host 10.0.0.20 \
  --port 502 \
  --scenario factory \
  --mode incident
```

Expected values exceed the recommended `active_power > 1200 W` rule:

```text
[modbus-sim] power=1900.0 W ... mode=incident
```

Keep incident mode running for at least two minutes. The recommended Power Spike
rule has a 60-second duration, and plan-aware polling can delay the second sample.
Confirm the elevated readings on the equipment dashboard and the Power Spike in
Hub Alerts.

## 5.3 Prove Recovery

Stop incident mode with `Ctrl+C`, then restart normal mode:

```bash
python scripts/modbus_simulator.py \
  --host 10.0.0.20 \
  --port 502 \
  --scenario factory \
  --mode normal
```

Confirm the dashboard returns below `1200 W`, telemetry remains fresh, and the
alert no longer presents the equipment as actively above threshold. Save both
incident and recovered screenshots.

# Optional Step 6 - Physical Offline Buffer Replay

Run this extension when you want to close the separate physical-CM4 buffering
gate, after the core go-live/alert/recovery path passes.

1. On Laptop 1 WSL, stop only the replay broker:

```bash
kill "$(cat .dev-pids/mosquitto-wsl.pid)"
```

2. Leave the normal Laptop 2 simulator running for at least one polling interval.
   Restart the Gateway while MQTT is still down to prove the buffer survives a
   process restart:

```bash
sudo systemctl restart novena-gateway
sudo journalctl -u novena-gateway -f
```

3. On Laptop 1, restart the local stack and recheck health:

```bash
cd /home/shouheng/Novena-Platform/Novena-Hub
bash scripts/start_wsl_dev_stack.sh
bash .agents/skills/novena-local-dev/scripts/health-check.sh
```

4. Confirm Pi logs show reconnection and SQLite replay, then confirm Hub receives
   the samples captured during the outage in timestamp order:

```text
Connected to MQTT broker at 192.168.100.7:1883
Replaying batch of ... events from SQLite...
SQLite database buffer is empty. Replay complete.
```

This extension is a pass only if buffered samples appear in Hub after reconnect;
a reconnect message by itself is transport evidence, not end-to-end persistence.

# Evidence Checklist

Capture these during the test:

```text
[ ] Laptop 1 helper output showing public key and MQTT 0.0.0.0:1883.
[ ] Laptop 1 health check output.
[ ] Laptop 2 simulator console with changing readings.
[ ] Pi wired interface showing 10.0.0.10/24 and both TCP checks passing.
[ ] Pi config-render helper output.
[ ] Pi --validate-only output.
[ ] Pi --preflight output.
[ ] Pi journal lines for MQTT connection.
[ ] Pi journal lines for scoped attributes.
[ ] Hub screenshot showing Ready to scan before the operator starts discovery.
[ ] Hub screenshot and Pi journal lines for the signed user-triggered scan and its scan ID.
[ ] Hub screenshot showing Found 1 device, or the completed manual fallback and validation.
[ ] Pi journal lines for signed config accepted/applied.
[ ] Pi journal lines for Modbus polling.
[ ] Hub screenshots for claim accepted, Gateway online, discovery match, validation, and config applied.
[ ] Hub dashboard showing live voltage/current/active power/frequency/energy.
[ ] Go-live page showing settings Active and live data Received.
[ ] Hub Power Spike alert from fixed incident mode.
[ ] Dashboard and alert recovery evidence after returning to fixed normal mode.
[ ] Optional: buffered outage samples persisted after broker and Gateway restart.
```

# Troubleshooting

## MQTT Broker Not Reachable

Symptoms:

```text
Pi /dev/tcp check fails, Gateway logs show MQTT connect errors, or Hub never sees Gateway online.
```

Check on Laptop 1:

```bash
ss -ltnp | grep ':1883'
```

Good:

```text
LISTEN ... 0.0.0.0:1883 ... mosquitto
```

Fix: rerun the Laptop 1 helper, confirm Windows firewall allows inbound TCP `1883`, and make sure no separate system Mosquitto is bound only to `127.0.0.1`.

## Wrong Laptop 1 IP

Symptoms:

```text
Laptop 1 services are healthy, but the Pi cannot ping or open 192.168.100.7:1883.
```

Check the Windows Wi-Fi IPv4 address in Windows PowerShell:

```powershell
ipconfig
```

Fix: rerun the Laptop 1 helper with the correct `--mqtt-host`, then rerun the Pi
config renderer with the same corrected IP. If WSL shows `0.0.0.0:1883` but the Pi
TCP check still fails, the remaining boundary is Windows/WSL forwarding or Windows
Firewall; `ss` inside WSL alone does not prove LAN reachability.

## Modbus Simulator Not Reachable

Symptoms:

```text
Pi cannot open 10.0.0.20:502, discovery finds nothing, or Gateway logs show Modbus TCP connection failures.
```

Check from the Pi:

```bash
ping -c 3 10.0.0.20
timeout 3 bash -c '</dev/tcp/10.0.0.20/502' && echo 'Laptop 2 Modbus reachable'
```

Fix: confirm Laptop 2 Ethernet is `10.0.0.20/24`, Pi Ethernet is
`10.0.0.10/24`, the simulator is still running, and the simulator terminal has
permission to bind port `502`.

## Scan Finds No Devices But TCP Is Reachable

Symptoms:

```text
The Pi can open 10.0.0.20:502, but Hub shows No devices found.
```

Check that the Pi Ethernet interface has an active private address in the same
directly attached network, for example `10.0.0.10/24`:

```bash
ip -4 addr show up
timeout 3 bash -c '</dev/tcp/10.0.0.20/502' && echo 'Laptop 2 Modbus reachable'
sudo journalctl -u novena-gateway -n 160 --no-pager | grep -E 'discovery|10.0.0.20|scan_id'
```

Then click **Retry scan**. Discovery deliberately scans only private physical
interfaces and at most the directly attached `/24` window. It excludes loopback,
containers, virtual interfaces, public addresses and cellular WAN. If the simulator
is routed rather than directly attached, use **Add device manually** with
`10.0.0.20:502`, slave `1`, and the register map in Step 4.1.

## Gateway Rejects Signed Config

Symptoms:

```text
Hub says config push failed, Gateway logs mention signature verification, untrusted key, or guided setup unavailable.
```

Check the Pi config:

```bash
sudo python3 - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path('/etc/novena-gateway/config.json').read_text())
print('remote_config:', cfg['features']['remote_config']['trusted_clock'], list(cfg['features']['remote_config']['trusted_config_keys']))
print('rpc:', cfg['features']['rpc']['trusted_clock'], list(cfg['features']['rpc']['trusted_command_keys']))
PY
```

Fix: copy the exact `GATEWAY_CONFIG_KEY_ID` and `GATEWAY_CONFIG_PUBLIC_KEY_B64` from Laptop 1 again, rerun the Pi config renderer, then restart `novena-gateway`.

## Telemetry Does Not Appear In Hub

Symptoms:

```text
Gateway is online, but the device dashboard is stale or empty.
```

Check Laptop 1 services:

```bash
bash .agents/skills/novena-local-dev/scripts/health-check.sh
tail -n 80 mqtt-consumer-wsl.log
```

Check Pi logs:

```bash
sudo journalctl -u novena-gateway -n 120 --no-pager
```

Fix: confirm the config was applied, Modbus polling started, and telemetry is publishing on `v1/gateway/NOV-AUDIT-FACTORY-HW/telemetry`.

## Replay Opens The Existing-Customer Setup Screen

This is expected when the audit team already has a Site. Click **Begin Setup**,
choose the existing replay Site, select **Novena Gateway**, and pair the replay
serial again. To create a deliberately separate Site, choose **New Site** instead.

```text
http://localhost:8000/a/pilot-factory-energy/onboarding/
```

After rerunning the Laptop 1 helper, pairing is required even when the old Gateway
is still visible. The helper marks the audit inventory unclaimed so Hub rejects
stale ownership until this deliberate claim step succeeds.

## Local MQTT Provisioning Shows Failed

The WSL replay listener on `1883` intentionally allows anonymous local-LAN traffic
and does not run the production Dynamic Security admin listener on `1884`.
Therefore operational-credential provisioning can show as unavailable in this
local test while serial-scoped MQTT and signed Guided Setup still work. Record this
as a scope limitation; do not count this replay as production ACL/TLS credential
validation.

# Scope Notes

This guide proves the Factory Owner Modbus TCP replay path with a Laptop 2 simulator.

Still separate tests:

```text
Cold-chain Modbus RTU hardware replay
Facilities/HVAC BACnet hardware replay
Governed write-back on representative devices
Production MQTT TLS, Dynamic Security provisioning, and serial ACL enforcement
```
