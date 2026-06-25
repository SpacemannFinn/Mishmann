import os
import time
import threading
import queue
import subprocess
import spidev
import gpiod
import json
import math
import io
import colorsys
import re
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
from genre_fill import GenreFillWorker
import stats
import system_health
import reliability
import boot_history

_LOG_T0 = time.monotonic()

def log(tag, msg):
    print(f"[{time.monotonic() - _LOG_T0:8.3f}] [{tag}] {msg}", flush=True)


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

    if s < 0.15 and v < 0.25:
        h, s, v = 0.52, 0.65, 0.45 
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


chip = None
dc_line = rst_line = None
btn_play_pause_line = btn_next_line = btn_prev_line = btn_vol_up_line = btn_vol_down_line = None
backlight = None
spi = None
_HAS_WRITEBYTES2 = False


def init_hardware():
    global chip, dc_line, rst_line
    global btn_play_pause_line, btn_next_line, btn_prev_line, btn_vol_up_line, btn_vol_down_line
    global backlight, spi, _HAS_WRITEBYTES2

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
    log("GPIO", f"chip={CHIP_NAME} dc={DC_LINE} rst={RESET_LINE} requested OK")

    backlight = Backlight()
    backlight.set_brightness(100)

    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEV)
    spi.max_speed_hz = 60_000_000
    spi.mode = 0
    _HAS_WRITEBYTES2 = hasattr(spi, "writebytes2")


PWM_CHIP_PATH = "/sys/class/pwm/pwmchip0"
PWM_CHANNEL = 0
PWM_PERIOD_NS = 1_000_000   

class Backlight:
    def __init__(self, chip_path=PWM_CHIP_PATH, channel=PWM_CHANNEL, period_ns=PWM_PERIOD_NS):
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

_BLIT_SLOW_THRESHOLD_S = 0.05  # only log a blit if it took longer than this --
                                # normal full-screen blits at 60MHz SPI should
                                # be well under this; logging every blit would
                                # flood the log and itself add overhead.
_blit_count = 0
_blit_total_time = 0.0

def blit_rect_buf(x, y, w, h, buf):
    global _blit_count, _blit_total_time
    t0 = time.perf_counter()
    set_address_window(x, y, x + w - 1, y + h - 1)
    dc_data()
    spi_write_bulk(buf)
    elapsed = time.perf_counter() - t0
    _blit_count += 1
    _blit_total_time += elapsed
    if elapsed > _BLIT_SLOW_THRESHOLD_S:
        avg_ms = (_blit_total_time / _blit_count) * 1000
        log("BLIT", f"SLOW: {w}x{h} took {elapsed*1000:.1f}ms (x={x},y={y})  "
                     f"[running avg over {_blit_count} blits: {avg_ms:.2f}ms]")

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
            if tags_full and hasattr(tags_full, "values"):
                try: audio_full = MutagenFile(abs_path)
                except Exception: audio_full = None

                if audio_full:
                    for key in audio_full.tags.keys():
                        if key.startswith("APIC:"):
                            try:
                                embedded_image = Image.open(io.BytesIO(audio_full.tags[key].data)).convert("RGB")
                                break
                            except: pass

            if embedded_image is None and hasattr(audio_full, "pictures"):
                for pic in audio_full.pictures:
                    try:
                        embedded_image = Image.open(io.BytesIO(pic.data)).convert("RGB")
                        break
                    except: pass
            
            if embedded_image is None and hasattr(audio_full, "pictures"):
                for pic in audio_full.pictures:
                    if hasattr(pic, "data"):
                        try:
                            embedded_image = Image.open(io.BytesIO(pic.data)).convert("RGB")
                            break
                        except: pass
                        
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
        dirnames[:] = [d for d in dirnames if d != reliability.QUARANTINE_DIRNAME]
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
    def __init__(self, backlight_obj, *lines):
        self.lines = {
            "play_pause": lines[0], 
            "next": lines[1], 
            "prev": lines[2], 
            "vol_up": lines[3], 
            "vol_down": lines[4]
        }
        self.states = {
            name: {
                'raw': 1, 
                'pressed': False, 
                'start': 0.0, 
                'long_fired': False, 
                'repeat': 0.0
            } for name in self.lines
        }
        
        self.backlight = backlight_obj
        self.event_queue = queue.Queue()
        self._running = True
        self._last_emit_log = 0.0  # throttle so rapid repeats don't flood the log
        
        self.last_interaction_time = time.monotonic()
        self.screen_is_sleeping = False
        self.prev_brightness = 100
        self.dim_val = 10
        
        self.click_history = []
        
        # Start background polling immediately
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="button-poll")
        self._thread.start()

    def _emit(self, kind, name, was_sleeping):
        """Queue a button event AND log it the instant it's queued -- this is
        the actual moment of "the button registered something," distinct
        from whenever the consumer loop happens to drain it. Comparing this
        timestamp against when the screen visually updates is what actually
        isolates a button-side stall from a draw-side stall. Repeats are
        throttled (one log line per 0.1s) so a held button doesn't flood
        the log -- clicks/holds always log, since those are rare and the
        ones that matter most for this kind of bug."""
        now = time.monotonic()
        if kind != "repeat" or (now - self._last_emit_log) >= 0.1:
            log("BUTTON", f"{kind}:{name}  (screen_was_sleeping={was_sleeping}, "
                          f"queue_depth_before={self.event_queue.qsize()})")
            self._last_emit_log = now
        self.event_queue.put((kind, name))

    def _poll_loop(self):
        """Centralized high-frequency loop implementing broad-envelope global inactivity dimming."""
        IDLE_TIMEOUT_S = 120.0  

        while self._running:
            now = time.monotonic()
            activity_detected = False
            was_sleeping = self.screen_is_sleeping  # snapshot for accurate event logging

            for name, line in self.lines.items():
                state = self.states[name]
                try: 
                    val = line.get_value()
                except (OSError, ValueError): 
                    continue
                
                # Button Press Detected (Falling Edge)
                if state['raw'] == 1 and val == 0:
                    state.update({'start': now, 'pressed': True, 'long_fired': False, 'repeat': now})
                    self.last_interaction_time = now
                    activity_detected = True
                
                # Button Release Detected (Rising Edge)
                elif state['raw'] == 0 and val == 1:
                    if (now - state['start']) >= 0.02: 
                        if (now - state['start']) < 0.5:
                            if name == "play_pause":
                                self.click_history = [t for t in self.click_history if now - t < 1.2]
                                self.click_history.append(now)
                                
                                if len(self.click_history) >= 4:
                                    self._emit("quad_click_pause", name, was_sleeping)
                                    self.click_history.clear()
                                else:
                                    self._emit("click", name, was_sleeping)
                            else:
                                self._emit("click", name, was_sleeping)
                                
                    state.update({'pressed': False, 'start': 0.0})
                    self.last_interaction_time = now
                    activity_detected = True
                
                # Handle active holds and repeats (Enforce a strict hold window ceiling)
                if state['pressed'] and val == 0:
                    activity_detected = True
                    self.last_interaction_time = now
                    if (now - state['start']) >= 0.5:
                        if not state['long_fired']:
                            state['long_fired'] = True
                            self._emit("hold", name, was_sleeping)
                            state['repeat'] = now
                        elif now - state['repeat'] >= 0.15:
                            self._emit("repeat", name, was_sleeping)
                            state['repeat'] = now
                            
                state['raw'] = val

            # --- GLOBAL ILLUMINATION TIMING LOOPS ---
            if activity_detected and self.screen_is_sleeping:
                self.screen_is_sleeping = False
                log("BUTTON", "wake: starting blocking fade_to -- button reads paused until this returns")
                t_fade0 = time.perf_counter()
                self.backlight.fade_to(self.prev_brightness, duration_s=0.15)
                log("BUTTON", f"wake: fade_to returned after {(time.perf_counter()-t_fade0)*1000:.1f}ms, "
                              f"resuming button reads")
                # The press/hold/release that caused this wake is no longer
                # suppressed (see edge-detection above) -- previously every
                # event was gated on `not self.screen_is_sleeping`, which
                # silently ate the very interaction that woke the screen,
                # making the first press/hold after idle look like nothing
                # happened. Now it's delivered normally, same as any other
                # press, and the queue is left alone rather than cleared.

            elif not self.screen_is_sleeping and (now - self.last_interaction_time >= IDLE_TIMEOUT_S):
                self.screen_is_sleeping = True
                self.prev_brightness = self.backlight.get_brightness() or 100
                log("BUTTON", f"idle timeout reached ({IDLE_TIMEOUT_S}s), dimming -- "
                              f"button reads paused until fade_to returns")
                t_fade0 = time.perf_counter()
                self.backlight.fade_to(self.dim_val, duration_s=0.25)
                log("BUTTON", f"dim: fade_to returned after {(time.perf_counter()-t_fade0)*1000:.1f}ms")
            
            time.sleep(0.01)

    def poll(self):
        clicks, holds, repeats = [], [], []
        drained = 0
        while True:
            try:
                ev_type, name = self.event_queue.get_nowait()
                drained += 1
                if ev_type == "quad_click_pause":
                    holds.append("quad_click_pause")
                elif ev_type == "click":
                    clicks.append(name)
                elif ev_type == "hold":
                    holds.append(name)
                elif ev_type == "repeat":
                    repeats.append(name)
            except queue.Empty:
                break
        if drained:
            log("BUTTON", f"poll() consumed {drained} event(s): clicks={clicks} holds={holds} repeats={repeats}")
        return clicks, holds, repeats

    def stop(self):
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)


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


