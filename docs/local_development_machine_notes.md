# Local Development Machine Notes

These notes are mostly historical. They capture quirks observed on the old Windows Laptop 1 development machine during local Novena Hub and Raspberry Pi CM4 hardware testing. The current default development host is the Ubuntu desktop at `/home/shouheng/Projects/Novena-Platform/Novena-Hub`.

## Dev Server Launch Quirks

- Django itself starts cleanly, but launching it through sandboxed PowerShell can fail when `Start-Process` inherits duplicate environment keys (`Path` and `PATH`). If this happens, launch the dev server outside the sandbox/elevated shell:
  ```powershell
  cd "D:\Novena Project\Novena"
  .\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
  ```
- Vite can fail in the sandbox with `Error: spawn EPERM` when it tries to spawn `esbuild`. If this happens, launch Vite outside the sandbox:
  ```powershell
  cd "D:\Novena Project\Novena"
  npm.cmd run dev -- --host 127.0.0.1 --force
  ```
- Docker CLI was not available on PATH during the June 2026 local test session. The project is currently being run with local services rather than Docker for day-to-day development.

## MQTT Hardware Test Setup

For the current Pi CM4 + Laptop 2 Modbus simulator test, use plain MQTT without TLS:

- Broker host from the Pi: `192.168.100.7`
- Broker port: `1883`
- TLS: off
- Authentication: local test can use anonymous/plain MQTT unless specifically testing dynamic-security credentials.

The telemetry path under test is:

```text
Laptop 2 Modbus simulator -> Pi CM4 -> Laptop 1 Mosquitto :1883 -> Novena Hub UI
```

The Windows Mosquitto service on this machine was observed listening only on `127.0.0.1:1883`, which is not reachable from the Pi over Wi-Fi. For Pi-facing tests, run a Mosquitto listener bound to the Laptop 1 LAN IP:

```powershell
& "C:\Program Files\Mosquitto\mosquitto.exe" -c "D:\Novena Project\Novena\mosquitto\lan-test.conf" -v
```

If the Pi cannot connect to `192.168.100.7:1883`, check Windows Firewall and add an inbound rule from an Administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Novena MQTT 1883" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 1883
```

## MQTT Port Policy

- `1883`: Local development and hardware testing without TLS.
- `8883`: Production MQTT over TLS.
- `1884`: Current codebase dynamic-security admin/provisioning listener. This is not the telemetry port.

The codebase currently has `MQTT_BROKER_PORT` defaulting to `1883` and `MQTT_DYNSEC_PORT` defaulting to `1884`. Production documentation already references `8883` for TLS MQTT.

## Dynamic Security Listener Note

The current Mosquitto dynamic-security design uses a separate listener on `1884` so Django can connect with admin credentials and publish provisioning commands to `$CONTROL/dynamic-security/#`, while edge devices use the normal broker listener.

This separate admin port is not strictly required by MQTT or Mosquitto. It is an isolation choice:

- Separate admin listener: easier to restrict by firewall/VPN/localhost and can have different auth policy.
- Same listener as devices (`1883` locally or `8883` in production): possible, but the dynamic-security admin account must share the public broker listener and be locked down very carefully with ACLs.

For production, prefer not exposing any admin/provisioning listener publicly. Better options are:

1. Keep a separate admin listener bound to localhost/private network only.
2. Use the production TLS listener `8883` with a tightly restricted admin client and ACLs.
3. Run provisioning from the same host/container network as Mosquitto so the control path is not internet-facing.

## Current Ubuntu Development Default

Moving forward, Novena development should default to the Ubuntu desktop native shell. Use the old Windows/WSL notes above only when deliberately reproducing that previous machine setup.

Use this project path and virtual environment:

```bash
cd ~/Projects/Novena-Platform/Novena-Hub
source ~/.venvs/novena/bin/activate
```

Do not use the old Windows `.venv` or WSL paths for current work unless the task explicitly asks about the previous Windows machine.

Current Ubuntu work should use `~/.venvs/novena/bin/python` and native Linux tools.
