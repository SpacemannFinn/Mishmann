import os
import time
import subprocess
import spidev
import gpiod
import math
import io
import colorsys
from PIL import Image, ImageFont, ImageDraw, ImageFilter
try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from bt_manager import BluetoothManager, BluetoothUnavailable
from upload_server import (
    MusicUploadServer, UploadServerUnavailable, wifi_get_status,
    wifi_start_hotspot, wifi_stop_hotspot, wifi_is_hotspot_active,
    HOTSPOT_SSID, HOTSPOT_PASSWORD, HOTSPOT_GATEWAY_IP,
)


# ================================
# DEBUG LOGGING
# Tagged prints so on-device output can be grepped per-section, e.g.
# `python3 music_player.py | grep '\[BT\]'` to isolate Bluetooth activity,
# or `| grep '\[MENU\]'` while debugging navigation. Timestamped (seconds
# since script start) so timing between events is visible at a glance.
# ================================
_LOG_T0 = time.monotonic()

def log(tag, msg):
    print(f"[{time.monotonic() - _LOG_T0:8.3f}] [{tag}] {msg}", flush=True)

# ================================
# GSTREAMER AUDIO PLAYER
# ================================
class GstPlayer:
    def __init__(self):
        Gst.init(None)
        self.playbin = Gst.ElementFactory.make("playbin", "player")
        if not self.playbin:
            raise RuntimeError("Could not create GStreamer playbin")

        audio_sink = Gst.ElementFactory.make("pulsesink", "bluetooth_audio_output") 
        if audio_sink:
            self.playbin.set_property("audio-sink", audio_sink)
        else:
            print("WARNING: Could not create pulsesink. Using default.")

        self._duration_ms = 0
        self._volume=1.0
        self.playbin.set_property("volume", self._volume)

    def load(self, path: str):
        uri = Gst.filename_to_uri(os.path.abspath(path))
        self.playbin.set_property("uri", uri)
        self._duration_ms = 0

    def play(self):
        self.playbin.set_state(Gst.State.PLAYING)

    def wait_until_playing(self, timeout_s=5.0):
        """Block until the pipeline actually reaches PLAYING or fails, using
        GStreamer's own state-change wait rather than guessing with a sleep
        loop. Returns True if PLAYING was reached."""
        timeout_ns = int(timeout_s * Gst.SECOND)
        state_change_return, state, pending = self.playbin.get_state(timeout_ns)
        if state_change_return == Gst.StateChangeReturn.FAILURE:
            return False
        return state == Gst.State.PLAYING

    def pause(self):
        self.playbin.set_state(Gst.State.PAUSED)

    def stop(self):
        self.playbin.set_state(Gst.State.NULL)

    def get_position_ms(self) -> int:
        try:
            ok, pos = self.playbin.query_position(Gst.Format.TIME)
            if not ok: return 0
            return pos // 1_000_000
        except Exception: return 0

    def get_duration_ms(self) -> int:
        if self._duration_ms > 0: return self._duration_ms
        try:
            ok, dur = self.playbin.query_duration(Gst.Format.TIME)
            if not ok: return 0
            self._duration_ms = dur // 1_000_000
            return self._duration_ms
        except Exception: return 0

    def set_volume(self, vol: float):
        self._volume = max(0.0, min(1.0, vol))
        self.playbin.set_property("volume", self._volume)

    def get_volume(self) -> float:
        return self._volume

    def change_volume(self, delta: float):
        self.set_volume(self._volume + delta)

    def seek_to_start(self):
        self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
        
    def seek_change(self, delta_secs: float):
        try: ok, pos_ns = self.playbin.query_position(Gst.Format.TIME)
        except Exception: pos_ns = 0
        if not ok: pos_ns = 0
            
        new_pos_ns = pos_ns + int(delta_secs * Gst.SECOND)
        new_pos_ns = max(0, new_pos_ns)
        if self._duration_ms > 0:
            new_pos_ns = min(self._duration_ms * 1_000_000, new_pos_ns)
            
        self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, new_pos_ns)

# ================================
# CONFIGURATION
# ================================
CHIP_NAME = "gpiochip3"
DC_LINE = 1       
RESET_LINE = 8    
BACKLIGHT_LINE = 10   
SPI_BUS = 3
SPI_DEV = 0

PLAY_PAUSE_BTN_LINE = 2   
NEXT_BTN_LINE       = 20   
PREV_BTN_LINE       = 4   
VOL_UP_BTN_LINE     = 11   
VOL_DOWN_BTN_LINE   = 12   

WIDTH = 480
HEIGHT = 320

MUSIC_ROOT = "/home/rock/music"
DEFAULT_ART_PATH="/home/rock/placeholder.jpg"

# ================================
# DYNAMIC PALETTE EXTRACTOR
# ================================
def extract_track_theme(pil_img):
    default_bg = (30, 30, 36)
    default_text = (245, 245, 250)
    default_subtext = (170, 175, 185)
    default_accent = (232, 106, 38)

    if pil_img is None:
        return {"bg": default_bg, "text": default_text, "subtext": default_subtext, "accent": default_accent}

    small = pil_img.copy()
    small.thumbnail((50, 50))
    try:
        paletted = small.convert('P', palette=Image.ADAPTIVE, colors=1)
        palette = paletted.getpalette()
        r, g, b = palette[0], palette[1], palette[2]
    except Exception:
        r, g, b = 40, 40, 45 

    h, s, v = colorsys.rgb_to_hsv(r/255.0, g/255.0, b/255.0)

    # Fallback to teal if totally black/grey
    if s < 0.15 and v < 0.25:
        h, s, v = 0.52, 0.65, 0.45 
    # If album art is very light, drop the background value to keep text legible
    elif v > 0.75 or (v > 0.6 and s < 0.3):
        v = 0.18           
        s = max(0.4, s)    
    else:
        if v < 0.30: v = 0.40 
        if v > 0.55: v = 0.55

    r_out, g_out, b_out = colorsys.hsv_to_rgb(h, s, v)
    bg = (int(r_out*255), int(g_out*255), int(b_out*255))

    text = (250, 250, 250)
    subtext = (200, 200, 210)

    acc_h = (h + 0.5) % 1.0
    acc_r, acc_g, acc_b = colorsys.hsv_to_rgb(acc_h, max(0.6, s), 0.8)
    accent = (int(acc_r*255), int(acc_g*255), int(acc_b*255))

    return {"bg": bg, "text": text, "subtext": subtext, "accent": accent}

# ================================
# GPIO & SPI SETUP
# ================================
chip = gpiod.Chip(CHIP_NAME)
dc_line = chip.get_line(DC_LINE)
rst_line = chip.get_line(RESET_LINE)

dc_line.request(consumer="ili9488-dc", type=gpiod.LINE_REQ_DIR_OUT)
rst_line.request(consumer="ili9488-rst", type=gpiod.LINE_REQ_DIR_OUT)

btn_play_pause_line = chip.get_line(PLAY_PAUSE_BTN_LINE)
btn_next_line       = chip.get_line(NEXT_BTN_LINE)
btn_prev_line       = chip.get_line(PREV_BTN_LINE)
btn_vol_up_line     = chip.get_line(VOL_UP_BTN_LINE)
btn_vol_down_line   = chip.get_line(VOL_DOWN_BTN_LINE)

btn_play_pause_line.request(consumer="btn-play", type=gpiod.LINE_REQ_DIR_IN)
btn_next_line.request(consumer="btn-next", type=gpiod.LINE_REQ_DIR_IN)
btn_prev_line.request(consumer="btn-prev", type=gpiod.LINE_REQ_DIR_IN)
btn_vol_up_line.request(consumer="btn-volup", type=gpiod.LINE_REQ_DIR_IN)
btn_vol_down_line.request(consumer="btn-voldown", type=gpiod.LINE_REQ_DIR_IN)
log("GPIO", f"chip={CHIP_NAME} dc={DC_LINE} rst={RESET_LINE} "
            f"buttons=(play={PLAY_PAUSE_BTN_LINE},next={NEXT_BTN_LINE},"
            f"prev={PREV_BTN_LINE},vol_up={VOL_UP_BTN_LINE},vol_down={VOL_DOWN_BTN_LINE}) requested OK")


# ================================
# BACKLIGHT — real PWM dimming via PWM9 (pin 18)
# ================================
PWM_CHIP_PATH = "/sys/class/pwm/pwmchip0"
PWM_CHANNEL = 0
PWM_PERIOD_NS = 1_000_000   

