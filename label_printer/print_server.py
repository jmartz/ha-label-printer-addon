#!/usr/bin/env python3
"""
HTTP print service for a networked Brother QL-820NWB, packaged as a Home
Assistant add-on. POST /print prints a breast-milk label stamped with the
current date/time, expiration deadlines, an Amount write-in field, and a
day/night ("sleepy time" milk) icon.

This is the same label as the standalone print_date_label.py, wrapped in a
tiny Flask server so Home Assistant can trigger it (e.g. from a Tapo S200D
button) without any PC involved -- the add-on talks straight to the printer.
"""

import calendar
import json
import math
import os
import re
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import Flask, jsonify
from PIL import Image, ImageDraw, ImageFont
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster

MODEL = "QL-820NWB"
PRINT_PORT = 9100
LABEL_WIDTH_PX = 696          # printable width for 62mm media at 300 dpi
MARGIN_PX = 24
max_text_w = LABEL_WIDTH_PX - 2 * MARGIN_PX

# Where we remember a freshly-discovered IP between runs (HA add-on data dir).
IP_CACHE = "/data/last_ip.txt"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "arialbd.ttf",
]


# ----------------------------------------------------------------------
# Config (HA writes the add-on options to /data/options.json)
# ----------------------------------------------------------------------

def load_config():
    cfg = {"printer_ip": "192.168.10.83", "label": "62"}
    try:
        with open("/data/options.json") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v})
    except FileNotFoundError:
        pass
    # Env vars override -- handy when testing outside Home Assistant.
    cfg["printer_ip"] = os.environ.get("PRINTER_IP", cfg["printer_ip"])
    cfg["label"] = os.environ.get("LABEL", cfg["label"])
    return cfg


# ----------------------------------------------------------------------
# Fonts / text helpers
# ----------------------------------------------------------------------

def load_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


_measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))


def fitted_font(texts, start_size, min_size=10, max_w=None):
    if max_w is None:
        max_w = max_text_w
    size = start_size
    while size > min_size:
        f = load_font(size)
        if all(_measure.textlength(t, font=f) <= max_w for t in texts):
            return f
        size -= 2
    return load_font(min_size)


def line_height(text, font):
    b = _measure.textbbox((0, 0), text, font=font)
    return b[3] - b[1], b[1]


def fmt_time(dt):
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def add_months(dt, months):
    m = dt.month - 1 + months
    y = dt.year + m // 12
    m = m % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return dt.replace(year=y, month=m, day=min(dt.day, last_day))


# ----------------------------------------------------------------------
# Day/night icon
# ----------------------------------------------------------------------

DAY_START, DAY_END = 7, 19        # 7am-7pm = day (sun), otherwise night (moon)


def draw_sun(d, cx, cy, r):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="black")
    ray_in, ray_out = r + 7, r + 7 + int(r * 0.55)
    w = max(3, int(r * 0.20))
    for k in range(8):
        a = math.pi / 4 * k
        d.line([(cx + math.cos(a) * ray_in, cy + math.sin(a) * ray_in),
                (cx + math.cos(a) * ray_out, cy + math.sin(a) * ray_out)],
               fill="black", width=w)


def draw_moon(d, cx, cy, r):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="black")
    off = int(r * 0.55)
    d.ellipse([cx - r + off, cy - r - int(r * 0.18),
               cx + r + off, cy + r - int(r * 0.18)], fill="white")


# ----------------------------------------------------------------------
# Build the label image
# ----------------------------------------------------------------------

