from __future__ import annotations

from apps.users.models import CustomUser

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

ROLE_CHOICES = (
    (ROLE_OWNER, "Owner"),
    (ROLE_ADMIN, "Administrator"),
    (ROLE_MANAGER, "Site Manager"),
    (ROLE_OPERATOR, "Operator"),
    (ROLE_VIEWER, "Viewer"),
)

ROLE_DESCRIPTIONS = {
    ROLE_OWNER: "Full access to all features, including billing and team ownership transfer.",
    ROLE_ADMIN: "Full access to manage devices, alerts, and teams.",
    ROLE_MANAGER: "Can manage devices and alerts. Site-assigned members currently have read-only monitoring access.",
    ROLE_OPERATOR: "Real-time monitoring and alert acknowledgment.",
    ROLE_VIEWER: "Read-only access to dashboards and reports.",
}

# Permission map
PERMISSIONS = {
    "view_dashboard": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR, ROLE_VIEWER],
    "manage_devices": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "view_devices": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR, ROLE_VIEWER],
    "manage_alerts": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "acknowledge_alerts": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR],
    "send_commands": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "send_critical_commands": [ROLE_OWNER, ROLE_ADMIN],
    "view_command_audit": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR],
    "request_low_risk_commands": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR],
    "request_high_risk_commands": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "request_critical_commands": [ROLE_OWNER, ROLE_ADMIN],
    "approve_high_risk_commands": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "approve_critical_commands": [ROLE_OWNER, ROLE_ADMIN],
    "manage_command_policies": [ROLE_OWNER, ROLE_ADMIN],
    "manage_commissioned_limits": [ROLE_OWNER, ROLE_ADMIN],
    "perform_commissioning": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "toggle_remote_control": [ROLE_OWNER, ROLE_ADMIN],
    "export_command_audit": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "manage_automations": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "view_automations": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "manage_team": [ROLE_OWNER, ROLE_ADMIN],
    "delete_team": [ROLE_OWNER],
    "manage_billing": [ROLE_OWNER],
    "manage_shared_links": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "manage_maintenance": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "view_maintenance": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR, ROLE_VIEWER],
    "view_business_impact": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR, ROLE_VIEWER],
    "manage_business_impact": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER],
    "manage_impact_reports": [ROLE_OWNER, ROLE_ADMIN],
    "download_impact_reports": [ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER, ROLE_OPERATOR, ROLE_VIEWER],
}


def is_member(user: CustomUser, team) -> bool:
    if not team:
        return False
    if getattr(team, "status", None) == "closed":
        return False
    return team.members.filter(id=user.id).exists()


def is_admin(user: CustomUser, team) -> bool:
    if not team:
        return False
    if getattr(team, "status", None) == "closed":
        return False

    from .models import Membership

    return Membership.objects.filter(team=team, user=user, role__in=[ROLE_OWNER, ROLE_ADMIN]).exists()


def has_permission(user: CustomUser, team, permission: str, *, site=None) -> bool:
    """Check if a user has a specific permission within a team."""
    if not team or not user.is_authenticated:
        return False
    if getattr(team, "status", None) == "closed":
        return False

    from .models import Membership

    try:
        membership = Membership.objects.get(user=user, team=team)
        if membership.role not in PERMISSIONS.get(permission, []):
            return False
        if site is not None and membership.site_access.exists():
            return membership.site_access.filter(site=site, team=team).exists()
        return True
    except Membership.DoesNotExist:
        return False


def visible_site_ids(user, team):
    """Return None for unrestricted members, otherwise their permitted site ids."""
    from .models import Membership

    membership = Membership.objects.filter(user=user, team=team).first()
    if membership is None:
        return []
    ids = list(membership.site_access.filter(team=team).values_list("site_id", flat=True))
    return ids if ids else None


def visible_sites(queryset, user, team, *, lookup="site_id"):
    ids = visible_site_ids(user, team)
    return queryset if ids is None else queryset.filter(**{f"{lookup}__in": ids})