class Backlight:
    def __init__(self, chip_path=PWM_CHIP_PATH, channel=PWM_CHANNEL,
                 period_ns=PWM_PERIOD_NS):
        self.chip_path = chip_path
        self.channel = channel
        self.period_ns = period_ns
        self._pwm_path = f"{chip_path}/pwm{channel}"
        self._brightness = 100
        self._available = False
        self._setup()

    def _write(self, relpath, value):
        with open(f"{self._pwm_path}/{relpath}", "w") as f:
            f.write(str(value))

    def _setup(self):
        try:
            if not os.path.exists(self._pwm_path):
                with open(f"{self.chip_path}/export", "w") as f:
                    f.write(str(self.channel))
                time.sleep(0.05)  
                log("BACKLIGHT", f"exported channel {self.channel} on {self.chip_path}")
            self._write("period", self.period_ns)
            self._write("duty_cycle", 0)   
            self._write("enable", 1)
            self._available = True
            log("BACKLIGHT", f"PWM ready: {self._pwm_path}, period={self.period_ns}ns")
        except Exception as e:
            log("BACKLIGHT", f"WARNING: PWM unavailable ({e})")
            self._available = False

    def set_brightness(self, percent):
        percent = max(0, min(100, percent))
        changed = percent != self._brightness
        self._brightness = percent
        if not self._available:
            return
        duty = int(round((100 - percent) / 100.0 * self.period_ns))
        try:
            self._write("duty_cycle", duty)
            if changed:
                log("BACKLIGHT", f"brightness -> {percent}%  (duty_cycle={duty})")
        except Exception as e:
            log("BACKLIGHT", f"WARNING: failed to set backlight brightness: {e}")

    def get_brightness(self):
        return self._brightness

    def fade_to(self, target_percent, duration_s=0.3, steps=30):
        if not self._available:
            self._brightness = max(0, min(100, target_percent))
            return
        start = self._brightness
        target = max(0, min(100, target_percent))
        if start == target:
            return
        log("BACKLIGHT", f"fade {start}% -> {target}% over {duration_s}s")
        delay = duration_s / max(1, steps)
        for i in range(1, steps + 1):
            self.set_brightness(start + (target - start) * i / steps)
            time.sleep(delay)

    def off(self):
        self.set_brightness(0)

    def on(self, percent=None):
        self.set_brightness(percent if percent is not None else self._brightness or 100)

    def shutdown(self):
        if not self._available:
            return
        try:
            self._write("duty_cycle", 0)
            self._write("enable", 0)
            log("BACKLIGHT", "shutdown: PWM disabled, left at full bright for next boot")
        except Exception as e:
            log("BACKLIGHT", f"WARNING: shutdown cleanup failed: {e}")

backlight = Backlight()
backlight.set_brightness(100)


spi = spidev.SpiDev()
spi.open(SPI_BUS, SPI_DEV)
spi.max_speed_hz = 60_000_000
spi.mode = 0

def dc_command(): dc_line.set_value(0)
def dc_data(): dc_line.set_value(1)

def write_cmd(cmd):
    dc_command()
    spi.xfer2([cmd & 0xFF])

def write_data_byte(b):
    dc_data()
    spi.xfer2([b & 0xFF])

def write_data(bytes_seq):
    dc_data()
    spi.xfer2(list(bytes_seq))

def hardware_reset():
    rst_line.set_value(0)
    time.sleep(0.05)
    rst_line.set_value(1)
    time.sleep(0.15)

_HAS_WRITEBYTES2 = hasattr(spi, "writebytes2")
def spi_write_bulk(buf):
    if _HAS_WRITEBYTES2: spi.writebytes2(buf)
    else:
        chunk = 4096
        for i in range(0, len(buf), chunk): spi.xfer2(list(buf[i:i + chunk]))

def ili9488_init():
    hardware_reset()
    write_cmd(0x01); time.sleep(0.1)
    write_cmd(0x11); time.sleep(0.12)
    write_cmd(0x3A); write_data_byte(0x66)
    write_cmd(0x36); write_data_byte(0x28)  
    write_cmd(0xB1); write_data([0xB0, 0x11])
    write_cmd(0xB4); write_data_byte(0x00)
    write_cmd(0xC0); write_data([0x10, 0x10])
    write_cmd(0xC1); write_data_byte(0x41)
    write_cmd(0xC5); write_data([0x00, 0x22])
    write_cmd(0xE0); write_data([0x00,0x03,0x09,0x08,0x16,0x0A,0x3F,0x78,0x4C,0x09,0x0A,0x08,0x16,0x1A,0x0F])
    write_cmd(0xE1); write_data([0x00,0x16,0x19,0x03,0x0F,0x05,0x32,0x45,0x46,0x04,0x0E,0x0D,0x35,0x37,0x0F])
    write_cmd(0x29); time.sleep(0.05)

def set_address_window(x0, y0, x1, y1):
    write_cmd(0x2A); write_data([(x0 >> 8) & 0xFF, x0 & 0xFF, (x1 >> 8) & 0xFF, x1 & 0xFF])
    write_cmd(0x2B); write_data([(y0 >> 8) & 0xFF, y0 & 0xFF, (y1 >> 8) & 0xFF, y1 & 0xFF])
    write_cmd(0x2C)

def fill_screen_rgb888(r, g, b):
    set_address_window(0, 0, WIDTH - 1, HEIGHT - 1)
    dc_data()
    spi_write_bulk(bytes([r & 0xFF, g & 0xFF, b & 0xFF]) * (WIDTH * HEIGHT))

def blit_rect_buf(x, y, w, h, buf):
    set_address_window(x, y, x + w - 1, y + h - 1)
    dc_data()
    spi_write_bulk(buf)

def make_solid_buf(w, h, r, g, b):
    return bytes([r, g, b]) * (w * h)

def get_duration_mutagen(path):
    if MutagenFile is None: return None
    try:
        audio = MutagenFile(path)
        if audio and hasattr(audio, "info") and hasattr(audio.info, "length"):
            return float(audio.info.length)
    except Exception: pass
    return None

def build_track_from_file(path, default_image_path=None):
    abs_path = os.path.abspath(path)
    title, artist, album = os.path.splitext(os.path.basename(abs_path))[0], "Unknown Artist", "Unknown Album"
    genre = "Unknown Genre"
    duration, embedded_image = 0.0, None

    if MutagenFile is not None:
        try: audio_easy = MutagenFile(abs_path, easy=True)
        except Exception: audio_easy = None

        if audio_easy:
            tags = getattr(audio_easy, "tags", None)
            if tags:
                def _first(key, default):
                    v = tags.get(key)
                    return str(v[0]) if isinstance(v, list) and v else (str(v) if v else default)
                title, artist, album = _first("title", title), _first("artist", artist), _first("album", album)
                genre = _first("genre", genre)
            
            length = getattr(getattr(audio_easy, "info", None), "length", None)
            if length:
                try: duration = float(length)
                except Exception: pass

        try: audio_full = MutagenFile(abs_path)
        except Exception: audio_full = None

        if audio_full:
            tags_full = getattr(audio_full, "tags", None)
            
            # 1. MP3 / ID3v2 (APIC frames)
            if tags_full and hasattr(tags_full, "values"):
                for frame in tags_full.values():
                    if hasattr(frame, "data"):
                        try:
                            embedded_image = Image.open(io.BytesIO(frame.data)).convert("RGB")
                            break
                        except: pass
            
            # 2. FLAC (pictures block)
            if embedded_image is None and hasattr(audio_full, "pictures"):
                for pic in audio_full.pictures:
                    if hasattr(pic, "data"):
                        try:
                            embedded_image = Image.open(io.BytesIO(pic.data)).convert("RGB")
                            break
                        except: pass
                        
            # 3. M4A / MP4 (covr atoms)
            if embedded_image is None and tags_full and hasattr(tags_full, "get"):
                covr = tags_full.get("covr")
                if covr:
                    for item in (covr if isinstance(covr, list) else [covr]):
                        try:
                            embedded_image = Image.open(io.BytesIO(bytes(item))).convert("RGB")
                            break
                        except: pass

    if duration <= 0:
        dur = get_duration_mutagen(abs_path)
        if dur: duration = float(dur)

    img_for_theme = embedded_image
    if img_for_theme is None and default_image_path:
        try: img_for_theme = Image.open(default_image_path).convert("RGB")
        except Exception: pass
    
    return {
        "title": title, "artist": artist, "album": album, "genre": genre,
        "duration": float(duration) if duration else 0.0,
        "image_path": default_image_path, "embedded_image": embedded_image,
        "file_path": abs_path, "theme": extract_track_theme(img_for_theme)
    }

def scan_music_folder(root_folder, default_image_path=None):
    tracks = []
    if not os.path.isdir(root_folder): return tracks
    for dirpath, dirnames, filenames in os.walk(root_folder):
        for name in sorted(filenames):
            if name.lower().endswith((".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav")):
                try: tracks.append(build_track_from_file(os.path.join(dirpath, name), default_image_path))
                except Exception: pass
    return tracks

