from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.teams.models import Team
from apps.users.models import CustomUser


@override_settings(ACCOUNT_ADAPTER="apps.teams.adapter.AcceptInvitationAdapter", TURNSTILE_SECRET=None)
class TestSignupView(TestCase):
    def test_signup_normal(self):
        self._run_test(team_name="Alice Team", expected_slug="alice-team")

    def test_signup_no_team(self):
        password = "Super Secret Pa$$word!"
        response = self.client.post(
            reverse("account_signup"),
            data={
                "email": "alice@example.com",
                "password1": password,
                "password2": password,
                "team_name": "",
                "terms_agreement": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(0, CustomUser.objects.count())
        self.assertEqual(0, Team.objects.count())
        self.assertFormError(response.context["form"], "team_name", "Please provide a Company or Team Name.")

    def test_signup_unicode_team(self):
        # unicode team names will fall back to the email address
        self._run_test(team_name="Сергей Петров", expected_slug="alice")

    def _run_test(self, team_name: str, expected_slug: str):
        password = "Super Secret Pa$$word!"
        data = {
            "email": "alice@example.com",
            "password1": password,
            "team_name": team_name,
            "terms_agreement": True,
        }
        data["password2"] = password

        response = self.client.post(
            reverse("account_signup"),
            data=data,
            follow=True,
        )
        if (response.status_code != 200 or CustomUser.objects.count() == 0) and "form" in response.context:
            print(f"FORM ERRORS: {response.context['form'].errors}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(1, CustomUser.objects.count())
        self.assertEqual(1, Team.objects.count())
        team = Team.objects.get()
        self.assertEqual(team_name or expected_slug, team.name)
        self.assertEqual(expected_slug, team.slug)


@override_settings(
    ACCOUNT_ADAPTER="apps.teams.adapter.AcceptInvitationAdapter",
    ACCOUNT_EMAIL_VERIFICATION="mandatory",
    ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    TURNSTILE_SECRET=None,
)
class TestSignupEmailVerificationCode(TestCase):
    def test_signup_requires_email_code_before_app_access(self):
        with patch(
            "apps.teams.adapter.AcceptInvitationAdapter.generate_email_verification_code",
            return_value="123456",
        ):
            response = self.client.post(
                reverse("account_signup"),
                data={
                    "email": "pilot@example.com",
                    "password1": "Super Secret Pa$$word!",
                    "team_name": "Pilot Team",
                    "terms_agreement": True,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("account_email_verification_sent"))
        self.assertEqual(CustomUser.objects.count(), 1)
        self.assertEqual(Team.objects.count(), 1)
        self.assertNotIn("_auth_user_id", self.client.session)
        email_address = EmailAddress.objects.get(email="pilot@example.com")
        self.assertFalse(email_address.verified)

        login_response = self.client.post(
            reverse("account_login"),
            data={"login": "pilot@example.com", "password": "Super Secret Pa$$word!"},
        )
        self.assertEqual(login_response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

        verification_mail = next(message for message in mail.outbox if "pilot@example.com" in message.to)
        self.assertIn("123456", verification_mail.body)
        html = verification_mail.alternatives[0].content
        self.assertIn("123456", html)
        self.assertNotIn('href=""', html)

        verification_url = reverse("account_email_verification_sent")
        verification_page = self.client.get(verification_url)
        self.assertContains(verification_page, "Verify your email")
        self.assertContains(verification_page, 'name="code"')
        response = self.client.post(verification_url, data={"code": "000000"})
        self.assertContains(response, "Incorrect code")
        email_address.refresh_from_db()
        self.assertFalse(email_address.verified)
        self.assertNotIn("_auth_user_id", self.client.session)

        response = self.client.post(verification_url, data={"code": "123456"})
        self.assertEqual(response.status_code, 302)
        email_address.refresh_from_db()
        self.assertTrue(email_address.verified)
        self.assertIn("_auth_user_id", self.client.session)
