from django.http import Http404

from apps.teams.helpers import get_open_invitations_for_user


def team(request):
    current_team = getattr(request, "team", None)
    permissions = {}
    if current_team and request.user.is_authenticated:
        from apps.teams.roles import has_permission, visible_site_ids

        permissions = {
            name: has_permission(request.user, current_team, name)
            for name in (
                "manage_devices",
                "view_dashboard",
                "view_devices",
                "manage_shared_links",
                "view_maintenance",
                "view_automations",
            )
        }
        if visible_site_ids(request.user, current_team) is not None:
            permissions["site_limited"] = True
            for name in ("manage_devices", "manage_shared_links", "view_automations"):
                permissions[name] = False
    return {
        "team": current_team,
        "nav_permissions": permissions,
    }


def user_teams(request):
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    try:
        current_team = getattr(request, "team", None)
        if not current_team:
            return {}
    except Http404:
        # if the above raises a 404 it can cause a 500 error instead of letting it propagate
        return {}

    other_membership = request.user.membership_set
    if current_team and current_team.pk:
        other_membership = other_membership.exclude(team=current_team)

    pending_invitations = get_open_invitations_for_user(request.user)
    return {
        "other_teams": {
            membership.team.name: membership.team.dashboard_url
            for membership in other_membership.select_related("team")
        },
        "user_pending_invitations": pending_invitations,
    }
