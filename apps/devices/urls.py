from django.urls import path

from . import template_help_views, views

app_name = "devices"

urlpatterns = [
    # Site URLs
    path("sites/", views.SiteListView.as_view(), name="site_list"),
    path("sites/create/", views.SiteCreateView.as_view(), name="site_create"),
    path("sites/<int:pk>/", views.SiteDetailView.as_view(), name="site_detail"),
    path("sites/<int:pk>/edit/", views.SiteUpdateView.as_view(), name="site_edit"),
    path("sites/<int:pk>/delete/", views.SiteDeleteView.as_view(), name="site_delete"),
    # Gateway URLs
    path("gateways/", views.GatewayListView.as_view(), name="gateway_list"),
    path("gateways/create/", views.GatewayCreateView.as_view(), name="gateway_create"),
    path("gateways/<int:pk>/", views.GatewayDetailView.as_view(), name="gateway_detail"),
    path("gateways/<int:pk>/edit/", views.GatewayUpdateView.as_view(), name="gateway_edit"),
    path("gateways/<int:pk>/delete/", views.GatewayDeleteView.as_view(), name="gateway_delete"),
    # Gateway management (Cloud ↔ Edge)
    path("gateways/<int:pk>/rotate-password/", views.gateway_rotate_password, name="gateway_rotate_password"),
    path("gateways/<int:pk>/retry-activation/", views.gateway_retry_activation, name="gateway_retry_activation"),
    path("gateways/<int:pk>/rpc/", views.gateway_send_rpc, name="gateway_send_rpc"),
    path("gateways/<int:pk>/config/", views.gateway_push_config, name="gateway_push_config"),
    path(
        "gateways/<int:pk>/config/<int:config_pk>/retry/",
        views.gateway_retry_config,
        name="gateway_retry_config",
    ),
    path("gateways/<int:pk>/logs/", views.gateway_logs, name="gateway_logs"),
    path("gateways/<int:pk>/rpc-history/", views.gateway_rpc_history, name="gateway_rpc_history"),
    path("gateways/<int:pk>/control/", views.gateway_control_center, name="gateway_control_center"),
    path("gateways/<int:pk>/control/disable/", views.gateway_emergency_disable, name="gateway_emergency_disable"),
    path(
        "gateways/<int:gateway_pk>/rpc-status/<uuid:request_id>/", views.gateway_rpc_status, name="gateway_rpc_status"
    ),
    path("gateways/<int:pk>/ota-update/", views.gateway_ota_update, name="gateway_ota_update"),
    # Device RPC command (via gateway)
    path(
        "gateways/<int:gateway_pk>/devices/<int:device_pk>/command/",
        views.device_rpc_command,
        name="device_rpc_command",
    ),
    path(
        "gateways/<int:gateway_pk>/devices/<int:device_pk>/rpc-status/<uuid:request_id>/",
        views.device_rpc_status,
        name="device_rpc_status",
    ),
    # Device URLs
    path("", views.DeviceListView.as_view(), name="device_list"),
    path("create/", views.DeviceCreateView.as_view(), name="device_create"),
    path("<int:pk>/", views.DeviceDetailView.as_view(), name="device_detail"),
    path("<int:pk>/signals/", views.device_datapoint_mapping, name="device_datapoint_mapping"),
    path("<int:pk>/edit/", views.DeviceUpdateView.as_view(), name="device_edit"),
    path("<int:pk>/delete/", views.DeviceDeleteView.as_view(), name="device_delete"),
    # HTMX endpoints
    path("htmx/device/create/", views.htmx_device_create, name="htmx_device_create"),
    path("htmx/templates/search/", views.template_library_search, name="template_library_search"),
    # AI Template Builder
    path("htmx/ai-template/generate/", views.ai_template_generate, name="ai_template_generate"),
    path("htmx/ai-template/status/<str:task_id>/", views.ai_template_status, name="ai_template_status"),
    path("htmx/ai-template/approve/", views.ai_template_approve, name="ai_template_approve"),
    # Template Library
    path("templates/", views.TemplateLibraryView.as_view(), name="template_library"),
    path("templates/help/", template_help_views.template_help_list, name="template_help_list"),
    path("templates/help/<int:pk>/", template_help_views.template_help_detail, name="template_help_detail"),
    path(
        "templates/help/<int:pk>/files/<str:kind>/",
        template_help_views.template_help_file,
        name="template_help_file",
    ),
    # Remote Control Command URLs
    path("<int:pk>/command/", views.device_send_command, name="device_send_command"),
    path("<int:pk>/command/status/<uuid:tx_id>/", views.device_command_status, name="device_command_status"),
    path("commands/<uuid:command_id>/approve/", views.remote_command_approve, name="remote_command_approve"),
    path("commands/audit/export.<str:format>/", views.command_audit_export, name="command_audit_export"),
    # API endpoints for Edge Gateway
    path("api/discovery/", views.gateway_discovery_api, name="gateway_discovery_api"),
]
