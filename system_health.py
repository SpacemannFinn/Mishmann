"""
system_health.py — CPU temperature, storage usage, and uptime.

Each function reads a real Linux source directly (no extra dependencies
like psutil) and returns a safe fallback on any failure rather than
raising -- these are Settings-screen nice-to-haves, never worth crashing
over.
"""

import os
import shutil
import time

THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"

# Rough, commonly-used thresholds for ARM SBCs (RK3566/3568-class). Real
# throttle points vary by board/heatsink; treat WARM/HOT as advisory, not
# a guarantee the SoC is actually about to throttle.
TEMP_WARM_C = 70.0
TEMP_HOT_C = 80.0


def get_cpu_temp_c():
    """CPU temperature in Celsius, or None if unreadable."""
    try:
        with open(THERMAL_ZONE_PATH) as f:
            raw = int(f.read().strip())
        return raw / 1000.0  # kernel reports millidegrees
    except Exception:
        return None


def get_thermal_status():
    """('OK'|'WARM'|'HOT'|'UNKNOWN', temp_c_or_None)."""
    temp = get_cpu_temp_c()
    if temp is None:
        return "UNKNOWN", None
    if temp >= TEMP_HOT_C:
        return "HOT", temp
    if temp >= TEMP_WARM_C:
        return "WARM", temp
    return "OK", temp


def get_disk_usage(path="/"):
    """
    (used_pct, free_gb, total_gb) for the filesystem containing `path`, or
    (None, None, None) if it can't be read. Resolves to the real mount
    point even if `path` itself doesn't exist yet (e.g. MUSIC_ROOT before
    its first scan) by walking up to an existing ancestor directory.
    """
    try:
        check_path = path
        while not os.path.exists(check_path):
            parent = os.path.dirname(check_path.rstrip("/")) or "/"
            if parent == check_path:
                break
            check_path = parent
        usage = shutil.disk_usage(check_path)
        used_pct = round(100 * usage.used / usage.total) if usage.total else None
        free_gb = round(usage.free / (1024 ** 3), 1)
        total_gb = round(usage.total / (1024 ** 3), 1)
        return used_pct, free_gb, total_gb
    except Exception:
        return None, None, None


def get_uptime_s():
    """System uptime in seconds (since boot), or None if unreadable."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def format_uptime(seconds):
    """123456.7 -> '1d 10h 17m'. Returns 'Unknown' for None."""
    if seconds is None:
        return "Unknown"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours or days: parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)
