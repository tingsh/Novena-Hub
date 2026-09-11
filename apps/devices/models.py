import logging
import uuid

from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class Site(BaseTeamModel):
    """A physical location (factory, cold room, building, solar farm) belonging to a team."""

    SOLUTION_PROFILE_CHOICES = (
        ("general_iot", _("General IoT")),
        ("cold_chain", _("Cold Chain Monitoring")),
        ("factory_energy", _("Factory Energy Monitoring")),
        ("facilities_hvac", _("Facilities / HVAC")),
    )

    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    timezone = models.CharField(max_length=50, default="Asia/Singapore")
    solution_profile = models.CharField(
        max_length=30,
        choices=SOLUTION_PROFILE_CHOICES,
        default="general_iot",
        help_text=_("UX preset for onboarding, dashboards, alerts, and reports."),
    )
    site_type = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Optional profile-specific site type, such as hotel, clinic, or warehouse."),
    )
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name


class Gateway(BaseTeamModel):
    """An edge gateway device (RPi running our forked TB Gateway)."""

    STATUS_CHOICES = (
        ("online", _("Online")),
        ("offline", _("Offline")),
        ("maintenance", _("Maintenance")),
    )
    LIFECYCLE_CHOICES = (
        ("claimed", _("Claimed")),
        ("bootstrap_seen", _("Bootstrap Seen")),
        ("activating", _("Activating")),
        ("online", _("Online")),
        ("commissioning", _("Commissioning")),
        ("active", _("Active")),
        ("release_pending", _("Release Pending")),
        ("released", _("Released")),
    )
    TLS_MODE_CHOICES = (
        ("none", _("None")),
        ("one-way", _("One-Way TLS")),
        ("mutual", _("Mutual TLS")),
    )

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="gateways")
    name = models.CharField(max_length=200)
    serial_number = models.CharField(max_length=100)
    access_token = models.CharField(max_length=64, unique=True, help_text=_("MQTT authentication token"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="offline")
    lifecycle_status = models.CharField(max_length=20, choices=LIFECYCLE_CHOICES, default="claimed")
    last_seen = models.DateTimeField(null=True, blank=True)
    firmware_version = models.CharField(max_length=50, blank=True)
    config = models.JSONField(default=dict, blank=True)
    capacity = models.PositiveIntegerField(default=8, help_text=_("Maximum number of devices this gateway can support"))
    discovery_data = models.JSONField(
        default=dict, blank=True, help_text=_("Most recent discovery report from the edge")
    )

    # MQTT credentials (per-gateway authentication)
    mqtt_username = models.CharField(max_length=100, unique=True, null=True, blank=True)
    mqtt_password = models.CharField(max_length=255, blank=True, help_text=_("Hashed operational MQTT password"))
    tls_mode = models.CharField(max_length=10, choices=TLS_MODE_CHOICES, default="one-way")
    client_cert_pem = models.TextField(blank=True, help_text=_("Client certificate PEM for mTLS"))
    client_key_pem = models.TextField(blank=True, help_text=_("Client private key PEM for mTLS"))
    mqtt_provisioning_status = models.CharField(max_length=20, default="not_started")
    mqtt_provisioning_error = models.TextField(blank=True)
    mqtt_provisioned_at = models.DateTimeField(null=True, blank=True)
    credential_rotation_status = models.CharField(max_length=20, default="not_started")
    last_bootstrap_seen_at = models.DateTimeField(null=True, blank=True)

    # Heartbeat / attribute sync fields (populated by edge gateway)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    uptime_seconds = models.IntegerField(null=True, blank=True)
    python_version = models.CharField(max_length=20, blank=True)
    platform_info = models.CharField(max_length=200, blank=True)
    active_connectors = models.JSONField(default=list, blank=True)
    connected_devices = models.JSONField(default=list, blank=True)
    gateway_capabilities = models.JSONField(default=list, blank=True)
    remote_control_protocol_version = models.PositiveIntegerField(default=0)
    remote_control_capabilities = models.JSONField(default=list, blank=True)
    remote_control_local_writeback_enabled = models.BooleanField(default=False)
    remote_control_policy_loaded = models.BooleanField(default=False)
    remote_control_policy_revision = models.PositiveIntegerField(default=0)
    remote_control_epoch = models.PositiveBigIntegerField(default=0)
    remote_control_clock_ready = models.BooleanField(default=False)
    remote_control_journal_ready = models.BooleanField(default=False)
    remote_control_event_spool_count = models.PositiveIntegerField(default=0)
    remote_control_storage_healthy = models.BooleanField(default=False)

    # Network Watchdog fields
    active_interface = models.CharField(max_length=20, blank=True, default="eth0")
    failover_count = models.IntegerField(default=0)
    ethernet_status = models.CharField(max_length=20, blank=True, default="unknown")
    wifi_status = models.CharField(max_length=20, blank=True, default="unknown")
    fourg_status = models.CharField(max_length=20, blank=True, default="unknown")
    signal_strength = models.IntegerField(null=True, blank=True)
    buffered_event_count = models.IntegerField(null=True, blank=True)
    last_replay_status = models.CharField(max_length=30, blank=True, default="")
    replay_failure_count = models.IntegerField(default=0)

    # Broker / firewall diagnostics emitted by Novena Gateway
    connectivity_checked_ts = models.PositiveBigIntegerField(null=True, blank=True)
    internet_reachable = models.BooleanField(null=True, blank=True)
    default_route_ok = models.BooleanField(null=True, blank=True)
    default_route_error = models.TextField(blank=True)
    dns_ok = models.BooleanField(null=True, blank=True)
    dns_error = models.TextField(blank=True)
    broker_host = models.CharField(max_length=255, blank=True)
    broker_port = models.IntegerField(null=True, blank=True)
    broker_tcp_ok = models.BooleanField(null=True, blank=True)
    broker_tcp_error = models.TextField(blank=True)
    tls_ok = models.BooleanField(null=True, blank=True)
    tls_error = models.TextField(blank=True)
    mqtt_connected = models.BooleanField(null=True, blank=True)
    mqtt_last_error = models.TextField(blank=True)

    # Edge diagnostics for customer support
    device_health = models.JSONField(default=dict, blank=True)
    ota_status = models.CharField(max_length=30, blank=True)
    ota_version = models.CharField(max_length=50, blank=True)
    ota_error = models.TextField(blank=True)
    ota_rollback_performed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} ({self.serial_number})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["serial_number"],
                condition=~models.Q(lifecycle_status="released"),
                name="unique_active_gateway_serial",
            )
        ]

    @property
    def freshness(self):
        from apps.devices.freshness import gateway_freshness_state

        return gateway_freshness_state(self)


