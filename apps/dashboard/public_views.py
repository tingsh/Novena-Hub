import hashlib

from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from .models import SharedDashboard


def public_dashboard(request, token):
    link = get_object_or_404(SharedDashboard, token=token)

    if not link.is_active:
        raise Http404("This shared link has been deactivated.")

    if link.is_expired:
        raise Http404("This shared link has expired.")

    # Check if a password is required
    # A changed password must revoke sessions unlocked with the old password.
    password_version = hashlib.sha256(link.password_hash.encode()).hexdigest()[:16]
    session_key = f"unlocked_dashboard_{link.token}_{password_version}"
    is_unlocked = request.session.get(session_key, False)

    if link.password_hash and not is_unlocked:
        if request.method == "POST":
            password = request.POST.get("password")
            if link.check_password(password):
                request.session[session_key] = True
            else:
                return render(
                    request, "dashboard/public/password_prompt.html", {"error": "Incorrect password.", "link": link}
                )
        else:
            return render(request, "dashboard/public/password_prompt.html", {"link": link})

    # Record view stats on successful load (not on password prompts)
    if request.method == "GET":
        link.view_count += 1
        link.last_viewed_at = timezone.now()
        link.save(update_fields=["view_count", "last_viewed_at"])

    # Gather data for the team's dashboard
    # Let's get the summary stats, active alerts, and site statuses
    from apps.alerts.models import Alert, AlertRule
    from apps.devices.datapoint_maps import effective_register_map
    from apps.telemetry.services import get_site_summary_stats

    # Kiosk mode hides navigation and applies tight padding
    kiosk_mode = request.GET.get("kiosk") == "1"

    sites = link.team.site_set.all()
    site_stats = []
    for site in sites:
        stats = get_site_summary_stats(site)
        devices = list(site.devices.select_related("template", "gateway"))
        offline_count = sum(device.freshness.status != "live" for device in devices)
        monitoring_active = (
            AlertRule.objects.filter(team=link.team, is_active=True)
            .filter(Q(site=site) | Q(device__site=site) | Q(site__isnull=True, device__isnull=True))
            .exists()
        )
        energy_keys = {
            "energy_import",
            "import_energy",
            "energy_import_total",
            "active_power_total",
            "active_power",
            "power",
        }
        energy_available = any(
            device.energy_category != "none" or energy_keys.intersection(effective_register_map(device))
            for device in devices
        )
        site_stats.append(
            {
                "site": site,
                "stats": stats,
                "device_count": len(devices),
                "active_alert_count": Alert.objects.filter(device__site=site, status="active").count(),
                "offline_count": offline_count,
                "monitoring_active": monitoring_active,
                "energy_available": energy_available,
            }
        )

    active_alerts = Alert.objects.filter(device__team=link.team, status="active").order_by("-triggered_at")[:10]

    context = {
        "link": link,
        "team": link.team,
        "site_stats": site_stats,
        "active_alerts": active_alerts,
        "kiosk_mode": kiosk_mode,
    }

    return render(request, "dashboard/public/public_dashboard.html", context)
