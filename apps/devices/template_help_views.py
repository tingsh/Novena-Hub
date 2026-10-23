"""Team-facing paid map-assistance request pages."""

from django.contrib import messages
from django.core.files.storage import storages
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.teams.decorators import require_permission

from .models import EquipmentTemplateRequest
from .template_help import TemplateHelpRequestForm, _send_help_email, create_help_request


@require_permission("manage_devices")
@require_http_methods(["GET", "POST"])
def template_help_list(request, team_slug):
    if request.method == "POST":
        form = TemplateHelpRequestForm(request.POST, request.FILES)
        if form.is_valid():
            help_request, _ = create_help_request(team=request.team, actor=request.user, form=form)
            messages.success(request, "Request received. Novena will assess it and send a quote before work starts.")
            return redirect("web_team:devices:template_help_detail", team_slug=team_slug, pk=help_request.pk)
    else:
        form = TemplateHelpRequestForm()
    requests = EquipmentTemplateRequest.objects.filter(team=request.team).select_related("delivered_template")
    return render(request, "devices/template_help_list.html", {"form": form, "help_requests": requests})


@require_permission("manage_devices")
@require_http_methods(["GET", "POST"])
def template_help_detail(request, team_slug, pk):
    help_request = get_object_or_404(
        EquipmentTemplateRequest.objects.select_related("delivered_template", "setup_item", "run"),
        pk=pk, team=request.team,
    )
    if request.method == "POST" and request.POST.get("action") == "provide_info":
        reply = request.POST.get("customer_reply", "").strip()[:3000]
        if not reply:
            messages.error(request, "Enter the equipment details Novena requested.")
        elif help_request.status != EquipmentTemplateRequest.Status.MORE_INFO:
            messages.error(request, "This request is not waiting for more information.")
        else:
            with transaction.atomic():
                help_request.customer_reply = reply
                help_request.status = EquipmentTemplateRequest.Status.SUBMITTED
                help_request.save(update_fields=["customer_reply", "status", "updated_at"])
                transaction.on_commit(lambda: _send_help_email(help_request=help_request))
            messages.success(request, "Your additional details were sent to Novena.")
        return redirect("web_team:devices:template_help_detail", team_slug=team_slug, pk=pk)
    return render(request, "devices/template_help_detail.html", {"help_request": help_request})


@require_permission("manage_devices")
def template_help_file(request, team_slug, pk, kind):
    help_request = get_object_or_404(EquipmentTemplateRequest, pk=pk, team=request.team)
    field = {"manual": help_request.documentation_file, "nameplate": help_request.nameplate_photo}.get(kind)
    if field is None or not field.name:
        raise Http404
    storage = storages["template_request_documents"]
    if not storage.exists(field.name):
        raise Http404
    return FileResponse(storage.open(field.name, "rb"), as_attachment=True)
