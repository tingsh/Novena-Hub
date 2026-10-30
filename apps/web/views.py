from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from health_check.views import MainView
from waffle import flag_is_active

from apps.devices.models import Site
from apps.teams.decorators import require_permission
from apps.teams.helpers import get_open_invitations_for_user
from apps.teams.roles import has_permission, visible_site_ids


def home(request):
    if request.user.is_authenticated:
        team = request.team
        if team:
            return HttpResponseRedirect(reverse("web_team:home", args=[team.slug]))
        else:
            if (open_invitations := get_open_invitations_for_user(request.user)) and len(open_invitations) > 1:
                invitation = open_invitations[0]
                return HttpResponseRedirect(reverse("teams:accept_invitation", args=[invitation["id"]]))

            messages.info(
                request,
                _("Teams are enabled but you have no teams. Create a team below to access the rest of the dashboard."),
            )
            return HttpResponseRedirect(reverse("teams:manage_teams"))
    else:
        from wagtail.views import serve

        return serve(request, request.path)


@require_permission("view_dashboard")
def team_home(request, team_slug):
    assert request.team.slug == team_slug

    team = request.team
    allowed_site_ids = visible_site_ids(request.user, team)
    visible_site_query = Site.objects.filter(team=team)
    if allowed_site_ids is not None:
        visible_site_query = visible_site_query.filter(id__in=allowed_site_ids)
    if not visible_site_query.exists():
        if allowed_site_ids is not None:
            raise Http404
        return HttpResponseRedirect(reverse("web_team:onboarding:start", args=[team_slug]))

    from apps.dashboard.command_center import client_config, panel_context, resolve_layout
    from apps.dashboard.services import build_team_operations_dashboard

    impact_site_ids = [
        site.id
        for site in Site.objects.filter(team=team).only("id")
        if has_permission(request.user, team, "view_business_impact", site=site)
    ]
    include_impact = bool(flag_is_active(request, "business_impact_roi") and impact_site_ids)
    customization_enabled = bool(flag_is_active(request, "command_center_customization") and allowed_site_ids is None)
    dashboard_context = build_team_operations_dashboard(
        team,
        include_impact=include_impact,
        impact_site_ids=impact_site_ids,
        visible_site_ids=allowed_site_ids,
    )
    layout_resolution = resolve_layout(team, request.user)
    single_device_mode = dashboard_context["sites_count"] == 1 and dashboard_context["devices_count"] == 1
    if single_device_mode and layout_resolution["source"] == "novena_default":
        for panel in layout_resolution["layout"]["panels"]:
            if panel["id"] in {"asset_mix", "device_fleet", "sites_actions"}:
                panel["hidden"] = True
    layout_urls = {
        "save": reverse("web_team:command_center:save_layout", args=[team.slug]),
        "reset": reverse("web_team:command_center:reset_layout", args=[team.slug]),
        "publish": reverse("web_team:command_center:publish_default", args=[team.slug]),
        "remove_default": reverse("web_team:command_center:remove_default", args=[team.slug]),
    }
    context = {
        "team": team,
        "active_tab": "dashboard",
        "page_title": _("{team} Dashboard").format(team=team),
        "command_center_customization_enabled": customization_enabled,
        "can_manage_alerts": allowed_site_ids is None and has_permission(request.user, team, "manage_alerts"),
        "single_device_mode": single_device_mode,
        "command_center_panels": panel_context(layout_resolution["layout"], include_impact=include_impact),
        "command_center_config": client_config(
            layout_resolution,
            include_impact=include_impact,
            can_publish=has_permission(request.user, team, "manage_team"),
            urls=layout_urls,
        ),
        **dashboard_context,
    }
    return render(request, "web/app_home.html", context=context)


def simulate_error(request):
    raise Exception("This is a simulated error.")


class HealthCheck(MainView):
    def get(self, request, *args, **kwargs):
        tokens = settings.HEALTH_CHECK_TOKENS
        if tokens and request.GET.get("token") not in tokens:
            raise Http404
        return super().get(request, *args, **kwargs)
