"""
upload_server.py — toggleable web upload server for adding music over WiFi.

Off by default. When turned on from Settings, runs a small Flask app in a
background thread, gated by a randomly generated password shown on-screen.
Accepts one or more audio files via a browser upload form and streams them
into MUSIC_ROOT, then signals the player to rescan its library.

Deliberately NOT exposed to the internet — this is a LAN-only convenience
tool, using Flask's built-in dev server, which is appropriate for personal,
trusted-network use but not for public-facing deployment.
"""

import os
import re
import secrets
import string
import threading
import time

ALLOWED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".wav"}
MAX_UPLOAD_SIZE_BYTES = 500 * 1024 * 1024  # 500MB per request — generous for a whole album


class UploadServerUnavailable(Exception):
    """Raised when Flask/werkzeug aren't installed, or the port can't be
    bound. Mirrors BluetoothUnavailable's pattern: an optional subsystem
    failing to start should never take down playback."""


def _generate_password(length=6):
    """Short, easy-to-type-on-a-phone password — digits only, since it'll be
    typed on a phone keyboard, not security-critical (LAN-only, rotates every
    time the server is turned on)."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def _safe_filename(name):
    """Strip path components and anything that isn't a sane filename
    character, so an uploaded filename can never escape MUSIC_ROOT or
    clobber something unexpected via '../' tricks or odd characters."""
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9 ._\-\(\)\[\]&,']", "_", name)
    name = name.strip().lstrip(".")  # no leading dots (hidden files) or blank names
    return name or f"upload_{int(time.time())}"


UPLOAD_PAGE_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Add Music</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: #18181e; color: #eee;
            margin: 0; padding: 24px; }}
    h1 {{ font-size: 20px; }}
    .card {{ background: #24242c; border-radius: 12px; padding: 20px; margin-top: 16px; }}
    input[type=password], input[type=file] {{ width: 100%; padding: 10px; margin-top: 8px;
            border-radius: 8px; border: 1px solid #444; background: #18181e; color: #eee;
            box-sizing: border-box; }}
    button {{ width: 100%; padding: 12px; margin-top: 16px; border-radius: 8px; border: none;
            background: #e86a26; color: white; font-size: 16px; font-weight: 600; }}
    .msg {{ margin-top: 16px; padding: 12px; border-radius: 8px; }}
    .msg.ok {{ background: #1e3a24; color: #8fd99f; }}
    .msg.err {{ background: #3a1e1e; color: #d98f8f; }}
    .filelist {{ font-size: 13px; color: #999; margin-top: 8px; }}
  </style>
</head>
<body>
  <h1>🎵 Add Music to Mishmann</h1>
  <div class="card">
    <form method="post" action="/upload" enctype="multipart/form-data">
      <label>Password</label>
      <input type="password" name="password" required autofocus>
      <label style="display:block; margin-top:14px;">Files (MP3, FLAC, OGG, M4A, AAC, WAV)</label>
      <input type="file" name="files" multiple accept=".mp3,.flac,.ogg,.m4a,.aac,.wav" required>
      <button type="submit">Upload</button>
    </form>
    {message_html}
  </div>
</body>
</html>
"""


