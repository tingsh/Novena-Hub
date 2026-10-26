from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.db import transaction
from django.http import FileResponse, Http404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    DeploymentSetupEvent,
    DeploymentSetupItem,
    DeploymentSetupRun,
    Device,
    DeviceDatapointMap,
    DeviceDatapointMapRevision,
    DeviceTemplate,
    EquipmentTemplateRequest,
    FirmwareRelease,
    Gateway,
    GatewayActivation,
    GatewayConfig,
    GatewayConfigOutbox,
    GatewayInventory,
    RpcCommand,
    Site,
    TemplateShareSubmission,
)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "team", "timezone")
    list_filter = ("team",)
    search_fields = ("name",)


@admin.register(Gateway)
class GatewayAdmin(admin.ModelAdmin):
    list_display = ("name", "serial_number", "site", "status", "lifecycle_status", "last_seen", "tls_mode")
    list_filter = ("status", "lifecycle_status", "tls_mode", "site__team")
    search_fields = ("name", "serial_number", "mqtt_username")
    readonly_fields = ("access_token", "mqtt_username", "mqtt_password")
    actions = ["recover_claim_codes"]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "team",
                    "site",
                    "name",
                    "serial_number",
                    "status",
                    "lifecycle_status",
                    "firmware_version",
                    "capacity",
                )
            },
        ),
        ("MQTT Credentials", {"fields": ("access_token", "mqtt_username", "mqtt_password", "tls_mode")}),
        (
            "Heartbeat Attributes",
            {
                "fields": (
                    "last_seen",
                    "ip_address",
                    "uptime_seconds",
                    "python_version",
                    "platform_info",
                    "active_connectors",
                    "connected_devices",
                )
            },
        ),
        (
            "Advanced",
            {
                "fields": ("config", "discovery_data", "client_cert_pem", "client_key_pem"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.action(description="Recover claim codes for selected gateways")
    def recover_claim_codes(self, request, queryset):
        from django.contrib import messages

        from .services import compute_claim_code

        for gw in queryset:
            code = compute_claim_code(gw.serial_number)
            messages.info(request, f"Gateway {gw.serial_number} → Claim Code: {code}")


@admin.register(GatewayInventory)
class GatewayInventoryAdmin(admin.ModelAdmin):
    list_display = ("serial_number", "status", "batch", "claimed_by_team", "gateway", "claimed_at")
    list_filter = ("status", "batch")
    search_fields = ("serial_number", "batch", "claimed_by_team__name")
    readonly_fields = ("manufactured_at", "claimed_at", "gateway", "claimed_by_team")

    actions = ["recover_claim_codes"]

    @admin.action(description="Recover claim codes for selected inventory records")
    def recover_claim_codes(self, request, queryset):
        from django.contrib import messages

        from .services import compute_claim_code

        for item in queryset:
            code = compute_claim_code(item.serial_number)
            messages.info(request, f"Gateway {item.serial_number} -> Claim Code: {code}")


@admin.register(DeviceTemplate)
class DeviceTemplateAdmin(admin.ModelAdmin):
    actions = ["retire_templates"]
    list_display = (
        "name",
        "manufacturer",
        "device_type",
        "protocol",
        "category",
        "is_verified",
        "source",
        "ai_confidence",
        "usage_count",
        "created_at",
    )
    list_filter = ("device_type", "protocol", "category", "is_verified", "source")
    search_fields = ("name", "manufacturer", "model_number", "model_aliases", "variant")
    readonly_fields = ("usage_count", "created_at")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and obj.catalog_key:
            return tuple(field.name for field in obj._meta.fields if field.name != "is_current")
        return fields

    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.catalog_key) and super().has_delete_permission(request, obj)

    @admin.action(description="Retire selected templates from new selection")
    def retire_templates(self, request, queryset):
        for template in queryset.filter(is_current=True):
            template.is_current = False
            template.evidence_status = "retired"
            template.is_verified = False
            template.save(update_fields=["is_current", "evidence_status", "is_verified"])
        self.message_user(request, "Selected templates retired; existing equipment assignments were retained.")


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "device_type", "energy_category", "site", "status", "last_telemetry_at")
    list_filter = ("device_type", "energy_category", "status", "site__team")
    search_fields = ("name",)


@admin.register(GatewayConfig)
class GatewayConfigAdmin(admin.ModelAdmin):
    list_display = ("gateway", "action", "status", "pushed_at", "acknowledged_at")
    list_filter = ("status", "action")
    search_fields = ("gateway__serial_number", "request_id")
    readonly_fields = ("request_id", "pushed_at")


@admin.register(GatewayConfigOutbox)
class GatewayConfigOutboxAdmin(admin.ModelAdmin):
    list_display = ("config", "status", "attempt_count", "next_attempt_at", "delivered_at")
    list_filter = ("status",)
    readonly_fields = ("config", "attempt_count", "claimed_at", "delivered_at", "dead_lettered_at")


@admin.register(DeploymentSetupRun)
class DeploymentSetupRunAdmin(admin.ModelAdmin):
    list_display = ("run_id", "team", "site", "gateway", "state", "current_step", "created_at")
    list_filter = ("state", "team")
    readonly_fields = ("run_id", "created_at", "updated_at")


@admin.register(DeploymentSetupItem)
class DeploymentSetupItemAdmin(admin.ModelAdmin):
    list_display = ("run", "device", "selected_template", "confidence_score", "trust_level", "state")
    list_filter = ("state", "trust_level")


@admin.register(DeviceDatapointMap)
class DeviceDatapointMapAdmin(admin.ModelAdmin):
    list_display = ("device", "team", "status", "validated_at", "confirmed_at")
    list_filter = ("status", "team")
    readonly_fields = ("tested_checksum", "confirmed_checksum", "last_validation", "datapoint_health")


@admin.register(DeviceDatapointMapRevision)
class DeviceDatapointMapRevisionAdmin(admin.ModelAdmin):
    list_display = ("mapping", "revision_number", "team", "validated_at", "confirmed_at")
    list_filter = ("team",)
    readonly_fields = (
        "mapping",
        "team",
        "revision_number",
        "datapoints",
        "datapoints_checksum",
        "confirmed_checksum",
        "validation_result",
        "validated_by",
        "validated_at",
        "confirmed_by",
        "confirmed_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeploymentSetupEvent)
class DeploymentSetupEventAdmin(admin.ModelAdmin):
    list_display = ("run", "sequence_number", "event_type", "message", "created_at")
    readonly_fields = ("run", "item", "sequence_number", "event_type", "message", "evidence", "actor", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EquipmentTemplateRequestAdminForm(forms.ModelForm):
    proposed_signals = forms.JSONField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 12, "cols": 100}),
        help_text=(
            'Read-only signal list. Example: [{"key":"active_power","label":"Active power",'
            '"address":100,"read_function_code":3,"data_type":"float32",'
            '"objects_count":2,"unit":"W","multiplier":1}]. '
            "Check the OEM address convention, type, word order, scale and invalid codes before delivery."
        ),
    )

    class Meta:
        model = EquipmentTemplateRequest
        fields = "__all__"

    def clean_proposed_signals(self):
        value = self.cleaned_data.get("proposed_signals") or []
        if not value:
            return []
        from .team_maps import reusable_signals

        return reusable_signals(value)