class GatewayInventory(models.Model):
    """Factory registry for physical gateways before customer claim."""

    STATUS_CHOICES = (
        ("unclaimed", _("Unclaimed")),
        ("claimed", _("Claimed")),
        ("released", _("Released")),
        ("retired", _("Retired")),
    )

    serial_number = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unclaimed")
    batch = models.CharField(max_length=100, blank=True)
    gateway = models.OneToOneField(
        Gateway, null=True, blank=True, on_delete=models.SET_NULL, related_name="inventory_record"
    )
    claimed_by_team = models.ForeignKey(
        "teams.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="claimed_gateway_inventory"
    )
    manufactured_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["serial_number"]
        verbose_name_plural = "gateway inventory"

    def __str__(self):
        return f"{self.serial_number} ({self.status})"


class GatewayActivation(BaseTeamModel):
    """Durable activation attempt for first-time operational MQTT credentials."""

    STATUS_CHOICES = (
        ("provisioning", _("Provisioning")),
        ("pending", _("Pending")),
        ("delivered", _("Delivered")),
        ("acknowledged", _("Acknowledged")),
        ("expired", _("Expired")),
        ("retried", _("Retried")),
        ("retry", _("Retry Scheduled")),
        ("failed", _("Failed")),
        ("superseded", _("Superseded")),
    )
    UNRESOLVED_STATUSES = ("provisioning", "pending", "delivered", "retried", "retry", "failed")

    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="activations")
    generation = models.PositiveIntegerField(default=1)
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    encrypted_mqtt_password = models.TextField(blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["gateway", "generation"], name="unique_gateway_activation_generation")
        ]
        indexes = [
            models.Index(fields=["gateway", "status"]),
            models.Index(fields=["expires_at", "status"]),
        ]

    def __str__(self):
        return f"Activation {self.request_id} -> {self.gateway.serial_number} ({self.status})"


