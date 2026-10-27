import json
import secrets
import time
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.alerts.models import Alert, AlertRule
from apps.alerts.services import check_alerts_for_payload
from apps.devices.models import Device, DeviceTemplate, Gateway, GatewayConfig, GatewayInventory, Site
from apps.devices.services import compute_claim_code
from apps.maintenance.models import MaintenanceTicket, PreventiveSchedule, SharedTicketLink, TicketTemplate
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_OWNER
from apps.telemetry.models import GatewayLog, TelemetryData
from apps.telemetry.mqtt_parser import parse_mqtt_payload
from apps.users.models import CustomUser

AUDIT_USER_EMAIL = "pilot.audit@novena.local"
AUDIT_USER_PASSWORD = "PilotReady123!"
AUDIT_BATCH = "pilot-readiness-2026-07"


@dataclass(frozen=True)
class Scenario:
    key: str
    persona: str
    team_name: str
    team_slug: str
    site_name: str
    gateway_name: str
    sim_serial: str
    replay_serial: str
    device_specs: tuple[dict, ...]
    alert_key: str
    alert_threshold: float
    normal_payload: dict
    incident_payload: dict
    recovery_payload: dict | None


TEMPLATE_SPECS = {
    "cold_room_sensor": {
        "name": "Novena Cold Room Sensor Pack",
        "manufacturer": "Novena",
        "model_number": "NCS-100",
        "device_type": "temp_sensor",
        "protocol": "modbus_rtu",
        "category": "cold_chain",
        "is_verified": True,
        "discovery_hints": {"signature_keywords": ["cold", "temperature", "door"]},
        "register_map": {
            "temperature": {
                "label": "Temperature",
                "unit": "degC",
                "type": "float32",
                "address": 3000,
                "functionCode": 3,
                "dashboard_role": "primary",
                "normal_min": 0,
                "normal_max": 4,
            },
            "humidity": {
                "label": "Humidity",
                "unit": "%",
                "type": "float32",
                "address": 3002,
                "functionCode": 3,
                "dashboard_role": "secondary",
            },
            "door_open": {
                "label": "Door Open",
                "type": "bool",
                "address": 10,
                "functionCode": 1,
                "dashboard_role": "health",
                "labels": ["Closed", "Open"],
            },
            "compressor_status": {
                "label": "Compressor",
                "type": "bool",
                "address": 11,
                "functionCode": 1,
                "dashboard_role": "health",
                "labels": ["Off", "Running"],
            },
        },
        "alert_presets": [
            {
                "name": "High Temperature",
                "key": "temperature",
                "condition": "gt",
                "threshold": 4,
                "severity": "critical",
            }
        ],
    },
    "power_meter": {
        "name": "Novena Power Meter PM-100",
        "manufacturer": "Novena",
        "model_number": "NPM-100",
        "device_type": "power_meter",
        "protocol": "modbus_tcp",
        "category": "energy",
        "is_verified": True,
        "discovery_hints": {"signature_keywords": ["power", "meter", "pm"]},
        "register_map": {
            "voltage": {"label": "Voltage", "unit": "V", "type": "float32", "address": 3028, "functionCode": 3},
            "current": {"label": "Current", "unit": "A", "type": "float32", "address": 3000, "functionCode": 3},
            "active_power": {
                "label": "Active Power",
                "unit": "W",
                "type": "float32",
                "address": 3060,
                "functionCode": 3,
                "dashboard_role": "trend",
            },
            "frequency": {"label": "Frequency", "unit": "Hz", "type": "float32", "address": 3100, "functionCode": 3},
            "energy": {"label": "Energy", "unit": "kWh", "type": "float32", "address": 3200, "functionCode": 3},
        },
        "alert_presets": [
            {"name": "Power Spike", "key": "active_power", "condition": "gt", "threshold": 1200, "severity": "warning"}
        ],
    },
    "vfd": {
        "name": "Novena VFD Drive Pack",
        "manufacturer": "Novena",
        "model_number": "NVFD-40",
        "device_type": "vfd",
        "protocol": "modbus_rtu",
        "category": "factory",
        "is_verified": True,
        "discovery_hints": {"signature_keywords": ["vfd", "drive", "motor"]},
        "register_map": {
            "output_frequency": {
                "label": "Output Frequency",
                "unit": "Hz",
                "type": "float32",
                "address": 4000,
                "functionCode": 3,
                "dashboard_role": "trend",
            },
            "motor_speed": {
                "label": "Motor Speed",
                "unit": "rpm",
                "type": "uint16",
                "address": 4002,
                "functionCode": 3,
            },
            "run_status": {"label": "Run Status", "type": "bool", "address": 20, "functionCode": 1},
            "fault": {"label": "Fault", "type": "bool", "address": 21, "functionCode": 1},
        },
    },
    "chiller": {
        "name": "Novena Chiller Monitor",
        "manufacturer": "Novena",
        "model_number": "NCH-200",
        "device_type": "chiller",
        "protocol": "bacnet",
        "category": "factory",
        "is_verified": True,
        "discovery_hints": {"signature_keywords": ["chiller", "ahu", "hvac"]},
        "register_map": {
            "temperature": {
                "label": "Supply Temperature",
                "unit": "degC",
                "type": "float32",
                "address": 100,
                "functionCode": 3,
            },
            "compressor_status": {"label": "Compressor", "type": "bool", "address": 101, "functionCode": 1},
            "active_power": {"label": "Power Draw", "unit": "W", "type": "float32", "address": 102, "functionCode": 3},
            "run_hours": {"label": "Run Hours", "unit": "h", "type": "float32", "address": 104, "functionCode": 3},
        },
        "alert_presets": [
            {
                "name": "Chiller Temperature Drift",
                "key": "temperature",
                "condition": "gt",
                "threshold": 9,
                "severity": "warning",
            }
        ],
    },
}


