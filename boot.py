"""
boot.py — startup sequence: splash screen, subsystem checks, then handoff.

Run this instead of music_player.py directly:
    python3 boot.py

What it does, in order:
  1. Initialize hardware (GPIO/SPI/backlight) and the display -- this one
     step is special-cased because every check after it needs a working
     screen to report results on. If it fails, there's nothing to draw to,
     so that failure goes to stdout/log only.
  2. Show a splash/logo screen briefly.
  3. Run each subsystem check in turn, drawing live pass/fail results to
     the screen as they complete (not a frozen logo the whole time).
  4. If every check critical to basic playback passed, hand off to
     music_player.ui_loop(). Non-critical failures (Bluetooth, WiFi) are
     shown but don't block boot -- those features just won't work this
     session, same as today.
  5. If a *critical* check fails (display already succeeded by definition,
     so this means SPI display content, audio, or the music library),
     show a clear failure screen and stay there rather than silently
     limping into a broken ui_loop().
"""

import os
import sys
import time
import traceback

import music_player as mp


# ================================
# SPLASH / LOGO
# ================================
def draw_splash():
    """
    Mishmann wordmark over a static pair of spools linked by a tape line --
    the same motif used on the real Now Playing screen (SpoolAnimator),
    rather than a generic centered-text title card. Uses the project's
    actual palette constants (MENU_BG/MENU_TEXT/MENU_SUBTEXT/MENU_ACCENT)
    so this screen reads as the same product, not a placeholder bolted on.
    """
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (mp.WIDTH, mp.HEIGHT), mp.MENU_BG)
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
        sub_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception:
        title_font = sub_font = ImageFont.load_default()

    title = "MISHMANN"
    tw = draw.textlength(title, font=title_font)
    title_y = mp.HEIGHT * 0.28
    draw.text(((mp.WIDTH - tw) / 2, title_y), title,
               font=title_font, fill=mp.MENU_TEXT)
    underline_w = 40
    draw.rectangle([(mp.WIDTH - underline_w) / 2, title_y + 38,
                    (mp.WIDTH + underline_w) / 2, title_y + 41], fill=mp.MENU_ACCENT)

    # Static spool pair + tape line -- same geometry/proportions as the real
    # Now Playing screen's SpoolAnimator (48px spools, accent-colored line),
    # just drawn once rather than driven by playback phase.
    spool_size = 48
    spool_y = title_y + 70
    gap = 76
    spool1_x = mp.WIDTH / 2 - gap / 2 - spool_size / 2
    spool2_x = mp.WIDTH / 2 + gap / 2 - spool_size / 2

    tape_y = spool_y + spool_size / 2
    draw.line([(spool1_x + spool_size, tape_y), (spool2_x, tape_y)],
               fill=mp.MENU_ACCENT, width=2)

    for sx in (spool1_x, spool2_x):
        cx, cy, r = sx + spool_size / 2, spool_y + spool_size / 2, spool_size / 2 - 2
        draw.ellipse([sx + 2, spool_y + 2, sx + spool_size - 3, spool_y + spool_size - 3],
                     outline=mp.MENU_ACCENT, width=2)
        inner_r = spool_size * 0.12
        draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill=mp.MENU_ACCENT)
        hole_r = spool_size * 0.04
        draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r], fill=mp.MENU_BG)

    sub = "starting up…"
    sw = draw.textlength(sub, font=sub_font)
    sub_y = spool_y + spool_size + 24
    draw.text(((mp.WIDTH - sw) / 2, sub_y), sub,
               font=sub_font, fill=mp.MENU_SUBTEXT)
    mp.blit_rect_buf(0, 0, mp.WIDTH, mp.HEIGHT, img.tobytes())


