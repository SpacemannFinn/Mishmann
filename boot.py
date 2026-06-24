"""
boot.py — startup sequence: permanent splash logo with integrated mechanical slide-and-spin animations.
"""

import os
import sys
import time
import traceback
import math
from PIL import Image, ImageDraw, ImageFont

import music_player as mp

BG_COLOR = mp.MENU_BG
ACCENT_COLOR = mp.MENU_ACCENT
TEXT_COLOR = mp.MENU_TEXT
SUBTEXT_COLOR = mp.MENU_SUBTEXT

def draw_cassette_spindle(draw, cx, cy, size, angle, bg, accent):
    """Draws a premium 6-tooth mechanical cassette drive gear with deep precision notches."""
    r = size / 2 - 1
    # Outer ring chassis layer
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent, width=2)
    
    # Generate a classic 6-tooth vintage gear core matrix
    num_teeth = 6
    for i in range(num_teeth):
        spoke_angle = math.radians(angle + (i * (360 / num_teeth)))
        
        # Inner teeth geometry vertices
        x1 = cx + (size * 0.22) * math.cos(spoke_angle)
        y1 = cy + (size * 0.22) * math.sin(spoke_angle)
        x2 = cx + r * math.cos(spoke_angle)
        y2 = cy + r * math.sin(spoke_angle)
        draw.line([(x1, y1), (x2, y2)], fill=accent, width=2)

    # Center hollow spindle capsule wheel
    inner_r = size * 0.22
    draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill=accent)
    
    # Hex drive mounting socket hole
    hole_r = size * 0.08
    draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r], fill=bg)

def draw_boot_frame(results=None, in_progress_label=None, spool_angle=0.0, anim_progress=1.0):
    """
    Renders the layout dynamically. 
    anim_progress=0.0 means centered hero splash view.
    anim_progress=1.0 means fully shifted top layout showing diagnostics below.
    """
    img = Image.new("RGB", (mp.WIDTH, mp.HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
        status_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10)
    except Exception:
        title_font = label_font = status_font = ImageFont.load_default()

    # --- DYNAMIC Y-COORDINATE INTERPOLATION ---
    title_y = int(95 - (70 * anim_progress))
    
    title = "MISHMANN"
    tw = draw.textlength(title, font=title_font)
    draw.text(((mp.WIDTH - tw) / 2, title_y), title, font=title_font, fill=TEXT_COLOR)

    # Spools slide from y=160 up to header y=82
    spool_size = 48
    spool_y = int(160 - (78 * anim_progress))
    
    # Widen gap completely to mimic true cassette standard tape width footprints
    gap = 130
    spool1_cx = mp.WIDTH / 2 - gap / 2
    spool2_cx = mp.WIDTH / 2 + gap / 2
    
    # Draw an industrial sharp cassette trapezoidal center window border line around spools
    win_w, win_h = gap + spool_size + 16, spool_size + 16
    wx, wy = (mp.WIDTH - win_w) // 2, spool_y - (win_h // 2)
    draw.rounded_rectangle([wx, wy, wx + win_w, wy + win_h], radius=6, outline=(42, 42, 50), width=2)
    
    # Blit custom vintage teeth gears
    draw_cassette_spindle(draw, spool1_cx, spool_y, spool_size, spool_angle, BG_COLOR, ACCENT_COLOR)
    draw_cassette_spindle(draw, spool2_cx, spool_y, spool_size, spool_angle, BG_COLOR, ACCENT_COLOR)

    # --- PANE EVALUATION ---
    if anim_progress < 1.0 and results is None:
        sub = "INITIALIZING HARDWARE LAYER…"
        sw = draw.textlength(sub, font=status_font)
        draw.text(((mp.WIDTH - sw) / 2, 245), sub, font=status_font, fill=SUBTEXT_COLOR)
    elif anim_progress >= 1.0 and results is not None:
        active_checks = CHECKS[1:]
        start_y = 145
        row_h = 24
        col_w = 170
        col_gap = 30
        
        for idx, (label, _, critical) in enumerate(active_checks):
            col = idx % 2
            row = idx // 2
            rx = 60 if col == 0 else 60 + col_w + col_gap
            ry = start_y + (row * row_h)
            
            status = results[idx + 1][1] 
            
            if status is True:
                dot_color, mark = (74, 222, 128), "●"
                txt_color = TEXT_COLOR
            elif status is False:
                dot_color = (239, 68, 68) if critical else SUBTEXT_COLOR
                txt_color = SUBTEXT_COLOR
                mark = "✕"
            else:
                if in_progress_label and label.upper() in in_progress_label:
                    dot_color, mark = (234, 179, 8), "○"
                    txt_color = TEXT_COLOR
                else:
                    dot_color, mark = (40, 40, 48), "·"
                    txt_color = SUBTEXT_COLOR

            draw.text((rx, ry), mark, font=label_font, fill=dot_color)
            draw.text((rx + 16, ry), label.upper(), font=label_font, fill=txt_color)

        feed_txt = in_progress_label if in_progress_label else "ALL SYSTEM CHANNELS ACTIVE. ROUTING ENGINE..."
        tf = draw.textlength(feed_txt, font=status_font)
        draw.text(((mp.WIDTH - tf) / 2, mp.HEIGHT - 28), feed_txt, font=status_font, fill=SUBTEXT_COLOR)

    mp.blit_rect_buf(0, 0, mp.WIDTH, mp.HEIGHT, img.tobytes())

def run_transition_animation():
    steps = 22
    angle = 0.0
    for i in range(steps):
        t = i / float(steps - 1)
        anim_progress = math.sin(t * (math.pi / 2))
        angle += 28.0 * math.sin(t * math.pi)
        draw_boot_frame(results=None, spool_angle=angle, anim_progress=anim_progress)
        time.sleep(0.016)
    return angle

def draw_fatal_screen(failed_label, detail):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (mp.WIDTH, mp.HEIGHT), (24, 14, 14))
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        row_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception: title_font = row_font = ImageFont.load_default()
    
    draw.text((24, 24), "BOOT FAILURE INTERCEPTED", font=title_font, fill=(239, 68, 68))
    draw.rectangle([24, 54, 84, 56], fill=(239, 68, 68))
    draw.text((24, 75), f"CRITICAL CONTEXT: {failed_label.upper()}", font=row_font, fill=TEXT_COLOR)
    
    max_chars = 52
    lines = [detail[i:i + max_chars] for i in range(0, len(detail), max_chars)][:6]
    y = 110
    for line in lines:
        draw.text((24, y), line, font=row_font, fill=SUBTEXT_COLOR)
        y += 22
    draw.text((24, mp.HEIGHT - 32), "CYCLE HARDWARE POWER LINE TO ATTEMPT RESCAN", font=row_font, fill=SUBTEXT_COLOR)
    mp.blit_rect_buf(0, 0, mp.WIDTH, mp.HEIGHT, img.tobytes())