class MusicUploadServer:
    """
    Owns a Flask app + background thread. start()/stop() are idempotent and
    safe to call from the Settings screen's button loop. on_library_changed
    is called (from the Flask request thread) after a successful upload —
    the player's main loop should poll is_rescan_pending()/consume it between
    tracks rather than the server touching the live track list directly,
    since that list is owned and read by a different thread.
    """

    def __init__(self, music_root, port=8080, log_fn=print):
        self.music_root = music_root
        self.port = port
        self.log = log_fn
        self.password = None
        self._server = None
        self._thread = None
        self._rescan_pending = threading.Event()
        self._lock = threading.Lock()
        self.last_upload_count = 0
        self.last_upload_names = []

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------
    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return  # already running, idempotent

            try:
                app = self._build_app()
                from werkzeug.serving import make_server
                server = make_server("0.0.0.0", self.port, app, threaded=True)
            except ModuleNotFoundError as e:
                self.log("UPLOAD", f"WARNING: can't start, missing dependency: {e}")
                raise UploadServerUnavailable(
                    f"Flask isn't installed. On the device, run: "
                    f"sudo apt install python3-flask  (original error: {e})"
                )
            except OSError as e:
                # Most commonly "address already in use" — e.g. a previous
                # process never released the port, or something else is
                # listening on it already.
                self.log("UPLOAD", f"WARNING: can't bind port {self.port}: {e}")
                raise UploadServerUnavailable(
                    f"Couldn't bind port {self.port} ({e}). "
                    f"Is something else already using it?"
                )

            self.password = _generate_password()
            self._server = server
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="upload-server")
            self._thread.start()
            self.log("UPLOAD", f"server started on port {self.port}, password={self.password}")

    def stop(self):
        with self._lock:
            if self._server is not None:
                self._server.shutdown()
                self._server = None
            if self._thread is not None:
                self._thread.join(timeout=3.0)
                self._thread = None
            self.log("UPLOAD", "server stopped")

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def get_url_hint(self):
        """Best-effort LAN IP for display, falling back to a generic hint if
        it can't be determined (multiple interfaces, no network, etc)."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return f"http://{ip}:{self.port}"
        except Exception:
            return f"http://<this-device's-ip>:{self.port}"

    # ---------------------------------------------------------------
    # Rescan signaling (cross-thread; player polls this between tracks)
    # ---------------------------------------------------------------
    def is_rescan_pending(self):
        return self._rescan_pending.is_set()

    def consume_rescan_flag(self):
        self._rescan_pending.clear()

    # ---------------------------------------------------------------
    # Flask app
    # ---------------------------------------------------------------
    def _build_app(self):
        from flask import Flask, request, abort

        app = Flask(__name__)
        app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE_BYTES

        @app.route("/", methods=["GET"])
        def index():
            return UPLOAD_PAGE_HTML.format(message_html="")

        @app.route("/upload", methods=["POST"])
        def upload():
            submitted_password = request.form.get("password", "")
            if not self.password or submitted_password != self.password:
                self.log("UPLOAD", "rejected upload: wrong password")
                return UPLOAD_PAGE_HTML.format(
                    message_html='<div class="msg err">Wrong password.</div>'), 403

            files = request.files.getlist("files")
            if not files:
                return UPLOAD_PAGE_HTML.format(
                    message_html='<div class="msg err">No files received.</div>'), 400

            saved, rejected = [], []
            os.makedirs(self.music_root, exist_ok=True)
            for f in files:
                if not f.filename:
                    continue
                ext = os.path.splitext(f.filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    rejected.append(f.filename)
                    continue
                safe_name = _safe_filename(f.filename)
                dest = os.path.join(self.music_root, safe_name)
                # Avoid silently overwriting an existing file of the same name.
                if os.path.exists(dest):
                    stem, ext2 = os.path.splitext(safe_name)
                    dest = os.path.join(self.music_root, f"{stem}_{int(time.time())}{ext2}")
                f.save(dest)
                saved.append(os.path.basename(dest))
                self.log("UPLOAD", f"saved: {os.path.basename(dest)}")

            self.last_upload_count = len(saved)
            self.last_upload_names = saved
            if saved:
                self._rescan_pending.set()

            msg_parts = []
            if saved:
                msg_parts.append(f'<div class="msg ok">Uploaded {len(saved)} file(s).'
                                  f'<div class="filelist">{", ".join(saved)}</div></div>')
            if rejected:
                msg_parts.append(f'<div class="msg err">Skipped (unsupported type): '
                                  f'{", ".join(rejected)}</div>')
            return UPLOAD_PAGE_HTML.format(message_html="".join(msg_parts))

        @app.errorhandler(413)
        def too_large(e):
            return UPLOAD_PAGE_HTML.format(
                message_html='<div class="msg err">Upload too large.</div>'), 413

        return app