class GatewayReleaseRequest(BaseTeamModel):
    """Durable, fail-closed release of one claimed Gateway ownership record."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        REVOKING = "revoking", _("Revoking credentials")
        RETRY = "retry", _("Retry scheduled")
        NEEDS_ATTENTION = "needs_attention", _("Needs attention")
        COMPLETED = "completed", _("Completed")

    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    gateway = models.ForeignKey(Gateway, on_delete=models.PROTECT, related_name="release_requests")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="gateway_release_requests",
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["gateway"],
                condition=models.Q(status__in=["queued", "revoking", "retry", "needs_attention"]),
                name="unique_active_gateway_release",
            )
        ]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class DeviceTemplate(models.Model):
    """Pre-configured register maps for known equipment."""

    DEVICE_TYPE_CHOICES = (
        ("power_meter", _("Power Meter")),
        ("solar_inverter", _("Solar Inverter")),
        ("vfd", _("Variable Frequency Drive")),
        ("plc", _("PLC")),
        ("temp_sensor", _("Temperature Sensor")),
        ("chiller", _("Chiller")),
        ("other", _("Other")),
    )
    PROTOCOL_CHOICES = (
        ("modbus_tcp", _("Modbus TCP")),
        ("modbus_rtu", _("Modbus RTU")),
        ("opcua", _("OPC-UA")),
        ("mqtt", _("MQTT")),
        ("bacnet", _("BACnet")),
    )
    MAPPING_STRATEGY_CHOICES = (
        ("fixed", _("Fixed equipment map")),
        ("site_defined", _("Site-defined signals")),
    )
    VERTICAL_CHOICES = (
        ("energy", _("Energy Monitoring")),
        ("cold_chain", _("Cold Chain")),
        ("factory", _("Smart Factory")),
    )

    name = models.CharField(max_length=200)
    manufacturer = models.CharField(max_length=200, blank=True)
    model_number = models.CharField(max_length=200, blank=True)
    device_type = models.CharField(max_length=30, choices=DEVICE_TYPE_CHOICES)
    protocol = models.CharField(max_length=20, choices=PROTOCOL_CHOICES)
    mapping_strategy = models.CharField(
        max_length=20,
        choices=MAPPING_STRATEGY_CHOICES,
        default="fixed",
        help_text=_("Whether the template supplies a deployable map or only a device/protocol starter."),
    )
    register_map = models.JSONField(help_text=_("Definition of registers. Keys can have 'writable': true."))
    discovery_hints = models.JSONField(default=dict, blank=True)
    default_polling_interval = models.IntegerField(default=5)
    category = models.CharField(max_length=20, choices=VERTICAL_CHOICES, default="energy")
    alert_presets = models.JSONField(default=list, blank=True)
    is_verified = models.BooleanField(default=False)
    datapoint_schema_version = models.PositiveSmallIntegerField(
        default=2,
        help_text=_("Version of the normalized datapoint representation stored in register_map."),
    )

    SOURCE_CHOICES = (
        ("curated", _("Curated (Novena)")),
        ("ai_generated", _("AI Generated")),
        ("user_created", _("User Created")),
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="curated")
    source_url = models.URLField(blank=True, max_length=500, help_text=_("URL where the register map was found"))
    ai_confidence = models.FloatField(null=True, blank=True, help_text=_("AI confidence score 0.0-1.0"))
    created_by_team = models.ForeignKey(
        "teams.Team",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("Team that originally created this template"),
    )
    usage_count = models.PositiveIntegerField(default=0, help_text=_("Number of devices using this template"))
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return self.name


class Device(BaseTeamModel):
    """A monitored device (sensor, PLC, VFD, etc.)."""

    STATUS_CHOICES = (
        ("online", _("Online")),
        ("offline", _("Offline")),
        ("alarm", _("Alarm")),
    )
    ENERGY_CATEGORY_CHOICES = (
        ("generation", _("Generation (Solar, Wind, etc.)")),
        ("utility", _("Utility (Main Grid)")),
        ("consumption", _("Consumption (Equipment, Building)")),
        ("none", _("None / Not Applicable")),
    )

    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="devices", null=True, blank=True)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="devices")
    template = models.ForeignKey(DeviceTemplate, null=True, blank=True, on_delete=models.SET_NULL)

    name = models.CharField(max_length=200)
    device_type = models.CharField(max_length=30, choices=DeviceTemplate.DEVICE_TYPE_CHOICES)
    protocol = models.CharField(max_length=20, choices=DeviceTemplate.PROTOCOL_CHOICES)
    port = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Interface name or address (e.g., /dev/ttyUSB0, 192.168.1.100:502)",
    )
    energy_category = models.CharField(max_length=20, choices=ENERGY_CATEGORY_CHOICES, default="none")

    connection_config = models.JSONField(default=dict)
    discovery_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text="Raw discovery data from Edge (interface, slave_id, baud_rate, etc.)",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="offline")
    last_telemetry_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.name} ({self.site.name})"

    @property
    def freshness(self):
        from apps.devices.freshness import device_freshness_state

        return device_freshness_state(self)

    @property
    def gateway_context_display(self):
        from apps.devices.freshness import device_gateway_context_display

        return device_gateway_context_display(self)


class DeviceDatapointMap(BaseTeamModel):
    """Customer/site-specific semantic signal mapping for programmable equipment."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        TESTING = "testing", _("Testing")
        AWAITING_CONFIRMATION = "awaiting_confirmation", _("Awaiting confirmation")
        CONFIRMED = "confirmed", _("Confirmed")
        NEEDS_ATTENTION = "needs_attention", _("Needs attention")

    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="datapoint_map")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    schema_version = models.PositiveSmallIntegerField(default=1)
    datapoints = models.JSONField(default=list, blank=True)
    last_validation = models.JSONField(default=dict, blank=True)
    datapoint_health = models.JSONField(default=dict, blank=True)
    tested_checksum = models.CharField(max_length=64, blank=True)
    confirmed_checksum = models.CharField(max_length=64, blank=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    validated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="validated_device_datapoint_maps",
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_device_datapoint_maps",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cloned_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="clones",
    )

    class Meta:
        ordering = ["device__name"]
        indexes = [models.Index(fields=["team", "status"])]

    def __str__(self):
        return f"{self.device.name} datapoints ({self.get_status_display()})"


