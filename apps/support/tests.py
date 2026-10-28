from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.events.services import TrackedEmailDeliveryError


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SalesInquiryTest(TestCase):
    def test_sales_inquiry_accepts_public_request(self):
        response = self.client.post(
            reverse("support:sales_inquiry"),
            {
                "name": "Avery Tan",
                "email": "avery@example.com",
                "company": "Example Manufacturing",
                "interest": "Energy monitoring pilot",
                "message": "We want to monitor a compressor room and main meter.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thanks")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Example Manufacturing", mail.outbox[0].subject)

    def test_sales_inquiry_rejects_invalid_email(self):
        response = self.client.post(
            reverse("support:sales_inquiry"),
            {
                "name": "Avery Tan",
                "email": "not-an-email",
                "company": "Example Manufacturing",
                "interest": "Energy monitoring pilot",
                "message": "We want to monitor a compressor room.",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    @patch("apps.support.views.send_tracked_email", side_effect=TrackedEmailDeliveryError([]))
    def test_sales_inquiry_shows_delivery_failure(self, _send_email):
        response = self.client.post(
            reverse("support:sales_inquiry"),
            {
                "name": "Avery Tan",
                "email": "avery@example.com",
                "company": "Example Manufacturing",
                "interest": "Energy monitoring pilot",
                "message": "We want to monitor a compressor room.",
            },
        )

        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "could not send", status_code=500)
