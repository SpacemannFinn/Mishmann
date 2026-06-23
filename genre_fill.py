import json
import os
import re
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

USER_AGENT = "MishmannPlayer/1.0 (genre-fill; contact: set-a-real-contact@example.com)"
MB_BASE_URL = "https://musicbrainz.org/ws/2/recording/"
MIN_REQUEST_INTERVAL_S = 1.05
REQUEST_TIMEOUT_S = 8.0


def _mb_request(url):
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


def clean_metadata_string(s):
    """Strips common track noise, commas, and Lucene special characters that ruin search match-rates."""
    if not s:
        return ""
    s = str(s)
    # Strip common parenthetical noise (e.g., "(Remastered 2020)", "[Live Mix]", etc.)[cite: 3]
    s = re.sub(r"\s*[\(\[][^\]\)]*?(remaster|live|edit|bonus|mix|version|feat|ft)[^\]\)]*?[\)\]]", "", s, flags=re.IGNORECASE)
    # Added ',' to the character strip block to prevent Lucene syntax breakage[cite: 3]
    s = re.sub(r'[\+\-\!\(\)\{\}\[\]\^"~\*\?\:\\\/\|\&\;\.\,]', " ", s)
    return " ".join(s.split())


def lookup_genre(artist, title, log=None):
    """
    Look up a single recording's genre via MusicBrainz. Cleans metadata noise
    and uses precise phrase-quoted parameters.[cite: 3]
    """
    clean_artist = clean_metadata_string(artist)
    clean_title = clean_metadata_string(title)

    if not clean_artist or not clean_title:
        return None

    # Reverting to explicit phrase quotes around stripped parameters for reliable execution
    query = f'artist:"{clean_artist}" AND recording:"{clean_title}"'
    url = (MB_BASE_URL + "?query=" + urllib.parse.quote(query) +
           "&fmt=json&limit=3&inc=genres+tags")[cite: 3]
    
    data = _mb_request(url)[cite: 3]
    if not data:
        return None

    recordings = data.get("recordings") or [][cite: 3]
    if not recordings:
        return None

    # Check top 3 search entries for usable tags[cite: 3]
    for rec in recordings:
        genres = rec.get("genres") or [][cite: 3]
        if genres:
            best = max(genres, key=lambda g: int(g.get("count", 0) or 0))[cite: 3]
            name = best.get("name")[cite: 3]
            if name:
                return name.title()[cite: 3]

        tags = rec.get("tags") or [][cite: 3]
        if tags:
            best = max(tags, key=lambda t: int(t.get("count", 0) or 0))[cite: 3]
            name = best.get("name")[cite: 3]
            if name:
                return name.title()[cite: 3]

    if log:
        log("GENRE", f"no genre/tags found for cleansed: {clean_artist!r} - {clean_title!r}")[cite: 3]
    return None


def write_genre_tag(file_path, genre, log=None):
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
                    continue

                status = self.get_wifi_status()
                if not status.get("connected"):
                    self.log("GENRE", "WiFi disconnected, pausing worker")
                    return

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