class DeviceDatapointMapRevision(BaseTeamModel):
    """Immutable snapshot of a confirmed site-defined datapoint map."""

    mapping = models.ForeignKey(DeviceDatapointMap, on_delete=models.CASCADE, related_name="revisions")
    revision_number = models.PositiveIntegerField()
    datapoints = models.JSONField(default=list)
    datapoints_checksum = models.CharField(max_length=64)
    confirmed_checksum = models.CharField(max_length=64, blank=True)
    validation_result = models.JSONField(default=dict, blank=True)
    validated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["mapping", "revision_number"],
                name="unique_device_datapoint_map_revision",
            )
        ]
        indexes = [models.Index(fields=["team", "mapping"], name="devices_dpr_team_map_idx")]

    def __str__(self):
        return f"{self.mapping.device.name} mapping revision {self.revision_number}"


class GatewayConfig(BaseTeamModel):
    """Tracks config versions pushed to gateways."""

    STATUS_CHOICES = (
        ("queued", _("Queued")),
        ("waiting_for_gateway", _("Waiting for Gateway")),
        ("published", _("Published")),
        ("accepted", _("Accepted")),
        ("active", _("Active")),
        ("failed", _("Failed")),
        ("rolled_back", _("Rolled Back")),
        ("timed_out", _("Timed Out")),
        ("superseded", _("Superseded")),
    )

    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="config_history")
    setup_run = models.ForeignKey(
        "DeploymentSetupRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="configurations",
    )
    config_json = models.JSONField()
    pushed_at = models.DateTimeField(auto_now_add=True)
    request_id = models.UUIDField(unique=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    revision = models.PositiveBigIntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True)
    envelope_json = models.JSONField(default=dict, blank=True)
    action = models.CharField(max_length=30, default="full_update")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    error_message = models.TextField(blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    technical_error = models.TextField(blank=True)
    rollback_performed = models.BooleanField(default=False)
    connector_results = models.JSONField(default=list, blank=True)
    rollback_connector_results = models.JSONField(default=list, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledgement_deadline_at = models.DateTimeField(null=True, blank=True)
    last_ack_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-pushed_at"]

    def __str__(self):
        return f"Config push {self.request_id} → {self.gateway.serial_number} ({self.status})"


class GatewayConfigOutbox(BaseTeamModel):
    """Transactional delivery record for a Gateway configuration."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        CLAIMED = "claimed", _("Claimed")
        WAITING_GATEWAY = "waiting_gateway", _("Waiting for Gateway")
        AWAITING_ACK = "awaiting_ack", _("Awaiting acknowledgement")
        COMPLETED = "completed", _("Completed")
        RETRY = "retry", _("Retry")
        DEAD_LETTER = "dead_letter", _("Dead letter")

    config = models.OneToOneField(GatewayConfig, on_delete=models.CASCADE, related_name="outbox")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["next_attempt_at", "created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class GatewayPlanReconciliation(BaseTeamModel):
    """Idempotent record of applying a subscription polling policy to a team."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        RUNNING = "running", _("Running")
        COMPLETED = "completed", _("Completed")
        NEEDS_ATTENTION = "needs_attention", _("Needs attention")

    source_key = models.CharField(max_length=255)
    previous_interval_seconds = models.FloatField()
    new_interval_seconds = models.FloatField()
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    queued_gateway_count = models.PositiveIntegerField(default=0)
    skipped_gateway_count = models.PositiveIntegerField(default=0)
    unsupported_gateway_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["team", "source_key"], name="unique_team_plan_reconcile_source")]


class DeploymentSetupRun(BaseTeamModel):
    """Durable customer setup journey, separate from control commissioning."""

    class State(models.TextChoices):
        DRAFT = "draft", _("Draft")
        GATEWAY_CHECK = "gateway_check", _("Gateway check")
        DISCOVERING = "discovering", _("Discovering")
        CONFIGURING = "configuring", _("Configuring")
        DEPLOYING = "deploying", _("Deploying")
        VERIFYING = "verifying", _("Verifying")
        COMPLETED = "completed", _("Completed")
        COMPLETED_ATTENTION = "completed_attention", _("Completed with attention")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    run_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="deployment_setup_runs")
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="deployment_setup_runs")
    state = models.CharField(max_length=30, choices=State.choices, default=State.DRAFT)
    current_step = models.CharField(max_length=30, default="location")
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deployment_setup_runs",
    )
    readiness = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["team", "gateway", "state"])]