def build_label_image(now):
    header_text = f"{now:%a} | {now:%b} {now.day} | {fmt_time(now)}"

    room = now + timedelta(hours=4)        # room temp 77F/25C: 4 hours
    fridge = now + timedelta(days=4)       # fridge 40F/4C: 4 days
    freezer = add_months(now, 6)           # freezer 0F/-18C: 6 months (best)
    exp_pairs = [
        ("Room temp (4 hrs):", f"{room:%a} {fmt_time(room)}"),
        ("Fridge (4 days):", f"{fridge:%a %b} {fridge.day}"),
        ("Freezer (6 mo):", f"{freezer:%b} {freezer.day}, {freezer.year}"),
    ]

    is_day = DAY_START <= now.hour < DAY_END

    ICON_R = 26
    ICON_RAY = int(ICON_R * 0.55)
    ICON_W = 2 * (ICON_R + 7 + ICON_RAY)
    ICON_GAP = 22

    header_font = fitted_font([header_text], 72,
                              max_w=max_text_w - ICON_W - ICON_GAP)
    amount_label, amount_unit = "Amount:", "oz"
    amount_font = fitted_font([f"{amount_label}      {amount_unit}"], 46)
    small_font = fitted_font(
        [f"{lbl}{'  ' * 4}{val}" for lbl, val in exp_pairs], 40)

    GAP_AFTER_HEADER = 18
    AMOUNT_GAP = 24
    LINE_GAP = 10

    hdr_h, _ = line_height(header_text, header_font)
    amount_h, _ = line_height(amount_label, amount_font)
    row_h = max(line_height(lbl + val, small_font)[0] for lbl, val in exp_pairs)
    band_h = max(hdr_h, ICON_W)

    total_h = (MARGIN_PX + band_h + GAP_AFTER_HEADER + amount_h + AMOUNT_GAP
               + len(exp_pairs) * row_h + (len(exp_pairs) - 1) * LINE_GAP
               + MARGIN_PX)

    img = Image.new("RGB", (LABEL_WIDTH_PX, total_h), "white")
    draw = ImageDraw.Draw(img)

    # Header band: centered header on the left, day/night icon on the right.
    region_w = LABEL_WIDTH_PX - 2 * MARGIN_PX - ICON_W - ICON_GAP
    hb = draw.textbbox((0, 0), header_text, font=header_font)
    hx = MARGIN_PX + (region_w - (hb[2] - hb[0])) / 2 - hb[0]
    draw.text((hx, MARGIN_PX + (band_h - hdr_h) / 2 - hb[1]),
              header_text, fill="black", font=header_font)

    icon_cx = LABEL_WIDTH_PX - MARGIN_PX - ICON_W / 2
    icon_cy = MARGIN_PX + band_h / 2
    (draw_sun if is_day else draw_moon)(draw, icon_cx, icon_cy, ICON_R)

    # Amount write-in field: "Amount:" ____________ "oz"
    y = MARGIN_PX + band_h + GAP_AFTER_HEADER
    al = draw.textbbox((0, 0), amount_label, font=amount_font)
    au = draw.textbbox((0, 0), amount_unit, font=amount_font)
    draw.text((MARGIN_PX - al[0], y - al[1]), amount_label,
              fill="black", font=amount_font)
    unit_x = LABEL_WIDTH_PX - MARGIN_PX - (au[2] - au[0]) - au[0]
    draw.text((unit_x, y - au[1]), amount_unit, fill="black", font=amount_font)
    line_x1 = MARGIN_PX + (al[2] - al[0]) + 15
    line_x2 = LABEL_WIDTH_PX - MARGIN_PX - (au[2] - au[0]) - 15
    draw.line([(line_x1, y + amount_h), (line_x2, y + amount_h)],
              fill="black", width=3)
    y += amount_h + AMOUNT_GAP

    # Expiration rows: label flush left, value flush right.
    for lbl, val in exp_pairs:
        lb = draw.textbbox((0, 0), lbl, font=small_font)
        vb = draw.textbbox((0, 0), val, font=small_font)
        draw.text((MARGIN_PX - lb[0], y - lb[1]), lbl,
                  fill="black", font=small_font)
        val_x = LABEL_WIDTH_PX - MARGIN_PX - (vb[2] - vb[0]) - vb[0]
        draw.text((val_x, y - vb[1]), val, fill="black", font=small_font)
        y += row_h + LINE_GAP

    return img, header_text


# ----------------------------------------------------------------------
# Find the printer: saved IP first, else rescan the LAN for the QL printer
# ----------------------------------------------------------------------

def port_open(ip, port=PRINT_PORT, timeout=0.7):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_ql_printer(ip):
    try:
        with urllib.request.urlopen(f"http://{ip}/", timeout=3) as resp:
            html = resp.read(8000).decode("latin-1", "ignore")
        return bool(re.search(r"QL-\w+", html, re.IGNORECASE))
    except Exception:
        return False


def local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0].rsplit(".", 1)[0]
    finally:
        s.close()


def scan_for_printer():
    base = local_subnet()
    hosts = [f"{base}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as ex:
        open_hosts = [h for h, ok in zip(hosts, ex.map(port_open, hosts)) if ok]
    for h in open_hosts:
        if is_ql_printer(h):
            return h
    return open_hosts[0] if open_hosts else None


def read_cached_ip():
    try:
        with open(IP_CACHE) as f:
            return f.read().strip()
    except OSError:
        return None


def write_cached_ip(ip):
    try:
        with open(IP_CACHE, "w") as f:
            f.write(ip)
    except OSError:
        pass


def resolve_printer_ip(configured_ip):
    # Prefer the last IP we discovered, then the configured one.
    for candidate in (read_cached_ip(), configured_ip):
        if candidate and port_open(candidate):
            return candidate
    print("Printer not reachable at known IP -- scanning the network...", flush=True)
    found = scan_for_printer()
    if not found:
        raise RuntimeError("Could not find the QL printer on the network.")
    print(f"Found printer at {found}.", flush=True)
    write_cached_ip(found)
    return found


# ----------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/print")
def do_print():
    cfg = load_config()
    now = datetime.now()
    img, header_text = build_label_image(now)
    try:
        ip = resolve_printer_ip(cfg["printer_ip"])
    except RuntimeError as e:
        return jsonify(status="error", error=str(e)), 503

    qlr = BrotherQLRaster(MODEL)
    qlr.exception_on_warning = True
    instructions = convert(
        qlr=qlr, images=[img], label=cfg["label"], rotate="0",
        threshold=70.0, dither=False, compress=False, red=False,
        dpi_600=False, hq=True, cut=True,
    )
    try:
        send(instructions=instructions, printer_identifier=f"tcp://{ip}",
             backend_identifier="network", blocking=True)
    except Exception as e:
        return jsonify(status="error", error=str(e)), 502

    print(f"Printed '{header_text}' to {ip}", flush=True)
    return jsonify(status="ok", printed=header_text, printer_ip=ip)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
