"""
reliability.py — tracks consecutive playback failures per file and
quarantines a file once it's failed too many times in a row, instead of
letting the player loop on a permanently broken file forever.

Persisted alongside stats.py's data style (small JSON, atomic write) so a
file that failed last session doesn't get a clean slate just because the
process restarted.
"""

import json
import os
import shutil
import time

FAILURES_PATH = os.path.expanduser("~/.mishmann_failures.json")
QUARANTINE_DIRNAME = "_quarantined"
MAX_CONSECUTIVE_FAILURES = 3

_data = {}
_quarantined_this_session = []  # diagnostics-screen-only; not persisted


def _load():
    global _data
    if os.path.exists(FAILURES_PATH):
        try:
            with open(FAILURES_PATH) as f:
                _data = json.load(f)
                return
        except Exception:
            pass
    _data = {}


_load()


def _save():
    try:
        tmp = FAILURES_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_data, f)
        os.replace(tmp, FAILURES_PATH)
    except Exception:
        pass  # never let a stats-write failure take down playback


def record_failure(file_path):
    """Increments the consecutive-failure count for a file. Returns the new
    count."""
    count = _data.get(file_path, 0) + 1
    _data[file_path] = count
    _save()
    return count


def record_success(file_path):
    """A file that loaded fine resets its failure streak -- a one-off glitch
    (e.g. a brief Bluetooth dropout) shouldn't count toward quarantine the
    same way genuinely repeated failures should."""
    if file_path in _data:
        del _data[file_path]
        _save()


def get_failure_count(file_path):
    return _data.get(file_path, 0)


def should_quarantine(file_path):
    return _data.get(file_path, 0) >= MAX_CONSECUTIVE_FAILURES


def quarantine_file(file_path, log=print):
    """
    Moves a repeatedly-failing file out of the library into a
    _quarantined/ subfolder next to it, so scan_music_folder() naturally
    stops seeing it (no special-case filtering needed elsewhere) while
    still keeping the file around for inspection rather than deleting it
    outright. Returns the new path, or None if the move failed.
    """
    try:
        folder = os.path.dirname(file_path)
        quarantine_dir = os.path.join(folder, QUARANTINE_DIRNAME)
        os.makedirs(quarantine_dir, exist_ok=True)
        dest = os.path.join(quarantine_dir, os.path.basename(file_path))
        if os.path.exists(dest):
            stem, ext = os.path.splitext(os.path.basename(file_path))
            dest = os.path.join(quarantine_dir, f"{stem}_{int(time.time())}{ext}")
        shutil.move(file_path, dest)
        log("RELIABILITY", f"quarantined after {get_failure_count(file_path)} "
                            f"consecutive failures: {file_path} -> {dest}")
        _quarantined_this_session.append(file_path)
        if file_path in _data:
            del _data[file_path]
            _save()
        return dest
    except Exception as e:
        log("RELIABILITY", f"failed to quarantine {file_path}: {e}")
        return None


def get_all_failure_counts():
    """For a diagnostics/history view: {file_path: count} for everything
    with at least one recorded failure right now."""
    return dict(_data)
