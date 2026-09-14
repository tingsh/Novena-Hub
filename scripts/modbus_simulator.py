"""Novena hardware replay Modbus TCP field-device simulator.

Run this on Laptop 2. The Raspberry Pi CM4 gateway discovers this process on
Modbus TCP port 502, then polls the template-backed registers after Novena Hub
pushes connector config.

Default setup:
  python -m pip install "pymodbus==3.8.0"
  python scripts/modbus_simulator.py --host 10.0.0.20 --port 502 --scenario factory --mode normal

Use an elevated/admin shell for port 502 on Windows, macOS, or Linux.
"""

import argparse
import random
import struct
import threading
import time

try:
    from pymodbus.datastore import ModbusSlaveContext
except ImportError:  # pymodbus >= 3.10 renamed this class.
    from pymodbus.datastore import ModbusDeviceContext as ModbusSlaveContext

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import StartTcpServer

try:
    from pymodbus.device import ModbusDeviceIdentification
except ImportError:
    try:
        from pymodbus.pdu.device import ModbusDeviceIdentification
    except ImportError:
        ModbusDeviceIdentification = None


POWER_METER_REGISTERS = {
    "current": 3000,
    "voltage": 3028,
    "active_power": 3060,
    "frequency": 3100,
    "energy": 3200,
}

COLD_ROOM_REGISTERS = {
    "temperature": 3000,
    "humidity": 3002,
}

COLD_ROOM_COILS = {
    "door_open": 10,
    "compressor_status": 11,
}

CHILLER_REGISTERS = {
    "temperature": 100,
    "active_power": 102,
    "run_hours": 103,
}

CHILLER_COILS = {
    "compressor_status": 101,
}


def float_to_registers(value: float):
    """Pack a 32-bit float into two big-endian 16-bit Modbus registers."""
    packed = struct.pack(">f", float(value))
    high_word = struct.unpack(">H", packed[0:2])[0]
    low_word = struct.unpack(">H", packed[2:4])[0]
    return [high_word, low_word]


def make_identity(scenario):
    if ModbusDeviceIdentification is None:
        return None
    products = {
        "factory": ("NPM-100", "Novena PM-100 power meter"),
        "cold": ("NCS-100", "Novena cold room sensor"),
        "facilities": ("NPM-100", "Novena PM-100 power meter"),
    }
    product_code, product_name = products[scenario]
    identity = ModbusDeviceIdentification()
    identity.VendorName = "Novena"
    identity.ProductCode = product_code
    identity.ProductName = product_name
    identity.ModelName = product_code
    identity.MajorMinorRevision = "2026.07"
    return identity


def set_float(store, address, value):
    # Pymodbus 3.8 device contexts translate client address N to data-block
    # address N+1. Store replay values at that translated address so the
    # documented register map remains correct on the wire.
    store.setValues(address + 1, float_to_registers(value))


def set_coil(store, address, value):
    store.setValues(address + 1, [bool(value)])


def update_values(hr_store, coil_store, scenario, interval_seconds, requested_mode, mode_seconds):
    energy = 43012.0
    run_hours = 1192.0
    mode = "normal" if requested_mode == "cycle" else requested_mode
    last_mode_change = time.time()

    while True:
        if requested_mode == "cycle" and time.time() - last_mode_change > mode_seconds:
            mode = {"normal": "incident", "incident": "recovery", "recovery": "normal"}[mode]
            last_mode_change = time.time()

        voltage = round(random.uniform(228.0, 235.0), 2)
        current = round(random.uniform(2.8, 4.2), 2)
        active_power = round(voltage * current, 1)
        if mode == "incident":
            current = round(random.uniform(7.2, 8.8), 2)
            active_power = round(voltage * current, 1)
        energy += active_power / 1000.0 * (interval_seconds / 3600.0)

        if scenario in ("factory", "facilities"):
            set_float(hr_store, POWER_METER_REGISTERS["current"], current)
            set_float(hr_store, POWER_METER_REGISTERS["voltage"], voltage)
            set_float(hr_store, POWER_METER_REGISTERS["active_power"], active_power)
            set_float(hr_store, POWER_METER_REGISTERS["frequency"], round(random.uniform(49.9, 50.1), 2))
            set_float(hr_store, POWER_METER_REGISTERS["energy"], round(energy, 2))

        cold_temp = round(random.uniform(2.2, 3.4), 2)
        door_open = False
        if mode == "incident":
            cold_temp = round(random.uniform(7.5, 9.2), 2)
            door_open = True
        if scenario == "cold":
            set_float(hr_store, COLD_ROOM_REGISTERS["temperature"], cold_temp)
            set_float(hr_store, COLD_ROOM_REGISTERS["humidity"], round(random.uniform(66.0, 73.0), 2))
            set_coil(coil_store, COLD_ROOM_COILS["door_open"], door_open)
            set_coil(coil_store, COLD_ROOM_COILS["compressor_status"], True)

        chiller_temp = round(random.uniform(6.4, 7.4), 2)
        if mode == "incident":
            chiller_temp = round(random.uniform(10.5, 12.0), 2)
        run_hours += interval_seconds / 3600.0
        if scenario == "facilities":
            set_float(hr_store, CHILLER_REGISTERS["temperature"], chiller_temp)
            set_coil(coil_store, CHILLER_COILS["compressor_status"], True)
            set_float(hr_store, CHILLER_REGISTERS["active_power"], round(active_power + 210, 1))
            set_float(hr_store, CHILLER_REGISTERS["run_hours"], round(run_hours, 2))

        shown = {
            "factory": f"power={active_power:.1f} W current={current:.2f} A voltage={voltage:.1f} V mode={mode}",
            "cold": f"temperature={cold_temp:.1f} degC door_open={door_open} mode={mode}",
            "facilities": f"chiller_temp={chiller_temp:.1f} degC power={active_power + 210:.1f} W mode={mode}",
        }
        print(f"[modbus-sim] {shown[scenario]}", flush=True)
        time.sleep(interval_seconds)