SCENARIOS = (
    Scenario(
        key="cold-chain",
        persona="Cold chain operator",
        team_name="Cold Room Pilot",
        team_slug="pilot-cold-room",
        site_name="Jurong Cold Room A",
        gateway_name="Cold Room Gateway",
        sim_serial="NOV-AUDIT-COLD-SIM",
        replay_serial="NOV-AUDIT-COLD-HW",
        device_specs=(
            {
                "name": "Cold Room Temperature Sensor",
                "template": "cold_room_sensor",
                "port": "RS485-1",
                "signature": "Novena cold room sensor",
            },
        ),
        alert_key="temperature",
        alert_threshold=4.0,
        normal_payload={"temperature": 2.8, "humidity": 68.0, "door_open": False, "compressor_status": True},
        incident_payload={"temperature": 8.6, "humidity": 72.0, "door_open": True, "compressor_status": True},
        recovery_payload=None,
    ),
    Scenario(
        key="factory-energy",
        persona="Factory owner",
        team_name="Factory Energy Pilot",
        team_slug="pilot-factory-energy",
        site_name="Tuas Assembly Line",
        gateway_name="Factory Energy Gateway",
        sim_serial="NOV-AUDIT-FACTORY-SIM",
        replay_serial="NOV-AUDIT-FACTORY-HW",
        device_specs=(
            {
                "name": "Main Incomer Power Meter",
                "template": "power_meter",
                "port": "10.0.0.20:502",
                "signature": "Novena PM-100 power meter",
            },
            {"name": "Packaging Line VFD", "template": "vfd", "port": "RS485-2", "signature": "Novena VFD drive"},
        ),
        alert_key="active_power",
        alert_threshold=1200.0,
        normal_payload={"voltage": 231.5, "current": 3.2, "active_power": 740.0, "frequency": 50.0, "energy": 43012.2},
        incident_payload={
            "voltage": 229.8,
            "current": 8.4,
            "active_power": 1940.0,
            "frequency": 49.9,
            "energy": 43015.8,
        },
        recovery_payload={
            "voltage": 232.0,
            "current": 3.8,
            "active_power": 820.0,
            "frequency": 50.0,
            "energy": 43018.1,
        },
    ),
    Scenario(
        key="facilities",
        persona="Facilities manager",
        team_name="Facilities Multi-Site Pilot",
        team_slug="pilot-facilities",
        site_name="Novena Office Tower",
        gateway_name="Facilities Gateway",
        sim_serial="NOV-AUDIT-FACILITY-SIM",
        replay_serial="NOV-AUDIT-FACILITY-HW",
        device_specs=(
            {
                "name": "Level 3 Chiller Monitor",
                "template": "chiller",
                "port": "BACNET-1",
                "signature": "Novena chiller monitor",
            },
            {
                "name": "Tenant Power Meter",
                "template": "power_meter",
                "port": "10.0.1.20:502",
                "signature": "Novena PM-100 power meter",
            },
        ),
        alert_key="temperature",
        alert_threshold=9.0,
        normal_payload={"temperature": 6.9, "compressor_status": True, "active_power": 980.0, "run_hours": 1192.0},
        incident_payload={"temperature": 11.4, "compressor_status": True, "active_power": 1310.0, "run_hours": 1194.0},
        recovery_payload=None,
    ),
)