class DeploymentSetupItem(BaseTeamModel):
    """One equipment candidate and its validation/deployment evidence."""

    class State(models.TextChoices):
        DISCOVERED = "discovered", _("Discovered")
        TEMPLATE_SELECTED = "template_selected", _("Template selected")
        VALIDATING = "validating", _("Validating")
        AWAITING_CONFIRMATION = "awaiting_confirmation", _("Awaiting confirmation")
        VALIDATED = "validated", _("Validated")
        QUEUED = "queued", _("Queued")
        APPLIED = "applied", _("Applied")
        TELEMETRY_CONFIRMED = "telemetry_confirmed", _("Telemetry confirmed")
        NEEDS_ATTENTION = "needs_attention", _("Needs attention")
        FAILED = "failed", _("Failed")
        ROLLED_BACK = "rolled_back", _("Rolled back")

    class Trust(models.TextChoices):
        NOVENA_VERIFIED = "novena_verified", _("Novena verified")
        CUSTOMER_VALIDATED = "customer_validated", _("Customer validated")
        AI_DRAFT = "ai_draft", _("AI draft")
        UNVALIDATED = "unvalidated", _("Unvalidated")

    run = models.ForeignKey(DeploymentSetupRun, on_delete=models.CASCADE, related_name="items")
    device = models.ForeignKey(
        Device,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deployment_setup_items",
    )
    discovery_index = models.PositiveIntegerField(null=True, blank=True)
    candidate_key = models.CharField(
        max_length=320,
        blank=True,
        db_index=True,
        help_text=_("Stable identity used to retain an equipment draft across discovery scans."),
    )
    candidate_data = models.JSONField(default=dict, blank=True)
    selected_template = models.ForeignKey(
        DeviceTemplate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deployment_setup_items",
    )
    confidence_score = models.PositiveSmallIntegerField(default=0)
    confidence_explanation = models.TextField(blank=True)
    trust_level = models.CharField(max_length=30, choices=Trust.choices, default=Trust.UNVALIDATED)
    state = models.CharField(max_length=30, choices=State.choices, default=State.DISCOVERED)
    connection = models.JSONField(default=dict, blank=True)
    datapoints = models.JSONField(default=list, blank=True)
    validation_result = models.JSONField(default=dict, blank=True)
    validation_command = models.ForeignKey(
        "RemoteCommand",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deployment_validation_items",
    )
    first_telemetry_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["run", "state"])]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "candidate_key"],
                condition=~models.Q(candidate_key=""),
                name="unique_setup_candidate_key_per_run",
            )
        ]


class DeploymentSetupEvent(models.Model):
    """Append-only setup timeline for customers and support."""

    run = models.ForeignKey(DeploymentSetupRun, on_delete=models.PROTECT, related_name="events")
    item = models.ForeignKey(
        DeploymentSetupItem,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="events",
    )
    sequence_number = models.PositiveIntegerField()
    event_type = models.CharField(max_length=80)
    message = models.TextField()
    evidence = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "sequence_number"],
                name="unique_deployment_setup_event_sequence",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk and DeploymentSetupEvent.objects.filter(pk=self.pk).exists():
            raise ValueError("Deployment setup events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Deployment setup events are append-only.")


class EquipmentTemplateRequest(BaseTeamModel):
    """Customer request for Novena to create a missing equipment template."""

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        REVIEWING = "reviewing", _("Reviewing")
        COMPLETED = "completed", _("Completed")

    run = models.ForeignKey(
        DeploymentSetupRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="template_requests",
    )
    manufacturer = models.CharField(max_length=200)
    model_number = models.CharField(max_length=200)
    protocol = models.CharField(max_length=20, choices=DeviceTemplate.PROTOCOL_CHOICES)
    documentation_url = models.URLField(blank=True, max_length=500)
    documentation_file = models.FileField(
        upload_to="equipment-template-requests/%Y/%m/",
        blank=True,
    )
    discovery_evidence = models.JSONField(default=dict, blank=True)
    support_reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)


class RpcCommand(BaseTeamModel):
    """Tracks RPC commands sent to gateways."""

    STATUS_CHOICES = (
        ("pending", _("Pending")),
        ("success", _("Success")),
        ("error", _("Error")),
        ("timeout", _("Timeout")),
    )

    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="rpc_commands")
    request_id = models.UUIDField(unique=True)
    method = models.CharField(max_length=50)
    params = models.JSONField(default=dict)
    sent_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    response_stage = models.CharField(max_length=40, blank=True)
    result = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    remote_command = models.ForeignKey(
        "RemoteCommand",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transport_attempts",
    )

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"RPC {self.method} → {self.gateway.serial_number} ({self.status})"


class DeviceCommand(BaseTeamModel):
    """A control command sent to a device (write-back)."""

    COMMAND_TYPE_CHOICES = (
        ("read", _("Read")),
        ("write", _("Write")),
    )
    STATUS_CHOICES = (
        ("pending", _("Pending")),
        ("sent", _("Sent to Gateway")),
        ("accepted", _("Accepted by Field Protocol")),
        ("executed", _("Executed Successfully")),
        ("failed", _("Failed")),
        ("timed_out", _("Timed Out")),
    )

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="commands")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    rpc_command = models.OneToOneField(
        RpcCommand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_command",
    )

    command_type = models.CharField(max_length=10, choices=COMMAND_TYPE_CHOICES, default="write")
    command_key = models.CharField(max_length=100)  # e.g., 'motor_speed'
    value = models.JSONField(null=True, blank=True)  # The value to write, if any

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    transaction_id = models.CharField(max_length=100, unique=True, null=True, blank=True)

    payload = models.JSONField(default=dict, blank=True, help_text=_("The exact payload sent to the gateway"))
    response_payload = models.JSONField(default=dict, blank=True)

    requested_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    remote_command = models.OneToOneField(
        "RemoteCommand",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legacy_device_command",
    )

    def __str__(self):
        return f"CMD: {self.command_key}={self.value} on {self.device.name} ({self.status})"


