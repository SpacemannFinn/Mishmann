import os
import time
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


# ================================
# BACKLIGHT — real PWM dimming via PWM9 (pin 18), confirmed working on-device.
#
# IMPORTANT, confirmed empirically on this exact hardware: the duty-cycle ->
# brightness relationship is INVERTED from the usual convention. duty_cycle=0
# is full bright; duty_cycle=PWM_PERIOD_NS (100%) is black. This class hides
# that inversion behind set_brightness(0-100) where 0=off and 100=full bright,
# so nothing else in the codebase needs to know about the inversion.
#
# Also confirmed: the response is genuinely smooth across the full range —
# what looked like a "cliff" in early testing was a bash-loop artifact (big
# jumps + long holds), not a hardware limitation. A continuous ramp (small
# steps, short delays) fades cleanly. set_brightness() here writes directly
# (no ramping) since UI calls already happen at a reasonable cadence from
# Next/Prev repeats; see fade_to() for an explicit smooth transition helper.
# ================================
PWM_CHIP_PATH = "/sys/class/pwm/pwmchip0"
PWM_CHANNEL = 0
PWM_PERIOD_NS = 1_000_000   # 1ms period — confirmed working during testing


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
                time.sleep(0.05)  # sysfs needs a moment to create the directory
            self._write("period", self.period_ns)
            self._write("duty_cycle", 0)   # 0 duty = full bright (inverted)
            self._write("enable", 1)
            self._available = True
        except Exception as e:
            print(f"WARNING: backlight PWM unavailable ({e}); "
                  f"screen will stay at whatever brightness it powered on with.")
            self._available = False

    def set_brightness(self, percent):
        """0 = off (screen dark), 100 = full bright. Clamped to [0, 100]."""
        percent = max(0, min(100, percent))
        self._brightness = percent
        if not self._available:
            return
        # Inverted relationship, confirmed on this hardware: duty_cycle=0 is
        # full bright, duty_cycle=period_ns is dark. So brightness% maps to
        # duty_cycle as (100 - percent)% of the period, not percent directly.
        duty = int(round((100 - percent) / 100.0 * self.period_ns))
        try:
            self._write("duty_cycle", duty)
        except Exception as e:
            print(f"WARNING: failed to set backlight brightness: {e}")

    def get_brightness(self):
        return self._brightness

    def fade_to(self, target_percent, duration_s=0.3, steps=30):
        """Smooth ramp to a target brightness — confirmed smooth on this
        hardware when done in small steps rather than one big jump."""
        if not self._available:
            self._brightness = max(0, min(100, target_percent))
            return
        start = self._brightness
        target = max(0, min(100, target_percent))
        if start == target:
            return
        delay = duration_s / max(1, steps)
        for i in range(1, steps + 1):
            self.set_brightness(start + (target - start) * i / steps)
            time.sleep(delay)

    def off(self):
        self.set_brightness(0)

    def on(self, percent=None):
        self.set_brightness(percent if percent is not None else self._brightness or 100)

    def shutdown(self):
        """Release the PWM channel cleanly on app exit."""
        if not self._available:
            return
        try:
            self._write("duty_cycle", 0)  # leave the screen bright, not stuck dark
            self._write("enable", 0)
        except Exception:
            pass


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
        "title": title, "artist": artist, "album": album,
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
# UI DRAWING: COMPOSITED LAYOUT 5
# ================================
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
        "animator": SpoolAnimator(spool_size, theme["accent"], bg)
    }

    # Extract & Resize Album Art using PIL
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

    # Compositing: Pre-render Art + Feathered Shadow + Border
    pad = 20 # Padding to ensure shadow isn't cut off
    asm_size = album_size + pad * 2
    asm_img = Image.new("RGB", (asm_size, asm_size), bg)

    # 1. Draw Feathered Drop Shadow
    shadow_mask = Image.new("RGBA", (asm_size, asm_size), (0,0,0,0))
    shadow_draw = ImageDraw.Draw(shadow_mask)
    sh_off, b_th = 8, 2
    sx, sy = pad + sh_off, pad + sh_off
    # Draw solid black rect, then blur it massively
    shadow_draw.rectangle([sx, sy, sx + album_size + b_th*2 - 1, sy + album_size + b_th*2 - 1], fill=(0,0,0, 160))
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(6))
    
    # Composite shadow onto our base image
    asm_img.paste(shadow_mask, (0,0), shadow_mask)

    # 2. Draw Subtle Border
    bx, by = pad, pad
    b_col = (15, 15, 20)
    ImageDraw.Draw(asm_img).rectangle([bx, by, bx + album_size + b_th*2 - 1, by + album_size + b_th*2 - 1], fill=b_col)

    # 3. Paste Album Art
    asm_img.paste(pil_img, (bx + b_th, by + b_th))

    # Save assembly data to layout for Volume Bar usage
    layout["asm_img"] = asm_img
    layout["asm_x"] = album_x - pad
    layout["asm_y"] = album_y - pad
    layout["asm_size"] = asm_size

    # Push final composited assembly to SPI
    blit_rect_buf(layout["asm_x"], layout["asm_y"], asm_size, asm_size, asm_img.tobytes())

    # Draw Text
    title_y = 270
    artist_y = title_y + 24
    time_y = 280

    draw_text_ttf(20, title_y, track.get("title", "Unknown Title"), theme["text"], bg, font_size=20, max_width=200)
    draw_text_ttf(20, artist_y, track.get("artist", "Unknown Artist"), theme["subtext"], bg, font_size=16, max_width=200)

    layout["duration_secs"] = int(track.get("duration", 4 * 60 + 12))
    layout["time_y"] = time_y

    # Tape track background
    track_color = (int(bg[0]*0.5), int(bg[1]*0.5), int(bg[2]*0.5))
    blit_rect_buf(tape_x, tape_y, tape_w, tape_h, make_solid_buf(tape_w, tape_h, *track_color))
    layout["track_color"] = track_color

    # Define Volume Bar (Perfectly positioned inside the assembly image space)
    vol_w, vol_h = 180, 10
    layout.update({
        "vol_x": (WIDTH - vol_w) // 2, "vol_y": album_y + (album_size // 2) - 5, 
        "vol_w": vol_w, "vol_h": vol_h, "last_vol": -1.0
    })
    return layout

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

