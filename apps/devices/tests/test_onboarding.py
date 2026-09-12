import base64
import json
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.alerts.models import AlertRule
from apps.devices.models import Device, DeviceDatapointMap, DeviceTemplate, Gateway, GatewayConfig, Site
from apps.devices.solution_profiles import apply_solution_profile_presets, rank_templates_for_profile
from apps.maintenance.models import PreventiveSchedule
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser


class OnboardingConnectionTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test-team")
        self.user = CustomUser.objects.create(email="test@example.com", username="testuser")
        Membership.objects.create(team=self.team, user=self.user, role=ROLE_ADMIN)
        self.site = Site.objects.create(team=self.team, name="Test Site")
        self.gateway = Gateway.objects.create(team=self.team, site=self.site, name="GW-1", serial_number="SN-1234")
        self.template = DeviceTemplate.objects.create(
            name="Smart Meter",
            manufacturer="Schneider Electric",
            model_number="PM5350",
            device_type="power_meter",
            protocol="modbus_tcp",
            category="energy",
            register_map={
                "voltage": {"address": 3028, "type": "float32", "functionCode": 3, "unit": "V"},
                "active_power": {"address": 3054, "type": "float32", "functionCode": 3, "unit": "W", "writable": True},
            },
        )
        self.client = Client()
        self.client.force_login(self.user)

    @override_settings(REMOTE_CONTROL_SIGNING_PRIVATE_KEY=base64.b64encode(b"1" * 32).decode())
    def test_gateway_config_endpoint_contract(self):
        url = reverse("web_team:devices:gateway_push_config", args=[self.team.slug, self.gateway.pk])
        self.gateway.gateway_capabilities = ["guided_setup_v1"]
        self.gateway.remote_control_clock_ready = True
        self.gateway.save(update_fields=["gateway_capabilities", "remote_control_clock_ready"])

        invalid = self.client.post(
            url,
            {"action": "connector_update", "config": json.dumps({"connectors": [{"name": "Missing type"}]})},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(GatewayConfig.objects.filter(gateway=self.gateway).exists())

        self.gateway.gateway_capabilities = []
        self.gateway.save(update_fields=["gateway_capabilities"])
        update_required = self.client.post(
            url,
            {"action": "connector_update", "config": json.dumps({"connectors": []})},
        )
        self.assertEqual(update_required.status_code, 409)

        self.gateway.gateway_capabilities = ["guided_setup_v1"]
        self.gateway.save(update_fields=["gateway_capabilities"])
        queued = self.client.post(
            url,
            {"action": "connector_update", "config": json.dumps({"connectors": []})},
        )
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(GatewayConfig.objects.get(gateway=self.gateway).status, "queued")

    def _victim_infrastructure(self):
        victim_team = Team.objects.create(name="Victim Team", slug="victim-team")
        victim_site = Site.objects.create(team=victim_team, name="Victim Site")
        victim_gateway = Gateway.objects.create(
            team=victim_team,
            site=victim_site,
            name="Victim GW",
            serial_number="SN-VICTIM",
            access_token="victim-token",
        )
        return victim_team, victim_site, victim_gateway

    def test_htmx_device_create_post_success(self):
        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={self.site.id}&gateway_id={self.gateway.id}&port=1"

        response = self.client.post(url, {"name": "New Test Device", "template_id": self.template.id})

        # Check HTTP response code is 200 OK (replaces legacy 204)
        self.assertEqual(response.status_code, 200)

        # Check HX-Trigger header exists
        self.assertEqual(response.headers.get("HX-Trigger"), "infrastructureChanged")

        # Check that success view content is rendered
        self.assertContains(response, "Equipment added")
        self.assertContains(response, "New Test Device")
        self.assertContains(response, "Live Connection Test")

        # Check that first readable register is identified and details shown
        self.assertContains(response, "voltage")
        self.assertContains(response, "Technician details: address 3028, function 3")

        # Check that writable active_power is NOT chosen as test_register
        self.assertNotContains(response, "active_power")

        # Check device was created in DB
        device = Device.objects.get(name="New Test Device")
        self.assertEqual(device.gateway, self.gateway)
        self.assertEqual(device.site, self.site)
        self.assertEqual(device.port, "1")

    def test_htmx_device_create_no_readable_registers(self):
        # Create a template with ONLY writable registers
        write_only_template = DeviceTemplate.objects.create(
            name="Write Only Device",
            manufacturer="Danfoss",
            model_number="VLT",
            device_type="vfd",
            protocol="modbus_rtu",
            register_map={"speed_setpoint": {"address": 10, "type": "uint16", "functionCode": 6, "writable": True}},
        )

        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={self.site.id}&gateway_id={self.gateway.id}&port=2"

        response = self.client.post(url, {"name": "Write Only Test Device", "template_id": write_only_template.id})

        self.assertEqual(response.status_code, 200)
        # Renders success page cleanly, saying no readable registers
        self.assertContains(response, "This equipment profile has no safe test reading")

    def test_htmx_device_create_rejects_cross_tenant_gateway_and_site(self):
        _, victim_site, victim_gateway = self._victim_infrastructure()
        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={victim_site.id}&gateway_id={victim_gateway.id}&port=9"

        with patch("apps.devices.views._push_gateway_config_after_commit") as push_config:
            response = self.client.post(url, {"name": "Forged Device", "template_id": self.template.id})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Device.objects.filter(name="Forged Device").exists())
        push_config.assert_not_called()

    def test_htmx_device_create_resolve_cannot_delete_victim_device(self):
        _, victim_site, victim_gateway = self._victim_infrastructure()
        victim_device = Device.objects.create(
            team=victim_gateway.team,
            site=victim_site,
            gateway=victim_gateway,
            name="Victim Meter",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
            port="9",
        )
        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={victim_site.id}&gateway_id={victim_gateway.id}&port=9&resolve=true"

        with patch("apps.devices.views._push_gateway_config_after_commit") as push_config:
            response = self.client.post(url, {"name": "Replacement", "template_id": self.template.id})

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Device.objects.filter(pk=victim_device.pk).exists())
        self.assertFalse(Device.objects.filter(name="Replacement").exists())
        push_config.assert_not_called()

    def test_htmx_device_create_rejects_same_team_gateway_site_mismatch(self):
        other_site = Site.objects.create(team=self.team, name="Other Site")
        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={other_site.id}&gateway_id={self.gateway.id}&port=3"

        response = self.client.post(url, {"name": "Mismatched Device", "template_id": self.template.id})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Device.objects.filter(name="Mismatched Device").exists())

    def test_htmx_device_create_rejects_other_team_private_template(self):
        victim_team, _, _ = self._victim_infrastructure()
        private_template = DeviceTemplate.objects.create(
            name="Victim Private Template",
            manufacturer="PrivateCo",
            model_number="P1",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1, "functionCode": 3}},
            created_by_team=victim_team,
        )
        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={self.site.id}&gateway_id={self.gateway.id}&port=4"

        with patch("apps.devices.views._push_gateway_config_after_commit") as push_config:
            response = self.client.post(url, {"name": "Private Attack", "template_id": private_template.id})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Device.objects.filter(name="Private Attack").exists())
        push_config.assert_not_called()

    def test_htmx_device_create_enforces_team_device_limit(self):
        for index in range(3):
            Device.objects.create(
                team=self.team,
                site=self.site,
                gateway=self.gateway,
                name=f"Existing Device {index}",
                device_type="power_meter",
                protocol="modbus_tcp",
            )
        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={self.site.id}&gateway_id={self.gateway.id}&port=limit-test"

        with patch("apps.devices.views._push_gateway_config_after_commit") as push_config:
            response = self.client.post(url, {"name": "Over Limit", "template_id": self.template.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You've reached your device limit")
        self.assertFalse(Device.objects.filter(team=self.team, name="Over Limit").exists())
        push_config.assert_not_called()

    def test_htmx_device_create_resolve_replaces_same_team_device(self):
        old_device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            name="Old Device",
            template=self.template,
            device_type="power_meter",
            protocol="modbus_tcp",
            port="5",
        )
        url = reverse("web_team:devices:htmx_device_create", args=[self.team.slug])
        url = f"{url}?site_id={self.site.id}&gateway_id={self.gateway.id}&port=5&resolve=true"

        response = self.client.post(url, {"name": "New Device", "template_id": self.template.id})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Device.objects.filter(pk=old_device.pk).exists())
        self.assertEqual(Device.objects.filter(team=self.team, gateway=self.gateway, port="5").count(), 1)
        self.assertTrue(
            Device.objects.filter(team=self.team, gateway=self.gateway, port="5", name="New Device").exists()
        )

    def test_template_search_hides_other_team_private_templates(self):
        victim_team, _, _ = self._victim_infrastructure()
        current_private = DeviceTemplate.objects.create(
            name="Current Private Meter",
            manufacturer="PrivateCo",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1}},
            created_by_team=self.team,
        )
        victim_private = DeviceTemplate.objects.create(
            name="Victim Private Meter",
            manufacturer="PrivateCo",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1}},
            created_by_team=victim_team,
        )
        url = reverse("web_team:devices:template_library_search", args=[self.team.slug])

        response = self.client.get(f"{url}?q=Private")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, current_private.name)
        self.assertNotContains(response, victim_private.name)

    def test_template_library_hides_other_team_private_templates(self):
        victim_team, _, _ = self._victim_infrastructure()
        current_private = DeviceTemplate.objects.create(
            name="Current Library Template",
            manufacturer="LibraryCo",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1}},
            created_by_team=self.team,
        )
        victim_private = DeviceTemplate.objects.create(
            name="Victim Library Template",
            manufacturer="LibraryCo",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1}},
            created_by_team=victim_team,
        )
        url = reverse("web_team:devices:template_library", args=[self.team.slug])

        response = self.client.get(f"{url}?q=Library")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, current_private.name)
        self.assertNotContains(response, victim_private.name)

    def test_device_create_form_rejects_cross_team_relationships(self):
        _, victim_site, victim_gateway = self._victim_infrastructure()
        url = reverse("web_team:devices:device_create", args=[self.team.slug])

        response = self.client.post(
            url,
            {
                "gateway": victim_gateway.id,
                "site": victim_site.id,
                "template": self.template.id,
                "name": "Forged Form Device",
                "device_type": "power_meter",
                "protocol": "modbus_tcp",
                "energy_category": "none",
                "connection_config": "{}",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Device.objects.filter(name="Forged Form Device").exists())

    def test_device_create_routes_site_defined_equipment_to_draft_mapping(self):
        starter = DeviceTemplate.objects.create(
            name="Siemens S7-1200 Starter",
            manufacturer="Siemens",
            model_number="S7-1200",
            device_type="plc",
            protocol="modbus_tcp",
            mapping_strategy="site_defined",
            register_map={},
        )
        url = reverse("web_team:devices:device_create", args=[self.team.slug])

        response = self.client.post(
            url,
            {
                "gateway": self.gateway.id,
                "site": self.site.id,
                "template": starter.id,
                "name": "Line PLC",
                "device_type": "plc",
                "protocol": "modbus_tcp",
                "energy_category": "none",
                "connection_config": '{"host": "192.168.1.50", "port": 502}',
            },
        )

        device = Device.objects.get(team=self.team, name="Line PLC")
        mapping = DeviceDatapointMap.objects.get(team=self.team, device=device)
        self.assertRedirects(
            response,
            reverse("web_team:devices:device_datapoint_mapping", args=[self.team.slug, device.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(mapping.status, DeviceDatapointMap.Status.DRAFT)
        self.assertEqual(mapping.datapoints, [])

        page = self.client.get(reverse("web_team:devices:device_datapoint_mapping", args=[self.team.slug, device.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Map Line PLC")
        self.assertContains(page, "Validate readings")

    def test_device_create_form_rejects_other_team_private_template(self):
        victim_team, _, _ = self._victim_infrastructure()
        private_template = DeviceTemplate.objects.create(
            name="Victim Form Template",
            manufacturer="PrivateCo",
            device_type="power_meter",
            protocol="modbus_tcp",
            register_map={"voltage": {"address": 1}},
            created_by_team=victim_team,
        )
        url = reverse("web_team:devices:device_create", args=[self.team.slug])

        response = self.client.post(
            url,
            {
                "gateway": self.gateway.id,
                "site": self.site.id,
                "template": private_template.id,
                "name": "Private Form Attack",
                "device_type": "power_meter",
                "protocol": "modbus_tcp",
                "energy_category": "none",
                "connection_config": "{}",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Device.objects.filter(name="Private Form Attack").exists())

    def test_datapoint_mapping_page_is_team_scoped(self):
        victim_team, victim_site, victim_gateway = self._victim_infrastructure()
        starter = DeviceTemplate.objects.create(
            name="Global PLC Starter",
            device_type="plc",
            protocol="modbus_tcp",
            mapping_strategy="site_defined",
            register_map={},
        )
        victim_device = Device.objects.create(
            team=victim_team,
            site=victim_site,
            gateway=victim_gateway,
            template=starter,
            name="Victim PLC",
            device_type="plc",
            protocol="modbus_tcp",
        )

        response = self.client.get(
            reverse(
                "web_team:devices:device_datapoint_mapping",
                args=[self.team.slug, victim_device.pk],
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(DeviceDatapointMap.objects.filter(device=victim_device).exists())

    def test_datapoint_csv_import_replaces_draft_atomically_and_exports(self):
        starter = DeviceTemplate.objects.create(
            name="CSV PLC Starter",
            device_type="plc",
            protocol="modbus_tcp",
            mapping_strategy="site_defined",
            register_map={},
        )
        device = Device.objects.create(
            team=self.team,
            site=self.site,
            gateway=self.gateway,
            template=starter,
            name="CSV PLC",
            device_type="plc",
            protocol="modbus_tcp",
            connection_config={"host": "192.168.1.50", "port": 502},
        )
        url = reverse("web_team:devices:device_datapoint_mapping", args=[self.team.slug, device.pk])
        valid_csv = b"key,label,address,function_code,data_type,unit\nline_speed,Line Speed,10,3,uint16,rpm\n"

        response = self.client.post(
            url,
            {
                "action": "import_csv",
                "csv_file": SimpleUploadedFile("map.csv", valid_csv, content_type="text/csv"),
            },
        )

        self.assertEqual(response.status_code, 302)
        mapping = DeviceDatapointMap.objects.get(device=device)
        self.assertEqual(mapping.status, DeviceDatapointMap.Status.DRAFT)
        self.assertEqual([point["key"] for point in mapping.datapoints], ["line_speed"])

        invalid_csv = b"key,address\nline_speed,not-an-address\n"
        response = self.client.post(
            url,
            {
                "action": "import_csv",
                "csv_file": SimpleUploadedFile("bad.csv", invalid_csv, content_type="text/csv"),
            },
            follow=True,
        )
        mapping.refresh_from_db()
        self.assertContains(response, "Row 2")
        self.assertEqual([point["key"] for point in mapping.datapoints], ["line_speed"])

        export = self.client.post(url, {"action": "export_csv"})
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Content-Type"], "text/csv; charset=utf-8")
        self.assertTrue(export.content.startswith(b"\xef\xbb\xbfkey,label,address"))
        self.assertIn(b"line_speed,Line Speed,10,3,uint16", export.content)

    def test_gateway_update_form_rejects_other_team_site(self):
        _, victim_site, _ = self._victim_infrastructure()
        url = reverse("web_team:devices:gateway_edit", args=[self.team.slug, self.gateway.pk])

        response = self.client.post(
            url,
            {
                "site": victim_site.id,
                "name": self.gateway.name,
                "serial_number": self.gateway.serial_number,
                "status": self.gateway.status,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.site, self.site)

    def test_gateway_update_cannot_change_claimed_identity_or_observed_status(self):
        url = reverse("web_team:devices:gateway_edit", args=[self.team.slug, self.gateway.pk])

        response = self.client.post(
            url,
            {
                "site": self.site.id,
                "name": "Renamed Gateway",
                "serial_number": "FORGED-SERIAL",
                "status": "online",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.gateway.refresh_from_db()
        self.assertEqual(self.gateway.name, "Renamed Gateway")
        self.assertEqual(self.gateway.serial_number, "SN-1234")
        self.assertEqual(self.gateway.status, "offline")


class SolutionProfileOnboardingTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Profile Team", slug="profile-team")
        self.user = CustomUser.objects.create(email="profile@example.com", username="profileuser")
        Membership.objects.create(team=self.team, user=self.user, role=ROLE_ADMIN)
        self.client = Client()
        self.client.force_login(self.user)

    def test_profile_selection_defaults_to_general_iot_without_vertical_assumptions(self):
        profile_url = reverse("web_team:onboarding:step_profile", args=[self.team.slug])

        response = self.client.get(profile_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_profile"], "general_iot")
        self.assertContains(response, "General IoT")
        self.assertContains(response, "Cold Chain Monitoring")
        self.assertContains(response, "Factory Energy Monitoring")
        self.assertContains(response, "Facilities / HVAC")

    def test_profile_selection_persists_to_created_site(self):
        profile_url = reverse("web_team:onboarding:step_profile", args=[self.team.slug])
        site_url = reverse("web_team:onboarding:step_1_site", args=[self.team.slug])

        response = self.client.post(profile_url, {"solution_profile": "facilities_hvac"})
        self.assertRedirects(response, site_url)

        response = self.client.post(
            site_url,
            {
                "name": "Boutique Hotel",
                "address": "Orchard",
                "timezone": "Asia/Singapore",
                "setup_goals": ["energy_usage", "abnormal_reading_alerts", "other"],
                "setup_goal_other": "Track compressor utilization",
                "operating_hours": "custom",
                "custom_operating_start": "07:30",
                "custom_operating_end": "21:15",
                "solution_profile": "facilities_hvac",
            },
        )

        site = Site.objects.get(team=self.team, name="Boutique Hotel")
        self.assertEqual(site.solution_profile, "facilities_hvac")
        self.assertEqual(site.site_type, "")
        self.assertEqual(site.timezone, "Asia/Singapore")
        self.assertEqual(
            site.metadata["onboarding"],
            {
                "setup_goals": ["energy_usage", "abnormal_reading_alerts", "other"],
                "setup_goal_other": "Track compressor utilization",
                "operating_hours": "custom",
                "operating_hours_custom": {
                    "start": "07:30",
                    "end": "21:15",
                },
            },
        )
        self.assertRedirects(response, reverse("web_team:onboarding:step_2_gateway", args=[self.team.slug]))

    def test_site_step_hides_manual_timezone_selector(self):
        site_url = reverse("web_team:onboarding:step_1_site", args=[self.team.slug])

        response = self.client.get(site_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<label for="timezone"')
        self.assertNotContains(response, '<select name="timezone"')
        self.assertContains(response, '<input type="hidden" name="timezone" id="timezone"')
        self.assertContains(response, "Intl.DateTimeFormat().resolvedOptions().timeZone")

    def test_site_step_hides_site_type_and_renders_optional_context_fields(self):
        site_url = reverse("web_team:onboarding:step_1_site", args=[self.team.slug])

        response = self.client.get(site_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="site_type"')
        self.assertNotContains(response, "Site Type")
        self.assertNotContains(response, 'name="setup_goal"')
        self.assertNotContains(response, '<select name="setup_goal"')
        self.assertContains(response, "What do you want Novena to help with first?")
        self.assertContains(response, 'name="setup_goals"')
        self.assertContains(response, "Monitor equipment health")
        self.assertContains(response, "Track energy usage")
        self.assertContains(response, "Get alerts for abnormal readings")
        self.assertContains(response, "Prepare reports / audit trail")
        self.assertContains(response, "Other")
        self.assertContains(response, 'id="setup-goal-other" class="mt-4" hidden')
        self.assertContains(response, "data-setup-goal-other-toggle")
        self.assertContains(response, "document.getElementById('setup-goal-other').hidden = !this.checked")
        self.assertContains(response, 'name="setup_goal_other"')
        self.assertContains(response, "Tell us what else you want to use Novena for")
        self.assertContains(response, "initOnboardingSetupGoals")
        self.assertContains(response, "When does this site usually operate?")
        self.assertContains(response, "24/7")
        self.assertContains(response, "Custom")
        self.assertContains(response, "Irregular schedule")
        self.assertContains(response, "Not sure yet")
        self.assertNotContains(response, "Weekdays, office hours")
        self.assertNotContains(response, "Weekdays, extended hours")
        self.assertNotContains(response, "Weekends included")
        self.assertContains(response, 'id="custom-operating-hours"')
        self.assertContains(response, 'name="custom_operating_start"')
        self.assertContains(response, 'name="custom_operating_end"')
        self.assertContains(response, 'operatingHoursInput.value !== "custom"')
        self.assertContains(response, "This helps Novena prioritize the first widgets, alerts, and recommendations.")
        self.assertContains(response, "This helps Novena avoid noisy recommendations")

    def test_site_step_uses_browser_timezone_when_posted(self):
        site_url = reverse("web_team:onboarding:step_1_site", args=[self.team.slug])

        self.client.post(
            site_url,
            {
                "name": "Jakarta Warehouse",
                "address": "Cakung",
                "timezone": "Asia/Jakarta",
                "site_type": "warehouse",
                "solution_profile": "general_iot",
            },
        )

        site = Site.objects.get(team=self.team, name="Jakarta Warehouse")
        self.assertEqual(site.timezone, "Asia/Jakarta")

    def test_site_step_rejects_invalid_browser_timezone(self):
        self.user.timezone = "Asia/Kuala_Lumpur"
        self.user.save(update_fields=["timezone"])
        site_url = reverse("web_team:onboarding:step_1_site", args=[self.team.slug])

        self.client.post(
            site_url,
            {
                "name": "Fallback Facility",
                "address": "KL",
                "timezone": "Not/A_Timezone",
                "site_type": "warehouse",
                "solution_profile": "general_iot",
            },
        )

        site = Site.objects.get(team=self.team, name="Fallback Facility")
        self.assertEqual(site.timezone, "Asia/Kuala_Lumpur")

    def test_site_step_ignores_invalid_optional_context_values(self):
        site_url = reverse("web_team:onboarding:step_1_site", args=[self.team.slug])

        self.client.post(
            site_url,
            {
                "name": "Invalid Context Site",
                "address": "",
                "timezone": "Asia/Singapore",
                "setup_goals": ["surprise_me"],
                "setup_goal_other": "Should not persist",
                "operating_hours": "moonlight_only",
                "site_type": "warehouse",
                "solution_profile": "general_iot",
            },
        )

        site = Site.objects.get(team=self.team, name="Invalid Context Site")
        self.assertEqual(site.site_type, "")
        self.assertEqual(site.metadata, {})

    def test_connectivity_step_marks_direct_cloud_as_coming_soon(self):
        connectivity_url = reverse("web_team:onboarding:step_connectivity", args=[self.team.slug])

        response = self.client.get(connectivity_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Novena Gateway")
        self.assertContains(response, 'name="connectivity" value="gateway"')
        self.assertContains(response, "Direct cloud connection")
        self.assertContains(response, "Coming soon")
        self.assertContains(response, "Direct cloud onboarding is planned")
        self.assertContains(response, 'name="connectivity" value="direct" class="hidden" disabled')
        self.assertNotContains(response, "For modern equipment that can send readings securely")

    def test_connectivity_step_rejects_direct_cloud_post(self):
        connectivity_url = reverse("web_team:onboarding:step_connectivity", args=[self.team.slug])

        response = self.client.post(connectivity_url, {"connectivity": "direct"})

        self.assertRedirects(response, connectivity_url)
        self.assertNotEqual(self.client.session.get("connectivity_type"), "direct")
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn(
            "Direct cloud onboarding is coming soon. Please use Novena Gateway setup for now.",
            messages,
        )

    def test_connectivity_step_accepts_gateway_post(self):
        connectivity_url = reverse("web_team:onboarding:step_connectivity", args=[self.team.slug])
        gateway_url = reverse("web_team:onboarding:step_2_gateway", args=[self.team.slug])

        response = self.client.post(connectivity_url, {"connectivity": "gateway"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], gateway_url)
        self.assertEqual(self.client.session["connectivity_type"], "gateway")

    def test_gateway_pairing_page_removes_static_progress_chips(self):
        site = Site.objects.create(team=self.team, name="Pairing Site")
        session = self.client.session
        session["onboarding_site_id"] = site.id
        session.save()
        gateway_url = reverse("web_team:onboarding:step_2_gateway", args=[self.team.slug])

        response = self.client.get(gateway_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Gateway found")
        self.assertNotContains(response, "Securing")
        self.assertNotContains(response, "Ready to scan")
        self.assertContains(response, "Securely connect your edge gateway")
        self.assertContains(response, "Find the sticker")
        self.assertContains(response, "Gateway Name")
        self.assertContains(response, "Serial Number")
        self.assertContains(response, "Claim Code")
        self.assertContains(response, "Pair Gateway")

    def test_template_ranking_prioritizes_selected_profile(self):
        hvac = DeviceTemplate.objects.create(
            name="Chiller Monitor",
            device_type="chiller",
            protocol="bacnet",
            category="factory",
            register_map={"temperature": {"address": 1}, "run_hours": {"address": 2}},
            is_verified=True,
        )
        generic = DeviceTemplate.objects.create(
            name="Generic PLC",
            device_type="plc",
            protocol="modbus_tcp",
            category="factory",
            register_map={"production_count": {"address": 1}},
            is_verified=True,
        )

        ranked = rank_templates_for_profile(DeviceTemplate.objects.all(), "facilities_hvac")

        self.assertEqual(ranked[0], hvac)
        self.assertIn(generic, ranked)

    def test_each_profile_has_distinct_recommended_template_ordering(self):
        power_meter = DeviceTemplate.objects.create(
            name="Factory Main Meter",
            device_type="power_meter",
            protocol="modbus_tcp",
            category="energy",
            register_map={
                "active_power": {"address": 1},
                "energy": {"address": 2},
                "voltage": {"address": 3},
                "current": {"address": 4},
            },
            is_verified=True,
        )
        cold_room = DeviceTemplate.objects.create(
            name="Cold Room Door Sensor",
            device_type="temp_sensor",
            protocol="modbus_rtu",
            category="cold_chain",
            register_map={
                "temperature": {"address": 1},
                "door_open": {"address": 2},
                "compressor_status": {"address": 3},
            },
            is_verified=True,
        )
        chiller = DeviceTemplate.objects.create(
            name="HVAC Chiller Monitor",
            device_type="chiller",
            protocol="bacnet",
            category="factory",
            register_map={
                "temperature": {"address": 1},
                "active_power": {"address": 2},
                "run_hours": {"address": 3},
                "compressor_status": {"address": 4},
            },
            is_verified=True,
        )
        generic = DeviceTemplate.objects.create(
            name="Mixed Equipment Telemetry",
            device_type="other",
            protocol="mqtt",
            category="factory",
            register_map={"status": {"address": 1}},
            is_verified=True,
            usage_count=50,
        )
        templates = DeviceTemplate.objects.all()

        self.assertEqual(rank_templates_for_profile(templates, "factory_energy")[0], power_meter)
        self.assertEqual(rank_templates_for_profile(templates, "cold_chain")[0], cold_room)
        self.assertEqual(rank_templates_for_profile(templates, "facilities_hvac")[0], chiller)
        self.assertEqual(rank_templates_for_profile(templates, "general_iot")[0], generic)

    def test_facilities_profile_creates_alert_and_runtime_maintenance_defaults(self):
        site = Site.objects.create(team=self.team, name="Hotel Plant Room", solution_profile="facilities_hvac")
        template = DeviceTemplate.objects.create(
            name="Chiller Monitor",
            device_type="chiller",
            protocol="bacnet",
            category="factory",
            register_map={
                "temperature": {"address": 1},
                "active_power": {"address": 2},
                "run_hours": {"address": 3},
            },
        )
        device = Device.objects.create(
            team=self.team,
            site=site,
            name="Chiller 1",
            template=template,
            device_type="chiller",
            protocol="bacnet",
        )

        created = apply_solution_profile_presets(site, self.user)

        self.assertEqual(created["alerts"], 3)
        self.assertEqual(created["maintenance_schedules"], 1)
        self.assertTrue(AlertRule.objects.filter(device=device, telemetry_key="temperature").exists())
        self.assertTrue(
            PreventiveSchedule.objects.filter(
                device=device,
                title="Runtime-based HVAC service",
                usage_telemetry_key="run_hours",
                usage_threshold=500,
            ).exists()
        )

    def test_general_iot_profile_does_not_create_vertical_alerts_or_maintenance(self):
        site = Site.objects.create(team=self.team, name="Mixed Lab", solution_profile="general_iot")
        template = DeviceTemplate.objects.create(
            name="Mixed Equipment",
            device_type="other",
            protocol="mqtt",
            category="factory",
            register_map={"status": {"address": 1}, "temperature": {"address": 2}},
        )
        Device.objects.create(
            team=self.team,
            site=site,
            name="Mixed Asset",
            template=template,
            device_type="other",
            protocol="mqtt",
        )

        created = apply_solution_profile_presets(site, self.user)

        self.assertEqual(created["alerts"], 0)
        self.assertEqual(created["maintenance_schedules"], 0)

    def test_solution_profile_presets_are_idempotent(self):
        site = Site.objects.create(
            team=self.team,
            name="Cold Room",
            solution_profile="cold_chain",
        )
        template = DeviceTemplate.objects.create(
            name="Cold Room Sensor",
            device_type="temp_sensor",
            protocol="modbus_rtu",
            category="cold_chain",
            register_map={
                "temperature": {"address": 1},
                "door_open": {"address": 2},
                "compressor_status": {"address": 3},
            },
        )
        Device.objects.create(
            team=self.team,
            site=site,
            name="Room Sensor",
            template=template,
            device_type="temp_sensor",
            protocol="modbus_rtu",
        )

        first = apply_solution_profile_presets(site, self.user)
        second = apply_solution_profile_presets(site, self.user)

        self.assertEqual(first["alerts"], 3)
        self.assertGreaterEqual(first["automations"], 1)
        self.assertEqual(second["alerts"], 0)
        self.assertEqual(second["automations"], 0)