def check_display():
    mp.init_hardware()
    mp.ili9488_init()
    mp.write_cmd(0x53); mp.write_data_byte(0x2C)
    mp.write_cmd(0x51); mp.write_data_byte(0xFF)
    return True, "Display claimed"

def check_buttons():
    lines = [mp.btn_play_pause_line, mp.btn_next_line, mp.btn_prev_line, mp.btn_vol_up_line, mp.btn_vol_down_line]
    return (True, "Lines linked") if all(l is not None for l in lines) else (False, "Line failure")

def check_backlight():
    return (True, "PWM active") if (mp.backlight and mp.backlight._available) else (False, "Dormant")

def check_audio():
    try:
        p = mp.GstPlayer()
        return True, "Pipeline sound"
    except Exception as e: return False, str(e)

def check_library():
    t = mp.scan_music_folder(mp.MUSIC_ROOT, default_image_path=mp.DEFAULT_ART_PATH)
    return (True, f"{len(t)} tracks mapped") if t else (False, "Directory vacant")

def check_bluetooth():
    try:
        from bt_manager import BluetoothManager
        bt = BluetoothManager()
        return True, "Adapter reachable"
    except Exception as e: return False, str(e)

def check_wifi():
    try: return (True, "Wi-Fi operational") if mp.wifi_get_status()["connected"] else (False, "Offline profile")
    except Exception as e: return False, str(e)

def check_upload_server_deps():
    try:
        import flask
        return True, "Flask ready"
    except ImportError: return False, "Dependencies missing"


CHECKS = [
    ("Display",             check_display,             True),
    ("Input Matrix",        check_buttons,             True),
    ("Backlight Panel",     check_backlight,           False),
    ("Audio Pipeline",      check_audio,               True),
    ("Storage Matrix",      check_library,             False),
    ("Bluetooth Layer",     check_bluetooth,           False),
    ("Wi-Fi Interface",     check_wifi,                False),
    ("Web Core Stack",      check_upload_server_deps,  False),
]

def run_boot_sequence():
    mp.log("BOOT", "Initialization block ignited")
    
    label, fn, critical = CHECKS[0]
    ok, msg = fn()
    if not ok: sys.exit(1)

    current_angle = 0.0
    draw_boot_frame(results=None, spool_angle=current_angle, anim_progress=0.0)
    time.sleep(1.5)
    
    current_angle = run_transition_animation()
    
    results = [(label, True)] + [(lbl, None) for lbl, _, _ in CHECKS[1:]]
    fatal = None
    
    for i, (label, fn, critical) in enumerate(CHECKS[1:], start=1):
        draw_boot_frame(results, in_progress_label=f"SCANNING SYSTEM INTERFACE: {label.upper()}…", spool_angle=current_angle, anim_progress=1.0)
        current_angle += 35.0
        ok, msg = fn()
        results[i] = (label, ok)
        draw_boot_frame(results, in_progress_label=f"COMPLETED CHANNELS: {label.upper()}", spool_angle=current_angle, anim_progress=1.0)
        if not ok and critical:
            fatal = (label, msg)
            break
        time.sleep(0.08)

    if fatal:
        label, msg = fatal
        mp.log("BOOT", f"FATAL: critical loop error '{label}': {msg}")
        draw_fatal_screen(label, msg)
        while True: time.sleep(1.0)

    draw_boot_frame(results, spool_angle=current_angle, anim_progress=1.0)
    time.sleep(0.4)

def main():
    run_boot_sequence()
    mp.ui_loop()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: pass
    finally:
        try: mp.fill_screen_rgb888(0, 0, 0)
        except Exception: pass
        try: mp.backlight.shutdown()
        except Exception: pass
        try:
            mp.spi.close(); mp.dc_line.set_value(0); mp.rst_line.set_value(1)
            mp.dc_line.release(); mp.rst_line.release()
            mp.btn_play_pause_line.release(); mp.btn_next_line.release()
            mp.btn_prev_line.release(); mp.btn_vol_up_line.release(); mp.btn_vol_down_line.release()
            mp.chip.close()
        except Exception: pass