def draw_shuffle_icon(x, y, size, color, bg, active):
    fg = color if active else tuple(int(c * 0.2 + bg_c * 0.8) for c, bg_c in zip(color, bg))
    scale = 4
    big_size = size * scale
    img = Image.new("RGB", (big_size, big_size), bg)
    draw = ImageDraw.Draw(img)

    w = big_size
    line_w = 2 * scale
    left_x = w * 0.15
    mid_l  = w * 0.35
    mid_r  = w * 0.65
    right_x = w * 0.80
    top_y  = w * 0.30
    bot_y  = w * 0.70

    draw.line([(left_x, top_y), (mid_l, top_y), (mid_r, bot_y), (right_x, bot_y)], fill=fg, width=line_w, joint="curve")
    gap_r = line_w * 1.5
    draw.ellipse([w*0.5 - gap_r, w*0.5 - gap_r, w*0.5 + gap_r, w*0.5 + gap_r], fill=bg)
    draw.line([(left_x, bot_y), (mid_l, bot_y), (mid_r, top_y), (right_x, top_y)], fill=fg, width=line_w, joint="curve")

    ah_l = w * 0.18  
    ah_h = w * 0.14  
    draw.polygon([(right_x + ah_l*0.6, bot_y), (right_x - ah_l*0.4, bot_y - ah_h), (right_x - ah_l*0.4, bot_y + ah_h)], fill=fg)
    draw.polygon([(right_x + ah_l*0.6, top_y), (right_x - ah_l*0.4, top_y - ah_h), (right_x - ah_l*0.4, top_y + ah_h)], fill=fg)

    img = img.resize((size, size), Image.LANCZOS)
    blit_rect_buf(x, y, size, size, img.tobytes())


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
    img = render_wifi_setup_screen(HOTSPOT_SSID, HOTSPOT_PASSWORD, f"http://{HOTSPOT_GATEWAY_IP}:8080")
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
        if "quad_click_pause" in holds: return
        if holds:
            log("WIFI", "setup screen skipped by user (held button)")
            return
        time.sleep(0.1)

