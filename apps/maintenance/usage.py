"""Supported cumulative readings for usage-based preventive maintenance."""

_ENERGY_KEYS = {"energy_import", "import_energy", "energy_import_total"}
_HOUR_KEYS = {"run_hours", "runtime_hours", "operating_hours", "total_run_hours"}
_NUMERIC_TYPES = {"uint16", "int16", "uint32", "int32", "float32", "uint64", "int64", "float64"}


def is_usage_counter(key, config):
    if not isinstance(config, dict) or config.get("writable"):
        return False
    if (config.get("data_type") or config.get("type")) not in _NUMERIC_TYPES:
        return False
    unit = str(config.get("unit") or "").strip().lower()
    if key in _ENERGY_KEYS:
        return unit in {"wh", "kwh", "mwh"}
    if key in _HOUR_KEYS:
        return unit in {"h", "hr", "hour", "hours"}
    return False
