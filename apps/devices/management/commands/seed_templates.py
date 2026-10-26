from django.core.management.base import BaseCommand

from apps.devices.models import DeviceTemplate


class Command(BaseCommand):
    help = "Seed the database with professional device templates for common industrial equipment."

    def handle(self, *args, **options):
        templates = [
            {
                "name": "Eastron SDM630-Modbus",
                "manufacturer": "Eastron",
                "model_number": "SDM630",
                "device_type": "power_meter",
                "protocol": "modbus_rtu",
                "category": "energy",
                "register_map": {
                    "voltage": {"address": 0, "type": "float32", "unit": "V", "functionCode": 4},
                    "active_power": {"address": 12, "type": "float32", "unit": "W", "functionCode": 4},
                    "current": {"address": 6, "type": "float32", "unit": "A", "functionCode": 4},
                    "frequency": {"address": 70, "type": "float32", "unit": "Hz", "functionCode": 4},
                    "energy": {"address": 342, "type": "float32", "unit": "kWh", "functionCode": 4},
                },
                "alert_presets": [
                    {
                        "name": "Low Voltage Warning",
                        "key": "voltage",
                        "condition": "lt",
                        "threshold": 210,
                        "severity": "warning",
                    },
                    {
                        "name": "Over-Current Critical",
                        "key": "current",
                        "condition": "gt",
                        "threshold": 60,
                        "severity": "critical",
                    },
                ],
            },
            {
                "name": "Schneider PM5350",
                "manufacturer": "Schneider Electric",
                "model_number": "PM5350",
                "device_type": "power_meter",
                "protocol": "modbus_tcp",
                "category": "energy",
                "register_map": {
                    "voltage": {"address": 3028, "type": "float32", "unit": "V", "functionCode": 3},
                    "active_power": {"address": 3054, "type": "float32", "unit": "W", "functionCode": 3},
                    "current": {"address": 3000, "type": "float32", "unit": "A", "functionCode": 3},
                    "energy": {"address": 2700, "type": "int64", "unit": "Wh", "functionCode": 3},
                },
                "alert_presets": [
                    {
                        "name": "High Power Demand",
                        "key": "active_power",
                        "condition": "gt",
                        "threshold": 20000,
                        "severity": "warning",
                    }
                ],
            },
            {
                "name": "ABB B23 Energy Meter",
                "manufacturer": "ABB",
                "model_number": "B23",
                "device_type": "power_meter",
                "protocol": "modbus_rtu",
                "category": "energy",
                "register_map": {
                    "energy": {"address": 20480, "type": "int64", "unit": "Wh", "functionCode": 3},
                    "voltage": {"address": 23296, "type": "int32", "unit": "mV", "functionCode": 3},
                },
                "alert_presets": [],
            },
            {
                "name": "ABB ACS580 VFD",
                "manufacturer": "ABB",
                "model_number": "ACS580",
                "device_type": "vfd",
                "protocol": "modbus_rtu",
                "category": "factory",
                "register_map": {
                    "frequency": {"address": 1, "type": "uint16", "unit": "Hz", "functionCode": 3},
                    "output_current": {"address": 4, "type": "uint16", "unit": "A", "functionCode": 3},
                    "output_power": {"address": 5, "type": "uint16", "unit": "kW", "functionCode": 3},
                    "motor_speed": {"address": 2, "type": "uint16", "unit": "rpm", "functionCode": 3},
                    "speed_setpoint": {
                        "address": 1,
                        "type": "uint16",
                        "unit": "rpm",
                        "functionCode": 6,
                        "writable": True,
                        "control": "input",
                        "min": 0,
                        "max": 3000,
                    },
                    "run_command": {
                        "address": 0,
                        "type": "bool",
                        "functionCode": 5,
                        "writable": True,
                        "control": "toggle",
                        "labels": ["Stop", "Start"],
                    },
                },
                "alert_presets": [
                    {
                        "name": "Motor Over-Speed",
                        "key": "motor_speed",
                        "condition": "gt",
                        "threshold": 1600,
                        "severity": "critical",
                    }
                ],
            },
            {
                "name": "Generic Modbus Temp/Humidity",
                "manufacturer": "Generic",
                "model_number": "TH-01",
                "device_type": "temp_sensor",
                "protocol": "modbus_rtu",
                "category": "cold_chain",
                "register_map": {
                    "temperature": {"address": 0, "type": "int16", "scale": 0.1, "unit": "°C", "functionCode": 4},
                    "humidity": {"address": 1, "type": "int16", "scale": 0.1, "unit": "%", "functionCode": 4},
                },
                "alert_presets": [
                    {
                        "name": "Critical Heat Alert",
                        "key": "temperature",
                        "condition": "gt",
                        "threshold": 45,
                        "severity": "critical",
                    },
                    {
                        "name": "High Humidity Warning",
                        "key": "humidity",
                        "condition": "gt",
                        "threshold": 80,
                        "severity": "warning",
                    },
                ],
            },
            {
                "name": "Cold Room Temp Door Compressor Monitor",
                "manufacturer": "Novena Curated",
                "model_number": "CCR-IO-01",
                "device_type": "temp_sensor",
                "protocol": "modbus_rtu",
                "category": "cold_chain",
                "register_map": {
                    "temperature": {"address": 0, "type": "int16", "scale": 0.1, "unit": "°C", "functionCode": 4},
                    "humidity": {"address": 1, "type": "int16", "scale": 0.1, "unit": "%", "functionCode": 4},
                    "door_open": {"address": 2, "type": "bool", "functionCode": 1},
                    "compressor_status": {"address": 3, "type": "bool", "functionCode": 1},
                },
                "alert_presets": [
                    {
                        "name": "Cold Room High Temperature",
                        "key": "temperature",
                        "condition": "gt",
                        "threshold": 4,
                        "severity": "critical",
                    },
                    {
                        "name": "Door Open Too Long",
                        "key": "door_open",
                        "condition": "eq",
                        "threshold": 1,
                        "severity": "warning",
                    },
                    {
                        "name": "Compressor Inactive",
                        "key": "compressor_status",
                        "condition": "eq",
                        "threshold": 0,
                        "severity": "warning",
                    },
                ],
            },
            {
                "name": "Eastron SDM120-Modbus",
                "manufacturer": "Eastron",
                "model_number": "SDM120",
                "device_type": "power_meter",
                "protocol": "modbus_rtu",
                "category": "energy",
                "register_map": {
                    "voltage": {"address": 0, "type": "float32", "unit": "V", "functionCode": 4},
                    "active_power": {"address": 12, "type": "float32", "unit": "W", "functionCode": 4},
                    "energy": {"address": 72, "type": "float32", "unit": "kWh", "functionCode": 4},
                },
                "alert_presets": [],
            },
            {
                "name": "SolarEdge SE Series Inverter",
                "manufacturer": "SolarEdge",
                "model_number": "SE-1000",
                "device_type": "solar_inverter",
                "protocol": "modbus_tcp",
                "category": "energy",
                "register_map": {
                    "solar_generation": {"address": 40084, "type": "uint16", "unit": "W", "functionCode": 3},
                    "ac_current": {"address": 40072, "type": "uint16", "unit": "A", "functionCode": 3},
                    "temp_heatsink": {"address": 40104, "type": "int16", "unit": "°C", "functionCode": 3},
                },
                "alert_presets": [
                    {
                        "name": "Inverter Overheating",
                        "key": "temp_heatsink",
                        "condition": "gt",
                        "threshold": 85,
                        "severity": "critical",
                    }
                ],
            },
            {
                "name": "Accuenergy AcuRev 1312",
                "manufacturer": "Accuenergy",
                "model_number": "AcuRev 1312",
                "device_type": "power_meter",
                "protocol": "modbus_rtu",
                "category": "energy",
                "register_map": {
                    "active_power": {"address": 4096, "type": "int32", "unit": "W", "functionCode": 3},
                    "energy": {"address": 4128, "type": "int64", "unit": "Wh", "functionCode": 3},
                },
                "alert_presets": [],
            },
            {
                "name": "Danfoss VLT HVAC Drive",
                "manufacturer": "Danfoss",
                "model_number": "VLT-FC102",
                "device_type": "vfd",
                "protocol": "modbus_rtu",
                "category": "factory",
                "register_map": {
                    "speed_pct": {"address": 16120, "type": "uint16", "scale": 0.01, "unit": "%", "functionCode": 3},
                    "current": {"address": 16140, "type": "uint16", "scale": 0.01, "unit": "A", "functionCode": 3},
                    "power_kw": {"address": 16100, "type": "uint16", "scale": 0.01, "unit": "kW", "functionCode": 3},
                    "speed_setpoint": {
                        "address": 16120,
                        "type": "uint16",
                        "scale": 0.01,
                        "unit": "%",
                        "functionCode": 6,
                        "writable": True,
                        "control": "slider",
                        "min": 0,
                        "max": 100,
                    },
                    "run_command": {
                        "address": 0,
                        "type": "bool",
                        "functionCode": 5,
                        "writable": True,
                        "control": "toggle",
                        "labels": ["Stop", "Start"],
                    },
                },
                "alert_presets": [
                    {
                        "name": "High Component Load",
                        "key": "current",
                        "condition": "gt",
                        "threshold": 45,
                        "severity": "warning",
                    }
                ],
            },
            {
                "name": "Generic HVAC Chiller Monitor",
                "manufacturer": "Novena Curated",
                "model_number": "HVAC-CH-01",
                "device_type": "chiller",
                "protocol": "bacnet",
                "category": "factory",
                "register_map": {
                    "temperature": {"address": 100, "type": "float32", "unit": "°C", "functionCode": 3},
                    "active_power": {"address": 101, "type": "float32", "unit": "kW", "functionCode": 3},
                    "run_hours": {"address": 102, "type": "float32", "unit": "h", "functionCode": 3},
                    "compressor_status": {"address": 103, "type": "bool", "functionCode": 1},
                    "fan_status": {"address": 104, "type": "bool", "functionCode": 1},
                },
                "alert_presets": [
                    {
                        "name": "Chiller Temperature Drift",
                        "key": "temperature",
                        "condition": "gt",
                        "threshold": 9,
                        "severity": "warning",
                    },
                    {
                        "name": "Abnormal Chiller Power",
                        "key": "active_power",
                        "condition": "gt",
                        "threshold": 1500,
                        "severity": "warning",
                    },
                ],
            },
            {
                "name": "Cactus Modbus I/O Module",
                "manufacturer": "Cactus",
                "model_number": "M-7017",
                "device_type": "plc",
                "protocol": "modbus_rtu",
                "category": "factory",
                "register_map": {
                    "ai_0": {"address": 0, "type": "int16", "unit": "mV", "functionCode": 4},
                    "ai_1": {"address": 1, "type": "int16", "unit": "mV", "functionCode": 4},
                    "do_0": {
                        "address": 16,
                        "type": "bool",
                        "functionCode": 5,
                        "writable": True,
                        "control": "toggle",
                        "labels": ["OFF", "ON"],
                    },
                    "do_1": {
                        "address": 17,
                        "type": "bool",
                        "functionCode": 5,
                        "writable": True,
                        "control": "toggle",
                        "labels": ["OFF", "ON"],
                    },
                },
                "alert_presets": [],
            },
        ]

        for t_data in templates:
            # The original energy seeds have no model-specific evidence. Keep
            # existing rows intact for devices already assigned to them; the
            # energy catalog importer retires them from new selection.
            if t_data["category"] == "energy":
                _, created = DeviceTemplate.objects.get_or_create(
                    name=t_data["name"],
                    defaults={
                        "manufacturer": t_data["manufacturer"],
                        "model_number": t_data["model_number"],
                        "device_type": t_data["device_type"],
                        "protocol": t_data["protocol"],
                        "mapping_strategy": "fixed",
                        "category": "energy",
                        "register_map": t_data["register_map"],
                        "alert_presets": [],
                        "is_verified": False,
                        "is_current": False,
                        "evidence_status": "retired",
                        "source": "curated",
                    },
                )
                status = "Created retired" if created else "Preserved"
                self.stdout.write(f"{status} legacy energy template: {t_data['name']}")
                continue
            template, created = DeviceTemplate.objects.update_or_create(
                name=t_data["name"],
                defaults={
                    "manufacturer": t_data["manufacturer"],
                    "model_number": t_data["model_number"],
                    "device_type": t_data["device_type"],
                    "protocol": t_data["protocol"],
                    "mapping_strategy": "site_defined" if t_data["device_type"] == "plc" else "fixed",
                    "category": t_data["category"],
                    "register_map": t_data["register_map"],
                    "alert_presets": t_data.get("alert_presets", []),
                    "is_verified": True,
                    "source": "curated",
                },
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created template: {template.name}"))
            else:
                self.stdout.write(f"Updated template: {template.name}")

        self.stdout.write(self.style.SUCCESS("Seeding complete!"))
