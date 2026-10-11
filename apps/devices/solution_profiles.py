from dataclasses import dataclass

GENERAL_IOT = "general_iot"
COLD_CHAIN = "cold_chain"
FACTORY_ENERGY = "factory_energy"
FACILITIES_HVAC = "facilities_hvac"


@dataclass(frozen=True)
class AlertPreset:
    name: str
    key: str
    condition: str
    threshold: float
    severity: str = "warning"
    duration_seconds: int = 0
    create_maintenance_ticket: bool = False


@dataclass(frozen=True)
class MaintenancePreset:
    title: str
    device_types: tuple[str, ...]
    usage_telemetry_key: str = ""
    usage_threshold: float = 0.0


@dataclass(frozen=True)
class SolutionProfile:
    key: str
    name: str
    short_name: str
    icon: str
    promise: str
    setup_copy: str
    site_placeholder: str
    recommended_device_types: tuple[str, ...]
    recommended_categories: tuple[str, ...]
    key_priority: tuple[str, ...]
    alert_presets: tuple[AlertPreset, ...]
    maintenance_presets: tuple[MaintenancePreset, ...] = ()
    site_types: tuple[tuple[str, str], ...] = ()
    reports: tuple[str, ...] = ()


PROFILES = {
    GENERAL_IOT: SolutionProfile(
        key=GENERAL_IOT,
        name="General IoT",
        short_name="General",
        icon="fa-sliders",
        promise="Connect mixed industrial equipment and build your own monitoring view.",
        setup_copy="Novena will keep the experience flexible and show every supported template.",
        site_placeholder="Main Site",
        recommended_device_types=(),
        recommended_categories=(),
        key_priority=("active_power", "temperature", "status", "voltage", "current"),
        alert_presets=(),
        reports=("Telemetry CSV", "Recent alerts", "Device health"),
    ),
    COLD_CHAIN: SolutionProfile(
        key=COLD_CHAIN,
        name="Cold Chain Monitoring",
        short_name="Cold Chain",
        icon="fa-snowflake",
        promise="Track temperatures, door events, and compressor health so teams can act before spoilage risk grows.",
        setup_copy=(
            "Novena will prioritize temperature sensors, door states, compressor status, "
            "excursion alerts, and audit-friendly CSV exports."
        ),
        site_placeholder="Jurong Cold Room A",
        recommended_device_types=("temp_sensor", "chiller"),
        recommended_categories=("cold_chain",),
        key_priority=(
            "temperature",
            "humidity",
            "door_open",
            "door_status",
            "compressor_status",
            "active_power",
        ),
        alert_presets=(
            AlertPreset("High Temperature", "temperature", "gt", 4.0, "critical", 60, True),
            AlertPreset("Door Open Too Long", "door_open", "eq", 1.0, "warning", 300, False),
            AlertPreset("Compressor Inactive", "compressor_status", "eq", 0.0, "warning", 300, True),
        ),
        site_types=(
            ("cold_room", "Cold room"),
            ("freezer", "Freezer"),
            ("central_kitchen", "Central kitchen"),
            ("food_storage", "Food storage"),
            ("pharma_storage", "Pharma storage"),
        ),
        reports=("Excursion log", "Time above threshold", "Audit CSV"),
    ),
    FACTORY_ENERGY: SolutionProfile(
        key=FACTORY_ENERGY,
        name="Factory Energy Monitoring",
        short_name="Factory Energy",
        icon="fa-bolt",
        promise="See demand, kWh, spikes, and abnormal loads across machines and meters.",
        setup_copy=(
            "Novena will prioritize power meters, VFDs, energy dashboards, spike alerts, and savings-evidence exports."
        ),
        site_placeholder="Tuas Assembly Line",
        recommended_device_types=("power_meter", "solar_inverter", "vfd", "plc"),
        recommended_categories=("energy", "factory"),
        key_priority=(
            "active_power",
            "energy",
            "voltage",
            "current",
            "frequency",
            "power_factor",
            "output_frequency",
            "run_status",
        ),
        alert_presets=(
            AlertPreset("Power Spike", "active_power", "gt", 1200.0, "warning", 60, False),
            AlertPreset("Over Voltage", "voltage", "gt", 245.0, "critical", 30, True),
            AlertPreset("Abnormal Current", "current", "gt", 80.0, "warning", 60, False),
        ),
        site_types=(
            ("factory", "Factory"),
            ("workshop", "Workshop"),
            ("production_line", "Production line"),
            ("warehouse", "Warehouse"),
            ("solar_site", "Solar or hybrid energy site"),
        ),
        reports=("Daily kWh", "Peak demand windows", "Spike events", "Top consuming devices"),
    ),
    FACILITIES_HVAC: SolutionProfile(
        key=FACILITIES_HVAC,
        name="Facilities / HVAC",
        short_name="Facilities",
        icon="fa-building",
        promise="Monitor AC, chillers, runtime, energy use, and maintenance work across your facility.",
        setup_copy=(
            "Novena will prioritize HVAC drift, runtime, power draw, maintenance tickets, "
            "and contractor-ready workflows."
        ),
        site_placeholder="Orchard Boutique Hotel",
        recommended_device_types=("chiller", "temp_sensor", "power_meter", "plc", "vfd"),
        recommended_categories=("factory", "energy", "cold_chain"),
        key_priority=(
            "temperature",
            "active_power",
            "run_hours",
            "compressor_status",
            "fan_status",
            "pump_status",
            "flow_rate",
            "pressure",
            "energy",
        ),
        alert_presets=(
            AlertPreset("HVAC Temperature Drift", "temperature", "gt", 9.0, "warning", 300, True),
            AlertPreset("Excessive Runtime", "run_hours", "gt", 500.0, "warning", 0, False),
            AlertPreset("Abnormal Power Draw", "active_power", "gt", 1500.0, "warning", 120, False),
        ),
        maintenance_presets=(
            MaintenancePreset("Runtime-based HVAC service", ("chiller", "vfd", "plc"), "run_hours", 500.0),
        ),
        site_types=(
            ("small_hotel", "Small hotel"),
            ("clinic", "Clinic"),
            ("office", "Office"),
            ("warehouse", "Warehouse"),
            ("school", "School"),
            ("retail_outlet", "Retail outlet"),
            ("central_kitchen", "Central kitchen"),
        ),
        reports=("HVAC drift events", "After-hours runtime", "Run-hours maintenance", "Ticket history"),
    ),
}


