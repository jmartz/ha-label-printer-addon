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

import io
import json
import os
import re
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_from_directory
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster

from label_render import build_label_image
import custom_render

MODEL = "QL-820NWB"
PRINT_PORT = 9100

# Where we remember a freshly-discovered IP between runs (HA add-on data dir).
IP_CACHE = "/data/last_ip.txt"

# Static designer UI + saved custom-label templates (HA add-on data dir).
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TEMPLATES_FILE = "/data/templates.json"


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
        # 1.5s tolerates a power-saving printer waking up without making the
        # full /24 sweep too slow.
        open_hosts = [h for h, ok in
                      zip(hosts, ex.map(lambda h: port_open(h, timeout=1.5), hosts))
                      if ok]
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
    # Prefer the last IP we discovered, then the configured one. Use a generous
    # timeout: a QL on WiFi power-save can take a couple seconds to answer the
    # first packet (the actual print wakes it fine, but a short probe would give
    # up and wrongly trigger a network rescan).
    cached = read_cached_ip()
    for candidate in (cached, configured_ip):
        if candidate and port_open(candidate, timeout=3.0):
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


def print_image(img, label_code):
    """Send a PIL image to the QL on the given brother_ql media code.

    Returns the printer IP on success; raises RuntimeError if the printer can't
    be found and propagates send() errors to the caller.
    """
    cfg = load_config()
    ip = resolve_printer_ip(cfg["printer_ip"])
    qlr = BrotherQLRaster(MODEL)
    qlr.exception_on_warning = True
    instructions = convert(
        qlr=qlr, images=[img], label=label_code, rotate="0",
        threshold=70.0, dither=False, compress=False, red=False,
        dpi_600=False, hq=True, cut=True,
    )
    send(instructions=instructions, printer_identifier=f"tcp://{ip}",
         backend_identifier="network", blocking=True)
    return ip


@app.post("/print")
def do_print():
    cfg = load_config()
    now = datetime.now()
    oz = _parse_oz()
    img, header_text = build_label_image(now, oz)
    try:
        ip = print_image(img, cfg["label"])
    except RuntimeError as e:
        return jsonify(status="error", error=str(e)), 503
    except Exception as e:
        return jsonify(status="error", error=str(e)), 502

    print(f"Printed '{header_text}'{f' ({oz:.1f} oz)' if oz is not None else ''} "
          f"to {ip}", flush=True)
    return jsonify(status="ok", printed=header_text, oz=oz, printer_ip=ip)


# ----------------------------------------------------------------------
# Custom-label designer: static UI, fonts, media, preview, print, templates
# ----------------------------------------------------------------------

@app.get("/")
@app.get("/designer")
def designer():
    return send_from_directory(WEB_DIR, "designer.html")


@app.get("/api/media")
def api_media():
    return jsonify(custom_render.media_table())


@app.get("/api/fonts")
def api_fonts():
    return jsonify(list(custom_render.FONTS.keys()))


@app.get("/fonts.css")
def fonts_css():
    """@font-face rules so the browser renders text with the SAME TTFs as PIL."""
    rules = []
    for family in custom_render.FONTS:
        for style, weight, fstyle in (("regular", "normal", "normal"),
                                      ("bold", "bold", "normal"),
                                      ("italic", "normal", "italic"),
                                      ("bolditalic", "bold", "italic")):
            bold, ital = "bold" in style, "italic" in style
            if custom_render.font_file(family, bold, ital):
                rules.append(
                    f"@font-face{{font-family:'{family}';"
                    f"src:url('/fonts/{family}/{style}');"
                    f"font-weight:{weight};font-style:{fstyle};font-display:block;}}")
    return Response("\n".join(rules), mimetype="text/css")


@app.get("/fonts/<family>/<style>")
def font_file_route(family, style):
    bold, ital = "bold" in style, "italic" in style
    path = custom_render.font_file(family, bold, ital)
    if not path or not os.path.exists(path):
        return Response("not found", status=404)
    with open(path, "rb") as f:
        return Response(f.read(), mimetype="font/ttf")


def _spec_from_request():
    spec = request.get_json(silent=True)
    if not isinstance(spec, dict):
        raise ValueError("expected a JSON design spec object")
    return spec


@app.post("/preview")
def preview():
    try:
        img = custom_render.render_spec(_spec_from_request())
    except Exception as e:
        return jsonify(status="error", error=str(e)), 400
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png")


@app.post("/print_custom")
def print_custom():
    try:
        spec = _spec_from_request()
        img = custom_render.render_spec(spec)
    except Exception as e:
        return jsonify(status="error", error=str(e)), 400
    try:
        ip = print_image(img, spec.get("media", "62"))
    except RuntimeError as e:
        return jsonify(status="error", error=str(e)), 503
    except Exception as e:
        return jsonify(status="error", error=str(e)), 502
    copies = max(1, int(spec.get("copies", 1)))
    for _ in range(copies - 1):
        try:
            print_image(img, spec.get("media", "62"))
        except Exception as e:
            return jsonify(status="error", error=str(e)), 502
    print(f"Printed custom label ({img.width}x{img.height}, "
          f"media {spec.get('media', '62')}, {copies}x) to {ip}", flush=True)
    return jsonify(status="ok", printer_ip=ip, copies=copies,
                   size=[img.width, img.height])


# --- saved design templates (named, reprintable) -----------------------

def _load_templates():
    try:
        with open(TEMPLATES_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_templates(data):
    with open(TEMPLATES_FILE, "w") as f:
        json.dump(data, f)


@app.get("/api/templates")
def list_templates():
    return jsonify(sorted(_load_templates().keys()))


@app.get("/api/templates/<name>")
def get_template(name):
    t = _load_templates().get(name)
    return jsonify(t) if t is not None else (jsonify(error="not found"), 404)


@app.post("/api/templates")
def save_template():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    spec = body.get("spec")
    if not name or not isinstance(spec, dict):
        return jsonify(error="need name + spec"), 400
    data = _load_templates()
    data[name] = spec
    _save_templates(data)
    return jsonify(status="ok", name=name)


@app.delete("/api/templates/<name>")
def delete_template(name):
    data = _load_templates()
    if data.pop(name, None) is None:
        return jsonify(error="not found"), 404
    _save_templates(data)
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
