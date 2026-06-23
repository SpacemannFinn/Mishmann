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
import subprocess
import threading
import time

_NMCLI_TIMEOUT_S = 10.0  # scan/connect can genuinely take a few seconds;
                         # never let a hung nmcli stall the web request forever


def wifi_get_status():
    """
    Current connection status via nmcli. Returns a dict:
      {"connected": bool, "ssid": str or None, "signal": int or None}
    Never raises -- on any failure, returns connected=False so callers can
    always render *something* rather than crash the WiFi page.
    """
    try:
        result = subprocess.run(
            ["sudo", "nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
            capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S,
        )
        if result.returncode != 0:
            return {"connected": False, "ssid": None, "signal": None}
        for line in result.stdout.splitlines():
            # nmcli -t separates fields with ':' -- format is yes:SSID:SIGNAL
            # for the currently active connection.
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
    """
    List of nearby networks: [{"ssid": str, "signal": int, "security": str}],
    deduplicated by SSID (keeping the strongest signal seen), sorted by
    signal strength descending. Empty list on any failure -- never raises.
    """
    try:
        # Ask nmcli to actually (re)scan rather than just returning a cached
        # list, then read results -- two steps because `nmcli dev wifi list
        # --rescan yes` can be slow/flaky on some adapters; doing a plain
        # rescan first and listing after is more reliable in practice.
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
                continue  # hidden networks show up with empty SSID; skip
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
    """
    Join a network by SSID, with nmcli managing the actual connection
    profile. Returns (ok: bool, message: str). Never raises.
    """
    if not ssid:
        return False, "No network selected."
    try:
        args = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        result = subprocess.run(args, capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S)
        if result.returncode == 0:
            return True, f"Connected to {ssid}."
        # nmcli's stderr is usually a reasonably human-readable reason
        # (wrong password, network unreachable, etc) -- surface it directly
        # rather than a generic failure message.
        reason = (result.stderr or result.stdout or "Unknown error").strip()
        return False, f"Failed to connect: {reason}"
    except subprocess.TimeoutExpired:
        return False, "Connection attempt timed out."
    except Exception as e:
        return False, f"Connection error: {e}"


# Fixed, predictable setup-mode AP. Kept simple and memorable since this is
# specifically the thing a non-technical person needs to find and join with
# zero prior context -- no QR code, no app, just "this is a WiFi network,
# join it like any other." Same name/password every time so the device's
# own screen can always show static, reliable instructions.
HOTSPOT_CON_NAME = "WalkmanSetup"
HOTSPOT_SSID = "Walkman Setup"
HOTSPOT_PASSWORD = "walkman123"
HOTSPOT_GATEWAY_IP = "10.42.0.1"  # NetworkManager's hotspot default


def wifi_is_hotspot_active():
    """True if our setup-mode hotspot connection is the currently active one."""
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
    """
    Start the device's own fallback access point so a phone can connect to
    it directly with no pre-existing network required -- this is what
    actually closes the bootstrap gap (phone-assisted setup needs *some*
    shared network, and on a brand new/never-configured device there isn't
    one yet). Returns (ok: bool, message: str). Never raises.
    """
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
    """Tear down the setup-mode hotspot, e.g. once a real network is joined.
    Never raises; returns (ok: bool, message: str)."""
    try:
        result = subprocess.run(
            ["nmcli", "connection", "down", HOTSPOT_CON_NAME],
            capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_S,
        )
        # returncode != 0 here commonly just means "wasn't running" -- not
        # a real failure worth surfacing as an error to the caller.
        return True, "Hotspot stopped."
    except Exception as e:
        return False, f"Error stopping hotspot: {e}"


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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>Mishmann Upload</title>
  <style>
    :root {{
      --bg: #18181e;
      --card: #24242c;
      --accent: #e86a26;
      --accent-hover: #f97316;
      --text: #f8fafc;
      --text-mut: #94a3b8;
      --border: #3f3f46;
      --radius: 12px;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 20px;
      display: flex;
      justify-content: center;
      min-height: 100vh;
    }}
    .container {{ width: 100%; max-width: 500px; margin-top: 2vh; }}
    h1 {{ font-size: 24px; margin-bottom: 24px; font-weight: 700; display: flex; align-items: center; gap: 10px; }}
    .card {{ background: var(--card); border-radius: var(--radius); padding: 24px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    
    label {{ display: block; font-size: 14px; font-weight: 600; color: var(--text-mut); margin-bottom: 8px; }}
    input[type=password] {{
      width: 100%; padding: 14px; border-radius: 8px; border: 1px solid var(--border);
      background: var(--bg); color: var(--text); font-size: 16px; box-sizing: border-box; transition: border-color 0.2s;
    }}
    input[type=password]:focus {{ outline: none; border-color: var(--accent); }}
    
    /* Custom File Dropzone */
    .dropzone {{
      margin-top: 20px; border: 2px dashed var(--border); border-radius: var(--radius);
      padding: 40px 20px; text-align: center; cursor: pointer; transition: all 0.2s;
      background: rgba(0,0,0,0.1); position: relative;
    }}
    .dropzone.dragover {{ border-color: var(--accent); background: rgba(232, 106, 38, 0.1); }}
    .dropzone input[type=file] {{ position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }}
    .dropzone-icon {{ margin-bottom: 12px; opacity: 0.7; color: var(--accent); }}
    .dropzone-text {{ font-size: 16px; font-weight: 600; pointer-events: none; }}
    .dropzone-sub {{ font-size: 13px; color: var(--text-mut); margin-top: 6px; pointer-events: none; }}
    
    /* Selected File List */
    #filelist {{ margin-top: 16px; display: flex; flex-direction: column; gap: 8px; }}
    .file-item {{
      display: flex; justify-content: space-between; align-items: center;
      background: var(--bg); padding: 12px 16px; border-radius: 8px; font-size: 14px;
      border: 1px solid var(--border);
    }}
    .file-name {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 75%; }}
    .file-size {{ color: var(--text-mut); font-size: 12px; }}
    
    button {{
      width: 100%; padding: 16px; margin-top: 24px; border-radius: var(--radius); border: none;
      background: var(--accent); color: white; font-size: 16px; font-weight: 700; 
      cursor: pointer; transition: transform 0.1s, opacity 0.2s;
    }}
    button:active {{ transform: scale(0.98); }}
    button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    
    /* Live Progress Bar */
    .progress-container {{ margin-top: 24px; display: none; }}
    .progress-info {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 8px; color: var(--text-mut); }}
    .progress-track {{ width: 100%; height: 8px; background: var(--bg); border-radius: 4px; overflow: hidden; }}
    .progress-bar {{ width: 0%; height: 100%; background: var(--accent); transition: width 0.2s linear; }}
    
    /* Server Messages */
    .msg-wrapper {{ margin-top: 20px; }}
    .msg {{ padding: 16px; border-radius: 8px; font-size: 14px; line-height: 1.5; }}
    .msg.ok {{ background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); color: #86efac; }}
    .msg.err {{ background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); color: #fca5a5; }}
    .filelist {{ font-size: 13px; color: rgba(255,255,255,0.6); margin-top: 8px; word-break: break-all; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>
      Add Music
    </h1>
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
          <div class="dropzone-icon">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
          </div>
          <div class="dropzone-text">Tap to select or drop files here</div>
          <div class="dropzone-sub">MP3, FLAC, M4A, AAC, WAV</div>
        </div>
        
        <div id="filelist"></div>
        
        <div class="progress-container" id="progress-container">
          <div class="progress-info">
            <span id="progress-text">Uploading...</span>
            <span id="progress-percent">0%</span>
          </div>
          <div class="progress-track">
            <div class="progress-bar" id="progress-bar"></div>
          </div>
        </div>
        
        <button type="submit" id="submit-btn" disabled>Upload Files</button>
      </form>
      
      <div id="msg-wrapper" class="msg-wrapper">{message_html}</div>
    </div>
  </div>

  <script>
    const form = document.getElementById('upload-form');
    const fileInput = document.getElementById('file-input');
    const dropzone = document.getElementById('dropzone');
    const fileList = document.getElementById('filelist');
    const submitBtn = document.getElementById('submit-btn');
    const pwdInput = document.getElementById('pwd');
    
    const progContainer = document.getElementById('progress-container');
    const progBar = document.getElementById('progress-bar');
    const progText = document.getElementById('progress-text');
    const progPercent = document.getElementById('progress-percent');
    const msgWrapper = document.getElementById('msg-wrapper');

    // Drag & Drop Styling
    ['dragenter', 'dragover'].forEach(ev => dropzone.addEventListener(ev, e => {{
      e.preventDefault(); dropzone.classList.add('dragover');
    }}));
    ['dragleave', 'drop'].forEach(ev => dropzone.addEventListener(ev, e => {{
      dropzone.classList.remove('dragover');
    }}));

    // Live File List Generation
    fileInput.addEventListener('change', () => {{
      fileList.innerHTML = '';
      const files = Array.from(fileInput.files);
      
      files.forEach(file => {{
        const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
        fileList.innerHTML += `
          <div class="file-item">
            <span class="file-name">${{file.name}}</span>
            <span class="file-size">${{sizeMB}} MB</span>
          </div>
        `;
      }});
      checkFormState();
    }});

    pwdInput.addEventListener('input', checkFormState);

    function checkFormState() {{
      submitBtn.disabled = !(fileInput.files.length > 0 && pwdInput.value.length > 0);
    }}

    // AJAX Form Submission & Progress Tracking
    form.addEventListener('submit', (e) => {{
      e.preventDefault();
      if(submitBtn.disabled) return;

      const formData = new FormData(form);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload', true);

      // Track Upload Progress
      xhr.upload.onprogress = (e) => {{
        if (e.lengthComputable) {{
          const percent = Math.round((e.loaded / e.total) * 100);
          const loadedMB = (e.loaded / (1024 * 1024)).toFixed(1);
          const totalMB = (e.total / (1024 * 1024)).toFixed(1);
          
          progBar.style.width = percent + '%';
          progPercent.textContent = percent + '%';
          progText.textContent = `Uploading... ${{loadedMB}} / ${{totalMB}} MB`;
        }}
      }};

      // Handle Response
      xhr.onload = () => {{
        submitBtn.disabled = false;
        progContainer.style.display = 'none';
        
        // Extract the Python-generated message box from the returned HTML
        const parser = new DOMParser();
        const doc = parser.parseFromString(xhr.responseText, 'text/html');
        const msg = doc.getElementById('msg-wrapper');
        
        if (msg) {{
          msgWrapper.innerHTML = msg.innerHTML;
        }} else {{
          msgWrapper.innerHTML = '<div class="msg err">Upload failed. Check connection.</div>';
        }}

        // Reset UI on success
        if (xhr.status === 200) {{
          fileInput.value = ''; 
          fileList.innerHTML = '';
          checkFormState();
        }}
      }};

      xhr.onerror = () => {{
        submitBtn.disabled = false;
        progContainer.style.display = 'none';
        msgWrapper.innerHTML = '<div class="msg err">Network error occurred.</div>';
      }};

      // Setup UI for upload start
      submitBtn.disabled = true;
      msgWrapper.innerHTML = '';
      progContainer.style.display = 'block';
      progBar.style.width = '0%';
      progPercent.textContent = '0%';
      progText.textContent = 'Starting upload...';
      
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
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg); color: var(--text); margin: 0; padding: 20px;
      display: flex; justify-content: center; min-height: 100vh;
    }}
    .container {{ width: 100%; max-width: 500px; margin-top: 2vh; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; font-weight: 700; }}
    .back-link {{ color: var(--text-mut); font-size: 13px; text-decoration: underline; }}
    .card {{ background: var(--card); border-radius: var(--radius); padding: 24px; margin-top: 16px;
             box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    label {{ display: block; font-size: 14px; font-weight: 600; color: var(--text-mut); margin-bottom: 8px; }}
    input[type=password], input[type=text] {{
      width: 100%; padding: 14px; border-radius: 8px; border: 1px solid var(--border);
      background: var(--bg); color: var(--text); font-size: 16px; box-sizing: border-box;
    }}
    button {{
      width: 100%; padding: 14px; margin-top: 16px; border-radius: var(--radius); border: none;
      background: var(--accent); color: white; font-size: 16px; font-weight: 700; cursor: pointer;
    }}
    button.secondary {{ background: var(--border); }}
    .status-row {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 12px 0; border-bottom: 1px solid var(--border); margin-bottom: 12px;
    }}
    .status-ssid {{ font-size: 16px; font-weight: 600; }}
    .status-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; }}
    .dot-on {{ background: #4ade80; }}
    .dot-off {{ background: #6b7280; }}
    .net-row {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 12px 0; border-bottom: 1px solid var(--border); cursor: pointer;
    }}
    .net-row:last-child {{ border-bottom: none; }}
    .net-name {{ font-size: 14px; }}
    .net-meta {{ font-size: 12px; color: var(--text-mut); }}
    .net-row.selected {{ background: rgba(232, 106, 38, 0.1); border-radius: 8px; padding-left: 8px; }}
    .msg {{ padding: 14px; border-radius: 8px; font-size: 14px; margin-bottom: 16px; }}
    .msg.ok {{ background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); color: #86efac; }}
    .msg.err {{ background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); color: #fca5a5; }}
    .pw-form {{ display: none; margin-top: 16px; }}
    .pw-form.visible {{ display: block; }}
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
      const pwForm = document.getElementById('pw-form');
      pwForm.classList.add('visible');
      if (security === 'Open' || security === '') {{
        document.getElementById('wifi-password').required = false;
        document.getElementById('wifi-password').placeholder = 'No password needed (open network)';
      }} else {{
        document.getElementById('wifi-password').required = true;
        document.getElementById('wifi-password').placeholder = 'Network password';
      }}
      pwForm.scrollIntoView({{behavior: 'smooth', block: 'center'}});
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
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg); color: var(--text); margin: 0; padding: 20px;
      display: flex; justify-content: center; min-height: 100vh;
    }}
    .container {{ width: 100%; max-width: 500px; margin-top: 2vh; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; font-weight: 700; }}
    .back-link {{ color: var(--text-mut); font-size: 13px; text-decoration: underline; }}
    .card {{ background: var(--card); border-radius: var(--radius); padding: 24px; margin-top: 16px;
             box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    label {{ display: block; font-size: 14px; font-weight: 600; color: var(--text-mut); margin-bottom: 8px; }}
    input[type=password] {{
      width: 100%; padding: 14px; border-radius: 8px; border: 1px solid var(--border);
      background: var(--bg); color: var(--text); font-size: 16px; box-sizing: border-box;
    }}
    button.unlock {{
      width: 100%; padding: 14px; margin-top: 16px; border-radius: var(--radius); border: none;
      background: var(--accent); color: white; font-size: 16px; font-weight: 700; cursor: pointer;
    }}
    .file-row {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 14px 0; border-bottom: 1px solid var(--border); gap: 12px;
    }}
    .file-row:last-child {{ border-bottom: none; }}
    .file-info {{ overflow: hidden; }}
    .file-name {{ font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .file-size {{ font-size: 12px; color: var(--text-mut); margin-top: 2px; }}
    .del-btn {{
      flex-shrink: 0; background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.3);
      border-radius: 8px; padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
    }}
    .del-btn:active {{ transform: scale(0.96); }}
    .empty {{ color: var(--text-mut); font-size: 14px; text-align: center; padding: 20px 0; }}
    .msg {{ padding: 14px; border-radius: 8px; font-size: 14px; margin-bottom: 16px; }}
    .msg.ok {{ background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); color: #86efac; }}
    .msg.err {{ background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); color: #fca5a5; }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back-link" href="/">&larr; Back to upload</a>
    <h1>Manage Library</h1>
    {body_html}
  </div>
  <script>
    function confirmDelete(form, name) {{
      if (confirm("Delete \\"" + name + "\\"? This can't be undone.")) {{
        form.submit();
      }}
      return false;
    }}
  </script>
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

        def _list_music_files():
            """Flat list of (filename, size_bytes), sorted by name. Only
            files directly in music_root with an allowed extension — this
            deliberately mirrors what scan_music_folder would pick up, so
            the manage page only ever shows/deletes things that are
            actually part of the library."""
            out = []
            if not os.path.isdir(self.music_root):
                return out
            for name in sorted(os.listdir(self.music_root)):
                path = os.path.join(self.music_root, name)
                if not os.path.isfile(path):
                    continue
                if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                out.append((name, size))
            return out

        def _render_manage(password, message_html=""):
            if not self.password or password != self.password:
                body = (
                    '<div class="card">'
                    + ('<div class="msg err">Wrong password.</div>' if password else "")
                    + '<form method="get" action="/manage">'
                    '<label>Device Password</label>'
                    '<input type="password" name="password" required autofocus>'
                    '<button class="unlock" type="submit">Unlock</button>'
                    '</form></div>'
                )
                return MANAGE_PAGE_HTML.format(body_html=body)

            files = _list_music_files()
            rows = []
            if not files:
                rows.append('<div class="empty">No music files found.</div>')
            for name, size in files:
                size_mb = size / (1024 * 1024)
                rows.append(
                    '<div class="file-row">'
                    '<div class="file-info">'
                    f'<div class="file-name">{name}</div>'
                    f'<div class="file-size">{size_mb:.1f} MB</div>'
                    '</div>'
                    f'<form method="post" action="/delete" '
                    f'onsubmit="return confirmDelete(this, {name!r})">'
                    f'<input type="hidden" name="password" value="{password}">'
                    f'<input type="hidden" name="filename" value="{name}">'
                    '<button class="del-btn" type="submit">Delete</button>'
                    '</form>'
                    '</div>'
                )
            body = f'<div class="card">{message_html}{"".join(rows)}</div>'
            return MANAGE_PAGE_HTML.format(body_html=body)

        @app.route("/manage", methods=["GET"])
        def manage():
            password = request.args.get("password", "")
            return _render_manage(password)

        @app.route("/delete", methods=["POST"])
        def delete():
            password = request.form.get("password", "")
            filename = request.form.get("filename", "")

            if not self.password or password != self.password:
                self.log("UPLOAD", "rejected delete: wrong password")
                return _render_manage(""), 403

            # Re-derive the safe form of the name and require it to match
            # exactly what was requested, and resolve to a real path that's
            # still inside music_root -- the same belt-and-suspenders
            # discipline as upload, applied to the destructive direction.
            safe_name = _safe_filename(filename)
            if safe_name != filename:
                self.log("UPLOAD", f"rejected delete: unsafe filename {filename!r}")
                return _render_manage(password,
                    '<div class="msg err">Invalid filename.</div>')

            target = os.path.abspath(os.path.join(self.music_root, safe_name))
            root = os.path.abspath(self.music_root)
            if os.path.dirname(target) != root or os.path.splitext(safe_name)[1].lower() not in ALLOWED_EXTENSIONS:
                self.log("UPLOAD", f"rejected delete: path outside music_root or bad ext ({filename!r})")
                return _render_manage(password,
                    '<div class="msg err">Invalid filename.</div>')

            if not os.path.isfile(target):
                return _render_manage(password,
                    '<div class="msg err">File not found (already deleted?).</div>')

            try:
                os.remove(target)
                self.log("UPLOAD", f"deleted: {safe_name}")
                self._rescan_pending.set()
                return _render_manage(password,
                    f'<div class="msg ok">Deleted {safe_name}.</div>')
            except OSError as e:
                self.log("UPLOAD", f"delete failed for {safe_name}: {e}")
                return _render_manage(password,
                    '<div class="msg err">Delete failed (see device log).</div>')

        def _render_wifi(password, message_html=""):
            if not self.password or password != self.password:
                body = (
                    '<div class="card">'
                    + ('<div class="msg err">Wrong password.</div>' if password else "")
                    + '<form method="get" action="/wifi">'
                    '<label>Device Password</label>'
                    '<input type="password" name="password" required autofocus>'
                    '<button type="submit">Unlock</button>'
                    '</form></div>'
                )
                return WIFI_PAGE_HTML.format(body_html=body)

            status = wifi_get_status()
            networks = wifi_scan()

            dot_class = "dot-on" if status["connected"] else "dot-off"
            ssid_label = status["ssid"] if status["connected"] else "Not connected"
            signal_html = f'<span class="net-meta">{status["signal"]}%</span>' if status["connected"] else ""
            status_html = (
                '<div class="card">'
                f'{message_html}'
                '<div class="status-row">'
                f'<span><span class="status-dot {dot_class}"></span>'
                f'<span class="status-ssid">{ssid_label}</span></span>'
                f'{signal_html}'
                '</div>'
            )

            rows = []
            if not networks:
                rows.append('<div class="net-meta" style="padding:12px 0;">'
                            'No networks found. Try refreshing.</div>')
            for n in networks:
                is_current = status["connected"] and n["ssid"] == status["ssid"]
                rows.append(
                    f'<div class="net-row{" selected" if is_current else ""}" id="row-{n["ssid"]}" '
                    f'onclick="selectNetwork({n["ssid"]!r}, {n["security"]!r})">'
                    f'<span class="net-name">{n["ssid"]}{"  (connected)" if is_current else ""}</span>'
                    f'<span class="net-meta">{n["signal"]}%  ·  {n["security"]}</span>'
                    '</div>'
                )

            pw_form = (
                '<div class="card pw-form" id="pw-form">'
                '<form method="post" action="/wifi/connect">'
                f'<input type="hidden" name="password" value="{password}">'
                '<input type="hidden" name="ssid" id="ssid-field" value="">'
                '<label>Connecting to: <span id="selected-ssid-label"></span></label>'
                '<input type="password" name="wifi_password" id="wifi-password" placeholder="Network password">'
                '<button type="submit">Connect</button>'
                '</form></div>'
            )

            body = (
                status_html
                + f'<div class="card">{"".join(rows)}</div>'
                + pw_form
                + '<form method="get" action="/wifi" style="margin-top:12px;">'
                + f'<input type="hidden" name="password" value="{password}">'
                + '<button type="submit" class="secondary">Refresh networks</button>'
                + '</form>'
            )
            return WIFI_PAGE_HTML.format(body_html=body)

        @app.route("/wifi", methods=["GET"])
        def wifi_page():
            password = request.args.get("password", "")
            return _render_wifi(password)

        @app.route("/wifi/connect", methods=["POST"])
        def wifi_connect_route():
            password = request.form.get("password", "")
            if not self.password or password != self.password:
                self.log("UPLOAD", "rejected wifi connect: wrong password")
                return _render_wifi(""), 403

            ssid = request.form.get("ssid", "").strip()
            wifi_password = request.form.get("wifi_password", "")
            self.log("UPLOAD", f"attempting wifi connect to {ssid!r}")
            ok, msg = wifi_connect(ssid, wifi_password)
            if ok and wifi_is_hotspot_active():
                # Successfully joined a real network -- the setup hotspot
                # has done its job, so tear it down. If this device's own
                # radio can't run both at once (most single-radio hardware
                # can't), this also avoids a stuck setup AP nobody can see
                # because the radio switched to the new network anyway.
                self.log("UPLOAD", "real network joined, stopping setup hotspot")
                wifi_stop_hotspot()
            css_class = "ok" if ok else "err"
            self.log("UPLOAD", f"wifi connect {'succeeded' if ok else 'failed'}: {msg}")
            return _render_wifi(password, f'<div class="msg {css_class}">{msg}</div>')

        return app