class TemplateControlDefinition(models.Model):
    """Novena-verified technical capability; customer policy may only narrow it."""

    template = models.ForeignKey(DeviceTemplate, on_delete=models.CASCADE, related_name="control_definitions")
    command_key = models.CharField(max_length=100)
    operation = models.CharField(max_length=64, default="write_device")
    data_type = models.CharField(max_length=32)
    unit = models.CharField(max_length=32, blank=True)
    connector_mapping = models.JSONField(default=dict)
    technical_limits = models.JSONField(default=dict)
    prerequisites = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=1)
    checksum = models.CharField(max_length=64)
    is_verified = models.BooleanField(default=False)
    is_enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["template", "command_key", "revision"],
                name="unique_template_control_revision",
            )
        ]


class CommissionedControlEnvelope(BaseTeamModel):
    """Site-specific limits accepted by a qualified customer commissioner."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="commissioned_control_envelopes")
    command_key = models.CharField(max_length=100)
    operating_limits = models.JSONField(default=dict)
    prerequisites = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    revision = models.PositiveIntegerField(default=1)
    checksum = models.CharField(max_length=64)
    commissioned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="commissioned_control_envelopes",
    )
    commissioned_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "command_key", "revision"],
                name="unique_commissioned_control_revision",
            )
        ]


class RemoteControlScope(BaseTeamModel):
    """Emergency/activation state at a customer-owned control boundary."""

    class Mode(models.TextChoices):
        DISABLED = "disabled", _("Disabled")
        COMMISSIONING = "commissioning", _("Commissioning")
        ENABLED = "enabled", _("Enabled")
        SUSPENDED = "suspended", _("Suspended")

    site = models.ForeignKey(Site, on_delete=models.CASCADE, null=True, blank=True)
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, null=True, blank=True)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, null=True, blank=True)
    command_key = models.CharField(max_length=100, blank=True)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.DISABLED)
    control_epoch = models.PositiveBigIntegerField(default=1)
    reason = models.TextField(blank=True)


class CommandPolicy(BaseTeamModel):
    """Customer-owned operational policy, bounded by technical and commissioned limits."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, null=True, blank=True)
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, null=True, blank=True)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, null=True, blank=True)
    command_key = models.CharField(max_length=100)
    allowed_roles = models.JSONField(default=list)
    customer_limits = models.JSONField(default=dict)
    prerequisites = models.JSONField(default=list, blank=True)
    approval_required = models.BooleanField(default=False)
    risk = models.CharField(
        max_length=16,
        choices=(
            ("diagnostic", _("Diagnostic")),
            ("low", _("Low")),
            ("high", _("High")),
            ("critical", _("Critical")),
        ),
        default="high",
    )
    revision = models.PositiveIntegerField(default=1)
    checksum = models.CharField(max_length=64)
    is_enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "command_key", "revision"],
                name="unique_command_policy_revision",
            )
        ]


class SiteMembershipAccess(BaseTeamModel):
    """Optional site boundary for a team membership; no rows means all team sites."""

    membership = models.ForeignKey(
        "teams.Membership",
        on_delete=models.CASCADE,
        related_name="site_access",
    )
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="membership_access")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "site"],
                name="unique_membership_site_access",
            )
        ]


class ControlReadinessAssessment(BaseTeamModel):
    class State(models.TextChoices):
        MONITORING_ONLY = "monitoring_only", _("Monitoring only")
        EVIDENCE_COLLECTING = "evidence_collecting", _("Evidence collecting")
        READY_FOR_COMMISSIONING = "ready_for_commissioning", _("Ready for commissioning")
        COMMISSIONING = "commissioning", _("Commissioning")
        READY_FOR_ACTIVATION = "ready_for_activation", _("Ready for activation")
        ACTIVE = "active", _("Active")
        SUSPENDED = "suspended", _("Suspended")
        RECOMMISSIONING_REQUIRED = "recommissioning_required", _("Recommissioning required")

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="control_readiness_assessments")
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="control_readiness_assessments")
    state = models.CharField(max_length=32, choices=State.choices, default=State.MONITORING_ONLY)
    observation_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    telemetry_coverage_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    evidence = models.JSONField(default=dict)
    blockers = models.JSONField(default=list, blank=True)
    waiver_reason = models.TextField(blank=True)
    waiver_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_control_readiness_waivers",
    )
    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="control_readiness_assessments",
    )
    assessed_at = models.DateTimeField(default=timezone.now)


class ControlCommissioningSession(BaseTeamModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        COMPLETED = "completed", _("Completed")
        EXPIRED = "expired", _("Expired")
        CANCELLED = "cancelled", _("Cancelled")

    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE)
    commissioner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="control_commissioning_sessions",
    )
    scope = models.JSONField(default=dict)
    evidence = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)


