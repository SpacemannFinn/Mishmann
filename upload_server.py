"""
upload_server.py — toggleable web upload server for adding music over WiFi.
"""

import os
import re
import secrets
import string
import subprocess
import threading
import time

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

_NMCLI_TIMEOUT_S = 10.0


def wifi_get_status():
    try:
        result = subprocess.run(
            ["sudo", "nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
            capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S,
        )
        if result.returncode != 0:
            return {"connected": False, "ssid": None, "signal": None}
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == "yes":
                ssid = parts[1] if parts[1] else None
                try:
                    signal = int(parts[2])
                except ValueError:
                    signal = None
                return {"connected": True, "ssid": ssid, "signal": signal}
        return {"connected": False, "ssid": None, "signal": None}
    except Exception:
        return {"connected": False, "ssid": None, "signal": None}


def wifi_scan():
    try:
        subprocess.run(["sudo", "nmcli", "dev", "wifi", "rescan"],
                        capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S)
        result = subprocess.run(
            ["sudo", "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S,
        )
        if result.returncode != 0:
            return []
        best = {}
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            ssid, signal_str, security = parts[0], parts[1], ":".join(parts[2:])
            if not ssid:
                continue
            try:
                signal = int(signal_str)
            except ValueError:
                signal = 0
            if ssid not in best or signal > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": signal, "security": security or "Open"}
        return sorted(best.values(), key=lambda n: -n["signal"])
    except Exception:
        return []


def wifi_connect(ssid, password):
    if not ssid:
        return False, "No network selected."
    try:
        args = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        result = subprocess.run(args, capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S)
        if result.returncode == 0:
            return True, f"Connected to {ssid}."
        reason = (result.stderr or result.stdout or "Unknown error").strip()
        return False, f"Failed to connect: {reason}"
    except subprocess.TimeoutExpired:
        return False, "Connection attempt timed out."
    except Exception as e:
        return False, f"Connection error: {e}"


HOTSPOT_CON_NAME = "WalkmanSetup"
HOTSPOT_SSID = "Walkman Setup"
HOTSPOT_PASSWORD = "walkman123"
HOTSPOT_GATEWAY_IP = "10.42.0.1"


def wifi_is_hotspot_active():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S,
        )
        if result.returncode != 0:
            return False
        return any(line.split(":")[0] == HOTSPOT_CON_NAME for line in result.stdout.splitlines())
    except Exception:
        return False


def wifi_start_hotspot():
    if wifi_is_hotspot_active():
        return True, "Hotspot already running."
    try:
        result = subprocess.run(
            ["nmcli", "dev", "wifi", "hotspot",
             "con-name", HOTSPOT_CON_NAME,
             "ssid", HOTSPOT_SSID,
             "password", HOTSPOT_PASSWORD],
            capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S,
        )
        if result.returncode == 0:
            return True, f"Hotspot '{HOTSPOT_SSID}' started."
        reason = (result.stderr or result.stdout or "Unknown error").strip()
        return False, f"Failed to start hotspot: {reason}"
    except subprocess.TimeoutExpired:
        return False, "Hotspot start timed out."
    except Exception as e:
        return False, f"Hotspot error: {e}"


def wifi_stop_hotspot():
    try:
        subprocess.run(["nmcli", "connection", "down", HOTSPOT_CON_NAME],
            capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S)
        return True, "Hotspot stopped."
    except Exception as e:
        return False, f"Error stopping hotspot: {e}"


ALLOWED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".wav"}
MAX_UPLOAD_SIZE_BYTES = 500 * 1024 * 1024


class UploadServerUnavailable(Exception):
    pass


def _generate_password(length=6):
    return "".join(secrets.choice(string.digits) for _ in range(length))


def _safe_filename(name):
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9 ._\-\(\)\[\]&,']", "_", name)
    name = name.strip().lstrip(".")
    return name or f"upload_{int(time.time())}"