# ================================
# MENU SYSTEM
# Hold Play/Pause (>=0.5s, the existing "long press" gesture from
# ButtonHandler) opens this. Inside any menu screen the same six buttons are
# repurposed: Next/Prev move the highlight, Play/Pause (click) selects, and
# Prev (hold) goes back a level / closes the menu from the root. Vol Up/Down
# always stay volume, even while a menu is open, since music keeps playing
# underneath. No new buttons, no new GPIO lines — just contextual meaning.
# ================================
MENU_BG = (24, 24, 30)
MENU_TEXT = (240, 240, 245)
MENU_SUBTEXT = (150, 155, 165)
MENU_ACCENT = (232, 106, 38)
MENU_HILITE_BG = (50, 50, 60)

ROW_H = 42
HEADER_H = 50


def render_list_screen(title, items, selected_index, footer=None):
    """
    Build (not blit) one full-screen PIL image for a simple scrollable list
    menu: a header, then rows, with the selected row highlighted. Returns the
    Image so callers can blit it in one shot.

    items: list of (label, subtext_or_None) tuples.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
        sub_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except Exception:
        title_font = row_font = sub_font = ImageFont.load_default()

    # Header
    draw.text((20, 14), title, font=title_font, fill=MENU_TEXT)
    draw.rectangle([0, HEADER_H - 1, WIDTH, HEADER_H - 1], fill=(50, 50, 58))
    draw.rectangle([20, HEADER_H - 4, 60, HEADER_H - 2], fill=MENU_ACCENT)

    # How many rows fit on screen, and scroll so the selection stays visible.
    visible_rows = max(1, (HEIGHT - HEADER_H - (28 if footer else 6)) // ROW_H)
    if len(items) <= visible_rows:
        scroll = 0
    else:
        scroll = max(0, min(selected_index - visible_rows // 2, len(items) - visible_rows))

    for i in range(scroll, min(len(items), scroll + visible_rows)):
        label, subtext = items[i]
        row_y = HEADER_H + (i - scroll) * ROW_H
        is_sel = (i == selected_index)

        if is_sel:
            draw.rectangle([0, row_y, WIDTH, row_y + ROW_H - 1], fill=MENU_HILITE_BG)
            draw.rectangle([0, row_y, 4, row_y + ROW_H - 1], fill=MENU_ACCENT)

        text_color = MENU_TEXT if is_sel else MENU_SUBTEXT
        ty = row_y + (8 if subtext else 12)
        draw.text((24, ty), label, font=row_font, fill=text_color)
        if subtext:
            draw.text((24, ty + 19), subtext, font=sub_font, fill=MENU_SUBTEXT)

    if footer:
        draw.rectangle([0, HEIGHT - 26, WIDTH, HEIGHT - 26], fill=(50, 50, 58))
        draw.text((20, HEIGHT - 21), footer, font=sub_font, fill=MENU_SUBTEXT)

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


def _is_named(dev):
    """A device counts as 'named' if BlueZ resolved a friendly name distinct
    from its bare MAC address — the common case for anything actually worth
    showing in a pairing list (headphones, speakers, watches...)."""
    return dev["name"] != dev["address"]


def run_bluetooth_screen(bt, buttons):
    """
    Bluetooth settings screen: scan, list devices (named-only by default,
    toggle for all), pair/connect/trust the selected one. Returns when the
    user backs out (Prev hold).
    """
    show_all = False
    selected = 0
    status_msg = None
    last_action_addr = None

    bt.start_scan()
    last_redraw = 0.0

    while True:
        now = time.monotonic()

        # Drain backend events; update status line on pair/connect results.
        for kind, payload in bt.get_events():
            if kind == "pair_result" and payload.get("address") == last_action_addr:
                status_msg = "Paired!" if payload["ok"] else f"Pair failed: {payload.get('error', '?')}"
            elif kind == "connect_result" and payload.get("address") == last_action_addr:
                status_msg = "Connected!" if payload["ok"] else f"Connect failed: {payload.get('error', '?')}"
                if payload["ok"]:
                    bt.trust(last_action_addr)

        devices = bt.get_discovered_devices()
        if not show_all:
            devices = [d for d in devices if _is_named(d)]
        # Paired devices first, then by signal strength (closer = more likely
        # to be the thing you're trying to pair right now).
        devices.sort(key=lambda d: (not d["paired"], -(d["rssi"] or -999)))

        items = [_bt_device_label(d) for d in devices]
        toggle_label = f"Show all devices: {'ON' if show_all else 'OFF'}"
        items.append((toggle_label, None))
        rescan_label = "Scanning…" if bt.is_scanning() else "Scan again"
        items.append((rescan_label, None))

        if selected >= len(items):
            selected = len(items) - 1

        if now - last_redraw > 0.2:  # menu doesn't need 1kHz redraws
            footer = status_msg or "Hold Prev: back   Play: select"
            img = render_list_screen("Bluetooth", items, selected, footer=footer)
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now

        clicks, holds, repeats = buttons.poll()

        for ev in holds:
            if ev == "prev":
                bt.stop_scan()
                return
            elif ev == "play_pause":
                bt.stop_scan()
                return

        for ev in clicks:
            if ev == "next":
                selected = min(selected + 1, len(items) - 1)
                status_msg = None
            elif ev == "prev":
                selected = max(selected - 1, 0)
                status_msg = None
            elif ev == "play_pause":
                if selected == len(devices):          # "Show all" toggle row
                    show_all = not show_all
                    selected = 0
                elif selected == len(devices) + 1:     # "Scan again" row
                    bt.start_scan()
                    status_msg = "Scanning…"
                else:
                    dev = devices[selected]
                    last_action_addr = dev["address"]
                    if dev["connected"]:
                        status_msg = "Already connected"
                    elif dev["paired"]:
                        status_msg = "Connecting…"
                        bt.connect(dev["address"])
                    else:
                        status_msg = "Pairing…"
                        bt.pair(dev["address"])

        time.sleep(0.02)


def run_settings_screen(bt, buttons):
    if bt is not None:
        bt_item = ("Bluetooth", "Pair & connect devices")
    else:
        bt_item = ("Bluetooth", "Unavailable on this device")

    selected = 0
    last_redraw = 0.0

    while True:
        items = [
            bt_item,
            ("Brightness", f"{backlight.get_brightness()}%  (Vol +/- to adjust)"),
            ("Screen Off", "Tap any button to wake"),
        ]
        selected = max(0, min(selected, len(items) - 1))

        now = time.monotonic()
        if now - last_redraw > 0.15:  # a touch faster than other menus, since
            img = render_list_screen("Settings", items, selected,         # brightness updates live while held
                                      footer="Hold Prev: back   Play: select")
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now

        clicks, holds, repeats = buttons.poll()
        for ev in holds:
            if ev in ("prev", "play_pause"):
                return

        # Brightness row: Vol Up/Down adjust live while it's highlighted,
        # instead of needing to drill into a separate screen for one slider.
        # This intentionally shadows normal volume-changing on this one row
        # only — every other row in every other screen leaves Vol +/- alone.
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
                    run_bluetooth_screen(bt, buttons)
                elif selected == 2:
                    run_screen_off(buttons)
        time.sleep(0.02)


def run_screen_off(buttons):
    """Turns the backlight off and waits for any button press to wake it.
    The display content underneath is untouched — only the backlight is
    switched off, so waking is instant (no redraw needed, just light again)."""
    prev_brightness = backlight.get_brightness()
    backlight.fade_to(0, duration_s=0.25)
    try:
        while True:
            clicks, holds, repeats = buttons.poll()
            if clicks or holds:
                break
            time.sleep(0.05)
    finally:
        backlight.fade_to(prev_brightness, duration_s=0.25)


def run_menu(bt, buttons):
    """
    Top-level menu, entered by holding Play/Pause during playback. Returns
    once the user exits back to Now Playing (Prev hold, or Play/Pause hold).
    """
    items = [("Settings", "Bluetooth & more")]
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
                return
        for ev in clicks:
            if ev == "next":
                selected = min(selected + 1, len(items) - 1)
            elif ev == "prev":
                selected = max(selected - 1, 0)
            elif ev == "play_pause":
                if selected == 0:
                    run_settings_screen(bt, buttons)
        time.sleep(0.02)


def play_single_track(player, track, buttons=None):
    if track.get("duration", 0) <= 0:
        meta_dur = get_duration_mutagen(track["file_path"])
        if meta_dur and meta_dur > 0: track["duration"] = meta_dur

    player.load(track["file_path"])
    player.play()

    # Block until GStreamer reports the pipeline is genuinely PLAYING (audio
    # actually flowing — caps negotiated, Bluetooth A2DP socket open, etc.)
    # rather than assuming readiness after a fixed sleep.
    if not player.wait_until_playing(timeout_s=5.0):
        print(f"WARNING: pipeline didn't reach PLAYING for {track['file_path']!r} "
              f"within timeout — continuing anyway, audio may glitch briefly.")

    duration_secs, t0 = 0.0, time.perf_counter()
    while time.perf_counter() - t0 < 1.0:
        dur_ms = player.get_duration_ms()
        if 0 < dur_ms < 10_000_000:
            duration_secs = dur_ms / 1000.0
            break
        time.sleep(0.05)

    if duration_secs <= 0: duration_secs = float(track.get("duration", 0.0))
    track["duration"] = duration_secs
    
    layout = draw_background_and_layout(track)
    layout.update({"vol_visible": False, "vol_last_change": 0.0})

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

            # Hold Play/Pause -> open the menu. A short click on Play/Pause
            # always just plays/pauses (handled below in `clicks`); ButtonHandler
            # guarantees a held press never also appears in `clicks`, so there's
            # no ambiguity between the two gestures on the same button.
            if "play_pause" in holds:
                player.pause()
                is_playing = False
                return "menu"

            for ev in clicks:
                if ev == "play_pause":
                    (player.pause() if is_playing else player.play())
                    is_playing = not is_playing
                elif ev == "next":
                    player.stop()
                    return "next"
                elif ev == "prev":
                    if pos_secs < 3.0: 
                        player.stop()
                        return "prev"
                    else: player.seek_to_start()
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


        if duration_secs > 0 and pos_secs >= duration_secs: break
        
        # Volume Auto-Hide Cleanup: The new composited magic
        if layout.get("vol_visible") and (time.monotonic() - layout["vol_last_change"] > 1.5):
            layout["vol_visible"] = False
            layout["last_vol"] = -1.0
            
            if "asm_img" in layout:
                # Instead of redrawing rectangles and trying to erase stuff, 
                # we just push the original, untouched assembly image back to the screen!
                asm_size = layout["asm_size"]
                blit_rect_buf(layout["asm_x"], layout["asm_y"], asm_size, asm_size, layout["asm_img"].tobytes())

        time.sleep(0.001)

    player.stop()
    return action

def ui_loop():
    tracks = scan_music_folder(MUSIC_ROOT, default_image_path=DEFAULT_ART_PATH)
    if not tracks:
        tracks = [build_track_from_file("/home/rock/Dash Out feat Spiro.mp3", default_image_path=DEFAULT_ART_PATH)]

    player = GstPlayer()
    player.set_volume(0.8)
    buttons = ButtonHandler(btn_play_pause_line, btn_next_line, btn_prev_line, btn_vol_up_line, btn_vol_down_line)

    bt = None
    try:
        bt = BluetoothManager()
        bt.start()
    except BluetoothUnavailable as e:
        print(f"Bluetooth unavailable, Settings > Bluetooth will be disabled: {e}")

    index = 0
    while True:
        track = tracks[index]
        result = play_single_track(player, track, buttons)

        if result == "menu":
            if bt is not None:
                run_menu(bt, buttons)
            else:
                # No Bluetooth backend — still let the menu open (Settings ->
                # Bluetooth just won't be reachable) rather than stranding the
                # user with a dead hold gesture and no way back to playback.
                run_menu(None, buttons)
            # play_single_track already paused before returning "menu"; resume
            # the same track exactly where it left off rather than advancing.
            player.play()
            continue

        index = (index - 1) % len(tracks) if result == "prev" else (index + 1) % len(tracks)

def main():
    ili9488_init()
    write_cmd(0x53); write_data_byte(0x2C)  
    write_cmd(0x51); write_data_byte(0xFF)  
    ui_loop()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: pass
    finally:
        try: fill_screen_rgb888(0, 0, 0)
        except Exception: pass
        try: backlight.shutdown()
        except Exception: pass
        spi.close(); dc_line.set_value(0); rst_line.set_value(1)
        dc_line.release(); rst_line.release()
        btn_play_pause_line.release(); btn_next_line.release(); btn_prev_line.release(); btn_vol_up_line.release(); btn_vol_down_line.release()
        chip.close()