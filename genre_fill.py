import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dotenv import load_dotenv

# Load variables from the .env file into the script's environment
load_dotenv()

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3NoHeaderError
except ImportError:
    MutagenFile = None
    ID3NoHeaderError = Exception

try:
    import acoustid
    ACOUSTID_AVAILABLE = True
except ImportError:
    ACOUSTID_AVAILABLE = False

UNKNOWN_GENRE = "Unknown Genre"

USER_AGENT = "MishmannPlayer/1.0 (genre-fill; contact: 3123yes@gmail.com)"
MB_BASE_URL = "https://musicbrainz.org/ws/2/recording/"
MIN_REQUEST_INTERVAL_S = 1.05
REQUEST_TIMEOUT_S = 8.0

# Pull the API key from the environment instead of hardcoding it
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY")

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
    """Strips common track noise, commas, and Lucene special characters."""
    if not s:
        return ""
    s = str(s)
    s = re.sub(r"\s*[\(\[][^\]\)]*?(remaster|live|edit|bonus|mix|version|feat|ft)[^\]\)]*?[\)\]]", "", s, flags=re.IGNORECASE)
    s = re.sub(r'[\+\-\!\(\)\{\}\[\]\^"~\*\?\:\\\/\|\&\;\.\,]', " ", s)
    return " ".join(s.split())

def lookup_genre_by_fingerprint(file_path, log=None):
    """Generates an audio fingerprint from the file and fetches the MusicBrainz ID."""
    if not ACOUSTID_AVAILABLE or not ACOUSTID_API_KEY:
        return None

    try:
        matches = acoustid.match(ACOUSTID_API_KEY, file_path)
        best_mbid = None
        
        # Grab the first match with a high acoustic confidence score
        for score, recording_id, title, artist in matches:
            if score > 0.75:
                best_mbid = recording_id
                break
        
        if not best_mbid:
            if log: log("GENRE", f"No strong acoustic match found for {os.path.basename(file_path)}")
            return None

        # Respect the MusicBrainz API rate limit
        time.sleep(MIN_REQUEST_INTERVAL_S)
        
        # Look up the MBID in MusicBrainz to pull genres and tags
        lookup_url = f"{MB_BASE_URL}{best_mbid}?inc=genres+tags&fmt=json"
        rec_data = _mb_request(lookup_url)
        
        if not rec_data:
            return None

        genres = rec_data.get("genres") or []
        if genres:
            best = max(genres, key=lambda g: int(g.get("count", 0) or 0))
            name = best.get("name")
            if name: return name.title()

        tags = rec_data.get("tags") or []
        if tags:
            best = max(tags, key=lambda t: int(t.get("count", 0) or 0))
            name = best.get("name")
            if name: return name.title()

        return None
    except Exception as e:
        if log: log("GENRE", f"Fingerprint error: {e}")
        return None

def lookup_genre_by_metadata(artist, title, log=None):
    """Fallback text search via MusicBrainz using a 2-step process."""
    clean_artist = clean_metadata_string(artist)
    clean_title = clean_metadata_string(title)

    if not clean_artist or not clean_title:
        return None

    query = f'artist:({clean_artist}) AND recording:({clean_title})'
    search_url = (MB_BASE_URL + "?query=" + urllib.parse.quote(query) + "&fmt=json&limit=1")
    
    search_data = _mb_request(search_url)
    if not search_data:
        return None

    recordings = search_data.get("recordings") or []
    if not recordings:
        return None

    mbid = recordings[0].get("id")
    if not mbid:
        return None

    time.sleep(MIN_REQUEST_INTERVAL_S)
    
    lookup_url = f"{MB_BASE_URL}{mbid}?inc=genres+tags&fmt=json"
    rec_data = _mb_request(lookup_url)
    
    if not rec_data:
        return None

    genres = rec_data.get("genres") or []
    if genres:
        best = max(genres, key=lambda g: int(g.get("count", 0) or 0))
        name = best.get("name")
        if name: return name.title()

    tags = rec_data.get("tags") or []
    if tags:
        best = max(tags, key=lambda t: int(t.get("count", 0) or 0))
        name = best.get("name")
        if name: return name.title()

    return None

def write_genre_tag(file_path, genre, log=None):
    if MutagenFile is None: return False
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None: return False
        if audio.tags is None: audio.add_tags()
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
            if log: log("GENRE", f"write failed for {file_path!r}: {e}")
            return False
    except Exception as e:
        if log: log("GENRE", f"write failed for {file_path!r}: {e}")
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
                genre = None

                # 1. Acoustic Fingerprinting
                if ACOUSTID_AVAILABLE and ACOUSTID_API_KEY:
                    self.log("GENRE", f"Fingerprinting audio data for: {os.path.basename(track['file_path'])}")
                    genre = lookup_genre_by_fingerprint(track["file_path"], log=self.log)

                # 2. Text/Metadata Fallback
                if not genre:
                    artist = track.get("artist")
                    title = track.get("title")
                    genre = lookup_genre_by_metadata(artist, title, log=self.log)

                if genre:
                    ok = write_genre_tag(track["file_path"], genre, log=self.log)
                    if ok:
                        track["genre"] = genre
                        self.tracks_filled += 1
                        self.log("GENRE", f"filled: {track.get('artist')} - {track.get('title')} -> {genre!r}")
                    else:
                        self.log("GENRE", f"lookup OK but write failed: {track.get('artist')} - {track.get('title')}")
                else:
                    self.log("GENRE", f"no match via fingerprint or text for: {os.path.basename(track['file_path'])}")
        finally:
            self.is_running = False
            self.log("GENRE", f"worker finished: checked={self.tracks_checked} "
                               f"filled={self.tracks_filled}")