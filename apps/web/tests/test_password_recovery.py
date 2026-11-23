import re
from urllib.parse import urlparse

from allauth.account.models import EmailAddress
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.users.models import CustomUser


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    ACCOUNT_EMAIL_VERIFICATION="mandatory",
)
class TestPasswordRecovery(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="pilot@example.com",
            email="pilot@example.com",
            password="Old Pilot Password 123!",
        )
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)

    def test_reset_email_changes_password_and_link_cannot_be_reused(self):
        login_page = self.client.get(reverse("account_login"))
        self.assertContains(login_page, reverse("account_reset_password"))

        response = self.client.post(reverse("account_reset_password"), {"email": self.user.email})
        self.assertRedirects(response, reverse("account_reset_password_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("expires in one hour", mail.outbox[0].body)

        match = re.search(r"https?://[^\s]+/accounts/password/reset/key/[^\s]+", mail.outbox[0].body)
        self.assertIsNotNone(match)
        reset_path = urlparse(match.group(0)).path
        response = self.client.get(reset_path)
        self.assertEqual(response.status_code, 302)
        safe_path = response.url
        self.assertNotEqual(reset_path, safe_path)
        self.assertIn("set-password/", safe_path)

        response = self.client.post(
            safe_path,
            {"password1": "New Pilot Password 456!", "password2": "New Pilot Password 456!"},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("Old Pilot Password 123!"))
        self.assertTrue(self.user.check_password("New Pilot Password 456!"))

        self.assertContains(self.client.get(reset_path, follow=True), "invalid")

    def test_unknown_email_receives_generic_reply_without_email(self):
        response = self.client.post(reverse("account_reset_password"), {"email": "unknown@example.com"}, follow=True)
        self.assertContains(response, "If an account matches")
        self.assertEqual(len(mail.outbox), 0)

    def test_signed_in_change_requires_current_password(self):
        self.client.force_login(self.user)
        profile = self.client.get(reverse("users:user_profile"))
        self.assertContains(profile, reverse("account_change_password"))

        change_url = reverse("account_change_password")
        response = self.client.post(
            change_url,
            {"oldpassword": "wrong", "password1": "New Pilot Password 456!", "password2": "New Pilot Password 456!"},
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Old Pilot Password 123!"))

        response = self.client.post(
            change_url,
            {
                "oldpassword": "Old Pilot Password 123!",
                "password1": "New Pilot Password 456!",
                "password2": "New Pilot Password 456!",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("New Pilot Password 456!"))