def draw_text_ttf(x, y, text, fg, bg, font_size=20, max_width=None):
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except Exception: font = ImageFont.load_default()

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1), bg))
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    if max_width is not None and text_w > max_width:
        txt = text
        while len(txt) > 3:
            txt = txt[:-4] + "…"
            bbox = draw.textbbox((0, 0), txt, font=font)
            if (bbox[2] - bbox[0]) <= max_width: break
        bbox = draw.textbbox((0, 0), txt, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        text = txt

    if text_w <= 0 or text_h <= 0: return 0, 0

    img = Image.new("RGB", (text_w, text_h), bg)
    ImageDraw.Draw(img).text((-bbox[0], -bbox[1]), text, font=font, fill=fg)
    blit_rect_buf(x, y, text_w, text_h, img.tobytes())
    return text_w, text_h

def format_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"

class ButtonHandler:
    def __init__(self, *lines):
        self.lines = {"play_pause": lines[0], "next": lines[1], "prev": lines[2], "vol_up": lines[3], "vol_down": lines[4]}
        self.states = {name: {'raw': 1, 'pressed': False, 'start': time.monotonic(), 'long_fired': False, 'repeat': time.monotonic()} for name in self.lines}

    def poll(self):
        clicks, holds, repeats, now = [], [], [], time.monotonic()
        for name, line in self.lines.items():
            state = self.states[name]
            try: val = line.get_value()
            except OSError: continue
            
            if state['raw'] == 1 and val == 0:
                state.update({'start': now, 'pressed': True, 'long_fired': False, 'repeat': now})
            elif state['raw'] == 0 and val == 1:
                if (now - state['start']) >= 0.05 and not state['long_fired']: clicks.append(name)
                state.update({'pressed': False, 'start': 0.0})
            
            if state['pressed'] and val == 0:
                if (now - state['start']) >= 0.5:
                    if not state['long_fired']:
                        state['long_fired'] = True
                        holds.append(name)
                        state['repeat'] = now
                    elif now - state['repeat'] >= 0.15:
                        repeats.append(name)
                        state['repeat'] = now
            state['raw'] = val
        return clicks, holds, repeats

# ================================
# SPOOL ANIMATOR (SINGLE BAR)
# ================================
class SpoolAnimator:
    def __init__(self, size, color, bg_color, num_frames=36):
        self.size = size
        self.num_frames = num_frames
        self.frames = []
        
        for i in range(num_frames):
            angle = (i / num_frames) * 360
            img = Image.new("RGB", (size, size), bg_color)
            draw = ImageDraw.Draw(img)
            
            draw.ellipse([2, 2, size-3, size-3], outline=color, width=2)
            
            cx, cy, r = size/2, size/2, size/2 - 2
            rad = math.radians(angle)
            x1, y1 = cx + r * math.cos(rad), cy + r * math.sin(rad)
            x2, y2 = cx - r * math.cos(rad), cy - r * math.sin(rad)
            draw.line([x1, y1, x2, y2], fill=color, width=2)
            
            inner_r = size * 0.12
            draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill=color)
            
            hole_r = size * 0.04
            draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r], fill=bg_color)
            
            self.frames.append(img.tobytes())
            
    def blit(self, x, y, phase):
        idx = int((phase % 65536) / 65536.0 * self.num_frames) % self.num_frames
        blit_rect_buf(x, y, self.size, self.size, self.frames[idx])

# ================================
# SHUFFLE COMPONENT
# ================================
def draw_shuffle_icon(x, y, size, color, bg, active):
    """
    Draws a small shuffle glyph with supersampled anti-aliasing so the 
    diagonal lines look buttery smooth instead of jagged. Features a 
    professional 'crossover' gap in the center.
    """
    # Determine foreground color (dimmed if inactive)
    fg = color if active else tuple(int(c * 0.2 + bg_c * 0.8) for c, bg_c in zip(color, bg))

    # Supersample scale: draw 4x larger, then resize down for anti-aliasing
    scale = 4
    big_size = size * scale
    img = Image.new("RGB", (big_size, big_size), bg)
    draw = ImageDraw.Draw(img)

    w = big_size
    line_w = 2 * scale

    # Anchor points for the paths
    left_x = w * 0.15
    mid_l  = w * 0.35
    mid_r  = w * 0.65
    right_x = w * 0.80
    top_y  = w * 0.30
    bot_y  = w * 0.70

    # Draw path 1: Top-Left to Bottom-Right
    draw.line([(left_x, top_y), (mid_l, top_y), (mid_r, bot_y), (right_x, bot_y)], fill=fg, width=line_w, joint="curve")
    
    # Erase a small circle in the middle to create the overlapping gap
    gap_r = line_w * 1.5
    draw.ellipse([w*0.5 - gap_r, w*0.5 - gap_r, w*0.5 + gap_r, w*0.5 + gap_r], fill=bg)

    # Draw path 2: Bottom-Left to Top-Right
    draw.line([(left_x, bot_y), (mid_l, bot_y), (mid_r, top_y), (right_x, top_y)], fill=fg, width=line_w, joint="curve")

    # Arrowheads
    ah_l = w * 0.18  
    ah_h = w * 0.14  
    
    # Bottom-right arrowhead
    draw.polygon([
        (right_x + ah_l*0.6, bot_y),
        (right_x - ah_l*0.4, bot_y - ah_h),
        (right_x - ah_l*0.4, bot_y + ah_h)
    ], fill=fg)
    
    # Top-right arrowhead
    draw.polygon([
        (right_x + ah_l*0.6, top_y),
        (right_x - ah_l*0.4, top_y - ah_h),
        (right_x - ah_l*0.4, top_y + ah_h)
    ], fill=fg)

    # Scale down to original size using Lanczos for perfect anti-aliasing
    img = img.resize((size, size), Image.LANCZOS)

    blit_rect_buf(x, y, size, size, img.tobytes())

# ================================
# MENU & LIBRARY SYSTEMS
# ================================
MENU_BG = (24, 24, 30)
MENU_TEXT = (240, 240, 245)
MENU_SUBTEXT = (150, 155, 165)
MENU_ACCENT = (232, 106, 38)
MENU_HILITE_BG = (50, 50, 60)

ROW_H = 42
HEADER_H = 50
SECTION_MARKER = "__section__"
SECTION_H = 26   

def truncate_text(draw, text, font, max_width):
    if not text: return ""
    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_width: return text
    txt = text
    while len(txt) > 1:
        txt = txt[:-1]
        bbox = draw.textbbox((0, 0), txt + "…", font=font)
        if bbox[2] - bbox[0] <= max_width:
            return txt + "…"
    return txt + "…"