class Command(BaseCommand):
    help = "Prepare and run repeatable pilot-readiness customer simulation scenarios for Novena."

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=["prepare", "simulate", "keepalive", "snapshot"],
            help=(
                "prepare creates tenants/templates/inventory, simulate creates scenario data, "
                "keepalive refreshes simulated telemetry only, snapshot prints readiness counts."
            ),
        )
        parser.add_argument(
            "--publish-mqtt",
            action="store_true",
            help=(
                "Also publish scenario messages to the configured MQTT broker. "
                "Direct DB ingestion still runs for repeatability."
            ),
        )
        parser.add_argument(
            "--minutes",
            type=int,
            default=45,
            help="How long keepalive should refresh audit simulation data.",
        )
        parser.add_argument(
            "--interval-seconds",
            type=int,
            default=20,
            help="Seconds between keepalive refreshes.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one keepalive refresh and exit.",
        )

    def handle(self, *args, **options):
        if options["action"] == "prepare":
            self._prepare()
        elif options["action"] == "simulate":
            self._prepare()
            self._simulate(publish_mqtt=options["publish_mqtt"])
        elif options["action"] == "keepalive":
            self._keepalive(
                minutes=options["minutes"],
                interval_seconds=options["interval_seconds"],
                once=options["once"],
                publish_mqtt=options["publish_mqtt"],
            )
        else:
            self._snapshot()

    @transaction.atomic
    def _prepare(self):
        user = self._ensure_user()
        templates = self._ensure_templates()
        self.stdout.write(self.style.SUCCESS("Pilot audit user and templates are ready."))
        self.stdout.write(f"Audit login: {AUDIT_USER_EMAIL} / {AUDIT_USER_PASSWORD}")
        self.stdout.write("")

        for scenario in SCENARIOS:
            team = self._ensure_team(scenario, user)
            for serial in (scenario.sim_serial, scenario.replay_serial):
                GatewayInventory.objects.update_or_create(
                    serial_number=serial,
                    defaults={
                        "status": "unclaimed",
                        "batch": AUDIT_BATCH,
                        "notes": f"{scenario.persona} audit gateway",
                    },
                )
            self.stdout.write(self.style.HTTP_INFO(f"{scenario.persona}:"))
            self.stdout.write(f"  Team URL: /a/{team.slug}/")
            self.stdout.write(f"  Onboarding URL: /a/{team.slug}/onboarding/")
            self.stdout.write(
                f"  Simulation serial: {scenario.sim_serial} claim {compute_claim_code(scenario.sim_serial)}"
            )
            self.stdout.write(
                f"  Hardware replay serial: {scenario.replay_serial} claim {compute_claim_code(scenario.replay_serial)}"
            )

        self.stdout.write("")
        self.stdout.write(f"Prepared {len(SCENARIOS)} personas and {len(templates)} templates.")

    @transaction.atomic
    def _simulate(self, publish_mqtt=False):
        now = timezone.now()
        for scenario in SCENARIOS:
            user = self._ensure_user()
            team = self._ensure_team(scenario, user)
            self._clear_scenario_runtime(team)
            site = self._ensure_site(team, scenario)
            gateway = self._ensure_gateway(team, site, scenario)
            devices = self._ensure_devices(team, site, gateway, scenario)
            self._ensure_discovery(gateway, scenario)
            self._ensure_gateway_activity(gateway, now)
            self._ensure_maintenance_template(team, scenario)
            self._ensure_alert_rule(team, scenario, devices[0])
            self._ensure_facilities_second_site(team, user)

            self._ingest_device_payload(
                gateway, devices[0], scenario.normal_payload, now - timezone.timedelta(minutes=3), publish_mqtt
            )
            self._ingest_device_payload(
                gateway, devices[0], scenario.incident_payload, now - timezone.timedelta(minutes=1), publish_mqtt
            )
            if scenario.recovery_payload:
                self._ingest_device_payload(gateway, devices[0], scenario.recovery_payload, now, publish_mqtt)

            for extra_device in devices[1:]:
                payload = self._secondary_payload(extra_device)
                self._ingest_device_payload(
                    gateway, extra_device, payload, now - timezone.timedelta(seconds=30), publish_mqtt
                )

            self._ensure_preventive_schedule(team, user, scenario, devices[0])
            self._ensure_shared_link(team, user, scenario)
            self.stdout.write(self.style.SUCCESS(f"Simulated {scenario.persona}: /a/{team.slug}/"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Pilot-readiness simulation data is ready. Run snapshot for counts."))

    def _snapshot(self):
        for scenario in SCENARIOS:
            team = Team.objects.filter(slug=scenario.team_slug).first()
            if not team:
                self.stdout.write(self.style.WARNING(f"{scenario.persona}: not prepared yet"))
                continue
            sites = Site.objects.filter(team=team).count()
            gateways = Gateway.objects.filter(team=team).count()
            devices = Device.objects.filter(team=team).count()
            points = TelemetryData.objects.filter(device__team=team).count()
            alerts = Alert.objects.filter(team=team).count()
            active_alerts = Alert.objects.filter(team=team, status="active").count()
            tickets = MaintenanceTicket.objects.filter(team=team).count()
            shared_links = SharedTicketLink.objects.filter(team=team).count()
            self.stdout.write(self.style.HTTP_INFO(f"{scenario.persona} ({team.slug})"))
            self.stdout.write(f"  URL: /a/{team.slug}/")
            self.stdout.write(f"  Sites {sites} | Gateways {gateways} | Devices {devices} | Telemetry points {points}")
            self.stdout.write(
                f"  Alerts {alerts} ({active_alerts} active) | Tickets {tickets} | Shared links {shared_links}"
            )
            for gateway in Gateway.objects.filter(team=team).order_by("name"):
                self.stdout.write(f"  Gateway {gateway.serial_number}: {gateway.freshness.display}")
            for device in Device.objects.filter(team=team).order_by("name"):
                self.stdout.write(f"  Device {device.name}: {device.freshness.display}")

    def _keepalive(self, minutes=45, interval_seconds=20, once=False, publish_mqtt=False):
        self._prepare()
        interval_seconds = max(1, int(interval_seconds or 20))
        end_at = timezone.now() + timezone.timedelta(minutes=max(1, int(minutes or 45)))
        self.stdout.write(
            self.style.WARNING(
                "Simulation keepalive only refreshes pilot audit simulation gateways. "
                "It does not change production freshness thresholds or hardware replay behavior."
            )
        )

        while True:
            refreshed = self._keepalive_once(publish_mqtt=publish_mqtt)
            self.stdout.write(self.style.SUCCESS(f"Refreshed {refreshed} simulated device payload(s)."))
            if once or timezone.now() >= end_at:
                break
            time.sleep(interval_seconds)

    @transaction.atomic
    def _keepalive_once(self, publish_mqtt=False):
        now = timezone.now()
        refreshed = 0
        for scenario in SCENARIOS:
            team = Team.objects.filter(slug=scenario.team_slug).first()
            if not team:
                continue
            gateway = Gateway.objects.filter(team=team, serial_number=scenario.sim_serial).first()
            if not gateway:
                continue
            devices = list(Device.objects.filter(team=team, gateway=gateway).select_related("template").order_by("id"))
            if not devices:
                continue

            primary_payload = scenario.recovery_payload or scenario.incident_payload
            self._ingest_device_payload(
                gateway,
                devices[0],
                primary_payload,
                now,
                publish_mqtt,
                evaluate_alerts=False,
            )
            refreshed += 1

            for extra_device in devices[1:]:
                self._ingest_device_payload(
                    gateway,
                    extra_device,
                    self._secondary_payload(extra_device),
                    now,
                    publish_mqtt,
                    evaluate_alerts=False,
                )
                refreshed += 1
        return refreshed

    def _ensure_user(self):
        user, created = CustomUser.objects.get_or_create(
            email=AUDIT_USER_EMAIL,
            defaults={"username": AUDIT_USER_EMAIL},
        )
        user.username = user.username or AUDIT_USER_EMAIL
        user.set_password(AUDIT_USER_PASSWORD)
        user.save()
        if created:
            self.stdout.write(f"Created audit user {AUDIT_USER_EMAIL}")
        return user

    def _clear_scenario_runtime(self, team):
        SharedTicketLink.objects.filter(team=team).delete()
        MaintenanceTicket.objects.filter(team=team).delete()
        PreventiveSchedule.objects.filter(team=team).delete()
        Alert.objects.filter(team=team).delete()
        AlertRule.objects.filter(team=team).delete()
        TelemetryData.objects.filter(device__team=team).delete()
        GatewayLog.objects.filter(gateway__team=team).delete()
        GatewayConfig.objects.filter(team=team).delete()

    def _ensure_team(self, scenario, user):
        team, _ = Team.objects.update_or_create(slug=scenario.team_slug, defaults={"name": scenario.team_name})
        Membership.objects.update_or_create(team=team, user=user, defaults={"role": ROLE_OWNER})
        return team

    def _ensure_templates(self):
        templates = {}
        for key, spec in TEMPLATE_SPECS.items():
            defaults = dict(spec)
            name = defaults.pop("name")
            template, _ = DeviceTemplate.objects.update_or_create(name=name, defaults=defaults)
            templates[key] = template
        return templates

    def _ensure_site(self, team, scenario):
        site, _ = Site.objects.update_or_create(
            team=team,
            name=scenario.site_name,
            defaults={"address": "Singapore pilot audit site", "timezone": "Asia/Singapore"},
        )
        return site

    def _ensure_gateway(self, team, site, scenario):
        GatewayInventory.objects.update_or_create(
            serial_number=scenario.sim_serial,
            defaults={
                "status": "claimed",
                "batch": AUDIT_BATCH,
                "claimed_by_team": team,
                "notes": "Simulation gateway",
            },
        )
        gateway, _ = Gateway.objects.update_or_create(
            serial_number=scenario.sim_serial,
            defaults={
                "team": team,
                "site": site,
                "name": scenario.gateway_name,
                "access_token": secrets.token_hex(20),
                "mqtt_username": scenario.sim_serial,
                "mqtt_password": make_password("pilot-sim-password"),
                "status": "online",
                "lifecycle_status": "active",
                "last_seen": timezone.now(),
                "firmware_version": "audit-sim-1.0.0",
                "mqtt_provisioning_status": "success",
                "credential_rotation_status": "success",
                "active_interface": "eth0",
                "ethernet_status": "connected",
                "wifi_status": "standby",
                "fourg_status": "standby",
            },
        )
        GatewayInventory.objects.filter(serial_number=scenario.sim_serial).update(gateway=gateway)
        return gateway

    def _ensure_devices(self, team, site, gateway, scenario):
        devices = []
        for spec in scenario.device_specs:
            template = DeviceTemplate.objects.get(name=TEMPLATE_SPECS[spec["template"]]["name"])
            device, _ = Device.objects.update_or_create(
                team=team,
                gateway=gateway,
                name=spec["name"],
                defaults={
                    "site": site,
                    "template": template,
                    "device_type": template.device_type,
                    "protocol": template.protocol,
                    "port": spec["port"],
                    "connection_config": template.register_map,
                    "status": "online",
                    "metadata": {"pilot_audit": True, "persona": scenario.persona},
                    "discovery_meta": {
                        "signature": spec["signature"],
                        "interface": spec["port"],
                        "connection": template.protocol,
                    },
                },
            )
            devices.append(device)
        return devices

    def _ensure_discovery(self, gateway, scenario):
        devices = []
        interfaces = []
        for index, spec in enumerate(scenario.device_specs, start=1):
            template = DeviceTemplate.objects.get(name=TEMPLATE_SPECS[spec["template"]]["name"])
            devices.append(
                {
                    "interface": spec["port"],
                    "connection": template.protocol,
                    "slave_id": index,
                    "baud_rate": 9600 if template.protocol == "modbus_rtu" else None,
                    "signature": spec["signature"],
                    "matched_template_id": template.id,
                    "matched_template_name": template.name,
                    "identification": {"manufacturer": template.manufacturer, "model": template.model_number},
                }
            )
            interfaces.append({"name": spec["port"], "status": "detected"})
        gateway.discovery_data = {
            "last_discovered_at": timezone.now().isoformat(),
            "scan_ts": int(timezone.now().timestamp() * 1000),
            "scan_type": "pilot_audit",
            "interfaces": interfaces,
            "devices": devices,
            "errors": [],
        }
        gateway.connected_devices = [spec["name"] for spec in scenario.device_specs]
        gateway.active_connectors = [{"name": spec["name"], "status": "running"} for spec in scenario.device_specs]
        gateway.save(update_fields=["discovery_data", "connected_devices", "active_connectors"])

    def _ensure_gateway_activity(self, gateway, now):
        GatewayLog.objects.get_or_create(
            gateway=gateway,
            timestamp=now,
            level="INFO",
            logger_name="pilot.audit",
            message="Pilot audit gateway online, discovery complete, config applied.",
            defaults={"module": "pilot_readiness_audit"},
        )
        GatewayConfig.objects.get_or_create(
            team=gateway.team,
            gateway=gateway,
            request_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"pilot-config-{gateway.serial_number}"),
            defaults={
                "config_json": {"scenario": "pilot-readiness", "devices": gateway.connected_devices},
                "action": "full_update",
                "status": "success",
                "acknowledged_at": now,
            },
        )

    def _ensure_maintenance_template(self, team, scenario):
        template, _ = TicketTemplate.objects.update_or_create(
            team=team,
            name=f"{scenario.persona} response checklist",
            defaults={
                "description": "Pilot audit checklist for turning telemetry incidents into clear maintenance action.",
                "estimated_duration_minutes": 45,
                "checklist": [
                    {"task": "Confirm affected device and latest reading", "required": True},
                    {"task": "Inspect equipment locally or assign contractor", "required": True},
                    {"task": "Record corrective action and close ticket", "required": True},
                ],
            },
        )
        return template

    def _ensure_alert_rule(self, team, scenario, device):
        maintenance_template = self._ensure_maintenance_template(team, scenario)
        rule, _ = AlertRule.objects.update_or_create(
            team=team,
            device=device,
            telemetry_key=scenario.alert_key,
            defaults={
                "name": f"{device.name} pilot threshold",
                "site": device.site,
                "condition": "gt",
                "threshold": scenario.alert_threshold,
                "duration_seconds": 0,
                "severity": "critical" if scenario.key == "cold-chain" else "warning",
                "is_active": True,
                "notify_email": False,
                "notify_whatsapp": False,
                "notify_webhook": "",
                "create_maintenance_ticket": True,
                "maintenance_template": maintenance_template,
                "cooldown_minutes": 0,
            },
        )
        return rule

    def _ensure_preventive_schedule(self, team, user, scenario, device):
        if scenario.key != "facilities":
            return None
        template = self._ensure_maintenance_template(team, scenario)
        schedule, _ = PreventiveSchedule.objects.update_or_create(
            team=team,
            device=device,
            title="Monthly chiller inspection",
            defaults={
                "template": template,
                "interval": PreventiveSchedule.IntervalChoices.MONTHLY,
                "next_due_at": timezone.now() + timezone.timedelta(days=14),
                "is_active": True,
                "assigned_to": user,
                "is_usage_based": True,
                "usage_telemetry_key": "run_hours",
                "usage_threshold": 1200,
                "last_trigger_usage_value": 1190,
                "send_email_notification": False,
                "send_whatsapp_notification": False,
            },
        )
        return schedule

    def _ensure_shared_link(self, team, user, scenario):
        ticket = MaintenanceTicket.objects.filter(team=team).order_by("-created_at").first()
        if not ticket:
            return None
        link, _ = SharedTicketLink.objects.get_or_create(
            team=team,
            ticket=ticket,
            defaults={"created_by": user, "is_active": True},
        )
        return link

    def _ensure_facilities_second_site(self, team, user):
        if team.slug != "pilot-facilities":
            return
        Site.objects.update_or_create(
            team=team,
            name="Orchard Retail Annex",
            defaults={"address": "Second-site audit target", "timezone": "Asia/Singapore"},
        )
        viewer, _ = CustomUser.objects.get_or_create(
            email="pilot.viewer@novena.local",
            defaults={"username": "pilot.viewer@novena.local"},
        )
        viewer.set_password("PilotViewer123!")
        viewer.save()
        Membership.objects.update_or_create(team=team, user=viewer, defaults={"role": "viewer"})

    def _ingest_device_payload(self, gateway, device, values, timestamp, publish_mqtt, evaluate_alerts=True):
        payload = {
            "serial_number": gateway.serial_number,
            "device_id": device.id,
            "device_name": device.name,
            "ts": int(timestamp.timestamp() * 1000),
            "values": {"device_name": device.name, **values},
        }
        if publish_mqtt:
            topic = f"v1/gateway/{gateway.serial_number}/telemetry"
            transaction.on_commit(lambda topic=topic, payload=payload: self._publish_mqtt(topic, payload))

        for event in parse_mqtt_payload(
            f"v1/gateway/{gateway.serial_number}/telemetry",
            payload,
            trusted_gateway_sn=gateway.serial_number,
        ):
            event_timestamp = event.get("timestamp") or timestamp
            event_values = dict(event.get("values") or {})
            event_values.pop("device_name", None)
            telemetry_rows = []
            for key, value in event_values.items():
                row = TelemetryData(device=device, timestamp=event_timestamp, key=key, cloud_received_at=timezone.now())
                if isinstance(value, bool):
                    row.value_bool = value
                elif isinstance(value, int | float):
                    row.value_numeric = float(value)
                else:
                    row.value_string = str(value)
                telemetry_rows.append(row)
            TelemetryData.objects.bulk_create(telemetry_rows)
            gateway.last_seen = timezone.now()
            gateway.status = "online"
            gateway.save(update_fields=["last_seen", "status"])
            device.last_telemetry_at = event_timestamp
            if device.status != "alarm":
                device.status = "online"
            device.save(update_fields=["last_telemetry_at", "status"])
            if evaluate_alerts:
                for key, value in event_values.items():
                    check_alerts_for_payload(device, key, value)

    def _secondary_payload(self, device):
        if device.device_type == "vfd":
            return {"output_frequency": 48.2, "motor_speed": 1450, "run_status": True, "fault": False}
        if device.device_type == "power_meter":
            return {"voltage": 230.4, "current": 2.8, "active_power": 620.0, "frequency": 50.0, "energy": 10810.5}
        return {"temperature": 7.2, "compressor_status": True, "active_power": 760.0, "run_hours": 840.0}

    def _publish_mqtt(self, topic, payload):
        try:
            import paho.mqtt.client as mqtt

            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.connect(settings.MQTT_BROKER_HOST, settings.MQTT_BROKER_PORT, 30)
            client.publish(topic, json.dumps(payload))
            client.disconnect()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"MQTT publish skipped: {exc}"))
