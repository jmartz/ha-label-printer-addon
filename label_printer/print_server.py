#!/usr/bin/env python3
"""
HTTP print service for a networked Brother QL-820NWB, packaged as a Home
Assistant add-on. POST /print prints a universal fridge label (breast milk +
leftovers) stamped with the current date/time, a day-of-week strip, the
computed Use-By deadlines, a thaw write-in, and a day/night ("sleepy time"
milk) icon. An optional `oz` value (from the M5Dial knob) prints beside the
icon.

The label itself is rendered in label_render.py (kept free of brother_ql so it
can be previewed off the printer). This module is just the printer plumbing:
find the QL on the LAN and send the raster.
"""

import json
import os
import re
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, request
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster

from label_render import build_label_image

MODEL = "QL-820NWB"
PRINT_PORT = 9100

# Where we remember a freshly-discovered IP between runs (HA add-on data dir).
IP_CACHE = "/data/last_ip.txt"


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
# Find the printer: saved IP first, else rescan the LAN for the QL printer
# ----------------------------------------------------------------------

def port_open(ip, port=PRINT_PORT, timeout=0.7):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch(url, n=12000):
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            return resp.read(n).decode("latin-1", "ignore")
    except Exception:
        return ""


def is_ql_printer(ip):
    """True only if the device identifies as a Brother QL label printer.

    Several Brother devices (e.g. an MFC inkjet all-in-one) also run the same
    'debut' web server with TCP 9100 open, so a bare port check isn't enough --
    we must positively match the QL model or we risk printing label raster to
    the wrong printer. The root path 301-redirects to a status page (urllib
    follows it); we also probe the status pages directly as a fallback.
    """
    for path in ("/", "/general/status.html", "/general/information.html"):
        if re.search(r"QL-\d", _fetch(f"http://{ip}{path}"), re.IGNORECASE):
            return True
    return False


def local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0].rsplit(".", 1)[0]
    finally:
        s.close()


def scan_for_printer(seed_ip=None):
    # Scan the /24 of the printer's last-known IP: when DHCP moves the printer
    # it stays on the same subnet, and this avoids local_subnet() picking the
    # container's Docker bridge subnet (the default route goes via Supervisor,
    # not the LAN, even with host networking).
    base = seed_ip.rsplit(".", 1)[0] if seed_ip else local_subnet()
    hosts = [f"{base}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as ex:
        open_hosts = [h for h, ok in zip(hosts, ex.map(port_open, hosts)) if ok]
    print(f"Scan of {base}.0/24: :9100 open on {open_hosts or 'no hosts'}", flush=True)
    for h in open_hosts:
        ok = is_ql_printer(h)
        print(f"  {h}: {'QL printer' if ok else 'not a QL'}", flush=True)
        if ok:
            return h
    # Don't guess: never fall back to an arbitrary :9100 host -- it could be a
    # different Brother printer (an inkjet), and we'd print labels to it.
    return None


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
    cached = read_cached_ip()
    for candidate in (cached, configured_ip):
        if candidate and port_open(candidate):
            return candidate
    print("Printer not reachable at known IP -- scanning the network...", flush=True)
    found = scan_for_printer(configured_ip or cached)
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


def _parse_oz():
    """Read an optional `oz` amount from the query string, form, or JSON body.

    Absent/blank means the M5Dial knob was never turned (screen shows '-.- oz')
    or the trigger was a plain button press -- the label omits the amount.
    """
    raw = request.values.get("oz")
    if raw is None and request.is_json:
        raw = (request.get_json(silent=True) or {}).get("oz")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@app.post("/print")
def do_print():
    cfg = load_config()
    now = datetime.now()
    oz = _parse_oz()
    img, header_text = build_label_image(now, oz)
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

    print(f"Printed '{header_text}'{f' ({oz:.1f} oz)' if oz is not None else ''} "
          f"to {ip}", flush=True)
    return jsonify(status="ok", printed=header_text, oz=oz, printer_ip=ip)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