def render_wifi_setup_screen(ssid, password, url):
    """
    Shown when the device has no WiFi yet and is running its own setup
    hotspot. This is the one screen a non-technical person needs to be able
    to follow with zero prior context: join a named WiFi network on their
    phone (exactly like joining any other WiFi network), then open one URL.
    No QR code, no app -- just the two pieces of information needed, drawn
    large and unambiguous.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        value_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
        hint_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except Exception:
        title_font = label_font = value_font = hint_font = ImageFont.load_default()

    draw.text((20, 16), "Set up Wi-Fi", font=title_font, fill=MENU_TEXT)
    draw.rectangle([20, 50, 60, 52], fill=MENU_ACCENT)

    draw.text((20, 76), "1. On your phone, join this Wi-Fi network:", font=label_font, fill=MENU_SUBTEXT)
    draw.text((20, 96), ssid, font=value_font, fill=MENU_ACCENT)
    draw.text((20, 130), f"Password: {password}", font=label_font, fill=MENU_TEXT)

    draw.text((20, 172), "2. Then open this address in a browser:", font=label_font, fill=MENU_SUBTEXT)
    draw.text((20, 192), url, font=value_font, fill=MENU_ACCENT)

    draw.text((20, HEIGHT - 30), "Waiting for connection…", font=hint_font, fill=MENU_SUBTEXT)
    return img


def run_wifi_setup_screen(buttons, get_status_fn, timeout_check_interval=2.0):
    """
    Displays render_wifi_setup_screen and polls get_status_fn() (expected to
    return True once a real WiFi connection is established) so this screen
    naturally hands off to normal boot the moment setup succeeds -- no
    button press required, since the person doing this might be doing it
    entirely from their phone with the device just sitting there.
    """
    img = render_wifi_setup_screen(HOTSPOT_SSID, HOTSPOT_PASSWORD,
                                    f"http://{HOTSPOT_GATEWAY_IP}:8080")
    blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
    last_check = time.monotonic()
    while True:
        now = time.monotonic()
        if now - last_check > timeout_check_interval:
            if get_status_fn():
                log("WIFI", "setup complete, connection established")
                return
            last_check = now
        clicks, holds, repeats = buttons.poll()
        if holds:
            # Allow a deliberate hold to skip setup and go straight to the
            # library anyway (e.g. someone who genuinely only wants local
            # playback and never intends to connect this device to WiFi).
            log("WIFI", "setup screen skipped by user (held button)")
            return
        time.sleep(0.1)


def render_list_screen(title, items, selected_index, footer=None):
    """
    Standard full-width list menu, used for Settings, Bluetooth, and root Menu.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
        sub_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
        section_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
    except Exception:
        title_font = row_font = sub_font = section_font = ImageFont.load_default()

    draw.text((20, 14), title, font=title_font, fill=MENU_TEXT)
    draw.rectangle([0, HEADER_H - 1, WIDTH, HEADER_H - 1], fill=(50, 50, 58))
    draw.rectangle([20, HEADER_H - 4, 60, HEADER_H - 2], fill=MENU_ACCENT)

    heights = [SECTION_H if it[0] == SECTION_MARKER else ROW_H for it in items]
    sel_positions = [i for i, it in enumerate(items) if it[0] != SECTION_MARKER]

    avail_h = HEIGHT - HEADER_H - (28 if footer else 6)
    target_item_idx = sel_positions[selected_index] if sel_positions else 0
    scroll_px = 0
    if sum(heights) > avail_h:
        y_of_target = sum(heights[:target_item_idx])
        scroll_px = max(0, min(y_of_target - avail_h // 2, sum(heights) - avail_h))

    thumb_size = ROW_H - 8
    text_x_with_thumb = 24 + thumb_size + 12

    y = HEADER_H - scroll_px
    for i, it in enumerate(items):
        h = heights[i]
        if y + h <= HEADER_H:
            y += h
            continue
        if y >= HEIGHT - (28 if footer else 0):
            break

        if y < HEADER_H:
            y += h
            continue

        if it[0] == SECTION_MARKER:
            draw.text((24, y + 6), it[1], font=section_font, fill=MENU_ACCENT)
        else:
            label = it[0]
            subtext = it[1] if len(it) > 1 else None
            thumb = it[2] if len(it) > 2 else None

            is_sel = (i == target_item_idx)
            if is_sel:
                draw.rectangle([0, y, WIDTH, y + h - 1], fill=MENU_HILITE_BG)
                draw.rectangle([0, y, 4, y + h - 1], fill=MENU_ACCENT)

            text_color = MENU_TEXT if is_sel else MENU_SUBTEXT
            if thumb is not None:
                thumb_y = y + (h - thumb_size) // 2
                img.paste(thumb, (24, thumb_y))
                text_x = text_x_with_thumb
            else:
                text_x = 24

            ty = y + (8 if subtext else 12)
            draw.text((text_x, ty), label, font=row_font, fill=text_color)
            if subtext:
                draw.text((text_x, ty + 19), subtext, font=sub_font, fill=MENU_SUBTEXT)
        y += h

    if footer:
        draw.rectangle([0, HEIGHT - 26, WIDTH, HEIGHT - 26], fill=(50, 50, 58))
        draw.text((20, HEIGHT - 21), footer, font=sub_font, fill=MENU_SUBTEXT)

    return img

def render_split_library_screen(title, items, selected_index):
    """
    Concept 2: Split View (iPod Style) for the Library.
    Left side (50%): Text list of tracks/albums/artists.
    Right side (50%): Deep context preview with large 140x140 album art and metadata.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        big_title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        sub_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        meta_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 12)
    except Exception:
        title_font = row_font = sub_font = meta_font = big_title_font = ImageFont.load_default()

    half_w = 240
    header_h = 40
    row_h = 44

    # --- LEFT PANE ---
    draw.rectangle([0, 0, half_w, header_h], fill=(30, 30, 36))
    draw.text((16, 12), title, font=title_font, fill=MENU_TEXT)
    draw.line([(0, header_h), (half_w, header_h)], fill=(50, 50, 58), width=1)

    avail_h = HEIGHT - header_h
    target_y = selected_index * row_h
    scroll_y = max(0, min(target_y - avail_h // 2 + (row_h // 2), len(items) * row_h - avail_h)) if len(items) * row_h > avail_h else 0

    start_idx = scroll_y // row_h
    end_idx = min(len(items), (scroll_y + avail_h) // row_h + 1)

    for i in range(start_idx, end_idx):
        y = header_h + (i * row_h) - scroll_y
        
        # Don't draw above header
        if y < header_h: 
            continue

        is_sel = (i == selected_index)
        if is_sel:
            label, rep_track, subtext, meta = items[i]
            ref_track = rep_track[0] if isinstance(rep_track, list) else rep_track
            highlight_color = ref_track["theme"]["accent"] if ref_track and "theme" in ref_track else MENU_ACCENT
            draw.rectangle([0, y, half_w, y + row_h], fill=highlight_color)
            text_col = (255, 255, 255)
        else:
            text_col = MENU_TEXT
        
        label_trunc = truncate_text(draw, items[i][0], row_font, half_w - 32)
        draw.text((16, y + 13), label_trunc, font=row_font, fill=text_col)

    # --- RIGHT PANE ---
    if items and selected_index < len(items):
        label, rep_track, subtext, meta = items[selected_index]
        ref_track = rep_track[0] if isinstance(rep_track, list) else rep_track
        
        # Dynamic right pane background based on highlighted track's theme!
        if ref_track and "theme" in ref_track:
            right_bg = ref_track["theme"]["bg"]
            accent_col = ref_track["theme"]["accent"]
        else:
            right_bg = (20, 20, 25)
            accent_col = MENU_ACCENT
    else:
        right_bg = (20, 20, 25)
        accent_col = MENU_ACCENT

    draw.rectangle([half_w, 0, WIDTH, HEIGHT], fill=right_bg)
    draw.line([(half_w, 0), (half_w, HEIGHT)], fill=(50, 50, 58), width=1)

    if items and selected_index < len(items):
        label, rep_track, subtext, meta = items[selected_index]
        
        # Pre-calculate center constraints
        cx = half_w + (half_w // 2)
        art_size = 140
        art_y = 40
        art_x = cx - (art_size // 2)

        # 1. Drop Shadow (offset +8px)
        sh_off, b_th = 8, 2
        sh_col = (int(right_bg[0]*0.2), int(right_bg[1]*0.2), int(right_bg[2]*0.2))
        draw.rectangle([art_x - b_th + sh_off, art_y - b_th + sh_off, 
                        art_x + art_size + b_th + sh_off, art_y + art_size + b_th + sh_off], fill=sh_col)
        
        # 2. Subtle Dark Border
        b_col = (15, 15, 20)
        draw.rectangle([art_x - b_th, art_y - b_th, 
                        art_x + art_size + b_th, art_y + art_size + b_th], fill=b_col)

        # 3. Fetch & Paste Artwork
        if isinstance(rep_track, list):
            thumb = _get_collage_thumbnail(rep_track, art_size)
        else:
            thumb = _get_thumbnail(rep_track, art_size)

        if thumb:
            img.paste(thumb, (art_x, art_y))
        else:
            draw.rectangle([art_x, art_y, art_x + art_size, art_y + art_size], fill=(100,100,100))

        # 4. Metadata
        text_y = art_y + art_size + 24
        
        # Main Title (Truncated & Centered)
        t_text = truncate_text(draw, label, big_title_font, half_w - 32)
        t_w = draw.textbbox((0,0), t_text, font=big_title_font)[2]
        draw.text((cx - t_w//2, text_y), t_text, font=big_title_font, fill=(255,255,255))
        
        # Subtext (Artist/Count)
        if subtext:
            s_text = truncate_text(draw, subtext, sub_font, half_w - 32)
            s_w = draw.textbbox((0,0), s_text, font=sub_font)[2]
            draw.text((cx - s_w//2, text_y + 24), s_text, font=sub_font, fill=MENU_SUBTEXT)
            
        # Tech Info / Meta (Duration)
        if meta:
            m_text = truncate_text(draw, meta, meta_font, half_w - 32)
            m_w = draw.textbbox((0,0), m_text, font=meta_font)[2]
            draw.text((cx - m_w//2, HEIGHT - 28), m_text, font=meta_font, fill=accent_col)

    return img

def _bt_device_label(dev):
    name = dev["name"]
    flags = []
    if dev["connected"]:
        flags.append("Connected")
    elif dev["paired"]:
        flags.append("Paired")
    sub = " · ".join(flags) if flags else None
    if dev.get("rssi") is not None:
        sub = f"{sub} · {dev['rssi']} dBm" if sub else f"{dev['rssi']} dBm"
    return name, sub


_WPCTL_TIMEOUT_S = 2.0  

def get_active_bt_codec():
    try:
        status = subprocess.run(
            ["wpctl", "status"], capture_output=True, text=True,
            timeout=_WPCTL_TIMEOUT_S,
        )
        if status.returncode != 0:
            return "Unknown (wpctl unavailable)"

        sink_id = None
        in_sinks = False
        for line in status.stdout.splitlines():
            stripped = line.strip()
            if "Sinks:" in stripped:
                in_sinks = True
                continue
            if in_sinks:
                if "Sink endpoints:" in stripped or "Sources:" in stripped:
                    break
                if "*" in line:
                    after_star = line.split("*", 1)[1].strip()
                    sink_id = after_star.split(".", 1)[0].strip()
                    break

        if not sink_id:
            return "No device connected"

        inspect = subprocess.run(
            ["wpctl", "inspect", sink_id], capture_output=True, text=True,
            timeout=_WPCTL_TIMEOUT_S,
        )
        if inspect.returncode != 0:
            return "Unknown"

        for line in inspect.stdout.splitlines():
            if "api.bluez5.codec" in line:
                codec = line.split("=", 1)[1].strip().strip('"')
                return codec.upper()

        return "Not a Bluetooth device"

    except FileNotFoundError:
        return "Unknown (wpctl not installed)"
    except subprocess.TimeoutExpired:
        return "Unknown (timed out)"
    except Exception as e:
        log("BT", f"codec lookup failed: {e}")
        return "Unknown"

def _is_named(dev):
    return dev["name"] != dev["address"]

def run_bluetooth_screen(bt, buttons):
    log("BT", "entered Bluetooth screen, starting scan")
    show_all = False
    selected = 0
    status_msg = None
    last_action_addr = None

    bt.start_scan()
    last_redraw = 0.0

    while True:
        now = time.monotonic()

        for kind, payload in bt.get_events():
            addr = payload.get("address") if isinstance(payload, dict) else None
            log("BT", f"event: {kind} {payload}")
            if addr != last_action_addr:
                continue
            if kind == "pair_result":
                status_msg = "Paired!" if payload["ok"] else f"Pair failed: {payload.get('error', '?')}"
            elif kind == "connect_result":
                status_msg = "Connected!" if payload["ok"] else f"Connect failed: {payload.get('error', '?')}"
                if payload["ok"]:
                    log("BT", f"auto-trusting {last_action_addr} after successful connect")
                    bt.trust(last_action_addr)
            elif kind == "disconnect_result":
                status_msg = "Disconnected" if payload["ok"] else f"Disconnect failed: {payload.get('error', '?')}"
            elif kind == "remove_result":
                status_msg = "Forgotten" if payload["ok"] else f"Remove failed: {payload.get('error', '?')}"

        all_discovered = bt.get_discovered_devices()
        paired = sorted([d for d in all_discovered if d["paired"]],
                         key=lambda d: (not d["connected"], d["name"]))

        available = [d for d in all_discovered if not d["paired"]]
        if not show_all:
            available = [d for d in available if _is_named(d)]
        available.sort(key=lambda d: -(d["rssi"] or -999))

        items = []
        selectable_devices = []

        items.append((SECTION_MARKER, "PAIRED"))
        if paired:
            for d in paired:
                items.append(_bt_device_label(d))
                selectable_devices.append(d)
                items.append((f"  Forget {d['name']}", None))
                selectable_devices.append(("forget", d["address"]))
        else:
            items.append(("No paired devices yet", None))
            selectable_devices.append(None)

        items.append((SECTION_MARKER,
                       f"AVAILABLE{'  (scanning…)' if bt.is_scanning() else ''}"))
        for d in available:
            items.append(_bt_device_label(d))
            selectable_devices.append(d)

        toggle_label = f"Show all devices: {'ON' if show_all else 'OFF'}"
        items.append((toggle_label, None))
        selectable_devices.append("toggle_show_all")

        rescan_label = "Scanning…" if bt.is_scanning() else "Scan again"
        items.append((rescan_label, None))
        selectable_devices.append("rescan")

        selected = max(0, min(selected, len(selectable_devices) - 1))

        if now - last_redraw > 0.2:
            footer = status_msg or "Hold Prev: back   Play: select"
            img = render_list_screen("Bluetooth", items, selected, footer=footer)
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now

        clicks, holds, repeats = buttons.poll()

        for ev in holds:
            if ev in ("prev", "play_pause"):
                log("BT", "leaving Bluetooth screen, stopping scan")
                bt.stop_scan()
                return

        for ev in clicks:
            if ev == "next":
                selected = min(selected + 1, len(selectable_devices) - 1)
                status_msg = None
            elif ev == "prev":
                selected = max(selected - 1, 0)
                status_msg = None
            elif ev == "play_pause":
                target = selectable_devices[selected]
                if target is None:
                    pass  
                elif target == "toggle_show_all":
                    show_all = not show_all
                    selected = 0
                    log("BT", f"show_all toggled -> {show_all}")
                elif target == "rescan":
                    log("BT", "manual rescan requested")
                    bt.start_scan()
                    status_msg = "Scanning…"
                elif isinstance(target, tuple) and target[0] == "forget":
                    addr = target[1]
                    last_action_addr = addr
                    status_msg = "Forgetting…"
                    log("BT", f"forgetting device {addr}")
                    bt.remove(addr)
                else:
                    dev = target
                    last_action_addr = dev["address"]
                    if dev["connected"]:
                        status_msg = "Disconnecting…"
                        log("BT", f"disconnecting {dev['address']} ({dev['name']})")
                        bt.disconnect(dev["address"])
                    elif dev["paired"]:
                        status_msg = "Connecting…"
                        log("BT", f"connecting {dev['address']} ({dev['name']})")
                        bt.connect(dev["address"])
                    else:
                        status_msg = "Pairing…"
                        log("BT", f"pairing {dev['address']} ({dev['name']})")
                        bt.pair(dev["address"])

        time.sleep(0.02)


_thumb_cache = {}

def _get_thumbnail(track, size):
    """Square RGB thumbnail for a track's art, resized once and cached by
    (id of source image, size) -- cheap to reuse across rows/redraws rather
    than re-resizing the same art on every 0.15s Library redraw."""
    if track is None:
        return None
    src = track.get("embedded_image")
    if src is None and track.get("image_path"):
        try:
            src = Image.open(track["image_path"]).convert("RGB")
        except Exception:
            src = None
    if src is None:
        return None

    key = (id(src), size)
    cached = _thumb_cache.get(key)
    if cached is not None:
        return cached

    try:
        thumb = src.copy()
        w, h = thumb.size
        side = min(w, h)
        thumb = thumb.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        thumb = thumb.resize((size, size), Image.LANCZOS)
        _thumb_cache[key] = thumb
        return thumb
    except Exception:
        return None

def _get_collage_thumbnail(tracks, size):
    """Creates a collage of up to 4 album covers. Splits in half for 2."""
    if not tracks: return None
    
    key = ("collage", tuple(t["file_path"] for t in tracks[:4]), size)
    if key in _thumb_cache: 
        return _thumb_cache[key]

    collage = Image.new("RGB", (size, size), (20, 20, 25))
    half = size // 2

    # If exactly 2 albums, do a clean 50/50 vertical split
    if len(tracks) == 2:
        th1 = _get_thumbnail(tracks[0], size)
        th2 = _get_thumbnail(tracks[1], size)
        if th1 and th2:
            # Paste left half of Album 1, right half of Album 2
            collage.paste(th1.crop((0, 0, half, size)), (0, 0))
            collage.paste(th2.crop((half, 0, size, size)), (half, 0))
            
            # Draw single dark line down the middle
            draw = ImageDraw.Draw(collage)
            draw.line([(half, 0), (half, size)], fill=(15, 15, 20), width=2)
            
            _thumb_cache[key] = collage
            return collage

    # 3 or 4 albums -> 2x2 Grid
    thumbs = []
    for t in tracks[:4]:
        th = _get_thumbnail(t, half)
        if th: thumbs.append(th)
        
    if not thumbs:
        return None

    positions = [(0, 0), (half, 0), (0, half), (half, half)]
    for i in range(4):
        th = thumbs[i % len(thumbs)]
        collage.paste(th, positions[i])

    draw = ImageDraw.Draw(collage)
    draw.line([(half, 0), (half, size)], fill=(15, 15, 20), width=2)
    draw.line([(0, half), (size, half)], fill=(15, 15, 20), width=2)

    _thumb_cache[key] = collage
    return collage

def _group_tracks(tracks):
    by_artist = {}
    for i, t in enumerate(tracks):
        artist = t.get("artist") or "Unknown Artist"
        album = t.get("album") or "Unknown Album"
        by_artist.setdefault(artist, {}).setdefault(album, []).append((i, t))
    return by_artist

def run_library_screen(tracks, buttons, bt=None, upload_srv=None):
    """
    Browse Artist -> Album -> Track using the Concept 2 Split View.
    Returns the chosen track's global index, or None if the user backed out
    without choosing a track.

    Holding Play/Pause opens Settings directly from here (same gesture as
    during playback) -- this used to just exit Library with None, which on
    a fresh boot (nothing playing yet) meant Settings was unreachable until
    you'd already started a track. bt/upload_srv are optional so this still
    works in contexts that don't have them, just without the Settings hold.
    """
    log("MENU", "entered Library (Split View)")
    by_artist = _group_tracks(tracks)
    artists = sorted(by_artist.keys())

    level = "artist"
    cur_artist, cur_album = None, None
    selected = 0
    last_redraw = 0.0

    while True:
        items = []
        if level == "artist":
            title = "Artists"
            for a in artists:
                albums_dict = by_artist[a]
                count = sum(len(v) for v in albums_dict.values())
                first_album = sorted(albums_dict.keys())[0]
                _, rep_track = albums_dict[first_album][0]
                
                # Fetch representing track list for artists to construct a dynamic collage
                rep_list = []
                for alb in sorted(albums_dict.keys()):
                    rep_list.append(albums_dict[alb][0][1])

                # Genre shown is the representative track's genre -- lets you
                # pick an artist by mood/genre at a glance, not just by name.
                genre = rep_track.get("genre") or "Unknown Genre"

                # Format: (List Label, Representative Track(s), Right Pane Subtext, Right Pane Meta)
                items.append((a, rep_list if len(rep_list) > 1 else rep_track, f"{count} track(s)", genre))
        
        elif level == "album":
            title = cur_artist
            albums = sorted(by_artist[cur_artist].keys())
            for al in albums:
                track_list = by_artist[cur_artist][al]
                _, rep_track = track_list[0]
                genre = rep_track.get("genre") or "Unknown Genre"
                items.append((al, rep_track, f"{len(track_list)} track(s)", f"{cur_artist} · {genre}"))
        
        else:  # level == "track"
            title = cur_album
            track_list = by_artist[cur_artist][cur_album]
            for _, t in track_list:
                t_title = t.get("title") or os.path.basename(t["file_path"])
                dur = format_time(t.get("duration", 0))
                items.append((t_title, t, t.get("artist", "Unknown Artist"), dur))

        selected = max(0, min(selected, len(items) - 1)) if items else 0

        now = time.monotonic()
        if now - last_redraw > 0.15:
            img = render_split_library_screen(title, items or [("(empty)", None, "", "")], selected)
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now

        clicks, holds, repeats = buttons.poll()
        for ev in holds:
            if ev == "play_pause":
                if bt is not None or upload_srv is not None:
                    log("MENU", "Library -> Settings (hold Play)")
                    run_settings_screen(bt, upload_srv, buttons)
                    log("MENU", "back in Library (from Settings)")
                    last_redraw = 0.0
                else:
                    log("MENU", "exiting Library (hold Play, no Settings available)")
                    return None
            if ev == "prev":
                if level == "track":
                    level, selected = "album", 0
                    last_redraw = 0.0
                elif level == "album":
                    level, selected = "artist", 0
                    last_redraw = 0.0
                else:
                    log("MENU", "exiting Library (hold Prev at top)")
                    return None

        for ev in clicks:
            if not items:
                continue
            if ev == "next":
                selected = min(selected + 1, len(items) - 1)
                last_redraw = 0.0
            elif ev == "prev":
                selected = max(selected - 1, 0)
                last_redraw = 0.0
            elif ev == "play_pause":
                if level == "artist":
                    cur_artist, level, selected = artists[selected], "album", 0
                    last_redraw = 0.0
                elif level == "album":
                    albums = sorted(by_artist[cur_artist].keys())
                    cur_album, level, selected = albums[selected], "track", 0
                    last_redraw = 0.0
                else:
                    chosen_index, _ = by_artist[cur_artist][cur_album][selected]
                    log("MENU", f"Library: picked track index {chosen_index}")
                    return chosen_index
        time.sleep(0.02)


def run_settings_screen(bt, upload_srv, buttons):
    log("MENU", "entered Settings")
    if bt is not None:
        bt_item = ("Bluetooth", "Pair & connect devices")
    else:
        bt_item = ("Bluetooth", "Unavailable on this device")

    selected = 0
    last_redraw = 0.0
    codec_status = get_active_bt_codec()
    last_codec_check = time.monotonic()
    upload_error = None
    wifi_status = wifi_get_status()
    last_wifi_check = time.monotonic()
    log("BT", f"initial codec check: {codec_status}")
    log("WIFI", f"initial status: {wifi_status}")

    while True:
        now_check = time.monotonic()
        if now_check - last_codec_check > 3.0:
            new_status = get_active_bt_codec()
            if new_status != codec_status:
                log("BT", f"codec status changed: {codec_status} -> {new_status}")
            codec_status = new_status
            last_codec_check = now_check

        if now_check - last_wifi_check > 5.0:
            new_wifi = wifi_get_status()
            if new_wifi != wifi_status:
                log("WIFI", f"status changed: {wifi_status} -> {new_wifi}")
            wifi_status = new_wifi
            last_wifi_check = now_check

        if upload_srv.is_running():
            upload_item = ("Upload Server: ON",
                           f"{upload_srv.get_url_hint()}  pass: {upload_srv.password}")
        elif upload_error:
            upload_item = ("Upload Server: OFF", upload_error)
        else:
            upload_item = ("Upload Server: OFF", "Select to enable Wi-Fi music upload")

        if wifi_status["connected"]:
            wifi_sub = f'{wifi_status["ssid"]}  ({wifi_status["signal"]}%)'
            if upload_srv.is_running():
                wifi_sub += " · join/switch via Upload Server URL > Wi-Fi"
        else:
            wifi_sub = "Not connected"

        items = [
            bt_item,
            ("Brightness", f"{backlight.get_brightness()}%  (Vol +/- to adjust)"),
            ("Screen Off", "Tap any button to wake"),
            ("Audio Codec", f"{codec_status}  (read-only)"),
            upload_item,
            ("Wi-Fi", wifi_sub),
        ]
        selected = max(0, min(selected, len(items) - 1))

        now = time.monotonic()
        if now - last_redraw > 0.15:  
            # Settings still uses the standard full-width list menu
            img = render_list_screen("Settings", items, selected,         
                                      footer="Hold Prev: back   Play: select")
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now

        clicks, holds, repeats = buttons.poll()
        for ev in holds:
            if ev in ("prev", "play_pause"):
                log("MENU", "exiting Settings (hold)")
                return

        if selected == 1:
            for ev in clicks + repeats:
                if ev == "vol_up":
                    backlight.set_brightness(backlight.get_brightness() + 5)
                elif ev == "vol_down":
                    backlight.set_brightness(backlight.get_brightness() - 5)

        for ev in clicks:
            if ev == "next":
                selected = min(selected + 1, len(items) - 1)
            elif ev == "prev":
                selected = max(selected - 1, 0)
            elif ev == "play_pause":
                if selected == 0 and bt is not None:
                    log("MENU", "Settings -> Bluetooth")
                    run_bluetooth_screen(bt, buttons)
                    log("MENU", "back in Settings (from Bluetooth)")
                elif selected == 2:
                    log("MENU", "Settings -> Screen Off")
                    run_screen_off(buttons)
                    log("MENU", "back in Settings (from Screen Off)")
                elif selected == 4:
                    if upload_srv.is_running():
                        log("UPLOAD", "stopping upload server (user toggle)")
                        upload_srv.stop()
                        upload_error = None
                    else:
                        log("UPLOAD", "starting upload server (user toggle)")
                        try:
                            upload_srv.start()
                            upload_error = None
                        except UploadServerUnavailable as e:
                            log("UPLOAD", f"start failed: {e}")
                            upload_error = "Flask not installed (see log)" \
                                if "Flask isn't installed" in str(e) \
                                else "Couldn't start (see log)"
        time.sleep(0.02)


def run_screen_off(buttons):
    prev_brightness = backlight.get_brightness()
    log("BACKLIGHT", f"screen off requested (was {prev_brightness}%)")
    backlight.fade_to(0, duration_s=0.25)
    try:
        while True:
            clicks, holds, repeats = buttons.poll()
            if clicks or holds:
                log("BACKLIGHT", f"wake button detected ({(clicks or holds)[0]}), restoring screen")
                break
            time.sleep(0.05)
    finally:
        backlight.fade_to(prev_brightness, duration_s=0.25)
        log("BACKLIGHT", f"screen restored to {prev_brightness}%")


def run_menu(bt, upload_srv, tracks, buttons):
    log("MENU", "opened (held Play/Pause)")
    items = [("Library", "Browse by artist"), ("Settings", "Bluetooth & more")]
    selected = 0
    last_redraw = 0.0

    while True:
        now = time.monotonic()
        if now - last_redraw > 0.2:
            img = render_list_screen("Menu", items, selected,
                                      footer="Hold Play: resume   Play: select")
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now

        clicks, holds, repeats = buttons.poll()
        for ev in holds:
            if ev in ("prev", "play_pause"):
                log("MENU", "closed, resuming playback")
                return None
        for ev in clicks:
            if ev == "next":
                selected = min(selected + 1, len(items) - 1)
            elif ev == "prev":
                selected = max(selected - 1, 0)
            elif ev == "play_pause":
                if selected == 0:
                    log("MENU", "Menu -> Library")
                    chosen = run_library_screen(tracks, buttons, bt, upload_srv)
                    if chosen is not None:
                        return chosen
                    log("MENU", "back in Menu (from Library)")
                elif selected == 1:
                    log("MENU", "Menu -> Settings")
                    run_settings_screen(bt, upload_srv, buttons)
                    log("MENU", "back in Menu (from Settings)")
        time.sleep(0.02)


class ShuffleState:
    def __init__(self, num_tracks):
        self.num_tracks = num_tracks
        self.active = False
        self.history = []     
        self.hist_pos = -1    
        self._order = []      

    def _reshuffle(self, exclude_index=None):
        import random
        order = list(range(self.num_tracks))
        if exclude_index is not None and exclude_index in order and self.num_tracks > 1:
            order.remove(exclude_index)
        random.shuffle(order)
        self._order = order
        log("SHUFFLE", f"reshuffled new cycle of {len(order)} tracks "
                        f"(excluded {exclude_index})")

    def toggle(self, current_index):
        self.active = not self.active
        if self.active:
            self.history = [current_index]
            self.hist_pos = 0
            self._reshuffle(exclude_index=current_index)
        log("SHUFFLE", f"{'ON' if self.active else 'OFF'} (current track index {current_index})")
        return self.active

    def next_index(self, current_index):
        if self.hist_pos < len(self.history) - 1:
            self.hist_pos += 1
            return self.history[self.hist_pos]

        if not self._order:
            self._reshuffle(exclude_index=current_index)
            if not self._order:
                return current_index
        nxt = self._order.pop(0)
        self.history.append(nxt)
        self.hist_pos = len(self.history) - 1
        return nxt

    def prev_index(self, current_index):
        if self.hist_pos > 0:
            self.hist_pos -= 1
            return self.history[self.hist_pos]
        return current_index 


def draw_background_and_layout(track):
    theme = track.get("theme")
    bg = theme["bg"]
    fill_screen_rgb888(*bg)

    album_size = 160
    album_x = (WIDTH - album_size) // 2 
    album_y = 25 

    spool_size = 48
    spool_y = album_y + album_size + 25 
    spool1_x = 180
    spool2_x = 252
    
    tape_x = 204
    tape_y = spool_y + (spool_size // 2) - 1
    tape_w = 72
    tape_h = 2

    layout = {
        "bg": bg, "theme": theme, 
        "album_size": album_size, "album_x": album_x, "album_y": album_y,
        "spool1_x": spool1_x, "spool2_x": spool2_x, "spool_y": spool_y, "spool_size": spool_size,
        "tape_x": tape_x, "tape_y": tape_y, "tape_w": tape_w, "tape_h": tape_h,
        "animator": SpoolAnimator(spool_size, theme["accent"], bg),
        "shuffle_icon_x": WIDTH - 20 - 22, "shuffle_icon_y": 16, "shuffle_icon_size": 22,
        "shuffle_active": False,
    }

    pil_img = track.get("embedded_image")
    if not pil_img and track.get("image_path"):
        try: pil_img = Image.open(track["image_path"])
        except Exception: pass
    if not pil_img:
        pil_img = Image.new("RGB", (album_size, album_size), (100, 100, 100))

    if pil_img.mode != "RGB": pil_img = pil_img.convert("RGB")
    w, h = pil_img.size
    if w != h:
        side = min(w, h)
        pil_img = pil_img.crop(((w - side)//2, (h - side)//2, (w + side)//2, (h + side)//2))
    pil_img = pil_img.resize((album_size, album_size), Image.LANCZOS)

    pad = 20 
    asm_size = album_size + pad * 2
    asm_img = Image.new("RGB", (asm_size, asm_size), bg)

    shadow_mask = Image.new("RGBA", (asm_size, asm_size), (0,0,0,0))
    shadow_draw = ImageDraw.Draw(shadow_mask)
    sh_off, b_th = 8, 2
    sx, sy = pad + sh_off, pad + sh_off
    shadow_draw.rectangle([sx, sy, sx + album_size + b_th*2 - 1, sy + album_size + b_th*2 - 1], fill=(0,0,0, 160))
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(6))
    asm_img.paste(shadow_mask, (0,0), shadow_mask)

    bx, by = pad, pad
    b_col = (15, 15, 20)
    ImageDraw.Draw(asm_img).rectangle([bx, by, bx + album_size + b_th*2 - 1, by + album_size + b_th*2 - 1], fill=b_col)
    asm_img.paste(pil_img, (bx + b_th, by + b_th))

    layout["asm_img"] = asm_img
    layout["asm_x"] = album_x - pad
    layout["asm_y"] = album_y - pad
    layout["asm_size"] = asm_size

    blit_rect_buf(layout["asm_x"], layout["asm_y"], asm_size, asm_size, asm_img.tobytes())

    title_y = 270
    artist_y = title_y + 24
    time_y = 280

    draw_text_ttf(20, title_y, track.get("title", "Unknown Title"), theme["text"], bg, font_size=20, max_width=200)
    draw_text_ttf(20, artist_y, track.get("artist", "Unknown Artist"), theme["subtext"], bg, font_size=16, max_width=200)

    layout["duration_secs"] = int(track.get("duration", 4 * 60 + 12))
    layout["time_y"] = time_y

    track_color = (int(bg[0]*0.5), int(bg[1]*0.5), int(bg[2]*0.5))
    blit_rect_buf(tape_x, tape_y, tape_w, tape_h, make_solid_buf(tape_w, tape_h, *track_color))
    layout["track_color"] = track_color

    vol_w, vol_h = 180, 10
    layout.update({
        "vol_x": (WIDTH - vol_w) // 2, "vol_y": album_y + (album_size // 2) - 5, 
        "vol_w": vol_w, "vol_h": vol_h, "last_vol": -1.0
    })

    draw_shuffle_icon(layout["shuffle_icon_x"], layout["shuffle_icon_y"],
                       layout["shuffle_icon_size"], theme["accent"], bg, active=False)
    return layout

def update_shuffle_icon(layout, active):
    """Redraw the shuffle icon only if its on/off state actually changed —
    avoids an SPI write every frame for something that changes rarely."""
    if layout.get("shuffle_active") == active:
        return
    layout["shuffle_active"] = active
    draw_shuffle_icon(layout["shuffle_icon_x"], layout["shuffle_icon_y"],
                       layout["shuffle_icon_size"], layout["theme"]["accent"],
                       layout["bg"], active=active)
    log("PLAYBACK", f"shuffle icon redrawn: {'ON' if active else 'OFF'}")

def update_progress_bar(layout, progress):
    progress = max(0.0, min(1.0, progress))
    tx, ty, tw, th = layout["tape_x"], layout["tape_y"], layout["tape_w"], layout["tape_h"]
    
    filled_w = int(tw * progress)
    if filled_w > 0:
        blit_rect_buf(tx, ty, filled_w, th, make_solid_buf(filled_w, th, *layout["theme"]["accent"]))
    if filled_w < tw:
        blit_rect_buf(tx + filled_w, ty, tw - filled_w, th, make_solid_buf(tw - filled_w, th, *layout["track_color"]))

def update_volume_bar(layout, volume):
    volume = max(0.0, min(1.0, volume))
    if abs(volume - layout.get("last_vol", -1)) < 0.005: return
    layout["last_vol"] = volume

    vol_x, vol_y, vol_w, vol_h = layout["vol_x"], layout["vol_y"], layout["vol_w"], layout["vol_h"]
    accent, text_col = layout["theme"]["accent"], layout["theme"]["text"]
    
    # Grab the clean album art assembly we created earlier
    asm_img = layout["asm_img"]
    asm_x, asm_y = layout["asm_x"], layout["asm_y"]
    asm_size = layout["asm_size"]
    
    # Coordinates of the volume bar relative to the assembly image
    rel_x = vol_x - asm_x
    rel_y = vol_y - asm_y
    
    # Use an RGBA overlay to draw perfectly see-through UI over the album art
    frame = asm_img.convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0,0,0,0))
    ov_draw = ImageDraw.Draw(overlay)
    
    filled = int(vol_w * volume)
    
    # Semi-transparent dark track for the unfilled portion (See-through!)
    track_bg = (20, 20, 25, 180) 
    if filled < vol_w:
        ov_draw.rectangle([rel_x + filled, rel_y, rel_x + vol_w - 1, rel_y + vol_h - 1], fill=track_bg)
    # Solid Accent track for the filled portion
    if filled > 0:
        ov_draw.rectangle([rel_x, rel_y, rel_x + filled - 1, rel_y + vol_h - 1], fill=(accent[0], accent[1], accent[2], 255))
        
    frame.alpha_composite(overlay)
    
    # Back to RGB to draw the text
    frame = frame.convert("RGB")
    draw = ImageDraw.Draw(frame)

    label = f"VOL {int(volume * 100)}%"
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except: font = ImageFont.load_default()
    
    bbox = draw.textbbox((0,0), label, font=font)
    lw = bbox[2] - bbox[0]
    lx = rel_x + (vol_w - lw) // 2
    ly = rel_y - 25
    
    # Text shadow guarantees text is readable regardless of the album art behind it
    draw.text((lx+1, ly+1), label, font=font, fill=(0,0,0))
    draw.text((lx, ly), label, font=font, fill=text_col)
    
    # To save SPI bandwidth, we only crop out the horizontal slice of the assembly that changed
    slice_y_start = max(0, ly - 5)
    slice_y_end = min(asm_size, rel_y + vol_h + 5)
    slice_h = slice_y_end - slice_y_start
    
    crop = frame.crop((0, slice_y_start, asm_size, slice_y_end))
    blit_rect_buf(asm_x, asm_y + slice_y_start, asm_size, slice_h, crop.tobytes())

def update_current_time_label(layout, cur_secs, last_str=None):
    cur_str = format_time(cur_secs)
    if cur_str == last_str: return last_str

    bg, accent = layout["bg"], layout["theme"]["accent"]
    clear_w, clear_h = 80, 24
    
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1), bg))
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except: font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), cur_str, font=font)
    text_w = bbox[2] - bbox[0]
    
    img = Image.new("RGB", (clear_w, clear_h), bg)
    ImageDraw.Draw(img).text((clear_w - text_w, 0), cur_str, font=font, fill=accent)
    blit_rect_buf(WIDTH - clear_w - 20, layout["time_y"], clear_w, clear_h, img.tobytes())
    
    return cur_str

def play_single_track(player, track, buttons=None, shuffle=None, current_index=0):
    log("PLAYBACK", f"loading: {track.get('title', track['file_path'])} "
                     f"by {track.get('artist', 'Unknown')}")
    if track.get("duration", 0) <= 0:
        meta_dur = get_duration_mutagen(track["file_path"])
        if meta_dur and meta_dur > 0: track["duration"] = meta_dur

    player.load(track["file_path"])
    player.play()

    if not player.wait_until_playing(timeout_s=5.0):
        log("PLAYBACK", f"WARNING: pipeline didn't reach PLAYING for {track['file_path']!r} "
                         f"within timeout — continuing anyway, audio may glitch briefly.")
    else:
        log("PLAYBACK", "pipeline reached PLAYING, audio flowing")

    duration_secs, t0 = 0.0, time.perf_counter()
    while time.perf_counter() - t0 < 1.0:
        dur_ms = player.get_duration_ms()
        if 0 < dur_ms < 10_000_000:
            duration_secs = dur_ms / 1000.0
            break
        time.sleep(0.05)

    if duration_secs <= 0: duration_secs = float(track.get("duration", 0.0))
    track["duration"] = duration_secs
    log("PLAYBACK", f"duration resolved: {duration_secs:.1f}s")
    
    layout = draw_background_and_layout(track)
    layout.update({"vol_visible": False, "vol_last_change": 0.0})
    if shuffle is not None:
        update_shuffle_icon(layout, shuffle.active)

    last_time_str, last_progress, spool_phase, last_spin, last_pos_poll = None, -1.0, 0, time.perf_counter(), 0.0
    pos_secs, progress, is_playing, action = 0.0, 0.0, True, "ended"
    animator = layout["animator"]

    while True:
        now = time.perf_counter()
        if now - last_pos_poll >= 0.05:
            last_pos_poll = now
            pos_secs = max(0, min(player.get_position_ms() / 1000.0, duration_secs)) if duration_secs > 0 else 0
            progress = pos_secs / duration_secs if duration_secs > 0 else 0.0

            if not layout.get("vol_visible"):
                if abs(progress - last_progress) >= 0.005:
                    update_progress_bar(layout, progress)
                    last_progress = progress
                last_time_str = update_current_time_label(layout, pos_secs, last_time_str)

        if now - last_spin >= 0.04:
            if is_playing:
                spool_phase = (spool_phase + 2000) & 0xFFFF
                animator.blit(layout["spool1_x"], layout["spool_y"], spool_phase)
                animator.blit(layout["spool2_x"], layout["spool_y"], spool_phase)
            last_spin = now
        
            if layout.get("vol_visible"):
                update_volume_bar(layout, player.get_volume())

        if buttons:
            clicks, holds, repeats = buttons.poll()
            vol_delta = 0.0

            if "play_pause" in holds:
                log("PLAYBACK", "Play/Pause held -> opening menu")
                player.pause()
                is_playing = False
                return "menu"

            if "prev" in holds:
                log("PLAYBACK", "Prev held -> opening Library")
                player.pause()
                is_playing = False
                return "library"

            if "next" in holds and shuffle is not None:
                shuffle.toggle(current_index)
                update_shuffle_icon(layout, shuffle.active)

            for ev in clicks:
                if ev == "play_pause":
                    (player.pause() if is_playing else player.play())
                    is_playing = not is_playing
                    log("PLAYBACK", f"{'paused' if not is_playing else 'resumed'} via click")
                elif ev == "next":
                    log("PLAYBACK", "next track requested")
                    player.stop()
                    return "next"
                elif ev == "prev":
                    if pos_secs < 3.0: 
                        log("PLAYBACK", "prev track requested")
                        player.stop()
                        return "prev"
                    else:
                        log("PLAYBACK", "prev click within track -> seek to start")
                        player.seek_to_start()
                elif ev == "vol_up":
                    vol_delta += 0.05
                elif ev == "vol_down":
                    vol_delta -= 0.05
                        
            for ev in holds + repeats:
                if ev == "vol_up":
                    vol_delta += 0.05
                elif ev == "vol_down":
                    vol_delta -= 0.05
                    
            if vol_delta != 0.0:
                player.change_volume(vol_delta)
                layout.update({"vol_visible": True, "vol_last_change": time.monotonic()})
                update_volume_bar(layout, player.get_volume())

        if duration_secs > 0 and pos_secs >= duration_secs:
            log("PLAYBACK", "track ended naturally")
            break
        
        # Volume Auto-Hide Cleanup: The new composited magic
        if layout.get("vol_visible") and (time.monotonic() - layout["vol_last_change"] > 1.5):
            layout["vol_visible"] = False
            layout["last_vol"] = -1.0
            
            if "asm_img" in layout:
                asm_size = layout["asm_size"]
                blit_rect_buf(layout["asm_x"], layout["asm_y"], asm_size, asm_size, layout["asm_img"].tobytes())

        time.sleep(0.001)

    player.stop()
    return action

def ui_loop():
    tracks = scan_music_folder(MUSIC_ROOT, default_image_path=DEFAULT_ART_PATH)
    if not tracks:
        log("LIBRARY", f"no tracks found under {MUSIC_ROOT}, falling back to single hardcoded file")
        tracks = [build_track_from_file("/home/rock/Dash Out feat Spiro.mp3", default_image_path=DEFAULT_ART_PATH)]
    else:
        log("LIBRARY", f"found {len(tracks)} track(s) under {MUSIC_ROOT}")

    player = GstPlayer()
    player.set_volume(0.8)
    buttons = ButtonHandler(btn_play_pause_line, btn_next_line, btn_prev_line, btn_vol_up_line, btn_vol_down_line)

    bt = None
    try:
        bt = BluetoothManager()
        bt.start()
        log("BT", "BluetoothManager started OK")
    except BluetoothUnavailable as e:
        log("BT", f"unavailable, Settings > Bluetooth will be disabled: {e}")

    upload_srv = MusicUploadServer(MUSIC_ROOT, log_fn=lambda tag, msg: log(tag, msg))

    # Bootstrap fallback: if there's no WiFi at all yet, phone-assisted setup
    # (the normal Settings > Wi-Fi flow) has nothing to connect over -- the
    # phone needs a shared network to reach the device's web server, and
    # there isn't one. So if we're not connected, host our own setup AP and
    # walk the person through it directly on-screen, no prior context needed.
    initial_wifi = wifi_get_status()
    if not initial_wifi["connected"]:
        log("WIFI", "no connection detected at boot, starting setup hotspot")
        ok, msg = wifi_start_hotspot()
        log("WIFI", f"hotspot start: ok={ok} msg={msg}")
        if ok:
            try:
                upload_srv.start()
                log("UPLOAD", "started for setup-mode access")
            except UploadServerUnavailable as e:
                log("UPLOAD", f"couldn't start during setup: {e}")
            run_wifi_setup_screen(buttons, lambda: wifi_get_status()["connected"])
            if wifi_is_hotspot_active():
                wifi_stop_hotspot()
        else:
            log("WIFI", "hotspot failed to start, continuing without WiFi setup screen")

    shuffle = ShuffleState(len(tracks))

    def rescan_if_needed(currently_playing_path):
        nonlocal tracks, shuffle
        if not upload_srv.is_rescan_pending():
            return None
        upload_srv.consume_rescan_flag()
        new_tracks = scan_music_folder(MUSIC_ROOT, default_image_path=DEFAULT_ART_PATH)
        if not new_tracks:
            return None
        tracks = new_tracks
        shuffle = ShuffleState(len(tracks))
        if currently_playing_path is None:
            log("LIBRARY", f"rescanned after upload: {len(tracks)} track(s)")
            return 0
        try:
            idx = next(i for i, t in enumerate(tracks) if t["file_path"] == currently_playing_path)
        except StopIteration:
            idx = 0
        log("LIBRARY", f"rescanned after upload: {len(tracks)} track(s), resuming at index {idx}")
        return idx

    try:
        log("LIBRARY", "boot: opening Library as the home screen")
        chosen = run_library_screen(tracks, buttons, bt, upload_srv)
        rescanned_idx = rescan_if_needed(None)
        if rescanned_idx is not None and chosen is None:
            chosen = rescanned_idx
        while chosen is None:
            chosen = run_library_screen(tracks, buttons, bt, upload_srv)
        index = chosen

        while True:
            track = tracks[index]
            result = play_single_track(player, track, buttons, shuffle=shuffle, current_index=index)
            log("PLAYBACK", f"play_single_track returned: {result!r}")

            if result in ("menu", "library"):
                if result == "menu":
                    chosen = run_menu(bt, upload_srv, tracks, buttons)
                else:
                    chosen = run_library_screen(tracks, buttons, bt, upload_srv)
                chosen_path = tracks[chosen]["file_path"] if chosen is not None else None

                rescanned_idx = rescan_if_needed(track["file_path"])
                if rescanned_idx is not None and chosen_path is None:
                    index = rescanned_idx

                if chosen_path is not None:
                    try:
                        index = next(i for i, t in enumerate(tracks) if t["file_path"] == chosen_path)
                    except StopIteration:
                        index = chosen  
                    log("PLAYBACK", f"jumping to Library selection, index {index}")
                elif rescanned_idx is None:
                    # Nothing chosen and no rescan happened -- just resume
                    # the same track exactly where it was paused.
                    player.play()
                    log("PLAYBACK", "resumed after menu/library close")
                continue

            if shuffle.active:
                index = shuffle.prev_index(index) if result == "prev" else shuffle.next_index(index)
            else:
                index = (index - 1) % len(tracks) if result == "prev" else (index + 1) % len(tracks)
            log("PLAYBACK", f"advancing to track index {index} (shuffle={'on' if shuffle.active else 'off'})")
    finally:
        if upload_srv.is_running():
            upload_srv.stop()


def main():
    log("MAIN", "initializing display")
    ili9488_init()
    write_cmd(0x53); write_data_byte(0x2C)  
    write_cmd(0x51); write_data_byte(0xFF)  
    log("MAIN", "display initialized, entering ui_loop")
    ui_loop()

if __name__ == "__main__":
    log("MAIN", "starting up")
    try:
        main()
    except KeyboardInterrupt:
        log("MAIN", "KeyboardInterrupt received, shutting down")
    except Exception as e:
        log("MAIN", f"FATAL: unhandled exception: {e!r}")
        raise
    finally:
        try: fill_screen_rgb888(0, 0, 0)
        except Exception: pass
        try: backlight.shutdown()
        except Exception: pass
        spi.close(); dc_line.set_value(0); rst_line.set_value(1)
        dc_line.release(); rst_line.release()
        btn_play_pause_line.release(); btn_next_line.release(); btn_prev_line.release(); btn_vol_up_line.release(); btn_vol_down_line.release()
        chip.close()
        log("MAIN", "shutdown complete, GPIO/SPI released")