# ================================
# CHECKS RENDERING
# ================================
def draw_checks_screen(results, in_progress_label=None):
    """
    results: list of (label, status) where status is True/False/None
    (None = not yet run). Drawn as a simple top-down checklist, redrawn
    fresh each time a check completes -- cheap enough that there's no need
    to diff/partial-update for a screen that's shown for a couple seconds.
    """
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (mp.WIDTH, mp.HEIGHT), mp.MENU_BG)
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        row_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    except Exception:
        title_font = row_font = ImageFont.load_default()

    # Same header treatment as every other screen (render_list_screen etc):
    # title + thin divider + short accent underline tab, not a bespoke style.
    draw.text((20, 14), "Checking systems…", font=title_font, fill=mp.MENU_TEXT)
    draw.rectangle([0, 48, mp.WIDTH, 49], fill=(50, 50, 58))
    draw.rectangle([20, 45, 60, 47], fill=mp.MENU_ACCENT)

    y = 64
    ok_color = (120, 210, 130)    # kept distinct from MENU_ACCENT (orange) --
    fail_color = (220, 90, 90)    # status needs its own green/red language,
    for label, status in results:                              # not the brand accent
        if status is True:
            mark, color = "OK", ok_color
        elif status is False:
            mark, color = "FAIL", fail_color
        else:
            mark, color = "…", mp.MENU_SUBTEXT
        draw.text((20, y), label, font=row_font, fill=mp.MENU_TEXT)
        mark_w = draw.textlength(mark, font=row_font)
        draw.text((mp.WIDTH - 24 - mark_w, y), mark, font=row_font, fill=color)
        y += 26

    if in_progress_label:
        draw.text((20, mp.HEIGHT - 28), in_progress_label, font=row_font, fill=mp.MENU_SUBTEXT)

    mp.blit_rect_buf(0, 0, mp.WIDTH, mp.HEIGHT, img.tobytes())


def draw_fatal_screen(failed_label, detail):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (mp.WIDTH, mp.HEIGHT), (30, 16, 16))  # deliberately distinct from
    draw = ImageDraw.Draw(img)                                   # MENU_BG -- this is a real
    try:                                                          # alarm state, not a menu
        title_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        row_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception:
        title_font = row_font = ImageFont.load_default()
    draw.text((20, 20), "Startup failed", font=title_font, fill=mp.MENU_TEXT)
    draw.text((20, 56), failed_label, font=row_font, fill=(230, 130, 130))
    # Wrap the detail text crudely so it doesn't run off-screen.
    max_chars = 48
    lines = [detail[i:i + max_chars] for i in range(0, len(detail), max_chars)][:8]
    y = 84
    for line in lines:
        draw.text((20, y), line, font=row_font, fill=mp.MENU_SUBTEXT)
        y += 20
    draw.text((20, mp.HEIGHT - 28), "Check the log for details. Power-cycle to retry.",
               font=row_font, fill=mp.MENU_SUBTEXT)
    mp.blit_rect_buf(0, 0, mp.WIDTH, mp.HEIGHT, img.tobytes())


# ================================
# INDIVIDUAL CHECKS
# Each returns (ok: bool, message: str). "critical" checks block boot on
# failure; everything else is reported but non-blocking, matching how the
# rest of the app already treats e.g. missing Bluetooth as a soft failure.
# ================================
def check_display():
    """Special-cased: this check IS init_hardware()+ili9488_init(), since
    every later check's results depend on having a working screen at all."""
    mp.init_hardware()
    mp.ili9488_init()
    mp.write_cmd(0x53); mp.write_data_byte(0x2C)
    mp.write_cmd(0x51); mp.write_data_byte(0xFF)
    return True, "Display + GPIO ready"


def check_buttons():
    # We can't press buttons programmatically, so this just confirms the
    # lines were claimed successfully (init_hardware already did that) --
    # a real functional check would need someone to press something, which
    # doesn't fit an unattended boot sequence.
    lines = [mp.btn_play_pause_line, mp.btn_next_line, mp.btn_prev_line,
             mp.btn_vol_up_line, mp.btn_vol_down_line]
    if all(l is not None for l in lines):
        return True, "Button GPIO lines claimed"
    return False, "Button GPIO lines not claimed"


def check_backlight():
    if mp.backlight is not None and mp.backlight._available:
        return True, "PWM backlight available"
    return False, "Backlight running without PWM (on/off only)"


def check_audio():
    try:
        player = mp.GstPlayer()
        return True, "GStreamer pipeline OK"
    except Exception as e:
        return False, f"GStreamer init failed: {e}"


def check_library():
    tracks = mp.scan_music_folder(mp.MUSIC_ROOT, default_image_path=mp.DEFAULT_ART_PATH)
    if tracks:
        return True, f"{len(tracks)} track(s) found"
    return False, f"No tracks found under {mp.MUSIC_ROOT}"


