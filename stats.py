"""
stats.py — persistent listening stats: play counts, skips, recently played,
favorites, and daily streaks.

Keyed by file_path (stable across rescans, unlike list index). Stored as
plain JSON so it's human-inspectable and trivially backed up. Writes are
debounced (not on every single play event) since this is hit from the
playback loop and shouldn't do disk I/O on every track transition under
heavy skip-mashing.
"""

import json
import os
import threading
import time
from datetime import date

STATS_PATH = os.path.expanduser("~/.mishmann_stats.json")
_SAVE_DEBOUNCE_S = 2.0

_lock = threading.Lock()
_dirty = False
_last_save = 0.0

_DEFAULT = {
    "tracks": {},       # file_path -> {"plays": int, "skips": int, "favorite": bool}
    "recent": [],        # list of file_path, most recent first, capped
    "play_days": [],     # list of "YYYY-MM-DD" strings, one per day with >=1 play
}
RECENT_CAP = 50


def _load():
    if not os.path.exists(STATS_PATH):
        return json.loads(json.dumps(_DEFAULT))  # deep copy
    try:
        with open(STATS_PATH) as f:
            data = json.load(f)
        for key, default in _DEFAULT.items():
            data.setdefault(key, json.loads(json.dumps(default)))
        return data
    except Exception:
        return json.loads(json.dumps(_DEFAULT))


_data = _load()


def _track_entry(file_path):
    return _data["tracks"].setdefault(file_path, {"plays": 0, "skips": 0, "favorite": False})


def _mark_dirty():
    global _dirty
    _dirty = True
    _maybe_save()


def _maybe_save(force=False):
    global _dirty, _last_save
    with _lock:
        now = time.monotonic()
        if not _dirty and not force:
            return
        if not force and (now - _last_save) < _SAVE_DEBOUNCE_S:
            return
        try:
            tmp_path = STATS_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(_data, f)
            os.replace(tmp_path, STATS_PATH)  # atomic on POSIX, avoids a half-written file
            _dirty = False
            _last_save = now
        except Exception:
            pass  # stats are nice-to-have; never let a write failure crash playback


def flush():
    """Force an immediate save, bypassing the debounce. Call on shutdown."""
    _maybe_save(force=True)


def record_play(file_path):
    entry = _track_entry(file_path)
    entry["plays"] += 1
    _data["recent"] = [file_path] + [p for p in _data["recent"] if p != file_path]
    _data["recent"] = _data["recent"][:RECENT_CAP]
    today = date.today().isoformat()
    if not _data["play_days"] or _data["play_days"][-1] != today:
        _data["play_days"].append(today)
    _mark_dirty()


def record_skip(file_path):
    entry = _track_entry(file_path)
    entry["skips"] += 1
    _mark_dirty()


def toggle_favorite(file_path):
    entry = _track_entry(file_path)
    entry["favorite"] = not entry["favorite"]
    _mark_dirty()
    return entry["favorite"]


def is_favorite(file_path):
    return _data["tracks"].get(file_path, {}).get("favorite", False)


def get_play_count(file_path):
    return _data["tracks"].get(file_path, {}).get("plays", 0)


def get_skip_count(file_path):
    return _data["tracks"].get(file_path, {}).get("skips", 0)


def get_skip_rate(file_path):
    """plays+skips as total exposures; returns 0.0-1.0, or 0.0 if never seen."""
    entry = _data["tracks"].get(file_path)
    if not entry:
        return 0.0
    total = entry["plays"] + entry["skips"]
    return (entry["skips"] / total) if total else 0.0


def get_recent(limit=10):
    return list(_data["recent"][:limit])


def get_favorites():
    return [path for path, e in _data["tracks"].items() if e.get("favorite")]


def get_most_played(limit=10):
    items = sorted(_data["tracks"].items(), key=lambda kv: -kv[1]["plays"])
    return [(path, e["plays"]) for path, e in items[:limit] if e["plays"] > 0]


def get_total_plays():
    return sum(e["plays"] for e in _data["tracks"].values())


def get_current_streak():
    """
    Consecutive days (ending today or yesterday -- a streak isn't broken
    until a full day is skipped) with at least one play. Returns 0 if the
    most recent play day is older than yesterday.
    """
    days = _data["play_days"]
    if not days:
        return 0
    play_dates = sorted({date.fromisoformat(d) for d in days}, reverse=True)
    today = date.today()
    if (today - play_dates[0]).days > 1:
        return 0  # streak already broken -- last play was 2+ days ago

    streak = 1
    for i in range(1, len(play_dates)):
        if (play_dates[i - 1] - play_dates[i]).days == 1:
            streak += 1
        else:
            break
    return streak