@admin.register(EquipmentTemplateRequest)
class EquipmentTemplateRequestAdmin(admin.ModelAdmin):
    form = EquipmentTemplateRequestAdminForm
    list_display = ("support_reference", "team", "manufacturer", "model_number", "protocol", "status", "assigned_to")
    list_filter = ("status", "protocol")
    search_fields = ("support_reference", "manufacturer", "model_number", "team__name")
    readonly_fields = (
        "team",
        "run",
        "requested_by",
        "support_reference",
        "discovery_evidence",
        "document_links",
        "delivered_template",
        "created_at",
        "updated_at",
    )
    exclude = ("documentation_file", "nameplate_photo")
    actions = ["deliver_private_maps"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom = [
            path(
                "<int:pk>/private-file/<str:kind>/",
                self.admin_site.admin_view(self.private_file),
                name="devices_equipmenttemplaterequest_private_file",
            ),
        ]
        return custom + super().get_urls()

    def private_file(self, request, pk, kind):
        obj = self.get_object(request, pk)
        if not obj or not self.has_view_permission(request, obj):
            raise Http404
        field = {"manual": obj.documentation_file, "nameplate": obj.nameplate_photo}.get(kind)
        if field is None or not field.name:
            raise Http404
        storage = storages["template_request_documents"]
        if not storage.exists(field.name):
            raise Http404
        return FileResponse(storage.open(field.name, "rb"), as_attachment=True)

    @admin.display(description="Private files")
    def document_links(self, obj):
        if not obj or not obj.pk:
            return "—"
        links = []
        for kind, label, field in (
            ("manual", "Manual PDF", obj.documentation_file),
            ("nameplate", "Nameplate", obj.nameplate_photo),
        ):
            if field:
                url = reverse("admin:devices_equipmenttemplaterequest_private_file", args=[obj.pk, kind])
                links.append(format_html('<a href="{}">{}</a>', url, label))
        return format_html(" · ".join("{}" for _ in links), *links) if links else "—"

    def save_model(self, request, obj, form, change):
        previous = EquipmentTemplateRequest.objects.filter(pk=obj.pk).values("status", "customer_update").first()
        super().save_model(request, obj, form, change)
        if previous and (previous["status"] != obj.status or previous["customer_update"] != obj.customer_update):
            from .template_help import _send_help_email

            transaction.on_commit(lambda: _send_help_email(help_request=obj, to_customer=True))

    @admin.action(description="Deliver saved signal maps to selected paid requests")
    def deliver_private_maps(self, request, queryset):
        from .template_help import deliver_help_map

        for item in queryset:
            try:
                template = deliver_help_map(help_request=item, actor=request.user)
                self.message_user(request, f"Delivered {template.name} to {item.team}.", messages.SUCCESS)
            except ValidationError as exc:
                self.message_user(request, f"{item.support_reference}: {'; '.join(exc.messages)}", messages.ERROR)


@admin.register(TemplateShareSubmission)
class TemplateShareSubmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "team", "manufacturer", "model_number", "protocol", "status", "submitted_at")
    list_filter = ("status", "protocol", "submitted_at")
    search_fields = ("manufacturer", "model_number", "team__name")
    readonly_fields = (
        "team",
        "template",
        "revision",
        "register_map_snapshot",
        "protocol",
        "device_type",
        "category",
        "submitted_by",
        "submitted_at",
        "status",
        "reviewed_by",
        "reviewed_at",
        "published_template",
    )
    actions = ["mark_reviewing", "reject_submission", "publish_documented", "publish_verified"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and obj.status == TemplateShareSubmission.Status.PUBLISHED:
            published_fields = (
                *fields,
                "manufacturer",
                "model_number",
                "source_document_url",
                "review_notes",
                "public_compatibility_notes",
                "portability_evidence",
            )
            if obj.published_template.evidence_status == "field_verified":
                return (*published_fields, "field_verification_evidence")
            return published_fields
        return fields

    @admin.action(description="Mark selected submissions as reviewing")
    def mark_reviewing(self, request, queryset):
        count = queryset.filter(status=TemplateShareSubmission.Status.SUBMITTED).update(
            status=TemplateShareSubmission.Status.REVIEWING
        )
        self.message_user(request, f"{count} submissions moved to review.")

    @admin.action(description="Reject selected submissions with review notes")
    def reject_submission(self, request, queryset):
        for submission in queryset.exclude(status=TemplateShareSubmission.Status.PUBLISHED):
            if not submission.review_notes.strip():
                self.message_user(request, f"Submission {submission.pk} needs review notes.", level=messages.ERROR)
                continue
            submission.status = TemplateShareSubmission.Status.REJECTED
            submission.reviewed_by = request.user
            submission.reviewed_at = timezone.now()
            submission.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    @admin.action(description="Publish selected maps as field verified after evidence review")
    def publish_verified(self, request, queryset):
        from .template_sharing import publish_verified_submission

        for submission in queryset:
            try:
                published = publish_verified_submission(submission=submission, reviewer=request.user)
            except ValidationError as exc:
                self.message_user(request, f"Submission {submission.pk}: {exc}", level=messages.ERROR)
            else:
                self.message_user(request, f"Published {published.name} revision {published.catalog_revision}.")

    @admin.action(description="Publish selected templates as OEM documented after review")
    def publish_documented(self, request, queryset):
        from .template_sharing import publish_reviewed_submission

        for submission in queryset:
            try:
                published = publish_reviewed_submission(
                    submission=submission, reviewer=request.user, evidence_status="documented"
                )
            except ValidationError as exc:
                self.message_user(request, f"Submission {submission.pk}: {exc}", level=messages.ERROR)
            else:
                self.message_user(request, f"Published {published.name} as OEM documented.")


@admin.register(GatewayActivation)
class GatewayActivationAdmin(admin.ModelAdmin):
    list_display = ("gateway", "status", "attempt_count", "expires_at", "delivered_at", "acknowledged_at")
    list_filter = ("status", "gateway__team")
    search_fields = ("gateway__serial_number", "request_id")
    readonly_fields = (
        "request_id",
        "status",
        "attempt_count",
        "last_attempt_at",
        "delivered_at",
        "acknowledged_at",
        "expires_at",
        "encrypted_mqtt_password",
        "last_error",
    )


@admin.register(RpcCommand)
class RpcCommandAdmin(admin.ModelAdmin):
    list_display = ("gateway", "method", "status", "sent_at", "responded_at")
    list_filter = ("status", "method")
    search_fields = ("gateway__serial_number", "request_id")
    readonly_fields = ("request_id", "sent_at")


@admin.register(FirmwareRelease)
class FirmwareReleaseAdmin(admin.ModelAdmin):
    list_display = ("version", "channel", "signing_status", "key_id", "is_active", "released_at")
    list_filter = ("is_active", "channel", "signing_status")
    search_fields = ("version", "release_notes")
    readonly_fields = (
        "sha256",
        "size_bytes",
        "manifest",
        "signature",
        "key_id",
        "signing_status",
        "signed_at",
        "expires_at",
        "released_at",
    )
