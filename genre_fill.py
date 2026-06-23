"""
genre_fill.py — fills in missing genre tags via the MusicBrainz API.

Only ever touches tracks whose genre is genuinely unknown (never overwrites
an existing tag, however messy). Runs as a background thread, gated on real
WiFi connectivity (checked via upload_server.wifi_get_status), respecting
MusicBrainz's "no more than 1 request/second" usage policy with a proper
identifying User-Agent. Results are written back into the actual file tag
via mutagen -- not just the in-memory dict -- so the lookup is a one-time
cost per track across the life of the library, and into the in-memory dict
immediately so the UI can reflect it without a rescan.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3NoHeaderError
except ImportError:
    MutagenFile = None
    ID3NoHeaderError = Exception

UNKNOWN_GENRE = "Unknown Genre"

# MusicBrainz's usage policy requires a real, identifying User-Agent and a
# rate limit no faster than 1 request/second for unauthenticated use. This
# app/contact pair is a placeholder -- MusicBrainz asks that it point to
# something real (project name + contact), which is worth filling in with
# an actual email/URL before this runs against the live API long-term.
USER_AGENT = "MishmannPlayer/1.0 (genre-fill; contact: set-a-real-contact@example.com)"
MB_BASE_URL = "https://musicbrainz.org/ws/2/recording/"
MIN_REQUEST_INTERVAL_S = 1.05  # a hair over 1.0 as margin against the limit
REQUEST_TIMEOUT_S = 8.0


def _mb_request(url):
    """Single HTTP GET against the MusicBrainz API. Returns parsed JSON
    dict, or None on any failure. Never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def lookup_genre(artist, title, log=None):
    """
    Look up a single recording's genre via MusicBrainz. Tries genres first,
    falls back to general tags (genres are themselves a kind of tag, and
    plenty of recordings have tags but no entries specifically promoted to
    "genre"). Returns a genre string, or None if nothing usable was found.
    Never raises -- a failed lookup just means this track stays unknown
    until a future run.
    """
    if not artist or not title:
        return None

    query = f'artist:"{artist}" AND recording:"{title}"'
    url = (MB_BASE_URL + "?query=" + urllib.parse.quote(query) +
           "&fmt=json&limit=1&inc=genres+tags")
    data = _mb_request(url)
    if not data:
        return None

    recordings = data.get("recordings") or []
    if not recordings:
        return None
    rec = recordings[0]

    genres = rec.get("genres") or []
    if genres:
        best = max(genres, key=lambda g: int(g.get("count", 0) or 0))
        name = best.get("name")
        if name:
            return name.title()

    tags = rec.get("tags") or []
    if tags:
        best = max(tags, key=lambda t: int(t.get("count", 0) or 0))
        name = best.get("name")
        if name:
            return name.title()

    if log:
        log("GENRE", f"no genre/tags found for {artist!r} - {title!r}")
    return None


def write_genre_tag(file_path, genre, log=None):
    """
    Writes the genre into the file's actual tag via mutagen (not just the
    in-memory dict), so this lookup is a one-time cost per track. Handles
    the case of a file with no existing tag header at all (common on
    freshly-ripped/converted files) by adding one rather than assuming it's
    already there. Returns True on success, False on any failure -- never
    raises, since a tag-write failure shouldn't take down the worker thread
    or the player.
    """
    if MutagenFile is None:
        return False
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        audio["genre"] = genre
        audio.save()
        return True
    except ID3NoHeaderError:
        # Some formats raise this distinctly rather than just leaving
        # .tags as None -- handled the same way, just a different path in.
        try:
            audio = MutagenFile(file_path, easy=True)
            audio.add_tags()
            audio["genre"] = genre
            audio.save()
            return True
        except Exception as e:
            if log:
                log("GENRE", f"write failed for {file_path!r}: {e}")
            return False
    except Exception as e:
        if log:
            log("GENRE", f"write failed for {file_path!r}: {e}")
        return False


class GenreFillWorker:
    """
    Background thread that walks a list of track dicts, looks up genre for
    any still showing UNKNOWN_GENRE, writes it to the file and updates the
    dict in place, and stops cleanly if WiFi drops mid-run or the player
    asks it to stop. Designed to be started once WiFi is confirmed
    connected and left to run at its own (rate-limited, deliberately slow)
    pace in the background -- never blocks playback or the UI thread.
    """

    def __init__(self, get_wifi_status_fn, log_fn=print):
        self.get_wifi_status = get_wifi_status_fn
        self.log = log_fn
        self._thread = None
        self._stop_event = threading.Event()
        self.tracks_checked = 0
        self.tracks_filled = 0
        self.is_running = False

    def start(self, tracks):
        if self._thread is not None and self._thread.is_alive():
            self.log("GENRE", "worker already running, not starting a second one")
            return
        self._stop_event.clear()
        self.tracks_checked = 0
        self.tracks_filled = 0
        self._thread = threading.Thread(
            target=self._run, args=(tracks,), daemon=True, name="genre-fill")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self, tracks):
        self.is_running = True
        self.log("GENRE", f"worker starting, {len(tracks)} track(s) to check")
        last_request_at = 0.0
        try:
            for track in tracks:
                if self._stop_event.is_set():
                    self.log("GENRE", "worker stopped (requested)")
                    return

                if (track.get("genre") or UNKNOWN_GENRE) != UNKNOWN_GENRE:
                    continue  # never touch a track that already has a real genre

                status = self.get_wifi_status()
                if not status.get("connected"):
                    self.log("GENRE", "WiFi disconnected, pausing worker")
                    return

                # Respect the rate limit regardless of how long the lookup
                # itself took -- measure from the start of the previous
                # request, not just sleep a fixed amount unconditionally.
                wait = MIN_REQUEST_INTERVAL_S - (time.monotonic() - last_request_at)
                if wait > 0:
                    time.sleep(wait)
                last_request_at = time.monotonic()

                self.tracks_checked += 1
                artist = track.get("artist")
                title = track.get("title")
                genre = lookup_genre(artist, title, log=self.log)

                if genre:
                    ok = write_genre_tag(track["file_path"], genre, log=self.log)
                    if ok:
                        track["genre"] = genre
                        self.tracks_filled += 1
                        self.log("GENRE", f"filled: {artist!r} - {title!r} -> {genre!r}")
                    else:
                        self.log("GENRE", f"lookup OK but write failed: {artist!r} - {title!r}")
                else:
                    self.log("GENRE", f"no match: {artist!r} - {title!r}")
        finally:
            self.is_running = False
            self.log("GENRE", f"worker finished: checked={self.tracks_checked} "
                               f"filled={self.tracks_filled}")
