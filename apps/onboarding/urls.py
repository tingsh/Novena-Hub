from django.urls import path

from . import views

app_name = "onboarding"

urlpatterns = [
    path("", views.onboarding_start, name="start"),
    path("profile/", views.step_profile, name="step_profile"),
    path("site/", views.step_1_site, name="step_1_site"),
    path("gateway/", views.step_2_gateway, name="step_2_gateway"),
    path("gateway/wait/", views.step_2b_wait, name="step_2b_wait"),
    path("gateway/status-poll/", views.gateway_status_poll, name="gateway_status_poll"),
    path("discover/", views.step_3_discover, name="step_3_discover"),
    path("discover/template-preview/", views.equipment_template_preview, name="equipment_template_preview"),
    path("discover/poll/", views.discovery_poll, name="discovery_poll"),
    path("device/", views.step_3_device, name="step_3_device"),
    path("alert/", views.step_4_alert, name="step_4_alert"),
    path("complete/", views.complete, name="complete"),
    path("support/<uuid:run_id>/", views.support_bundle_download, name="support_bundle"),
    # Setup Wizard for existing customers
    path("setup/", views.setup_start, name="setup_start"),
    path("setup/site/", views.setup_step_site, name="step_site"),
    path("setup/connectivity/", views.setup_step_connectivity, name="step_connectivity"),
]