class ControlActivation(BaseTeamModel):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        SUSPENDED = "suspended", _("Suspended")
        EXPIRED = "expired", _("Expired")
        REVOKED = "revoked", _("Revoked")

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="control_activations")
    command_key = models.CharField(max_length=100)
    readiness_assessment = models.ForeignKey(ControlReadinessAssessment, on_delete=models.PROTECT)
    commissioning_session = models.ForeignKey(ControlCommissioningSession, on_delete=models.PROTECT)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="control_activations",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    control_epoch = models.PositiveBigIntegerField()
    activated_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    suspended_reason = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "command_key", "control_epoch"],
                name="unique_control_activation_epoch",
            )
        ]


class RemoteCommand(BaseTeamModel):
    """Canonical governed command intent, separate from MQTT transport attempts."""

    class Source(models.TextChoices):
        USER = "user", _("User")
        AUTOMATION = "automation", _("Automation")
        SYSTEM = "system", _("System")
        SUPPORT = "support", _("Support")

    class Risk(models.TextChoices):
        DIAGNOSTIC = "diagnostic", _("Diagnostic")
        LOW = "low", _("Low")
        HIGH = "high", _("High")
        CRITICAL = "critical", _("Critical")

    class Status(models.TextChoices):
        REQUESTED = "requested", _("Requested")
        POLICY_DENIED = "policy_denied", _("Policy denied")
        AWAITING_APPROVAL = "awaiting_approval", _("Awaiting approval")
        APPROVED = "approved", _("Approved")
        QUEUED = "queued_for_dispatch", _("Queued for dispatch")
        DISPATCHING = "dispatching", _("Dispatching")
        PUBLISH_ACCEPTED = "publish_accepted", _("Publish accepted")
        BROKER_ACKNOWLEDGED = "broker_acknowledged", _("Broker acknowledged")
        GATEWAY_RECEIVED = "gateway_received", _("Gateway received")
        EXECUTING = "executing", _("Executing")
        FIELD_PROTOCOL_ACCEPTED = "field_protocol_accepted", _("Field protocol accepted")
        VERIFICATION_PENDING = "verification_pending", _("Verification pending")
        VERIFIED = "verified", _("Verified")
        ACTION_INITIATED = "action_initiated", _("Action initiated")
        ACTION_COMPLETED = "action_completed", _("Action completed")
        REJECTED = "rejected", _("Rejected")
        FAILED = "failed", _("Failed")
        EXPIRED = "expired", _("Expired")
        TIMED_OUT = "timed_out", _("Timed out")
        CANCELLED = "cancelled", _("Cancelled")
        OUTCOME_UNKNOWN = "outcome_unknown", _("Outcome unknown")
        RECONCILED_VERIFIED = "reconciled_verified", _("Reconciled: verified")
        RECONCILED_NOT_APPLIED = "reconciled_not_applied", _("Reconciled: not applied")
        RECONCILED_UNRESOLVED = "reconciled_unresolved", _("Reconciled: unresolved")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gateway = models.ForeignKey(Gateway, on_delete=models.PROTECT, related_name="remote_commands")
    device = models.ForeignKey(
        Device,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_commands",
    )
    target_snapshot = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_remote_commands",
    )
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.USER)
    operation = models.CharField(max_length=64)
    command_key = models.CharField(max_length=100, blank=True)
    requested_value = models.JSONField(null=True, blank=True)
    normalized_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    risk = models.CharField(max_length=16, choices=Risk.choices)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.REQUESTED)
    transport_status = models.CharField(max_length=32, default="request_accepted")
    execution_status = models.CharField(max_length=32, default="not_started")
    actor_snapshot = models.JSONField(default=dict, blank=True)
    policy_snapshot = models.JSONField(default=dict, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    schema_version = models.PositiveIntegerField(default=1)
    template_revision = models.PositiveIntegerField(default=0)
    commissioning_revision = models.PositiveIntegerField(default=0)
    policy_revision = models.PositiveIntegerField(default=0)
    policy_checksum = models.CharField(max_length=64, blank=True)
    signing_key_id = models.CharField(max_length=64, blank=True)
    signature = models.TextField(blank=True)
    control_epoch = models.PositiveBigIntegerField(default=1)
    sequence_number = models.PositiveBigIntegerField(default=0)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = models.DateTimeField()
    broker_acknowledged_at = models.DateTimeField(null=True, blank=True)
    gateway_received_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status", "created_at"]),
            models.Index(fields=["gateway", "status"]),
            models.Index(fields=["device", "status"]),
        ]

    def __str__(self):
        target = self.device.name if self.device_id else self.gateway.serial_number
        return f"{self.operation} -> {target} ({self.status})"


class CommandEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("CommandEvent rows are append-only.")

    def delete(self):
        raise TypeError("CommandEvent rows are append-only.")