def check_bluetooth():
    try:
        from bt_manager import BluetoothManager, BluetoothUnavailable
        bt = BluetoothManager()
        bt.start(timeout=5.0)
        bt.stop()
        return True, "Bluetooth adapter OK"
    except Exception as e:
        return False, f"Unavailable: {e}"


def check_wifi():
    try:
        status = mp.wifi_get_status()
        if status["connected"]:
            return True, f"Connected: {status['ssid']}"
        return False, "Not connected (setup hotspot will start)"
    except Exception as e:
        return False, f"Check failed: {e}"


def check_upload_server_deps():
    try:
        import flask  # noqa: F401
        return True, "Flask available"
    except ImportError:
        return False, "Flask not installed (Upload Server disabled)"


# Order matters for display purposes; "critical" determines whether a
# failure blocks boot entirely vs just shows as FAIL and continues.
CHECKS = [
    ("Display & GPIO",     check_display,             True),
    ("Buttons",             check_buttons,             True),
    ("Backlight",           check_backlight,           False),
    ("Audio pipeline",      check_audio,               True),
    ("Music library",       check_library,             False),
    ("Bluetooth",           check_bluetooth,           False),
    ("Wi-Fi",               check_wifi,                False),
    ("Upload server deps",  check_upload_server_deps,  False),
]


def run_boot_sequence():
    mp.log("BOOT", "boot sequence starting")

    # Step 1: display check is special -- run it outside the normal loop
    # since we need it to succeed before we can draw anything else.
    label, fn, critical = CHECKS[0]
    try:
        ok, msg = fn()
    except Exception as e:
        mp.log("BOOT", f"FATAL: {label} check raised: {e}")
        print(f"FATAL during {label}: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    if not ok:
        mp.log("BOOT", f"FATAL: {label} failed: {msg}")
        print(f"FATAL: {label} failed: {msg}", file=sys.stderr)
        sys.exit(1)
    mp.log("BOOT", f"{label}: OK ({msg})")

    draw_splash()
    time.sleep(0.8)

    results = [(label, True)] + [(lbl, None) for lbl, _, _ in CHECKS[1:]]
    draw_checks_screen(results)
    time.sleep(0.4)

    fatal = None
    for i, (label, fn, critical) in enumerate(CHECKS[1:], start=1):
        draw_checks_screen(results, in_progress_label=f"Checking {label}…")
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"raised {e}"
            mp.log("BOOT", f"{label} check raised an exception: {e}")
        results[i] = (label, ok)
        mp.log("BOOT", f"{label}: {'OK' if ok else 'FAIL'} ({msg})")
        draw_checks_screen(results)
        if not ok and critical:
            fatal = (label, msg)
            break
        time.sleep(0.15)

    if fatal:
        label, msg = fatal
        mp.log("BOOT", f"FATAL: critical check '{label}' failed: {msg}")
        draw_fatal_screen(label, msg)
        # Stay here -- there's no reasonable ui_loop() to hand off to
        # without, say, a working audio pipeline. Power-cycle to retry.
        while True:
            time.sleep(1.0)

    time.sleep(0.6)
    mp.log("BOOT", "all critical checks passed, handing off to ui_loop")


def main():
    run_boot_sequence()
    mp.ui_loop()


if __name__ == "__main__":
    mp.log("MAIN", "boot.py starting up")
    try:
        main()
    except KeyboardInterrupt:
        mp.log("MAIN", "KeyboardInterrupt received, shutting down")
    except Exception as e:
        mp.log("MAIN", f"FATAL: unhandled exception: {e!r}")
        raise
    finally:
        try: mp.fill_screen_rgb888(0, 0, 0)
        except Exception: pass
        try: mp.backlight.shutdown()
        except Exception: pass
        try:
            mp.spi.close(); mp.dc_line.set_value(0); mp.rst_line.set_value(1)
            mp.dc_line.release(); mp.rst_line.release()
            mp.btn_play_pause_line.release(); mp.btn_next_line.release()
            mp.btn_prev_line.release(); mp.btn_vol_up_line.release()
            mp.btn_vol_down_line.release()
            mp.chip.close()
        except Exception:
            pass
        mp.log("MAIN", "shutdown complete, GPIO/SPI released")