def profile_choices():
    return [(profile.key, profile.name) for profile in PROFILES.values()]


def get_profile(key):
    return PROFILES.get(key or GENERAL_IOT, PROFILES[GENERAL_IOT])


def get_site_profile(site):
    return get_profile(getattr(site, "solution_profile", GENERAL_IOT))


def profile_for_request(request):
    site = None
    site_id = request.session.get("onboarding_site_id") if hasattr(request, "session") else None
    if site_id:
        from apps.devices.models import Site

        site = Site.objects.filter(id=site_id, team=request.team).first()
    if site:
        return get_site_profile(site)
    return get_profile(request.session.get("solution_profile") if hasattr(request, "session") else GENERAL_IOT)


def template_profile_score(template, profile):
    if not isinstance(profile, SolutionProfile):
        profile = get_profile(profile)

    score = 0
    if template.device_type in profile.recommended_device_types:
        score += 60
    if template.category in profile.recommended_categories:
        score += 40
    if profile.key == GENERAL_IOT:
        return score
    if getattr(template, "is_verified", False):
        score += 10
    register_map = template.register_map if isinstance(template.register_map, dict) else {}
    for index, key in enumerate(profile.key_priority):
        if key in register_map:
            score += max(1, 25 - index)
    return score


def rank_templates_for_profile(templates, profile):
    profile = get_profile(profile.key if isinstance(profile, SolutionProfile) else profile)
    return sorted(
        list(templates),
        key=lambda template: (
            -template_profile_score(template, profile),
            not getattr(template, "is_verified", False),
            -getattr(template, "usage_count", 0),
            template.name.lower(),
        ),
    )


def recommended_alerts_for_device(device, profile=None):
    from .datapoint_maps import effective_register_map

    profile = get_site_profile(device.site) if profile is None else get_profile(profile)
    register_map = effective_register_map(device)
    presets = []
    for preset in profile.alert_presets:
        if preset.key in register_map:
            presets.append(preset)
    return presets


def profile_key_order(site):
    return {key: index for index, key in enumerate(get_site_profile(site).key_priority)}
