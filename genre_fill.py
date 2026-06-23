import json
import re
import threading
import time
import urllib.parse
import urllib.request
from mutagen import File as MutagenFile

MB_BASE_URL = "https://musicbrainz.org/ws/2/"
MIN_REQUEST_INTERVAL_S = 1.05
USER_AGENT = "MishmannPlayer/1.0 (contact: 3123yes@gmail.com)"

def clean_metadata_string(s):
    if not s: return ""
    s = re.sub(r'\s+feat\.?.*$', '', str(s), flags=re.IGNORECASE)
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

def lookup_genre_by_metadata(metadata):
    artist = clean_metadata_string(metadata.get("artist"))
    title = clean_metadata_string(metadata.get("title"))
    album = clean_metadata_string(metadata.get("album"))
    if not artist or not title: return None

    query = f'artist:"{artist}" AND recording:"{title}"'
    if album: query += f' AND release:"{album}"'
    
    url = f"{MB_BASE_URL}recording?query={urllib.parse.quote(query)}&fmt=json&limit=1&inc=genres+tags"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            recordings = data.get("recordings", [])
            if recordings:
                rec = recordings[0]
                # Try genres, then tags
                g = rec.get("genres") or rec.get("tags")
                if g: return max(g, key=lambda x: int(x.get("count", 0) or 0))["name"]
    except: pass
    return None

# Add this function to genre_fill.py
def write_genre_tag(file_path, genre, log=print):
    """Writes the genre string to the file's metadata."""
    try:
        # Load the file in 'easy' mode for simple tag access
        audio = MutagenFile(file_path, easy=True)
        if audio is None:
            return False
        
        # Update the genre tag
        audio["genre"] = genre
        
        # Save the changes to the file
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
        # Add these two lines:
        self.tracks_checked = 0
        self.tracks_filled = 0

    def start(self, tracks):
        # Reset counters when starting a new scan
        self.tracks_checked = 0
        self.tracks_filled = 0
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(tracks,), daemon=True)
        self._thread.start()

    # ... keep stop() as is ...

def _run(self, tracks):
        try:
            for track in tracks:
                if self._stop_event.is_set(): break
                if not self.get_wifi_status().get("connected"): break
                
                meta = get_file_metadata(track["file_path"])
                if meta:
                    genre = lookup_genre_by_metadata(meta)
                    if genre:
                        # --- CALL THE WRITE FUNCTION HERE ---
                        success = write_genre_tag(track["file_path"], genre, self.log)
                        
                        if success:
                            # Update the local dictionary so the UI reflects it immediately
                            track["genre"] = genre 
                            self.log("GENRE", f"Filled: {track['file_path']} -> {genre}")
                time.sleep(MIN_REQUEST_INTERVAL_S)
        finally:
            self.is_running = False