UPLOAD_PAGE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>Mishmann Upload</title>
  <style>
    :root {{
      --bg: #18181e; --card: #24242c; --accent: #e86a26; --text: #f8fafc;
      --text-mut: #94a3b8; --border: #3f3f46; --radius: 12px;
    }}
    body {{
      font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 20px;
      display: flex; justify-content: center; min-height: 100vh;
    }}
    .container {{ width: 100%; max-width: 500px; margin-top: 2vh; }}
    h1 {{ font-size: 24px; margin-bottom: 24px; display: flex; align-items: center; gap: 10px; }}
    .card {{ background: var(--card); border-radius: var(--radius); padding: 24px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    label {{ display: block; font-size: 14px; font-weight: 600; color: var(--text-mut); margin-bottom: 8px; }}
    input[type=password] {{
      width: 100%; padding: 14px; border-radius: 8px; border: 1px solid var(--border);
      background: var(--bg); color: var(--text); font-size: 16px; box-sizing: border-box;
    }}
    .dropzone {{
      margin-top: 20px; border: 2px dashed var(--border); border-radius: var(--radius);
      padding: 40px 20px; text-align: center; cursor: pointer; position: relative; background: rgba(0,0,0,0.1);
    }}
    .dropzone input[type=file] {{ position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }}
    .dropzone-icon {{ margin-bottom: 12px; color: var(--accent); }}
    .dropzone-text {{ font-size: 16px; font-weight: 600; }}
    .dropzone-sub {{ font-size: 13px; color: var(--text-mut); margin-top: 6px; }}
    #filelist {{ margin-top: 16px; display: flex; flex-direction: column; gap: 8px; }}
    .file-item {{ display: flex; justify-content: space-between; align-items: center; background: var(--bg); padding: 12px 16px; border-radius: 8px; font-size: 14px; border: 1px solid var(--border); }}
    button {{ width: 100%; padding: 16px; margin-top: 24px; border-radius: var(--radius); border: none; background: var(--accent); color: white; font-size: 16px; font-weight: 700; cursor: pointer; }}
    button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .progress-container {{ margin-top: 24px; display: none; }}
    .progress-info {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 8px; color: var(--text-mut); }}
    .progress-track {{ width: 100%; height: 8px; background: var(--bg); border-radius: 4px; overflow: hidden; }}
    .progress-bar {{ width: 0%; height: 100%; background: var(--accent); }}
    .msg-wrapper {{ margin-top: 20px; }}
    .msg {{ padding: 16px; border-radius: 8px; font-size: 14px; line-height: 1.5; }}
    .msg.ok {{ background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); color: #86efac; }}
    .msg.err {{ background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); color: #fca5a5; }}
    .filelist {{ font-size: 13px; color: rgba(255,255,255,0.6); margin-top: 8px; word-break: break-all; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Mishmann Player</h1>
    <div style="margin: -16px 0 16px; text-align: right;">
      <a href="/manage" style="color: var(--text-mut); font-size: 13px; text-decoration: underline;">Manage library files &rarr;</a>
      &nbsp;&middot;&nbsp;
      <a href="/wifi" style="color: var(--text-mut); font-size: 13px; text-decoration: underline;">Wi-Fi settings &rarr;</a>
    </div>
    <div class="card">
      <form id="upload-form" method="post" action="/upload" enctype="multipart/form-data">
        <label>Device Password</label>
        <input type="password" name="password" id="pwd" placeholder="Enter PIN shown on device screen" required autofocus>
        <div class="dropzone" id="dropzone">
          <input type="file" name="files" id="file-input" multiple accept=".mp3,.flac,.m4a,.aac,.wav" required>
          <div class="dropzone-icon">▲</div>
          <div class="dropzone-text">Tap to select or drop files here</div>
          <div class="dropzone-sub">MP3, FLAC, M4A, AAC, WAV</div>
        </div>
        <div id="filelist"></div>
        <div class="progress-container" id="progress-container">
          <div class="progress-info"><span id="progress-text">Uploading...</span><span id="progress-percent">0%</span></div>
          <div class="progress-track"><div class="progress-bar" id="progress-bar"></div></div>
        </div>
        <button type="submit" id="submit-btn" disabled>Upload Files</button>
      </form>
      <div id="msg-wrapper" class="msg-wrapper">{message_html}</div>
    </div>
  </div>
  <script>
    const form = document.getElementById('upload-form');
    const fileInput = document.getElementById('file-input');
    const fileList = document.getElementById('filelist');
    const submitBtn = document.getElementById('submit-btn');
    const pwdInput = document.getElementById('pwd');
    const progContainer = document.getElementById('progress-container');
    const progBar = document.getElementById('progress-bar');
    const progText = document.getElementById('progress-text');
    const progPercent = document.getElementById('progress-percent');
    const msgWrapper = document.getElementById('msg-wrapper');

    fileInput.addEventListener('change', () => {{
      fileList.innerHTML = '';
      Array.from(fileInput.files).forEach(file => {{
        const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
        fileList.innerHTML += `<div class="file-item"><span>\${{file.name}}</span><span style="color:var(--text-mut);">\${{sizeMB}} MB</span></div>`;
      }});
      checkFormState();
    }});
    pwdInput.addEventListener('input', checkFormState);
    function checkFormState() {{ submitBtn.disabled = !(fileInput.files.length > 0 && pwdInput.value.length > 0); }}

    form.addEventListener('submit', (e) => {{
      e.preventDefault();
      const formData = new FormData(form);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload', true);
      xhr.upload.onprogress = (e) => {{
        if (e.lengthComputable) {{
          const percent = Math.round((e.loaded / e.total) * 100);
          progBar.style.width = percent + '%';
          progPercent.textContent = percent + '%';
          progText.textContent = `Uploading... \${{(e.loaded / (1024*1024)).toFixed(1)}} / \dots`;
        }}
      }};
      xhr.onload = () => {{
        submitBtn.disabled = false; progContainer.style.display = 'none';
        const doc = new DOMParser().parseFromString(xhr.responseText, 'text/html');
        const msg = doc.getElementById('msg-wrapper');
        msgWrapper.innerHTML = msg ? msg.innerHTML : '<div class="msg err">Upload failed.</div>';
        if (xhr.status === 200) {{ fileInput.value = ''; fileList.innerHTML = ''; checkFormState(); }}
      }};
      submitBtn.disabled = true; msgWrapper.innerHTML = ''; progContainer.style.display = 'block';
      xhr.send(formData);
    }});
  </script>
</body>
</html>
"""


WIFI_PAGE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>Wi-Fi Settings</title>
  <style>
    :root {{
      --bg: #18181e; --card: #24242c; --accent: #e86a26; --text: #f8fafc;
      --text-mut: #94a3b8; --border: #3f3f46; --radius: 12px;
    }}
    body {{ font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 20px; display: flex; justify-content: center; }}
    .container {{ width: 100%; max-width: 500px; margin-top: 2vh; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    .back-link {{ color: var(--text-mut); font-size: 13px; }}
    .card {{ background: var(--card); border-radius: var(--radius); padding: 24px; margin-top: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    label {{ display: block; font-size: 14px; font-weight: 600; color: var(--text-mut); margin-bottom: 8px; }}
    input[type=password] {{ width: 100%; padding: 14px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--text); font-size: 16px; box-sizing: border-box; }}
    button {{ width: 100%; padding: 14px; margin-top: 16px; border-radius: var(--radius); border: none; background: var(--accent); color: white; font-size: 16px; font-weight: 700; cursor: pointer; }}
    .status-row {{ display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border); margin-bottom: 12px; }}
    .status-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; }}
    .dot-on {{ background: #4ade80; }} .dot-off {{ background: #6b7280; }}
    .net-row {{ display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border); cursor: pointer; }}
    .net-row.selected {{ background: rgba(232, 106, 38, 0.1); border-radius: 8px; padding-left: 8px; }}
    .msg {{ padding: 14px; border-radius: 8px; font-size: 14px; margin-bottom: 16px; }}
    .msg.ok {{ background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); color: #86efac; }}
    .msg.err {{ background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); color: #fca5a5; }}
    .pw-form {{ display: none; margin-top: 16px; }} .pw-form.visible {{ display: block; }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back-link" href="/">&larr; Back to upload</a>
    <h1>Wi-Fi Settings</h1>
    {body_html}
  </div>
  <script>
    function selectNetwork(ssid, security) {{
      document.getElementById('selected-ssid-label').textContent = ssid;
      document.getElementById('ssid-field').value = ssid;
      document.querySelectorAll('.net-row').forEach(r => r.classList.remove('selected'));
      document.getElementById('row-' + ssid).classList.add('selected');
      document.getElementById('pw-form').classList.add('visible');
      document.getElementById('wifi-password').placeholder = (security === 'Open' || security === '') ? 'No password needed' : 'Network password';
    }}
  </script>
</body>
</html>
"""


MANAGE_PAGE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>Manage Library</title>
  <style>
    :root {{
      --bg: #18181e; --card: #24242c; --accent: #e86a26; --text: #f8fafc;
      --text-mut: #94a3b8; --border: #3f3f46; --radius: 12px;
    }}
    body {{ font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 20px; display: flex; justify-content: center; }}
    .container {{ width: 100%; max-width: 600px; margin-top: 2vh; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    .back-link {{ color: var(--text-mut); font-size: 13px; }}
    .card {{ background: var(--card); border-radius: var(--radius); padding: 24px; margin-top: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    .track-box {{ background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    .track-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 12px; }}
    .filename {{ font-size: 13px; font-family: monospace; color: var(--text-mut); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 70%; }}
    .meta-form {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; align-items: end; }}
    .field-group {{ display: flex; flex-direction: column; }}
    .field-group label {{ font-size: 11px; font-weight: 600; color: var(--text-mut); margin-bottom: 4px; }}
    .meta-form input {{ padding: 8px; border-radius: 6px; border: 1px solid var(--border); background: var(--card); color: var(--text); font-size: 13px; }}
    .save-btn {{ background: var(--accent); color: white; border: none; padding: 8px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }}
    .del-btn {{ background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }}
    .msg {{ padding: 14px; border-radius: 8px; font-size: 14px; margin-bottom: 16px; }}
    .msg.ok {{ background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); color: #86efac; }}
    .msg.err {{ background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); color: #fca5a5; }}
    input[type=password] {{ width: 100%; padding: 14px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--text); font-size: 16px; box-sizing: border-box; }}
    .unlock-btn {{ width: 100%; padding: 14px; margin-top: 16px; border-radius: var(--radius); border: none; background: var(--accent); color: white; font-size: 16px; font-weight: 700; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back-link" href="/">&larr; Back to upload</a>
    <h1>Manage Library</h1>
    {body_html}
  </div>
</body>
</html>
"""


class MusicUploadServer:
    def __init__(self, music_root, port=8080, log_fn=print):
        self.music_root = music_root
        self.port = port
        self.log = log_fn
        self.password = None
        self._server = None
        self._thread = None
        self._rescan_pending = threading.Event()
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            try:
                app = self._build_app()
                from werkzeug.serving import make_server
                server = make_server("0.0.0.0", self.port, app, threaded=True)
            except ModuleNotFoundError as e:
                self.log("UPLOAD", f"WARNING: dependency missing: {e}")
                raise UploadServerUnavailable(f"Flask isn't installed.")
            except OSError as e:
                self.log("UPLOAD", f"WARNING: can't bind port {self.port}: {e}")
                raise UploadServerUnavailable(f"Port bound.")

            self.password = _generate_password()
            self._server = server
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="upload-server")
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
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return f"http://{ip}:{self.port}"
        except Exception:
            return f"http://<device-ip>:{self.port}"

    def is_rescan_pending(self):
        return self._rescan_pending.is_set()

    def consume_rescan_flag(self):
        self._rescan_pending.clear()

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
                return UPLOAD_PAGE_HTML.format(message_html='<div class="msg err">Wrong password.</div>'), 403

            files = request.files.getlist("files")
            if not files:
                return UPLOAD_PAGE_HTML.format(message_html='<div class="msg err">No files received.</div>'), 400

            saved = []
            os.makedirs(self.music_root, exist_ok=True)
            for f in files:
                if not f.filename:
                    continue
                ext = os.path.splitext(f.filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue
                safe_name = _safe_filename(f.filename)
                dest = os.path.join(self.music_root, safe_name)
                if os.path.exists(dest):
                    stem, ext2 = os.path.splitext(safe_name)
                    dest = os.path.join(self.music_root, f"{stem}_{int(time.time())}{ext2}")
                f.save(dest)
                saved.append(os.path.basename(dest))
            
            if saved:
                self._rescan_pending.set()
            return UPLOAD_PAGE_HTML.format(message_html=f'<div class="msg ok">Uploaded {len(saved)} file(s).</div>')

        def _list_music_files():
            out = []
            if not os.path.isdir(self.music_root):
                return out
            for name in sorted(os.listdir(self.music_root)):
                path = os.path.join(self.music_root, name)
                if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
                    continue
                try:
                    size = os.path.getsize(path)
                    title, artist, album = name, "Unknown Artist", "Unknown Album"
                    if MutagenFile is not None:
                        audio = MutagenFile(path, easy=True)
                        if audio:
                            title = audio.get("title", [title])[0]
                            artist = audio.get("artist", [artist])[0]
                            album = audio.get("album", [album])[0]
                except OSError:
                    continue
                out.append((name, size, title, artist, album))
            return out

        def _render_manage(password, message_html=""):
            if not self.password or password != self.password:
                body = (
                    '<div class="card">'
                    + ('<div class="msg err">Wrong password.</div>' if password else "")
                    + '<form method="get" action="/manage">'
                    '<label>Device Password</label>'
                    '<input type="password" name="password" required autofocus>'
                    '<button class="unlock-btn" type="submit">Unlock</button>'
                    '</form></div>'
                )
                return MANAGE_PAGE_HTML.format(body_html=body)

            files = _list_music_files()
            rows = []
            if not files:
                rows.append('<div class="msg">No music files found.</div>')
            for name, size, title, artist, album in files:
                size_mb = size / (1024 * 1024)
                rows.append(
                    f'<div class="track-box">'
                    f'  <div class="track-header">'
                    f'    <div class="filename">{name} ({size_mb:.1f} MB)</div>'
                    f'    <form method="post" action="/delete" style="margin:0;" onsubmit="return confirm(\'Delete this file?\');">'
                    f'      <input type="hidden" name="password" value="{password}">'
                    f'      <input type="hidden" name="filename" value="{name}">'
                    f'      <button class="del-btn" type="submit">Delete</button>'
                    f'    </form>'
                    f'  </div>'
                    f'  <form method="post" action="/update_metadata" class="meta-form">'
                    f'    <input type="hidden" name="password" value="{password}">'
                    f'    <input type="hidden" name="filename" value="{name}">'
                    f'    <div class="field-group">'
                    f'      <label>Title</label>'
                    f'      <input type="text" name="title" value="{title}">'
                    f'    </div>'
                    f'    <div class="field-group">'
                    f'      <label>Artist</label>'
                    f'      <input type="text" name="artist" value="{artist}">'
                    f'    </div>'
                    f'    <div class="field-group">'
                    f'      <label>Album</label>'
                    f'      <input type="text" name="album" value="{album}">'
                    f'    </div>'
                    f'    <button class="save-btn" type="submit" style="grid-column: span 3; margin-top:8px;">Update Tags</button>'
                    f'  </form>'
                    f'</div>'
                )
            body = f'{message_html}{"".join(rows)}'
            return MANAGE_PAGE_HTML.format(body_html=body)

        @app.route("/manage", methods=["GET"])
        def manage():
            return _render_manage(request.args.get("password", ""))

        @app.route("/update_metadata", methods=["POST"])
        def update_metadata():
            password = request.form.get("password", "")
            if not self.password or password != self.password:
                return _render_manage(""), 403

            filename = request.form.get("filename", "")
            safe_name = _safe_filename(filename)
            target = os.path.join(self.music_root, safe_name)

            if safe_name != filename or not os.path.isfile(target):
                return _render_manage(password, '<div class="msg err">Invalid file.</div>')

            try:
                if MutagenFile is not None:
                    audio = MutagenFile(target, easy=True)
                    if audio is not None:
                        audio["title"] = request.form.get("title", "").strip()
                        audio["artist"] = request.form.get("artist", "").strip()
                        audio["album"] = request.form.get("album", "").strip()
                        audio.save()
                        self.log("UPLOAD", f"Updated tags for {safe_name}")
                        self._rescan_pending.set()
                        return _render_manage(password, f'<div class="msg ok">Tags updated for {safe_name}</div>')
                return _render_manage(password, '<div class="msg err">Mutagen wrapper unavailable.</div>')
            except Exception as e:
                self.log("UPLOAD", f"Tag update failure: {e}")
                return _render_manage(password, f'<div class="msg err">Update failure: {e}</div>')

        @app.route("/delete", methods=["POST"])
        def delete():
            password = request.form.get("password", "")
            if not self.password or password != self.password:
                return _render_manage(""), 403

            filename = request.form.get("filename", "")
            safe_name = _safe_filename(filename)
            target = os.path.abspath(os.path.join(self.music_root, safe_name))

            if safe_name != filename or os.path.dirname(target) != os.path.abspath(self.music_root) or not os.path.isfile(target):
                return _render_manage(password, '<div class="msg err">Invalid file context.</div>')

            try:
                os.remove(target)
                self.log("UPLOAD", f"deleted: {safe_name}")
                self._rescan_pending.set()
                return _render_manage(password, f'<div class="msg ok">Deleted {safe_name}.</div>')
            except OSError as e:
                return _render_manage(password, '<div class="msg err">Delete failed.</div>')

        @app.route("/wifi", methods=["GET"])
        def wifi_page():
            return _render_wifi(request.args.get("password", ""))

        @app.route("/wifi/connect", methods=["POST"])
        def wifi_connect_route():
            password = request.form.get("password", "")
            if not self.password or password != self.password:
                return _render_wifi(""), 403

            ssid = request.form.get("ssid", "").strip()
            wifi_password = request.form.get("wifi_password", "")
            ok, msg = wifi_connect(ssid, wifi_password)
            if ok and wifi_is_hotspot_active():
                wifi_stop_hotspot()
            css_class = "ok" if ok else "err"
            return _render_wifi(password, f'<div class="msg {css_class}">{msg}</div>')

        return app