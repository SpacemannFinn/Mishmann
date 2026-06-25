"""
boot_history.py — persists boot-check results across runs, so the hidden
diagnostics menu can show "did this ever fail before" rather than only
the current session's checks.

Same JSON-on-disk pattern as stats.py/reliability.py: small, atomic writes,
never lets a write failure take down boot itself.
"""

import json
import os
import time

HISTORY_PATH = os.path.expanduser("~/.mishmann_boot_history.json")
MAX_ENTRIES = 30  # keep a reasonable trailing window, not unbounded growth


def _load():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def record_boot(results_with_messages, fatal=None):
    """
    results_with_messages: list of (label, ok, msg) tuples, in the same
    order CHECKS appears in boot.py.
    fatal: None, or (label, msg) for whichever check caused a hard stop.
    """
    history = _load()
    entry = {
        "timestamp": time.time(),
        "checks": [{"label": label, "ok": ok, "msg": msg} for label, ok, msg in results_with_messages],
        "fatal": {"label": fatal[0], "msg": fatal[1]} if fatal else None,
    }
    history.append(entry)
    history = history[-MAX_ENTRIES:]
    try:
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f)
        os.replace(tmp, HISTORY_PATH)
    except Exception:
        pass  # never let a history-write failure block boot


def get_history():
    """Most recent boot first."""
    return list(reversed(_load()))


def get_last_fatal():
    """The most recent fatal failure, if any, or None. Useful for a quick
    'has this ever actually failed to boot' check without scanning the
    whole history by hand."""
    for entry in get_history():
        if entry.get("fatal"):
            return entry
    return None


def get_failure_frequency():
    """{check_label: fail_count} across all recorded history -- surfaces a
    check that's flaky/intermittently failing even when it's not currently
    fatal, which a single most-recent-boot view wouldn't show."""
    counts = {}
    for entry in _load():
        for check in entry["checks"]:
            if not check["ok"]:
                counts[check["label"]] = counts.get(check["label"], 0) + 1
    return counts