def build_context():
    hr_store = ModbusSequentialDataBlock(1, [0] * 10000)
    coil_store = ModbusSequentialDataBlock(1, [False] * 1000)

    slave_context = ModbusSlaveContext(co=coil_store, di=coil_store, hr=hr_store, ir=hr_store)
    try:
        context = ModbusServerContext(slaves=slave_context, single=True)
    except TypeError:
        # Pymodbus 3.10 renamed both SlaveContext and the ServerContext
        # collection argument. The imports above already alias DeviceContext.
        context = ModbusServerContext(devices=slave_context, single=True)
    return context, hr_store, coil_store


def parse_args():
    parser = argparse.ArgumentParser(description="Run the Novena Modbus TCP field-device simulator.")
    parser.add_argument("--host", default="10.0.0.20", help="IP address to bind on Laptop 2.")
    parser.add_argument("--port", type=int, default=502, help="Modbus TCP port. Use 502 for gateway discovery.")
    parser.add_argument(
        "--scenario",
        choices=("factory", "cold", "facilities"),
        default="factory",
        help="Which pilot scenario register map to serve.",
    )
    parser.add_argument(
        "--mode",
        choices=("normal", "incident", "cycle"),
        default="cycle",
        help=(
            "Fixed normal/incident values, or a repeating normal -> incident -> recovery cycle. "
            "Use fixed modes for deterministic alert and recovery evidence."
        ),
    )
    parser.add_argument(
        "--mode-seconds",
        type=float,
        default=90.0,
        help="Seconds per phase when --mode cycle is used. Default: 90.",
    )
    parser.add_argument("--interval-seconds", type=float, default=5.0, help="Seconds between value updates.")
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be greater than zero")
    if args.mode_seconds <= 0:
        parser.error("--mode-seconds must be greater than zero")
    return args


if __name__ == "__main__":
    args = parse_args()
    context, hr_store, coil_store = build_context()

    print("Novena Modbus TCP hardware replay simulator")
    print(f"Listening on {args.host}:{args.port}")
    print("Power meter registers: current=3000, voltage=3028, active_power=3060, frequency=3100, energy=3200")
    print("Cold-room registers/coils: temperature=3000, humidity=3002, door_open coil=10, compressor coil=11")
    print("Chiller registers/coils: temperature=100, compressor coil=101, active_power=102, run_hours=103")
    print(f"Operating mode: {args.mode}")
    print("Press Ctrl+C to stop.\n")

    updater = threading.Thread(
        target=update_values,
        args=(
            hr_store,
            coil_store,
            args.scenario,
            args.interval_seconds,
            args.mode,
            args.mode_seconds,
        ),
        daemon=True,
    )
    updater.start()
    time.sleep(1)

    try:
        kwargs = {"context": context, "address": (args.host, args.port)}
        identity = make_identity(args.scenario)
        if identity is not None:
            kwargs["identity"] = identity
        StartTcpServer(**kwargs)
    except PermissionError:
        print(f"\n[error] Binding to port {args.port} requires an elevated/admin shell.")
    except OSError as e:
        print(f"\n[error] Could not bind to {args.host}:{args.port}: {e}")
        print("[hint] Confirm Laptop 2 Ethernet uses the requested static IP and no other Modbus server is running.")