class CommandEvent(models.Model):
    """Append-only evidence for a governed command lifecycle transition."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    command = models.ForeignKey(RemoteCommand, on_delete=models.PROTECT, related_name="events")
    sequence_number = models.PositiveBigIntegerField(default=0)
    event_type = models.CharField(max_length=80)
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    actor_snapshot = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    checksum = models.CharField(max_length=64, blank=True)
    happened_at = models.DateTimeField(auto_now_add=True)
    objects = CommandEventQuerySet.as_manager()

    class Meta:
        ordering = ["sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["command", "sequence_number"],
                name="unique_command_event_sequence",
            )
        ]
        indexes = [
            models.Index(
                fields=["command", "sequence_number"],
                name="devices_com_command_5dc736_idx",
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("CommandEvent rows are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("CommandEvent rows are append-only.")


class RemoteCommandApproval(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        EXPIRED = "expired", _("Expired")
        INVALIDATED = "invalidated", _("Invalidated")

    command = models.OneToOneField(RemoteCommand, on_delete=models.CASCADE, related_name="approval")
    requested_by_snapshot = models.JSONField(default=dict)
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_command_approvals",
    )
    approver_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    binding_checksum = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    mfa_verified = models.BooleanField(default=False)
    recent_auth_at = models.DateTimeField(null=True, blank=True)


class CommandOutbox(models.Model):
    """Transactional dispatch intent claimed by a dedicated MQTT dispatcher."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        CLAIMED = "claimed", _("Claimed")
        PUBLISHED = "published", _("Published")
        RETRY = "retry", _("Retry scheduled")
        FAILED = "failed", _("Failed")
        DEAD_LETTER = "dead_letter", _("Dead letter")
        CANCELLED = "cancelled", _("Cancelled")

    command = models.OneToOneField(RemoteCommand, on_delete=models.CASCADE, related_name="outbox")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at"],
                name="devices_com_status_797fed_idx",
            )
        ]


class CommandTransportAttempt(models.Model):
    """One bounded pre-ack publish attempt; no retry is allowed after ambiguity."""

    command = models.ForeignKey(RemoteCommand, on_delete=models.CASCADE, related_name="publish_attempts")
    outbox = models.ForeignKey(CommandOutbox, on_delete=models.CASCADE, related_name="transport_attempts")
    attempt_number = models.PositiveIntegerField()
    request_id = models.UUIDField(null=True, blank=True)
    state = models.CharField(max_length=32, default="started")
    broker_message_id = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["command", "attempt_number"],
                name="unique_command_transport_attempt",
            )
        ]


class GatewayControlPolicyBundle(BaseTeamModel):
    """Signed retained edge policy and its acknowledgement state."""

    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="control_policy_bundles")
    revision = models.PositiveIntegerField()
    control_epoch = models.PositiveBigIntegerField()
    payload = models.JSONField(default=dict)
    checksum = models.CharField(max_length=64)
    signing_key_id = models.CharField(max_length=64)
    signature = models.TextField()
    published_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["gateway", "revision"],
                name="unique_gateway_policy_bundle_revision",
            )
        ]


class RemoteControlSigningKey(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        NEXT = "next", _("Next")
        RETIRED = "retired", _("Retired")
        REVOKED = "revoked", _("Revoked")

    key_id = models.CharField(max_length=64, unique=True)
    public_key = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices)
    private_key_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Reference to a managed secret; private key material is never stored here."),
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CommandAuditLegalHold(BaseTeamModel):
    command = models.ForeignKey(RemoteCommand, on_delete=models.PROTECT, related_name="legal_holds")
    reason = models.TextField()
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="command_audit_legal_holds",
    )
    placed_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True, blank=True)


class FirmwareRelease(models.Model):
    """Firmware binaries uploaded by administrators for OTA updates."""

    CHANNEL_CHOICES = (
        ("stable", _("Stable")),
        ("pilot", _("Pilot")),
        ("canary", _("Canary")),
    )
    SIGNING_STATUS_CHOICES = (
        ("unsigned", _("Unsigned")),
        ("signed", _("Signed")),
        ("failed", _("Failed")),
    )

    version = models.CharField(max_length=50, unique=True, help_text=_("Version string, e.g. '1.1.0'"))
    release_notes = models.TextField(blank=True)
    file = models.FileField(upload_to="firmware/", help_text=_("The firmware binary tarball (.tar.gz)"))
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default="stable")
    minimum_gateway_version = models.CharField(max_length=50, blank=True, default="0.1.0")
    maximum_gateway_version = models.CharField(max_length=50, blank=True)
    manifest = models.JSONField(default=dict, blank=True)
    signature = models.TextField(blank=True)
    key_id = models.CharField(max_length=80, blank=True)
    signing_status = models.CharField(max_length=20, choices=SIGNING_STATUS_CHOICES, default="unsigned")
    signed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False, help_text=_("Whether this release is available to gateways"))
    released_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-released_at"]

    @property
    def is_signed(self):
        return self.signing_status == "signed" and bool(self.manifest and self.signature and self.key_id)

    def __str__(self):
        return f"Firmware v{self.version} ({'Active' if self.is_active else 'Draft'})"


@receiver(post_save, sender=Device)
def auto_generate_dashboard_on_template_match(sender, instance, **kwargs):
    if instance.template:
        try:
            from apps.dashboard.services import generate_default_dashboard

            generate_default_dashboard(instance)
        except Exception as e:
            logger = logging.getLogger("novena_hub")
            logger.error("Failed to auto-generate dashboard for device %s: %s", instance.name, e)
