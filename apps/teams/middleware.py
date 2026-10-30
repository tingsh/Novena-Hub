import contextlib

from django.http import HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin
from django.utils.functional import SimpleLazyObject

from apps.teams.helpers import get_team_for_request
from apps.teams.models import Membership


def _get_team(request, view_kwargs):
    if not hasattr(request, "_cached_team"):
        team = get_team_for_request(request, view_kwargs)
        if team:
            request.session["team"] = team.id
        request._cached_team = team
    return request._cached_team


def _get_team_membership(request):
    if not hasattr(request, "_cached_team_membership"):
        team_membership = None
        if request.user.is_authenticated and request.team:
            with contextlib.suppress(Membership.DoesNotExist):
                team_membership = Membership.objects.get(team=request.team, user=request.user)
        request._cached_team_membership = team_membership
    return request._cached_team_membership


class TeamsMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        request.team = SimpleLazyObject(lambda: _get_team(request, view_kwargs))
        request.team_membership = SimpleLazyObject(lambda: _get_team_membership(request))


class SiteBoundaryMiddleware(MiddlewareMixin):
    """Fail closed on team routes not audited for site-limited membership."""

    SAFE_ROUTES = {
        "web_team:home",
        "web_team:devices:site_list",
        "web_team:devices:site_detail",
        "web_team:devices:gateway_list",
        "web_team:devices:gateway_detail",
        "web_team:devices:device_list",
        "web_team:devices:device_detail",
        "web_team:alerts:alert_list",
        "web_team:alerts:rule_list",
        "web_team:maintenance:ticket_list",
        "web_team:maintenance:ticket_detail",
        "web_team:maintenance:schedule_list",
        "web_team:telemetry:chart_partial",
        "web_team:telemetry:kpi_partial",
        "web_team:telemetry:device_metrics_api",
        "web_team:telemetry:device_telemetry_history_api",
        "web_team:telemetry:device_telemetry_samples_api",
        "web_team:telemetry:export_telemetry_csv",
        "web_team:telemetry:site_profile_report",
        "web_team:telemetry:export_site_profile_report_csv",
        "web_team:telemetry:telemetry_analyzer",
    }

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.user.is_authenticated or "team_slug" not in view_kwargs:
            return None
        from apps.teams.roles import visible_site_ids

        team = request.team
        if not team or visible_site_ids(request.user, team) is None:
            return None
        if request.method not in ("GET", "HEAD") or request.resolver_match.view_name not in self.SAFE_ROUTES:
            return HttpResponseForbidden("This route is unavailable to site-limited members.")
        return None
