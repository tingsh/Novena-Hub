from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert, AlertRule
from apps.devices.models import Device, Site
from apps.maintenance.forms import (
    MaintenanceTicketForm,
    PreventiveScheduleForm,
    SharedTicketLinkForm,
    TicketTemplateForm,
)
from apps.maintenance.models import MaintenanceTicket, PreventiveSchedule, SharedTicketLink, TicketTemplate
from apps.maintenance.services import auto_create_ticket, process_incoming_whatsapp
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_MANAGER, ROLE_VIEWER
from apps.telemetry.models import TelemetryData
from apps.users.models import CustomUser


class MaintenanceTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test")
        self.manager = CustomUser.objects.create_user(
            username="m", email="m@ex.com", password="pwd", phone_number="+15551112222"
        )
        self.viewer = CustomUser.objects.create_user(
            username="v", email="v@ex.com", password="pwd", phone_number="+15553334444"
        )
        Membership.objects.create(team=self.team, user=self.manager, role=ROLE_MANAGER)
        Membership.objects.create(team=self.team, user=self.viewer, role=ROLE_VIEWER)

        self.site = Site.objects.create(team=self.team, name="HQ")
        self.device = Device.objects.create(
            team=self.team, site=self.site, name="Pump 1", device_type="power_meter", protocol="modbus_tcp"
        )

    def test_auto_create_ticket_from_critical_alert(self):
        rule = AlertRule.objects.create(
            team=self.team,
            device=self.device,
            name="High Temp",
            telemetry_key="temp",
            condition="gt",
            threshold=100.0,
            severity="critical",
            create_maintenance_ticket=True,
            notify_email=True,
            notify_whatsapp=False,
        )
        alert = Alert.objects.create(team=self.team, rule=rule, device=self.device, trigger_value=105.0)

        ticket = auto_create_ticket(alert)

        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.priority, MaintenanceTicket.PriorityChoices.CRITICAL)
        self.assertEqual(ticket.ticket_type, MaintenanceTicket.TypeChoices.REACTIVE)
        self.assertEqual(ticket.device, self.device)
        self.assertTrue(ticket.send_email_notification)
        self.assertFalse(ticket.send_whatsapp_notification)
        self.assertIn("High Temp", ticket.title)
        self.assertIn("Observed value: 105", ticket.description)
        self.assertIn("Alert limit: above 100", ticket.description)
        self.assertIn("confirm the reading on site", ticket.description)
        self.assertNotIn("Threshold: gt", ticket.description)

    def test_auto_create_ticket_with_template(self):
        template = TicketTemplate.objects.create(
            team=self.team,
            name="Calibration Checklist",
            checklist=[
                {"task": "Verify zero offset", "required": True},
                {"task": "Test span response", "required": False},
            ],
        )
        rule = AlertRule.objects.create(
            team=self.team,
            device=self.device,
            name="Volt Spike",
            telemetry_key="volts",
            condition="gt",
            threshold=240.0,
            severity="warning",
            create_maintenance_ticket=True,
            maintenance_template=template,
        )
        alert = Alert.objects.create(team=self.team, rule=rule, device=self.device, trigger_value=245.0)

        ticket = auto_create_ticket(alert)

        self.assertIsNotNone(ticket)
        self.assertEqual(len(ticket.checklist_state), 2)
        self.assertEqual(ticket.checklist_state[0]["task"], "Verify zero offset")
        self.assertFalse(ticket.checklist_state[0]["done"])

    def test_ticket_form_validation_channels(self):
        # Work can be logged without dispatching a message.
        data = {
            "device": self.device.id,
            "title": "Broken Switch",
            "priority": "medium",
            "status": "open",
            "send_email_notification": False,
            "send_whatsapp_notification": False,
        }
        form = MaintenanceTicketForm(data=data, team=self.team)
        self.assertTrue(form.is_valid(), form.errors)

        # A selected channel requires an assignee with that contact method.
        data["send_email_notification"] = True
        form = MaintenanceTicketForm(data=data, team=self.team)
        self.assertFalse(form.is_valid())
        self.assertIn("assigned_to", form.errors)
        data["assigned_to"] = self.manager.id
        form = MaintenanceTicketForm(data=data, team=self.team)
        self.assertTrue(form.is_valid(), form.errors)

    @patch("apps.maintenance.services.send_ticket_assignment_email")
    @patch("apps.maintenance.services.send_ticket_assignment_whatsapp")
    def test_unassigned_manual_ticket_sends_nothing(self, whatsapp, email):
        form = MaintenanceTicketForm(
            data={"device": self.device.pk, "title": "Log inspection", "priority": "medium", "status": "open"},
            team=self.team,
        )
        self.assertTrue(form.is_valid(), form.errors)
        ticket = form.save(commit=False)
        ticket.team, ticket.reported_by = self.team, self.manager
        ticket.save()
        self.assertIsNone(ticket.assigned_to)
        email.assert_not_called()
        whatsapp.assert_not_called()

    @patch("apps.alerts.tasks.send_whatsapp_message_task")
    @patch("apps.maintenance.services.send_tracked_email")
    def test_ticket_assignment_signals(self, mock_send_tracked_email, mock_whatsapp_task):
        MaintenanceTicket.objects.create(
            team=self.team,
            device=self.device,
            title="Manual Inspection",
            ticket_type="preventive",
            send_email_notification=True,
            send_whatsapp_notification=True,
            assigned_to=self.manager,
        )

        # Signal should fire notifications
        self.assertTrue(mock_whatsapp_task.delay.called)
        self.assertTrue(mock_send_tracked_email.called)

        # Check call arguments
        wa_args, wa_kwargs = mock_whatsapp_task.delay.call_args
        self.assertEqual(wa_args[0], self.manager.phone_number)
        self.assertIn("Manual Inspection", wa_args[1])

        mail_kwargs = mock_send_tracked_email.call_args[1]
        self.assertEqual(mail_kwargs["recipients"], [self.manager.email])

    @patch("apps.alerts.tasks.send_whatsapp_message_task")
    def test_whatsapp_webhook_inbound_commands(self, mock_whatsapp_task):
        ticket = MaintenanceTicket.objects.create(
            team=self.team,
            device=self.device,
            title="Calibrate Valve",
            status="open",
            checklist_state=[
                {"task": "Loosen bolt", "required": True, "done": False},
                {"task": "Adjust spring", "required": True, "done": False},
            ],
            assigned_to=self.manager,
        )

        # Test command: DONE 1
        process_incoming_whatsapp(self.manager.phone_number, f"TKT-{ticket.id} DONE 1")
        ticket.refresh_from_db()
        self.assertTrue(ticket.checklist_state[0]["done"])
        self.assertFalse(ticket.checklist_state[1]["done"])

        # Test command: STATUS Resolved
        process_incoming_whatsapp(self.manager.phone_number, f"TKT-{ticket.id} STATUS Resolved")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, MaintenanceTicket.StatusChoices.RESOLVED)

        # Test command: LIST
        process_incoming_whatsapp(self.manager.phone_number, "LIST")
        self.assertTrue(mock_whatsapp_task.delay.called)

    def test_rbac_view_access(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("web_team:maintenance:ticket_list", args=[self.team.slug]))
        self.assertEqual(response.status_code, 200)

    def test_rbac_manage_access(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("web_team:maintenance:ticket_create", args=[self.team.slug]))
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.manager)
        response = self.client.get(reverse("web_team:maintenance:ticket_create", args=[self.team.slug]))
        self.assertEqual(response.status_code, 200)

    def test_schedule_edit_view(self):
        schedule = PreventiveSchedule.objects.create(
            team=self.team, device=self.device, title="Edit Test Schedule", interval="monthly"
        )
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("web_team:maintenance:schedule_edit", args=[self.team.slug, schedule.id]))
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.manager)
        response = self.client.get(reverse("web_team:maintenance:schedule_edit", args=[self.team.slug, schedule.id]))
        self.assertEqual(response.status_code, 200)

    def test_preventive_schedule_auto_assignment(self):
        template = TicketTemplate.objects.create(
            team=self.team, name="Checklist", checklist=[{"task": "Verify Valve", "required": True}]
        )
        # Create schedule with assignment
        schedule = PreventiveSchedule.objects.create(
            team=self.team,
            device=self.device,
            template=template,
            title="Annual Checkup",
            interval="yearly",
            next_due_at=timezone.now() - timedelta(minutes=1),
            assigned_to=self.manager,
            send_email_notification=True,
            send_whatsapp_notification=False,
        )

        from apps.maintenance.tasks import generate_preventive_tickets

        with (
            patch("apps.alerts.tasks.send_whatsapp_message_task"),
            patch("apps.maintenance.services.send_tracked_email") as mock_send_tracked_email,
        ):
            generate_preventive_tickets()

        # Ticket should be created and auto-assigned
        ticket = MaintenanceTicket.objects.filter(schedule_reference=schedule).first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.assigned_to, self.manager)
        self.assertTrue(ticket.send_email_notification)
        self.assertFalse(ticket.send_whatsapp_notification)
        self.assertTrue(mock_send_tracked_email.called)


class ComplianceWorkflowTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test")
        self.manager = CustomUser.objects.create_user(
            username="m2", email="m2@ex.com", password="pwd", phone_number="+15551112223"
        )
        Membership.objects.create(team=self.team, user=self.manager, role=ROLE_MANAGER)
        self.site = Site.objects.create(team=self.team, name="HQ")
        self.device = Device.objects.create(
            team=self.team, site=self.site, name="Pump 1", device_type="power_meter", protocol="modbus_tcp"
        )
        self.ticket = MaintenanceTicket.objects.create(
            team=self.team,
            device=self.device,
            title="Compliance Test Ticket",
            checklist_state=[
                {"task": "Verify Valve", "required": True, "done": False},
                {"task": "Clean Cover", "required": False, "done": False},
            ],
            reported_by=self.manager,
        )

    def test_shared_link_lifecycle(self):
        link = SharedTicketLink.objects.create(
            team=self.team, ticket=self.ticket, created_by=self.manager, auto_revoke_on_resolve=True
        )
        self.assertFalse(link.is_expired)
        self.assertTrue(link.is_active)
        self.assertEqual(link.view_count, 0)

        # Access guest portal
        url = reverse("maintenance_public:public_ticket_view", args=[str(link.token)])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        link.refresh_from_db()
        self.assertEqual(link.view_count, 1)
        self.assertIsNotNone(link.last_viewed_at)

        # Inactive link rendering
        link.is_active = False
        link.save()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_public_toggle_checklist(self):
        link = SharedTicketLink.objects.create(team=self.team, ticket=self.ticket, created_by=self.manager)
        url = reverse("maintenance_public:public_toggle_checklist_item", args=[str(link.token), 0])

        # Toggle via HTMX
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.ticket.refresh_from_db()
        self.assertTrue(self.ticket.checklist_state[0]["done"])

        # Verify comment audit log
        comment = self.ticket.comments.last()
        self.assertIsNotNone(comment)
        self.assertIn("via Shareable Link", comment.content)
        self.assertIsNone(comment.author)

    @patch("apps.maintenance.public_views.send_tracked_email")
    def test_public_update_status_compliance_and_revocation(self, mock_send_tracked_email):
        link = SharedTicketLink.objects.create(
            team=self.team, ticket=self.ticket, created_by=self.manager, auto_revoke_on_resolve=True
        )
        url = reverse("maintenance_public:public_update_status", args=[str(link.token)])

        # 1. Try resolving without completing required tasks
        response = self.client.post(url, {"status": "resolved"})
        # Should redirect back to detail view
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "open")  # Stays open
        link.refresh_from_db()
        self.assertTrue(link.is_active)  # Link stays active

        # 2. Complete required task
        self.ticket.checklist_state[0]["done"] = True
        self.ticket.save()

        # 3. Resolve now
        response = self.client.post(url, {"status": "resolved"})
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "resolved")

        # Verify auto-revocation
        link.refresh_from_db()
        self.assertFalse(link.is_active)

        # Verify manager notification
        self.assertTrue(mock_send_tracked_email.called)
        mail_args = mock_send_tracked_email.call_args[1]
        self.assertEqual(mail_args["recipients"], [self.manager.email])

    def test_public_link_cannot_close_or_mutate_audit(self):
        link = SharedTicketLink.objects.create(
            team=self.team, ticket=self.ticket, created_by=self.manager, expires_at=timezone.now() + timedelta(days=1)
        )
        url = reverse("maintenance_public:public_update_status", args=[str(link.token)])
        old_comments = self.ticket.comments.count()
        response = self.client.post(url, {"status": "closed"})
        self.assertEqual(response.status_code, 403)
        self.ticket.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(self.ticket.status, "open")
        self.assertEqual(self.ticket.comments.count(), old_comments)
        self.assertTrue(link.is_active)
        link.expires_at = timezone.now() - timedelta(seconds=1)
        link.save(update_fields=["expires_at"])
        self.assertEqual(
            self.client.get(reverse("maintenance_public:public_ticket_view", args=[str(link.token)])).status_code, 404
        )
        self.assertEqual(self.client.post(url, {"status": "resolved"}).status_code, 404)

    def test_template_checklist_edits_affect_future_tickets_only(self):
        form = TicketTemplateForm(
            data={
                "name": "Inspection",
                "estimated_duration_minutes": 30,
                "checklist_steps": "* Inspect guard\nRecord vibration",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        template = form.save(commit=False)
        template.team = self.team
        template.save()
        self.assertEqual(template.checklist[0], {"task": "Inspect guard", "required": True})
        first = MaintenanceTicketForm(
            data={
                "device": self.device.pk,
                "title": "First",
                "priority": "medium",
                "status": "open",
                "template": template.pk,
            },
            team=self.team,
        )
        self.assertTrue(first.is_valid(), first.errors)
        ticket = first.save(commit=False)
        ticket.team, ticket.reported_by = self.team, self.manager
        ticket.save()
        edited = TicketTemplateForm(
            data={
                "name": "Inspection",
                "estimated_duration_minutes": 30,
                "checklist_steps": "* Inspect cover\nRecord vibration",
            },
            instance=template,
        )
        self.assertTrue(edited.is_valid(), edited.errors)
        edited.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.checklist_state[0]["task"], "Inspect guard")
        self.assertEqual(template.checklist[0]["task"], "Inspect cover")
        bad = TicketTemplateForm(data={"name": "Inspection", "estimated_duration_minutes": 30, "checklist_steps": "* "})
        self.assertFalse(bad.is_valid())

    def test_active_calendar_schedule_requires_due_date_and_contractor_link_expiry(self):
        form = PreventiveScheduleForm(
            data={"device": self.device.pk, "title": "Inspection", "interval": "weekly", "is_active": True},
            team=self.team,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("next_due_at", form.errors)
        link_form = SharedTicketLinkForm(data={"auto_revoke_on_resolve": True})
        self.assertFalse(link_form.is_valid())
        self.assertIn("expires_at", link_form.errors)

    def test_guest_comment_upload(self):
        link = SharedTicketLink.objects.create(team=self.team, ticket=self.ticket, created_by=self.manager)
        url = reverse("maintenance_public:public_add_comment", args=[str(link.token)])

        mock_file = SimpleUploadedFile("checklist.png", b"file_content", content_type="image/png")
        data = {"guest_name": "Acme Contractors Ltd", "content": "Completed onsite testing.", "attachment": mock_file}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        comment = self.ticket.comments.exclude(is_system_generated=True).first()
        self.assertIsNotNone(comment)
        self.assertEqual(comment.guest_name, "Acme Contractors Ltd")
        self.assertEqual(comment.content, "Completed onsite testing.")
        self.assertIsNotNone(comment.attachment)
        self.assertTrue("checklist" in comment.attachment.name)
        self.assertTrue(comment.attachment.name.endswith(".png"))


class PreventiveTriggerTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Novena team", slug="novena")
        self.manager = CustomUser.objects.create_user(username="mgr", email="mgr@ex.com", password="pwd")
        self.viewer = CustomUser.objects.create_user(username="view", email="view@ex.com", password="pwd")

        from apps.teams.models import Membership
        from apps.teams.roles import ROLE_MANAGER, ROLE_VIEWER

        Membership.objects.create(team=self.team, user=self.manager, role=ROLE_MANAGER)
        Membership.objects.create(team=self.team, user=self.viewer, role=ROLE_VIEWER)

        self.site = Site.objects.create(team=self.team, name="Lift Station")
        self.device = Device.objects.create(
            team=self.team, site=self.site, name="Lift 1", device_type="custom", protocol="modbus_tcp"
        )
        self.template = TicketTemplate.objects.create(
            team=self.team, name="Lift Checklist", checklist=[{"task": "Inspect ropes", "required": True}]
        )
        self.schedule = PreventiveSchedule.objects.create(
            team=self.team,
            device=self.device,
            template=self.template,
            title="Annual Lift maintenance",
            interval="yearly",
            next_due_at=timezone.now() + timedelta(days=365),
            is_active=True,
        )

    def test_calendar_due_runs_once_and_advances(self):
        from apps.maintenance.tasks import generate_preventive_tickets

        self.schedule.next_due_at = timezone.now() - timedelta(minutes=1)
        self.schedule.save(update_fields=["next_due_at"])
        generate_preventive_tickets()
        generate_preventive_tickets()
        self.assertEqual(MaintenanceTicket.objects.filter(schedule_reference=self.schedule).count(), 1)
        self.schedule.refresh_from_db()
        self.assertGreater(self.schedule.next_due_at, timezone.now())

    def test_usage_schedule_baseline_and_delta_trigger_once(self):
        from apps.maintenance.tasks import generate_preventive_tickets

        TelemetryData.objects.create(device=self.device, key="run_hours", timestamp=timezone.now(), value_numeric=100)
        with patch(
            "apps.devices.datapoint_maps.effective_register_map",
            return_value={"run_hours": {"type": "float32", "unit": "h"}},
        ):
            form = PreventiveScheduleForm(
                data={
                    "device": self.device.pk,
                    "title": "Run hours service",
                    "is_active": "on",
                    "is_usage_based": "on",
                    "usage_telemetry_key": "run_hours",
                    "usage_threshold": 10,
                },
                team=self.team,
            )
            self.assertTrue(form.is_valid(), form.errors)
            usage_schedule = form.save(commit=False)
            usage_schedule.team = self.team
            usage_schedule.save()
        self.assertEqual(usage_schedule.last_trigger_usage_value, 100)
        TelemetryData.objects.create(device=self.device, key="run_hours", timestamp=timezone.now(), value_numeric=110)
        generate_preventive_tickets()
        generate_preventive_tickets()
        self.assertEqual(MaintenanceTicket.objects.filter(schedule_reference=usage_schedule).count(), 1)
        usage_schedule.refresh_from_db()
        self.assertEqual(usage_schedule.last_trigger_usage_value, 110)

    def test_manual_trigger_unauthorized(self):
        # Viewer (view-only role) should not be able to trigger
        self.client.force_login(self.viewer)
        url = reverse("web_team:maintenance:schedule_trigger", args=[self.team.slug, self.schedule.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_manual_trigger_success_and_advancement(self):
        # Manager should successfully trigger
        self.client.force_login(self.manager)
        url = reverse("web_team:maintenance:schedule_trigger", args=[self.team.slug, self.schedule.id])

        original_due = self.schedule.next_due_at
        response = self.client.post(url)

        # Should redirect to ticket detail page
        self.assertEqual(response.status_code, 302)

        # Verify ticket creation
        ticket = MaintenanceTicket.objects.filter(schedule_reference=self.schedule).first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.title, "[PM] Annual Lift maintenance on Lift 1")
        self.assertEqual(ticket.ticket_type, "preventive")
        self.assertEqual(len(ticket.checklist_state), 1)
        self.assertEqual(ticket.checklist_state[0]["task"], "Inspect ropes")

        # Assert redirect url targets this new ticket
        expected_redirect = reverse("web_team:maintenance:ticket_detail", args=[self.team.slug, ticket.id])
        self.assertRedirects(response, expected_redirect)

        # Verify schedule due date is advanced
        self.schedule.refresh_from_db()
        self.assertGreater(self.schedule.next_due_at, original_due)
