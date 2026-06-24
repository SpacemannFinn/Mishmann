import json
import re
import threading
import time
import urllib.parse
import urllib.request
from mutagen import File as MutagenFile

MB_BASE_URL = "https://musicbrainz.org/ws/2/"
MIN_REQUEST_INTERVAL_S = 1.15  # Strict safety margin above MusicBrainz's 1.0s limit
USER_AGENT = "MishmannPlayer/1.0 (contact: 3123yes@gmail.com)"

# Centralized pacing controls
_network_lock = threading.Lock()
_last_request_time = 0.0

def _mb_get(url):
    """Centralized, rate-limited network wrapper. Employs a thread lock to 
    guarantee that no two requests fire back-to-back within the safety window."""
    global _last_request_time
    with _network_lock:
        now = time.monotonic()
        delta = now - _last_request_time
        if delta < MIN_REQUEST_INTERVAL_S:
            time.sleep(MIN_REQUEST_INTERVAL_S - delta)
        _last_request_time = time.monotonic()

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))

def clean_metadata_string(s):
    if not s: return ""
    s = str(s)
    # Strip explicit features
    s = re.sub(r'\s+feat\.?.*$', '', s, flags=re.IGNORECASE)
    # Strip nested parens/brackets entirely to clear out noise (e.g., "(Remastered 2011)")
    s = re.sub(r'[\(\[][^\]\)]*[\)\]]', ' ', s)
    # Wipe remaining special punctuation elements
    s = re.sub(r'[\+\-\!\(\)\{\}\[\]\^"~\*\?\:\\\/\|\&\;\.\,]', " ", s)
    return " ".join(s.split())

def get_file_metadata(file_path):
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None: return None
        return {
            "artist": audio.get("artist", [None])[0],
            "title": audio.get("title", [None])[0],
            "album": audio.get("album", [None])[0]
        }
    except Exception:
        return None

def _extract_highest_genre(entity_dict):
    """Parses MusicBrainz structures for 'genres' or 'tags' lists, returning
    the item name boasting the highest weight count."""
    g_list = entity_dict.get("genres") or entity_dict.get("tags") or []
    if g_list:
        return max(g_list, key=lambda x: int(x.get("count", 0) or 0))["name"]
    return None

def lookup_genre_by_metadata(metadata):
    artist_name = clean_metadata_string(metadata.get("artist"))
    title = clean_metadata_string(metadata.get("title"))
    album = clean_metadata_string(metadata.get("album"))
    
    if not artist_name or not title: 
        return None

    # Track structural state for reuse optimizations across fallback blocks
    artist_id = None

    # ==========================================
    # STEP 1: Recording Level Search
    # ==========================================
    rec_query = f'artist:"{artist_name}" AND recording:"{title}"'
    if album: rec_query += f' AND release:"{album}"'
    
    rec_url = f"{MB_BASE_URL}recording?query={urllib.parse.quote(rec_query)}&fmt=json&limit=1&inc=genres+tags+artist-credits"
    
    try:
        rec_data = _mb_get(rec_url)
        recordings = rec_data.get("recordings", [])
        if recordings:
            rec = recordings[0]
            
            # Cache the artist ID if available for later steps
            artist_credits = rec.get("artist-credit", [])
            if artist_credits and "artist" in artist_credits[0]:
                artist_id = artist_credits[0]["artist"].get("id")

            genre = _extract_highest_genre(rec)
            if genre: 
                return genre
    except Exception:
        pass

    # ==========================================
    # STEP 2: Album Level Search (Release-Group tags)
    # ==========================================
    if album:
        rg_query = f'releasegroup:"{album}" AND artist:"{artist_name}"'
        rg_url = f"{MB_BASE_URL}release-group?query={urllib.parse.quote(rg_query)}&fmt=json&limit=1&inc=genres+tags"
        try:
            rg_data = _mb_get(rg_url)
            release_groups = rg_data.get("release-groups", [])
            if release_groups:
                genre = _extract_highest_genre(release_groups[0])
                if genre: 
                    return genre
        except Exception:
            pass

    # ==========================================
    # STEP 3: Artist Level Fallback Lookup
    # ==========================================
    try:
        # If Step 1 gave us an exact artist ID, execute a direct lookup to save a search request
        if artist_id:
            art_url = f"{MB_BASE_URL}artist/{artist_id}?fmt=json&inc=genres+tags"
            art_data = _mb_get(art_url)
            genre = _extract_highest_genre(art_data)
            if genre: 
                return genre
        else:
            art_query = f'artist:"{artist_name}"'
            art_url = f"{MB_BASE_URL}artist?query={urllib.parse.quote(art_query)}&fmt=json&limit=1&inc=genres+tags"
            art_data = _mb_get(art_url)
            artists = art_data.get("artists", [])
            if artists:
                genre = _extract_highest_genre(artists[0])
                if genre: 
                    return genre
    except Exception:
        pass
        
    return None

def write_genre_tag(file_path, genre, log=print):
    """Writes the genre string safely to native tags without breaking embedded art."""
    try:
        if file_path.lower().endswith(".mp3"):
            from mutagen.easyid3 import EasyID3
            audio_tags = EasyID3(file_path)
            audio_tags["genre"] = genre
            audio_tags.save()
        else:
            audio = MutagenFile(file_path)
            if audio is None: return False
            audio["genre"] = genre
            audio.save()
        return True
    except Exception as e:
        log("GENRE", f"Failed to write tag to {file_path}: {e}")
        return False

class GenreFillWorker:
    def __init__(self, get_wifi_status_fn, log_fn=print):
        self.get_wifi_status = get_wifi_status_fn
        self.log = log_fn
        self._thread = None
        self._stop_event = threading.Event()
        self.is_running = False
        self.tracks_checked = 0
        self.tracks_filled = 0

    def start(self, tracks, force_full=False):
        """Starts worker. force_full=False targets untagged tracks only."""
        if self.is_running:
            return
        self.tracks_checked = 0
        self.tracks_filled = 0
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(tracks, force_full), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread: self._thread.join(timeout=3.0)
        self.is_running = False

    def _run(self, tracks, force_full):
        try:
            for track in tracks:
                if self._stop_event.is_set(): break
                if not self.get_wifi_status().get("connected"): break
                
                current_genre = track.get("genre", "")
                if not force_full and current_genre and current_genre.lower() != "unknown genre":
                    continue
                
                self.tracks_checked += 1
                meta = get_file_metadata(track["file_path"])
                if meta:
                    genre = lookup_genre_by_metadata(meta)
                    if genre:
                        if write_genre_tag(track["file_path"], genre, self.log):
                            track["genre"] = genre
                            self.tracks_filled += 1
                            self.log("GENRE", f"Filled: {track['file_path']} -> {genre}")
        finally:
            self.is_running = False