def render_list_screen(title, items, selected_index, footer=None):
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
        sub_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        panel_hdr_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        panel_body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
        section_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11)
    except Exception:
        title_font = row_font = sub_font = panel_hdr_font = panel_body_font = section_font = ImageFont.load_default()

    left_pane_w = 255
    draw.text((24, 15), title.upper(), font=title_font, fill=MENU_TEXT)
    draw.rectangle([0, HEADER_H - 1, WIDTH, HEADER_H - 1], fill=(44, 44, 52))
    draw.rectangle([24, HEADER_H - 4, 64, HEADER_H - 2], fill=MENU_ACCENT)

    heights = [SECTION_H if it[0] == SECTION_MARKER else ROW_H for it in items]
    sel_positions = [i for i, it in enumerate(items) if it[0] != SECTION_MARKER]
    target_item_idx = sel_positions[selected_index] if sel_positions else 0

    avail_h = HEIGHT - HEADER_H - (30 if footer else 10)
    scroll_px = 0
    if sum(heights) > avail_h:
        y_of_target = sum(heights[:target_item_idx])
        scroll_px = max(0, min(y_of_target - avail_h // 2 + (ROW_H // 2), sum(heights) - avail_h))

    # --- LEFT PANE ---
    y = HEADER_H - scroll_px
    for i, it in enumerate(items):
        h = heights[i]
        if y + h <= HEADER_H:
            y += h
            continue
        if y >= HEIGHT - (30 if footer else 0): break

        if it[0] == SECTION_MARKER:
            draw.text((24, y + 8), it[1].upper(), font=section_font, fill=MENU_ACCENT)
        else:
            label = it[0]
            is_sel = (i == target_item_idx)
            
            if is_sel:
                draw.rectangle([12, y + 3, left_pane_w - 8, y + h - 3], fill=(44, 44, 56))
                draw.rectangle([12, y + 3, 16, y + h - 3], fill=MENU_ACCENT)
                txt_color = MENU_TEXT
            else:
                txt_color = (200, 202, 210)

            draw.text((28, y + 12), label, font=row_font, fill=txt_color)
        y += h

    # --- RIGHT PANEL ---
    panel_bg = (18, 18, 22)
    draw.rectangle([left_pane_w, HEADER_H, WIDTH, HEIGHT], fill=panel_bg)
    draw.line([(left_pane_w, HEADER_H), (left_pane_w, HEIGHT)], fill=(44, 44, 52), width=1)

    if selected_index < len(sel_positions):
        active_row_idx = sel_positions[selected_index]
        label_text = items[active_row_idx][0]
        sub_text = items[active_row_idx][1] if len(items[active_row_idx]) > 1 else ""

        px = left_pane_w + 18
        draw.text((px, HEADER_H + 20), "SYSTEM VARIABLE", font=sub_font, fill=MENU_ACCENT)
        draw.text((px, HEADER_H + 36), label_text.upper(), font=panel_hdr_font, fill=MENU_TEXT)
        draw.line([(px, HEADER_H + 58), (WIDTH - 18, HEADER_H + 58)], fill=(36, 36, 42), width=1)

        if sub_text:
            text_space = WIDTH - px - 18
            lines, current_line = [], ""
            for word in sub_text.split(" "):
                test_line = f"{current_line} {word}".strip()
                if draw.textlength(test_line, font=panel_body_font) <= text_space:
                    current_line = test_line
                else:
                    lines.append(current_line)
                    current_line = word
            if current_line: lines.append(current_line)

            ly = HEADER_H + 74
            for line in lines[:5]:
                draw.text((px, ly), line, font=panel_body_font, fill=MENU_SUBTEXT)
                ly += 20

    if footer:
        draw.rectangle([0, HEIGHT - 28, left_pane_w, HEIGHT], fill=MENU_BG)
        draw.line([(0, HEIGHT - 28), (left_pane_w, HEIGHT - 28)], fill=(44, 44, 52), width=1)
        draw.text((24, HEIGHT - 19), footer.lower(), font=sub_font, fill=MENU_SUBTEXT)

    return img

def render_split_library_screen(title, items, selected_index):
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
        if y < header_h: continue

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

    if items and selected_index < len(items):
        label, rep_track, subtext, meta = items[selected_index]
        ref_track = rep_track[0] if isinstance(rep_track, list) else rep_track
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
        cx = half_w + (half_w // 2)
        art_size = 140
        art_y = 40
        art_x = cx - (art_size // 2)

        sh_off, b_th = 8, 2
        sh_col = (int(right_bg[0]*0.2), int(right_bg[1]*0.2), int(right_bg[2]*0.2))
        draw.rectangle([art_x - b_th + sh_off, art_y - b_th + sh_off, art_x + art_size + b_th + sh_off, art_y + art_size + b_th + sh_off], fill=sh_col)
        
        b_col = (15, 15, 20)
        draw.rectangle([art_x - b_th, art_y - b_th, art_x + art_size + b_th, art_y + art_size + b_th], fill=b_col)

        if isinstance(rep_track, list): thumb = _get_collage_thumbnail(rep_track, art_size)
        else: thumb = _get_thumbnail(rep_track, art_size)

        if thumb: img.paste(thumb, (art_x, art_y))
        else: draw.rectangle([art_x, art_y, art_x + art_size, art_y + art_size], fill=(100,100,100))

        text_y = art_y + art_size + 24
        t_text = truncate_text(draw, label, big_title_font, half_w - 32)
        t_w = draw.textbbox((0,0), t_text, font=big_title_font)[2]
        draw.text((cx - t_w//2, text_y), t_text, font=big_title_font, fill=(255,255,255))
        
        if subtext:
            s_text = truncate_text(draw, subtext, sub_font, half_w - 32)
            s_w = draw.textbbox((0,0), s_text, font=sub_font)[2]
            draw.text((cx - s_w//2, text_y + 24), s_text, font=sub_font, fill=MENU_SUBTEXT)
            
        if meta:
            m_text = truncate_text(draw, meta, meta_font, half_w - 32)
            m_w = draw.textbbox((0,0), m_text, font=meta_font)[2]
            draw.text((cx - m_w//2, HEIGHT - 28), m_text, font=meta_font, fill=accent_col)

    return img

def _bt_device_label(dev):
    name = dev["name"]
    flags = []
    if dev["connected"]: flags.append("Connected")
    elif dev["paired"]: flags.append("Paired")
    sub = " · ".join(flags) if flags else None
    if dev.get("rssi") is not None: sub = f"{sub} · {dev['rssi']} dBm" if sub else f"{dev['rssi']} dBm"
    return name, sub

_WPCTL_TIMEOUT_S = 2.0  

def get_active_bt_codec():
    try:
        status = subprocess.run(["wpctl", "status"], capture_output=True, text=True, timeout=_WPCTL_TIMEOUT_S)
        if status.returncode != 0: return "Unknown (wpctl unavailable)"
        sink_id = None
        in_sinks = False
        for line in status.stdout.splitlines():
            stripped = line.strip()
            if "Sinks:" in stripped:
                in_sinks = True
                continue
            if in_sinks:
                if "Sink endpoints:" in stripped or "Sources:" in stripped: break
                if "*" in line:
                    after_star = line.split("*", 1)[1].strip()
                    sink_id = after_star.split(".", 1)[0].strip()
                    break
        if not sink_id: return "No device connected"
        inspect = subprocess.run(["wpctl", "inspect", sink_id], capture_output=True, text=True, timeout=_WPCTL_TIMEOUT_S)
        if inspect.returncode != 0: return "Unknown"
        for line in inspect.stdout.splitlines():
            if "api.bluez5.codec" in line:
                return line.split("=", 1)[1].strip().strip('"').upper()
        return "Not a Bluetooth device"
    except FileNotFoundError: return "Unknown (wpctl not installed)"
    except subprocess.TimeoutExpired: return "Unknown (timed out)"
    except Exception as e:
        log("BT", f"codec lookup failed: {e}")
        return "Unknown"

def _is_named(dev): return dev["name"] != dev["address"]

def run_bluetooth_screen(bt, buttons):
    log("BT", "entered Bluetooth screen, starting scan")
    
    if buttons: buttons.poll()
        
    show_all = False
    selected = 0
    status_msg = None
    last_action_addr = None
    bt.start_scan()
    last_redraw = 0.0
    last_rendered_items, last_rendered_selected = None, None

    while True:
        now = time.monotonic()
        
        clicks, holds, repeats = buttons.poll()
        if "quad_click_pause" in holds:
            bt.stop_scan()
            return "quad_click_pause"
            
        for ev in holds:
            if ev in ("prev", "play_pause"):
                bt.stop_scan()
                return

        for kind, payload in bt.get_events():
            addr = payload.get("address") if isinstance(payload, dict) else None
            if addr != last_action_addr: continue
            if kind == "pair_result": status_msg = "Paired!" if payload["ok"] else f"Pair failed: {payload.get('error', '?')}"
            elif kind == "connect_result":
                status_msg = "Connected!" if payload["ok"] else f"Connect failed: {payload.get('error', '?')}"
                if payload["ok"]: bt.trust(last_action_addr)
            elif kind == "disconnect_result": status_msg = "Disconnected" if payload["ok"] else f"Disconnect failed: {payload.get('error', '?')}"
            elif kind == "remove_result": status_msg = "Forgotten" if payload["ok"] else f"Remove failed: {payload.get('error', '?')}"

        all_discovered = bt.get_discovered_devices()
        paired = sorted([d for d in all_discovered if d["paired"]], key=lambda d: (not d["connected"], d["name"]))
        available = [d for d in all_discovered if not d["paired"]]
        if not show_all: available = [d for d in available if _is_named(d)]
        available.sort(key=lambda d: -(d["rssi"] or -999))

        items = [(SECTION_MARKER, "PAIRED")]
        selectable_devices = []
        if paired:
            for d in paired:
                items.append(_bt_device_label(d))
                selectable_devices.append(d)
                items.append((f"  Forget {d['name']}", None))
                selectable_devices.append(("forget", d["address"]))
        else:
            items.append(("No paired devices yet", None))
            selectable_devices.append(None)

        items.append((SECTION_MARKER, f"AVAILABLE{'  (scanning…)' if bt.is_scanning() else ''}"))
        for d in available:
            items.append(_bt_device_label(d))
            selectable_devices.append(d)

        items.append((f"Show all devices: {'ON' if show_all else 'OFF'}", None))
        selectable_devices.append("toggle_show_all")
        items.append(("Scanning…" if bt.is_scanning() else "Scan again", None))
        selectable_devices.append("rescan")

        selected = max(0, min(selected, len(selectable_devices) - 1))
        content_changed = (items != last_rendered_items) or (selected != last_rendered_selected)
        heartbeat_due = now - last_redraw > 1.0  # catches the "scanning…" label ticking even with no input
        if content_changed or (heartbeat_due and now - last_redraw > 0.1):
            img = render_list_screen("Bluetooth", items, selected, footer="Hold Prev: back   Play: select")
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now
            last_rendered_items, last_rendered_selected = items, selected

        for ev in clicks:
            if ev == "next":
                selected = min(selected + 1, len(selectable_devices) - 1)
                status_msg = None
            elif ev == "prev":
                selected = max(selected - 1, 0)
                status_msg = None
            elif ev == "play_pause":
                target = selectable_devices[selected]
                if target is None: pass  
                elif target == "toggle_show_all":
                    show_all = not show_all
                    selected = 0
                elif target == "rescan":
                    bt.start_scan()
                    status_msg = "Scanning…"
                elif isinstance(target, tuple) and target[0] == "forget":
                    addr = target[1]
                    last_action_addr = addr
                    status_msg = "Forgetting…"
                    bt.remove(addr)
                else:
                    dev = target
                    last_action_addr = dev["address"]
                    if dev["connected"]:
                        status_msg = "Disconnecting…"
                        bt.disconnect(dev["address"])
                    elif dev["paired"]:
                        status_msg = "Connecting…"
                        bt.connect(dev["address"])
                    else:
                        status_msg = "Pairing…"
                        bt.pair(dev["address"])
        time.sleep(0.02)


class ThumbnailCacheWorker:
    def __init__(self, log_fn=print):
        self.log = log_fn
        self._thread = None
        self.is_running = False
        self._stop_event = threading.Event()

    def start(self, tracks, sizes=[140, 160]):
        if self.is_running: return
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(tracks, sizes), daemon=True, name="thumbnail-cache")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive(): self._thread.join(timeout=2.0)
        self.is_running = False

    def _run(self, tracks, sizes):
        self.log("CACHE", f"Starting background pre-cache for {len(tracks)} tracks...")
        try:
            for track in tracks:
                if self._stop_event.is_set(): break
                for size in sizes:
                    if self._stop_event.is_set(): break
                    _get_thumbnail(track, size)
            self.log("CACHE", "Background pre-cache cycle complete.")
        except Exception as e:
            self.log("CACHE", f"Error during pre-cache execution: {e}")
        finally:
            self.is_running = False

_thumb_cache = {}

def _get_thumbnail(track, size):
    if track is None: return None
    src = track.get("embedded_image")
    if src is None and track.get("image_path"):
        try: src = Image.open(track["image_path"]).convert("RGB")
        except Exception: src = None
    if src is None: return None

    key = (id(src), size)
    cached = _thumb_cache.get(key)
    if cached is not None: return cached

    t0 = time.perf_counter()
    try:
        thumb = src.copy()
        w, h = thumb.size
        side = min(w, h)
        thumb = thumb.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        # Large album art (1200px+ covers are common) made LANCZOS resize
        # alone take hundreds of ms -- enough to visibly stall navigation on
        # the first render of a never-before-thumbnailed artist/album. A
        # cheap pre-downsample (BILINEAR via .thumbnail(), which itself uses
        # a fast box filter for big reductions) gets close to the target
        # size first; the final LANCZOS pass then only has to do fine
        # resampling on an already-small image, which is fast.
        if side > size * 2:
            thumb.thumbnail((size * 2, size * 2), Image.BILINEAR)
        thumb = thumb.resize((size, size), Image.LANCZOS)
        _thumb_cache[key] = thumb
        elapsed = time.perf_counter() - t0
        if elapsed > 0.05:
            log("THUMB", f"SLOW: resize from {w}x{h} -> {size}x{size} took {elapsed*1000:.1f}ms "
                          f"(uncached, first render of this image)")
        return thumb
    except Exception: return None

def _get_collage_thumbnail(tracks, size):
    if not tracks: return None
    key = ("collage", tuple(t["file_path"] for t in tracks[:4]), size)
    if key in _thumb_cache: return _thumb_cache[key]

    collage = Image.new("RGB", (size, size), (20, 20, 25))
    half = size // 2

    if len(tracks) == 2:
        th1 = _get_thumbnail(tracks[0], size)
        th2 = _get_thumbnail(tracks[1], size)
        if th1 and th2:
            collage.paste(th1.crop((0, 0, half, size)), (0, 0))
            collage.paste(th2.crop((half, 0, size, size)), (half, 0))
            ImageDraw.Draw(collage).line([(half, 0), (half, size)], fill=(15, 15, 20), width=2)
            _thumb_cache[key] = collage
            return collage

    thumbs = []
    for t in tracks[:4]:
        th = _get_thumbnail(t, half)
        if th: thumbs.append(th)
    if not thumbs: return None

    positions = [(0, 0), (half, 0), (0, half), (half, half)]
    for i in range(4): collage.paste(thumbs[i % len(thumbs)], positions[i])

    draw = ImageDraw.Draw(collage)
    draw.line([(half, 0), (half, size)], fill=(15, 15, 20), width=2)
    draw.line([(0, half), (size, half)], fill=(15, 15, 20), width=2)
    _thumb_cache[key] = collage
    return collage

def _get_primary_artist(artist_string):
    if not artist_string: return "Unknown Artist"
    delimiters = r'(?i)\s+(feat\.?|ft\.?|featuring|vs\.?|with|and|\&)\s+|[,;/\\]+\s*'
    primary = re.split(delimiters, artist_string)[0]
    return primary.strip() or "Unknown Artist"

def _group_tracks(tracks):
    by_artist = {}
    for i, t in enumerate(tracks):
        raw_artist = t.get("artist") or "Unknown Artist"
        artist = _get_primary_artist(raw_artist)
        album = t.get("album") or "Unknown Album"
        by_artist.setdefault(artist, {}).setdefault(album, []).append((i, t))
    return by_artist

def run_library_screen(tracks, buttons, bt=None, upload_srv=None, genre_worker=None):
    log("MENU", "entered Library (Split View)")
    
    if buttons: buttons.poll()
        
    by_artist = _group_tracks(tracks)
    artists = sorted(by_artist.keys())

    level = "artist"
    cur_artist, cur_album = None, None
    selected = 0
    last_redraw = 0.0
    last_rendered_items, last_rendered_selected = None, None

    while True:
        items = []
        if level == "artist":
            title = "Artists"
            for a in artists:
                albums_dict = by_artist[a]
                count = sum(len(v) for v in albums_dict.values())
                first_album = sorted(albums_dict.keys())[0]
                _, rep_track = albums_dict[first_album][0]
                rep_list = [albums_dict[alb][0][1] for alb in sorted(albums_dict.keys())]
                genre = rep_track.get("genre") or "Unknown Genre"
                items.append((a, rep_list if len(rep_list) > 1 else rep_track, f"{count} track(s)", genre))
        elif level == "album":
            title = cur_artist
            albums = sorted(by_artist[cur_artist].keys())
            for al in albums:
                track_list = by_artist[cur_artist][al]
                _, rep_track = track_list[0]
                genre = rep_track.get("genre") or "Unknown Genre"
                items.append((al, rep_track, f"{len(track_list)} track(s)", f"{cur_artist} · {genre}"))
        else:
            title = cur_album
            track_list = by_artist[cur_artist][cur_album]
            for _, t in track_list:
                t_title = t.get("title") or os.path.basename(t["file_path"])
                dur = format_time(t.get("duration", 0))
                items.append((t_title, t, t.get("artist", "Unknown Artist"), dur))

        selected = max(0, min(selected, len(items) - 1)) if items else 0

        now = time.monotonic()
        content_changed = (items != last_rendered_items) or (selected != last_rendered_selected)
        if content_changed:
            t_render0 = time.perf_counter()
            img = render_split_library_screen(title, items or [("(empty)", None, "", "")], selected)
            render_elapsed = time.perf_counter() - t_render0
            if render_elapsed > 0.1:
                log("LIBRARY", f"SLOW render: {render_elapsed*1000:.1f}ms for level={level} "
                                f"selected={selected} ({len(items)} items)")
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now
            last_rendered_items, last_rendered_selected = items, selected

        clicks, holds, repeats = buttons.poll()
        if "quad_click_pause" in holds: return "quad_click_pause"

        for ev in holds:
            if ev == "play_pause":
                if bt is not None or upload_srv is not None:
                    res = run_settings_screen(bt, upload_srv, buttons, genre_worker, tracks)
                    if res == "quad_click_pause": return "quad_click_pause"
                    last_redraw = 0.0
                else:
                    return None
            if ev == "prev":
                if level == "track": level, selected, last_redraw = "album", 0, 0.0
                elif level == "album": level, selected, last_redraw = "artist", 0, 0.0
                else: return None

        for ev in clicks:
            if not items: continue
            if ev == "next": selected, last_redraw = min(selected + 1, len(items) - 1), 0.0
            elif ev == "prev": selected, last_redraw = max(selected - 1, 0), 0.0
            elif ev == "play_pause":
                if level == "artist": cur_artist, level, selected, last_redraw = artists[selected], "album", 0, 0.0
                elif level == "album": cur_album, level, selected, last_redraw = sorted(by_artist[cur_artist].keys())[selected], "track", 0, 0.0
                else: return by_artist[cur_artist][cur_album][selected][0]
        time.sleep(0.02)


def run_settings_screen(bt, upload_srv, buttons, genre_worker=None, tracks=None):
    log("MENU", "entered Settings")
    
    if buttons: buttons.poll()
        
    bt_item = ("Bluetooth", "Pair & connect devices") if bt is not None else ("Bluetooth", "Unavailable on this device")

    selected, last_redraw = 0, 0.0
    last_rendered_items, last_rendered_selected = None, None  # cache for change detection
    codec_status = get_active_bt_codec()
    last_codec_check = last_wifi_check = last_health_check = time.monotonic()
    upload_error = None
    wifi_status = wifi_get_status()
    used_pct, free_gb, total_gb = system_health.get_disk_usage(MUSIC_ROOT)
    thermal_status, cpu_temp = system_health.get_thermal_status()

    while True:
        now_check = time.monotonic()
        if now_check - last_codec_check > 3.0: codec_status, last_codec_check = get_active_bt_codec(), now_check
        if now_check - last_wifi_check > 5.0: wifi_status, last_wifi_check = wifi_get_status(), now_check
        if now_check - last_health_check > 10.0:
            used_pct, free_gb, total_gb = system_health.get_disk_usage(MUSIC_ROOT)
            thermal_status, cpu_temp = system_health.get_thermal_status()
            last_health_check = now_check

        if upload_srv.is_running(): upload_item = ("Upload Server: ON", f"{upload_srv.get_url_hint()}  pass: {upload_srv.password}")
        elif upload_error: upload_item = ("Upload Server: OFF", upload_error)
        else: upload_item = ("Upload Server: OFF", "Select to enable Wi-Fi music upload")

        wifi_sub = f'{wifi_status["ssid"]}  ({wifi_status["signal"]}%)' if wifi_status["connected"] else "Not connected"
        if wifi_status["connected"] and upload_srv.is_running(): wifi_sub += " · change via Upload URL"

        if genre_worker is None: genre_item = None
        elif genre_worker.is_running: genre_item = ("Genre Tagging: Running", f"Checked {genre_worker.tracks_checked}, filled {genre_worker.tracks_filled}")
        elif genre_worker.tracks_checked > 0: genre_item = ("Genre Tagging: Idle", f"Last run: checked {genre_worker.tracks_checked}, filled {genre_worker.tracks_filled} (Click to trigger)")
        else: genre_item = ("Genre Tagging: Idle", "Click to scan / fetch missing genres")

        if total_gb:
            storage_sub = f"{free_gb}GB free of {total_gb}GB ({used_pct}% used)"
            if used_pct is not None and used_pct >= 90:
                storage_sub += "  ⚠ low space"
        else:
            storage_sub = "Unable to read"

        if cpu_temp is not None:
            temp_sub = f"{cpu_temp:.0f}°C"
            if thermal_status == "HOT": temp_sub += "  ⚠ HOT"
            elif thermal_status == "WARM": temp_sub += "  (warm)"
        else:
            temp_sub = "Unable to read"

        uptime_sub = system_health.format_uptime(system_health.get_uptime_s())

        items = [
            bt_item,
            ("Brightness", f"{backlight.get_brightness()}%  (Vol +/- to adjust)"),
            ("Screen Off", "Tap any button to wake"),
            ("Audio Codec", f"{codec_status}  (read-only)"),
            upload_item,
            ("Wi-Fi", wifi_sub),
            ("Listening Stats", f"{stats.get_total_plays()} plays  ·  streak {stats.get_current_streak()}d"),
            ("Storage", storage_sub),
            ("CPU Temp", temp_sub),
            ("Uptime", uptime_sub),
        ]
        if genre_item is not None: items.append(genre_item)
        selected = max(0, min(selected, len(items) - 1))

        now = time.monotonic()
        content_changed = (items != last_rendered_items) or (selected != last_rendered_selected)
        heartbeat_due = now - last_redraw > 1.0  # catches Uptime/CPU-temp ticking even with no input
        if content_changed or (heartbeat_due and now - last_redraw > 0.1):
            img = render_list_screen("Settings", items, selected, footer="Hold Prev: back   Play: select")
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now
            last_rendered_items, last_rendered_selected = items, selected

        clicks, holds, repeats = buttons.poll()
        if "quad_click_pause" in holds: return "quad_click_pause"
        for ev in holds:
            if ev in ("prev", "play_pause"): return

        if selected == 1:
            for ev in clicks + repeats:
                if ev == "vol_up": backlight.set_brightness(backlight.get_brightness() + 5)
                elif ev == "vol_down": backlight.set_brightness(backlight.get_brightness() - 5)

        for ev in clicks:
            if ev == "next": selected = min(selected + 1, len(items) - 1)
            elif ev == "prev": selected = max(selected - 1, 0)
            elif ev == "play_pause":
                if selected == 0 and bt is not None:
                    res = run_bluetooth_screen(bt, buttons)
                    if res == "quad_click_pause": return "quad_click_pause"
                elif selected == 2: run_screen_off(buttons)
                elif selected == 4:
                    if upload_srv.is_running():
                        upload_srv.stop()
                        upload_error = None
                    else:
                        try:
                            upload_srv.start()
                            upload_error = None
                        except UploadServerUnavailable as e:
                            upload_error = "Flask not installed" if "Flask isn't installed" in str(e) else "Couldn't start"
                elif selected == 6:
                    res = run_stats_screen(buttons, tracks)
                    if res == "quad_click_pause": return "quad_click_pause"
                elif genre_item is not None and selected == len(items) - 1:
                    if not genre_worker.is_running:
                        if wifi_get_status()["connected"] and tracks: genre_worker.start(tracks, force_full=True)
        time.sleep(0.02)


def _track_label(tracks_by_path, file_path):
    t = tracks_by_path.get(file_path)
    if t is None:
        return os.path.basename(file_path), ""
    return t.get("title") or os.path.basename(file_path), t.get("artist", "")


def _draw_stats_summary(tracks_by_path):
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)

    draw.text((20, 14), "Listening Stats", font=title_font, fill=MENU_TEXT)
    draw.rectangle([0, 48, WIDTH, 49], fill=(50, 50, 58))

    total = stats.get_total_plays()
    streak = stats.get_current_streak()
    draw.text((20, 60), f"{total}", font=ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26), fill=MENU_ACCENT)
    draw.text((20, 90), "total plays", font=row_font, fill=MENU_SUBTEXT)
    draw.text((180, 60), f"{streak}d", font=ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26), fill=MENU_ACCENT)
    draw.text((180, 90), "current streak", font=row_font, fill=MENU_SUBTEXT)

    y = 120
    draw.text((20, y), "MOST PLAYED", font=label_font, fill=MENU_SUBTEXT)
    y += 20
    for path, plays in stats.get_most_played(4):
        title, artist = _track_label(tracks_by_path, path)
        draw.text((20, y), f"{title[:30]}", font=row_font, fill=MENU_TEXT)
        draw.text((WIDTH - 60, y), f"{plays}x", font=row_font, fill=MENU_ACCENT)
        y += 18

    y += 8
    draw.text((20, y), "RECENTLY PLAYED", font=label_font, fill=MENU_SUBTEXT)
    y += 20
    for path in stats.get_recent(3):
        title, artist = _track_label(tracks_by_path, path)
        draw.text((20, y), f"{title[:34]}", font=row_font, fill=MENU_TEXT)
        y += 18

    draw.text((20, HEIGHT - 26), "Next: genre chart   Hold Prev: back", font=row_font, fill=MENU_SUBTEXT)
    return img


_PIE_COLORS = [
    (232, 106, 38), (90, 160, 220), (120, 210, 130), (220, 90, 130),
    (200, 180, 70), (150, 110, 220), (90, 200, 190), (220, 140, 90),
]


def _draw_genre_pie(tracks_by_path):
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)

    draw.text((20, 14), "Genre Breakdown", font=title_font, fill=MENU_TEXT)
    draw.rectangle([0, 48, WIDTH, 49], fill=(50, 50, 58))

    counts = {}
    for t in tracks_by_path.values():
        g = (t.get("genre") or "Unknown Genre").strip() or "Unknown Genre"
        counts[g] = counts.get(g, 0) + 1
    total = sum(counts.values())

    if total == 0:
        draw.text((20, 80), "No tracks in library.", font=row_font, fill=MENU_SUBTEXT)
        return img

    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    cx, cy, r = 130, 175, 90
    start_angle = -90.0
    for i, (genre, count) in enumerate(ordered):
        sweep = 360.0 * count / total
        color = _PIE_COLORS[i % len(_PIE_COLORS)]
        draw.pieslice([cx - r, cy - r, cx + r, cy + r], start_angle, start_angle + sweep, fill=color)
        start_angle += sweep

    legend_x, legend_y = 250, 70
    for i, (genre, count) in enumerate(ordered[:8]):
        color = _PIE_COLORS[i % len(_PIE_COLORS)]
        yy = legend_y + i * 22
        draw.rectangle([legend_x, yy + 3, legend_x + 12, yy + 15], fill=color)
        pct = round(100 * count / total)
        draw.text((legend_x + 18, yy), f"{genre[:16]} ({pct}%)", font=row_font, fill=MENU_TEXT)

    draw.text((20, HEIGHT - 26), "Prev: back to stats   Hold Prev: exit", font=row_font, fill=MENU_SUBTEXT)
    return img


def run_stats_screen(buttons, tracks):
    """Two-page stats view: summary (plays/streak/most-played/recent), then
    a genre breakdown pie chart. Next/Prev (click) flips pages; hold-Prev
    or hold-Play exits back to Settings -- same exit gesture as every other
    sub-screen."""
    tracks_by_path = {t["file_path"]: t for t in (tracks or [])}
    page, last_redraw, last_rendered_page = 0, 0.0, None

    while True:
        now = time.monotonic()
        if page != last_rendered_page:
            img = _draw_stats_summary(tracks_by_path) if page == 0 else _draw_genre_pie(tracks_by_path)
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now
            last_rendered_page = page

        clicks, holds, repeats = buttons.poll()
        if "quad_click_pause" in holds: return "quad_click_pause"
        for ev in holds:
            if ev in ("prev", "play_pause"):
                return
        for ev in clicks:
            if ev == "next" and page == 0:
                page = 1
                last_redraw = 0.0
            elif ev == "prev" and page == 1:
                page = 0
                last_redraw = 0.0
        time.sleep(0.02)


def _draw_diagnostics_page1():
    """Boot history: last boot's per-check results, plus any recorded
    fatal failure history and flaky-check frequency across recent boots."""
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)

    draw.text((20, 14), "Diagnostics: Boot History", font=title_font, fill=MENU_TEXT)
    draw.rectangle([0, 48, WIDTH, 49], fill=(50, 50, 58))

    history = boot_history.get_history()
    y = 62
    if not history:
        draw.text((20, y), "No boot history recorded yet.", font=row_font, fill=MENU_SUBTEXT)
    else:
        last = history[0]
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last["timestamp"]))
        draw.text((20, y), f"LAST BOOT: {ts}", font=label_font, fill=MENU_SUBTEXT)
        y += 20
        for check in last["checks"]:
            color = (120, 210, 130) if check["ok"] else (220, 90, 90)
            mark = "OK" if check["ok"] else "FAIL"
            draw.text((20, y), check["label"], font=row_font, fill=MENU_TEXT)
            mw = draw.textlength(mark, font=row_font)
            draw.text((WIDTH - 24 - mw, y), mark, font=row_font, fill=color)
            y += 17

        freq = boot_history.get_failure_frequency()
        flaky = sorted(freq.items(), key=lambda kv: -kv[1])[:3]
        if flaky:
            y += 6
            draw.text((20, y), f"MOST FREQUENT FAILURES (last {len(history)} boots)",
                      font=label_font, fill=MENU_SUBTEXT)
            y += 18
            for label, count in flaky:
                draw.text((20, y), f"{label}: failed {count}x", font=row_font, fill=(220, 150, 90))
                y += 17

    draw.text((20, HEIGHT - 26), "Next: reliability   Hold Prev/Play: exit", font=row_font, fill=MENU_SUBTEXT)
    return img


def _draw_diagnostics_page2():
    """Reliability data: current per-file failure counts and what's been
    quarantined."""
    img = Image.new("RGB", (WIDTH, HEIGHT), MENU_BG)
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)

    draw.text((20, 14), "Diagnostics: Reliability", font=title_font, fill=MENU_TEXT)
    draw.rectangle([0, 48, WIDTH, 49], fill=(50, 50, 58))

    y = 62
    failures = reliability.get_all_failure_counts()
    draw.text((20, y), "CURRENT FAILURE STREAKS", font=label_font, fill=MENU_SUBTEXT)
    y += 18
    if not failures:
        draw.text((20, y), "No tracks currently showing failures.", font=row_font, fill=MENU_TEXT)
        y += 17
    else:
        for path, count in list(failures.items())[:8]:
            name = os.path.basename(path)
            draw.text((20, y), f"{name[:38]}", font=row_font, fill=MENU_TEXT)
            draw.text((WIDTH - 50, y), f"{count}/{reliability.MAX_CONSECUTIVE_FAILURES}",
                      font=row_font, fill=(220, 150, 90))
            y += 17

    y += 10
    draw.text((20, y), "QUARANTINED THIS SESSION", font=label_font, fill=MENU_SUBTEXT)
    y += 18
    quarantined = getattr(reliability, "_quarantined_this_session", [])
    if not quarantined:
        draw.text((20, y), "None.", font=row_font, fill=MENU_TEXT)
    else:
        for path in quarantined[:6]:
            draw.text((20, y), os.path.basename(path)[:42], font=row_font, fill=(220, 90, 90))
            y += 17

    draw.text((20, HEIGHT - 26), "Prev: back   Hold Prev/Play: exit", font=row_font, fill=MENU_SUBTEXT)
    return img


def run_diagnostics_screen(buttons):
    """
    Hidden screen, reached by holding Vol Up + Vol Down together (see
    play_single_track) -- intentionally not advertised in any visible menu.
    Two pages: boot-check history, then reliability/quarantine data.
    """
    log("DIAG", "entered hidden diagnostics screen")
    page, last_redraw, last_rendered_page = 0, 0.0, None
    while True:
        now = time.monotonic()
        if page != last_rendered_page:
            img = _draw_diagnostics_page1() if page == 0 else _draw_diagnostics_page2()
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = now
            last_rendered_page = page

        clicks, holds, repeats = buttons.poll()
        for ev in holds:
            if ev in ("prev", "play_pause"):
                log("DIAG", "exiting hidden diagnostics screen")
                return
        for ev in clicks:
            if ev == "next" and page == 0:
                page = 1
            elif ev == "prev" and page == 1:
                page = 0
        time.sleep(0.02)


def run_screen_off(buttons):
    prev_brightness = backlight.get_brightness()
    backlight.fade_to(0, duration_s=0.25)
    try:
        while True:
            clicks, holds, repeats = buttons.poll()
            if clicks or holds: break
            time.sleep(0.05)
    finally:
        backlight.fade_to(prev_brightness, duration_s=0.25)


def run_menu(bt, upload_srv, tracks, buttons, genre_worker=None):
    log("MENU", "entered Menu Layout")
    
    if buttons: buttons.poll()
        
    items = [("Library", "Browse by artist"), ("Settings", "Bluetooth & more")]
    selected, last_redraw, last_rendered_selected = 0, 0.0, None

    while True:
        if selected != last_rendered_selected:
            img = render_list_screen("Menu", items, selected, footer="Hold Play: resume   Play: select")
            blit_rect_buf(0, 0, WIDTH, HEIGHT, img.tobytes())
            last_redraw = time.monotonic()
            last_rendered_selected = selected

        clicks, holds, repeats = buttons.poll()
        if "quad_click_pause" in holds: return "quad_click_pause"
        for ev in holds:
            if ev in ("prev", "play_pause"): return None
            
        for ev in clicks:
            if ev == "next": selected = min(selected + 1, len(items) - 1)
            elif ev == "prev": selected = max(selected - 1, 0)
            elif ev == "play_pause":
                if selected == 0:
                    chosen = run_library_screen(tracks, buttons, bt, upload_srv, genre_worker)
                    if chosen == "quad_click_pause": return "quad_click_pause"
                    if chosen is not None: return chosen
                elif selected == 1:
                    res = run_settings_screen(bt, upload_srv, buttons, genre_worker, tracks)
                    if res == "quad_click_pause": return "quad_click_pause"
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
        if exclude_index is not None and exclude_index in order and self.num_tracks > 1: order.remove(exclude_index)
        random.shuffle(order)
        self._order = order

    def toggle(self, current_index):
        self.active = not self.active
        if self.active:
            self.history = [current_index]
            self.hist_pos = 0
            self._reshuffle(exclude_index=current_index)
        return self.active

    def next_index(self, current_index):
        if self.hist_pos < len(self.history) - 1:
            self.hist_pos += 1
            return self.history[self.hist_pos]
        if not self._order:
            self._reshuffle(exclude_index=current_index)
            if not self._order: return current_index
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
    album_x, album_y = (WIDTH - album_size) // 2, 25 
    spool_size = 48
    spool_y = album_y + album_size + 25 
    spool1_x, spool2_x = 180, 252
    tape_x, tape_y, tape_w, tape_h = 204, spool_y + (spool_size // 2) - 1, 72, 2

    layout = {
        "bg": bg, "theme": theme, "album_size": album_size, "album_x": album_x, "album_y": album_y,
        "spool1_x": spool1_x, "spool2_x": spool2_x, "spool_y": spool_y, "spool_size": spool_size,
        "tape_x": tape_x, "tape_y": tape_y, "tape_w": tape_w, "tape_h": tape_h,
        "animator": SpoolAnimator(spool_size, theme["accent"], bg),
        "shuffle_icon_x": WIDTH - 20 - 22, "shuffle_icon_y": 16, "shuffle_icon_size": 22, "shuffle_active": False,
    }

    pil_img = track.get("embedded_image")
    if not pil_img and track.get("image_path"):
        try: pil_img = Image.open(track["image_path"])
        except Exception: pass
    if not pil_img: pil_img = Image.new("RGB", (album_size, album_size), (100, 100, 100))

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
    sh_draw = ImageDraw.Draw(shadow_mask)
    sh_off, b_th = 8, 2
    sx, sy = pad + sh_off, pad + sh_off
    sh_draw.rectangle([sx, sy, sx + album_size + b_th*2 - 1, sy + album_size + b_th*2 - 1], fill=(0,0,0, 160))
    asm_img.paste(shadow_mask.filter(ImageFilter.GaussianBlur(6)), (0,0), shadow_mask)

    ImageDraw.Draw(asm_img).rectangle([pad, pad, pad + album_size + b_th*2 - 1, pad + album_size + b_th*2 - 1], fill=(15,15,20))
    asm_img.paste(pil_img, (pad + b_th, pad + b_th))

    layout.update({"asm_img": asm_img, "asm_x": album_x - pad, "asm_y": album_y - pad, "asm_size": asm_size, "duration_secs": int(track.get("duration", 252)), "time_y": 270 + 10})

    draw_text_ttf(20, 270, track.get("title", "Unknown Title"), theme["text"], bg, font_size=20, max_width=200)
    draw_text_ttf(20, 270 + 24, track.get("artist", "Unknown Artist"), theme["subtext"], bg, font_size=16, max_width=200)

    track_color = (int(bg[0]*0.5), int(bg[1]*0.5), int(bg[2]*0.5))
    layout["track_color"] = track_color

    vol_w, vol_h = 180, 10
    layout.update({"vol_x": (WIDTH - vol_w) // 2, "vol_y": album_y + (album_size // 2) - 5, "vol_w": vol_w, "vol_h": vol_h, "last_vol": -1.0})
    draw_shuffle_icon(layout["shuffle_icon_x"], layout["shuffle_icon_y"], layout["shuffle_icon_size"], theme["accent"], bg, active=False)
    blit_rect_buf(layout["asm_x"], layout["asm_y"], layout["asm_size"], layout["asm_size"], layout["asm_img"].tobytes())

    return layout

def update_shuffle_icon(layout, active):
    if layout.get("shuffle_active") == active: return
    layout["shuffle_active"] = active
    draw_shuffle_icon(layout["shuffle_icon_x"], layout["shuffle_icon_y"], layout["shuffle_icon_size"], layout["theme"]["accent"], layout["bg"], active=active)

def update_progress_bar(layout, progress):
    progress = max(0.0, min(1.0, progress))
    tx, ty, tw, th = layout["tape_x"], layout["tape_y"], layout["tape_w"], layout["tape_h"]
    filled_w = int(tw * progress)
    if filled_w > 0: blit_rect_buf(tx, ty, filled_w, th, make_solid_buf(filled_w, th, *layout["theme"]["accent"]))
    if filled_w < tw: blit_rect_buf(tx + filled_w, ty, tw - filled_w, th, make_solid_buf(tw - filled_w, th, *layout["track_color"]))

def update_volume_bar(layout, volume):
    volume = max(0.0, min(1.0, volume))
    if abs(volume - layout.get("last_vol", -1)) < 0.005: return
    layout["last_vol"] = volume

    vol_x, vol_y, vol_w, vol_h = layout["vol_x"], layout["vol_y"], layout["vol_w"], layout["vol_h"]
    accent, text_col = layout["theme"]["accent"], layout["theme"]["text"]
    asm_x, asm_y, asm_size = layout["asm_x"], layout["asm_y"], layout["asm_size"]
    
    frame = layout["asm_img"].convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0,0,0,0))
    ov_draw = ImageDraw.Draw(overlay)
    
    filled = int(vol_w * volume)
    if filled < vol_w: ov_draw.rectangle([vol_x - asm_x + filled, vol_y - asm_y, vol_x - asm_x + vol_w - 1, vol_y - asm_y + vol_h - 1], fill=(20, 20, 25, 180))
    if filled > 0: ov_draw.rectangle([vol_x - asm_x, vol_y - asm_y, vol_x - asm_x + filled - 1, vol_y - asm_y + vol_h - 1], fill=(accent[0], accent[1], accent[2], 255))
        
    frame.alpha_composite(overlay)
    frame = frame.convert("RGB")
    draw = ImageDraw.Draw(frame)

    label = f"VOL {int(volume * 100)}%"
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except: font = ImageFont.load_default()
    
    bbox = draw.textbbox((0,0), label, font=font)
    draw.text((vol_x - asm_x + (vol_w - (bbox[2] - bbox[0])) // 2 + 1, vol_y - asm_y - 24), label, font=font, fill=(0,0,0))
    draw.text((vol_x - asm_x + (vol_w - (bbox[2] - bbox[0])) // 2, vol_y - asm_y - 25), label, font=font, fill=text_col)
    
    slice_y_start, slice_y_end = max(0, vol_y - asm_y - 30), min(asm_size, vol_y - asm_y + vol_h + 5)
    blit_rect_buf(asm_x, asm_y + slice_y_start, asm_size, slice_y_end - slice_y_start, frame.crop((0, slice_y_start, asm_size, slice_y_end)).tobytes())

def update_current_time_label(layout, cur_secs, last_str=None):
    current_str = format_time(cur_secs)
    if current_str == last_str: return last_str

    bg, accent = layout["bg"], layout["theme"]["accent"]
    clear_w, clear_h = 80, 24
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except: font = ImageFont.load_default()
    
    img = Image.new("RGB", (clear_w, clear_h), bg)
    ImageDraw.Draw(img).text((clear_w - ImageDraw.Draw(img).textbbox((0, 0), current_str, font=font)[2], 0), current_str, font=font, fill=accent)
    blit_rect_buf(WIDTH - clear_w - 20, layout["time_y"], clear_w, clear_h, img.tobytes())
    return current_str

def play_single_track(player, track, buttons=None, shuffle=None, current_index=0):
    if track.get("duration", 0) <= 0:
        meta_dur = get_duration_mutagen(track["file_path"])
        if meta_dur and meta_dur > 0: track["duration"] = meta_dur

    _, state, _ = player.playbin.get_state(0)
    if state != Gst.State.PLAYING:
        player.load(track["file_path"])
        player.play()

    t0 = time.monotonic()
    load_failed = False
    while time.monotonic() - t0 < 5.0:
        state_change_return, state, _ = player.playbin.get_state(0)
        if state == Gst.State.PLAYING:
            break
        if state_change_return == Gst.StateChangeReturn.FAILURE:
            load_failed = True
            break
        if buttons: buttons.poll()
        time.sleep(0.01)
    else:
        # Loop ran the full 5s without reaching PLAYING or FAILURE -- e.g. a
        # hung/corrupt file that never resolves either way. Treat the same
        # as a failure rather than falling through into a UI for a track
        # that was never actually confirmed playing.
        _, state, _ = player.playbin.get_state(0)
        load_failed = state != Gst.State.PLAYING

    if load_failed:
        player.stop()
        count = reliability.record_failure(track["file_path"])
        log("RELIABILITY", f"playback failed for {track['file_path']!r} "
                            f"({count}/{reliability.MAX_CONSECUTIVE_FAILURES})")
        if reliability.should_quarantine(track["file_path"]):
            reliability.quarantine_file(track["file_path"], log=log)
        return "load_failed"

    reliability.record_success(track["file_path"])

    duration_secs = 0.0
    t0 = time.perf_counter()
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
    if shuffle is not None: update_shuffle_icon(layout, shuffle.active)

    last_time_str, last_progress, spool_phase, last_spin, last_pos_poll = None, -1.0, 0, time.perf_counter(), 0.0
    pos_secs, progress, is_playing, action = 0.0, 0.0, True, "ended"
    
    if buttons:
        buttons.poll()
        buttons.last_interaction_time = time.monotonic()

    last_vol_up_hold_t, last_vol_down_hold_t = -999.0, -999.0  # for the hidden
    COMBO_WINDOW_S = 0.3  # diagnostics-menu combo: hold Vol Up + Vol Down together

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
                layout["animator"].blit(layout["spool1_x"], layout["spool_y"], spool_phase)
                layout["animator"].blit(layout["spool2_x"], layout["spool_y"], spool_phase)
            if layout.get("vol_visible"): update_volume_bar(layout, player.get_volume())
            last_spin = now

        if buttons:
            clicks, holds, repeats = buttons.poll()
            vol_delta = 0.0
            
            if "quad_click_pause" in holds or any(c == "quad_click_pause" for c in clicks):
                log("AUDIO", "Global emergency halt captured.")
                player.pause()
                is_playing = False

            if "play_pause" in holds: return "menu"
            if "prev" in holds: return "library"
            if "next" in holds and shuffle is not None:
                shuffle.toggle(current_index)
                update_shuffle_icon(layout, shuffle.active)

            # Hidden diagnostics menu: hold Vol Up and Vol Down together
            # (within a short window of each other, since two real button
            # presses are never perfectly simultaneous). Deliberately not
            # advertised anywhere in the normal UI -- this is for your own
            # troubleshooting, not a feature a casual user should stumble
            # into by holding volume buttons during normal use.
            now_combo = time.monotonic()
            if "vol_up" in holds: last_vol_up_hold_t = now_combo
            if "vol_down" in holds: last_vol_down_hold_t = now_combo
            if abs(last_vol_up_hold_t - last_vol_down_hold_t) <= COMBO_WINDOW_S and \
               now_combo - max(last_vol_up_hold_t, last_vol_down_hold_t) <= COMBO_WINDOW_S and \
               last_vol_up_hold_t > 0 and last_vol_down_hold_t > 0:
                last_vol_up_hold_t, last_vol_down_hold_t = -999.0, -999.0  # consume, avoid re-triggering
                log("DIAG", "hidden diagnostics combo detected (hold Vol+ and Vol- together)")
                player.pause()
                is_playing = False
                run_diagnostics_screen(buttons)
                player.play()
                is_playing = True

            for ev in clicks:
                if ev == "play_pause":
                    player.pause() if is_playing else player.play()
                    is_playing = not is_playing
                elif ev == "next":
                    player.stop()
                    if duration_secs > 0 and pos_secs / duration_secs >= 0.5:
                        stats.record_play(track["file_path"])
                    else:
                        stats.record_skip(track["file_path"])
                    return "next"
                elif ev == "prev":
                    if pos_secs < 3.0:
                        player.stop()
                        stats.record_skip(track["file_path"])
                        return "prev"
                    player.seek_to_start()
                elif ev == "vol_up": vol_delta += 0.05
                elif ev == "vol_down": vol_delta -= 0.05
                        
            for ev in holds + repeats:
                if ev == "vol_up": vol_delta += 0.05
                elif ev == "vol_down": vol_delta -= 0.05
                    
            if vol_delta != 0.0:
                player.change_volume(vol_delta)
                layout.update({"vol_visible": True, "vol_last_change": time.monotonic()})
                update_volume_bar(layout, player.get_volume())

        if duration_secs > 0 and pos_secs >= duration_secs:
            stats.record_play(track["file_path"])
            break
        
        if layout.get("vol_visible") and (time.monotonic() - layout["vol_last_change"] > 1.5):
            layout["vol_visible"] = False
            layout["last_vol"] = -1.0
            blit_rect_buf(layout["asm_x"], layout["asm_y"], layout["asm_size"], layout["asm_size"], layout["asm_img"].tobytes())

        time.sleep(0.001)

    player.stop()
    return action

def ui_loop():
    tracks = scan_music_folder(MUSIC_ROOT, default_image_path=DEFAULT_ART_PATH)
    if not tracks:
        tracks = [build_track_from_file("/home/rock/Dash Out feat Spiro.mp3", default_image_path=DEFAULT_ART_PATH)]

    player = GstPlayer()
    player.set_volume(0.8)
    buttons = ButtonHandler(backlight, btn_play_pause_line, btn_next_line, btn_prev_line, btn_vol_up_line, btn_vol_down_line)

    bt = None
    try:
        bt = BluetoothManager()
        bt.start()
    except BluetoothUnavailable: pass

    upload_srv = MusicUploadServer(MUSIC_ROOT, log_fn=lambda tag, msg: log(tag, msg))

    if not wifi_get_status()["connected"]:
        ok, msg = wifi_start_hotspot()
        if ok:
            try: upload_srv.start()
            except UploadServerUnavailable: pass
            run_wifi_setup_screen(buttons, lambda: wifi_get_status()["connected"])
            if wifi_is_hotspot_active(): wifi_stop_hotspot()

    genre_worker = GenreFillWorker(wifi_get_status, log_fn=log)
    if wifi_get_status()["connected"]: genre_worker.start(tracks, force_full=False)
        
    cache_worker = ThumbnailCacheWorker(log_fn=log)
    cache_worker.start(tracks)
    shuffle = ShuffleState(len(tracks))

    def rescan_if_needed(currently_playing_path):
        nonlocal tracks, shuffle
        if not upload_srv.is_rescan_pending(): return None
        upload_srv.consume_rescan_flag()
        new_tracks = scan_music_folder(MUSIC_ROOT, default_image_path=DEFAULT_ART_PATH)
        if not new_tracks: return None
        tracks = new_tracks
        shuffle = ShuffleState(len(tracks))
        if cache_worker.is_running: cache_worker.stop()
        cache_worker.start(tracks)

        if currently_playing_path is None: return 0
        try: return next(i for i, t in enumerate(tracks) if t["file_path"] == currently_playing_path)
        except StopIteration: return 0

    try:
        chosen = run_library_screen(tracks, buttons, bt, upload_srv, genre_worker)
        rescanned_idx = rescan_if_needed(None)
        if rescanned_idx is not None and (chosen is None or isinstance(chosen, str)): chosen = rescanned_idx
        while chosen is None or isinstance(chosen, str): chosen = run_library_screen(tracks, buttons, bt, upload_srv, genre_worker)
        index = chosen

        while True:
            result = play_single_track(player, tracks[index], buttons, shuffle=shuffle, current_index=index)
            current_track_path = tracks[index]["file_path"] if index < len(tracks) else None

            rescanned_idx = rescan_if_needed(current_track_path)
            if rescanned_idx is not None:
                index = rescanned_idx
                if genre_worker.is_running: genre_worker.stop()
                if wifi_get_status().get("connected"): genre_worker.start(tracks, force_full=False)

            if result == "quad_click_pause":
                player.pause()
                chosen = run_library_screen(tracks, buttons, bt, upload_srv, genre_worker)
                if chosen is not None and isinstance(chosen, int): index = chosen
                continue

            # CRITICAL FIX HERE: Explicitly limit string checking strictly to menu context tokens
            if result in ("menu", "library"):
                if result == "menu": chosen = run_menu(bt, upload_srv, tracks, buttons, genre_worker)
                else: chosen = run_library_screen(tracks, buttons, bt, upload_srv, genre_worker)
                
                if chosen == "quad_click_pause" or (chosen is not None and isinstance(chosen, str)):
                    player.pause()
                    chosen = run_library_screen(tracks, buttons, bt, upload_srv, genre_worker)

                if chosen is not None and isinstance(chosen, int): index = chosen
                continue

            if result == "load_failed":
                # Already recorded/quarantined inside play_single_track.
                # Re-scan if a quarantine just happened so the dead file
                # actually disappears from `tracks`, not just from disk.
                fresh = scan_music_folder(MUSIC_ROOT, default_image_path=DEFAULT_ART_PATH)
                if fresh:
                    tracks[:] = fresh
                    index = index % len(tracks)
                else:
                    log("RELIABILITY", "library is empty after quarantine, returning to Library screen")
                    chosen = run_library_screen(tracks, buttons, bt, upload_srv, genre_worker)
                    if chosen is not None and isinstance(chosen, int): index = chosen
                continue

            if shuffle.active: index = shuffle.prev_index(index) if result == "prev" else shuffle.next_index(index)
            else: index = (index - 1) % len(tracks) if result == "prev" else (index + 1) % len(tracks)
                
    finally:
        buttons.stop()
        if upload_srv.is_running(): upload_srv.stop()
        if genre_worker.is_running: genre_worker.stop()
        if cache_worker.is_running: cache_worker.stop()


def main():
    init_hardware()
    ili9488_init()
    write_cmd(0x53); write_data_byte(0x2C)  
    write_cmd(0x51); write_data_byte(0xFF)  
    ui_loop()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: pass
    finally:
        try: stats.flush()
        except Exception: pass
        try: fill_screen_rgb888(0, 0, 0)
        except Exception: pass
        try: backlight.shutdown()
        except Exception: pass
        spi.close(); dc_line.set_value(0); rst_line.set_value(1)
        dc_line.release(); rst_line.release()
        btn_play_pause_line.release(); btn_next_line.release(); btn_prev_line.release(); btn_vol_up_line.release(); btn_vol_down_line.release()
        chip.close()