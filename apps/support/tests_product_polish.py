from unittest.mock import patch

import pytest
from django.test import RequestFactory

from apps.events.services import TrackedEmailDeliveryError
from apps.support.views import contact_support
from apps.teams.models import Team
from apps.users.models import CustomUser


@pytest.mark.django_db
def test_support_acknowledgement_only_after_accepted_delivery():
    user = CustomUser.objects.create_user(username="support-polish", email="support-polish@example.com", password="pwd")
    team = Team.objects.create(name="Support fixture", slug="support-fixture")
    request = RequestFactory().post(
        "/support/contact/", {"category": "Technical Issue", "subject": "Pump", "message": "Please inspect"}
    )
    request.user, request.team = user, team
    with patch("apps.support.views.send_tracked_email", return_value=[]):
        response = contact_support(request)
    assert response.status_code == 200
    assert b"Support request sent" in response.content
    assert b"within 2 hours" not in response.content

    with patch("apps.support.views.send_tracked_email", side_effect=TrackedEmailDeliveryError([])):
        response = contact_support(request)
    assert response.status_code == 500
    assert b"Support request sent" not in response.content
