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

import base64
import io
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_from_directory
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster

from label_render import build_label_image, build_niimbot_milk_label
import custom_render

MODEL = "QL-820NWB"
PRINT_PORT = 9100

# The Niimbot B1 is BLE-only and owned by Home Assistant's hass-niimbot
# integration -- the add-on can't share that Bluetooth adapter. So we print to
# it by rendering the design to a PNG and calling HA's `niimbot.print` service
# (as a single full-canvas `dlimg` element, so it's pixel-for-pixel identical to
# the on-screen design) through the Supervisor's Core API proxy. Needs
# `homeassistant_api: true` in config.yaml, which populates SUPERVISOR_TOKEN.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
CORE_API = "http://supervisor/core/api"

# Tuned hass-niimbot options. The stock defaults (600 / 50 / 1) make a 240-line
# label take ~30 s; these cut it to ~6 s. Kept here because the options-flow
# schema only reports factory defaults, so the keep-alive toggle has to re-assert
# them on every write (see /niimbot_keepalive).
NIIMBOT_SCAN_INTERVAL = 60      # seconds between polls
NIIMBOT_LINE_WAIT_MS = 10       # ms pause between print lines
NIIMBOT_CONFIRM_EVERY = 8       # confirm reception every N lines
WATCHDOG_INTERVAL = 90          # seconds between orphaned-link checks
WATCHDOG_STREAK = 2             # consecutive positives before acting (hysteresis)

# Deferred printing. The B1 stops advertising when idle and Home Assistant
# cannot wake it -- that needs a physical tap. Without this, pressing the button
# while the printer is asleep fails within seconds and the label is silently
# lost, which is exactly when you least want to lose it. So a failed Niimbot
# print is PARKED instead of dropped, and reprinted the moment the printer comes
# back. Press the button, walk over, tap the printer, and it prints.
#
# One slot, not a queue: a second press replaces the first, because two
# identical milk labels is a papercut and a growing backlog spitting out five
# labels at once is worse.
NIIMBOT_PENDING_TTL = 1800      # drop a parked label after 30 min unprinted
NIIMBOT_PENDING_POLL = 10       # seconds between "is it back yet?" checks

# Where we remember a freshly-discovered IP between runs (HA add-on data dir).
IP_CACHE = "/data/last_ip.txt"

# Static designer UI (served from the add-on's own folder).
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# ----------------------------------------------------------------------
# Config (HA writes the add-on options to /data/options.json)
# ----------------------------------------------------------------------

def load_config():
    cfg = {"printer_ip": "192.168.10.120", "label": "62",
           "niimbot_device_id": "18e876934ab0f9f57f0593b24044ae1b"}
    try:
        with open("/data/options.json") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v})
    except FileNotFoundError:
        pass
    # Env vars override -- handy when testing outside Home Assistant.
    cfg["printer_ip"] = os.environ.get("PRINTER_IP", cfg["printer_ip"])
    cfg["label"] = os.environ.get("LABEL", cfg["label"])
    cfg["niimbot_device_id"] = os.environ.get("NIIMBOT_DEVICE_ID", cfg["niimbot_device_id"])
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


# ----------------------------------------------------------------------
# Niimbot "keep BLE connection" toggle
# ----------------------------------------------------------------------
# keep_connection is a config-ENTRY OPTION of the hass-niimbot integration, not
# an entity, so there's no switch to put on a dashboard. This drives the
# integration's options flow through the Supervisor's Core-API proxy, which
# gives Home Assistant a real toggle (input_boolean -> automation -> here).

def _core_req(path, method="GET", body=None, timeout=25):
    req = urllib.request.Request(
        f"{CORE_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _niimbot_entry():
    for e in _core_req("/config/config_entries/entry") or []:
        if e.get("domain") == "niimbot":
            return e
    raise RuntimeError("niimbot config entry not found")


def _flow_current_options(flow):
    """Current option values, read out of the options-flow's schema.

    HA's /config/config_entries API deliberately does NOT return an entry's
    options, but starting the options flow hands back a schema whose defaults /
    suggested values ARE the current settings -- so we read them from there and
    resubmit them untouched, which is what keeps a toggle from clobbering the
    other options.
    """
    out = {}
    for fld in (flow or {}).get("data_schema") or []:
        name = fld.get("name")
        if not name:
            continue
        if "default" in fld:
            out[name] = fld["default"]
        elif isinstance(fld.get("description"), dict) and \
                "suggested_value" in fld["description"]:
            out[name] = fld["description"]["suggested_value"]
    return out


def _start_options_flow():
    entry = _niimbot_entry()
    flow = _core_req("/config/config_entries/options/flow", "POST",
                     {"handler": entry["entry_id"]})
    return flow


@app.get("/niimbot_keepalive")
def niimbot_keepalive_get():
    """Read the live keep_connection value (opens a flow, reads it, closes it)."""
    if not SUPERVISOR_TOKEN:
        return jsonify(status="error", error="no SUPERVISOR_TOKEN"), 503
    try:
        flow = _start_options_flow()
        cur = _flow_current_options(flow)
        try:                                   # don't leave the flow dangling
            _core_req(f"/config/config_entries/options/flow/{flow['flow_id']}",
                      "DELETE")
        except Exception:
            pass
    except Exception as e:
        return jsonify(status="error", error=str(e)), 502
    return jsonify(status="ok", keep_connection=bool(cur.get("keep_connection")),
                   options=cur)


@app.post("/niimbot_keepalive")
def niimbot_keepalive_set():
    """Turn the integration's Keep-BLE-Connection option on/off (?enable=1|0).

    'enable' (not 'on') because YAML would coerce on/off into booleans.
    """
    want = str(request.values.get("enable", "1")).strip().lower() in (
        "1", "true", "on", "yes")
    if not SUPERVISOR_TOKEN:
        return jsonify(status="error",
                       error="no SUPERVISOR_TOKEN (needs homeassistant_api)"), 503
    try:
        flow = _start_options_flow()
        opts = _flow_current_options(flow)
        # CAREFUL: that schema hands back the integration's FACTORY defaults
        # (scan 600 / wait 50 / confirm 1), not the live settings -- submitting
        # them would silently undo the print-speed tuning. So pin the tuned
        # values explicitly; only keep_connection is actually being toggled.
        opts.update({
            "use_sound": True,
            "scan_interval": NIIMBOT_SCAN_INTERVAL,
            "wait_between_each_print_line": NIIMBOT_LINE_WAIT_MS,
            "confirm_every_nth_print_line": NIIMBOT_CONFIRM_EVERY,
            "keep_connection": want,
        })
        res = _core_req(f"/config/config_entries/options/flow/{flow['flow_id']}",
                        "POST", opts)
    except Exception as e:
        return jsonify(status="error", error=str(e)), 502
    print(f"Niimbot keep_connection -> {want}", flush=True)
    return jsonify(status="ok", keep_connection=want,
                   flow=(res or {}).get("type"))


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


def ble_disconnect(mac):
    """Drop an orphaned BlueZ link to the printer.

    The B1 accepts exactly ONE BLE connection. When the hass-niimbot
    integration reloads (options change, Core restart, add-on update) the old
    connection can survive at the BlueZ level and keep holding that slot, so the
    new instance can never connect and every print fails instantly with "BLE
    device not available" -- while the printer sits there showing a solid blue
    (connected) LED. Disconnecting the orphan frees it immediately.
    """
    if not mac:
        return "no mac configured"
    try:
        r = subprocess.run(["bluetoothctl", "disconnect", mac],
                           capture_output=True, text=True, timeout=20)
        out = (r.stdout or "").strip().splitlines()
        return out[-1] if out else f"rc={r.returncode}"
    except Exception as e:                      # bluez missing / no dbus access
        return f"disconnect failed: {e}"


def ble_connected(mac):
    """True if BlueZ currently holds a link to `mac`."""
    if not mac:
        return False
    try:
        r = subprocess.run(["bluetoothctl", "info", mac],
                           capture_output=True, text=True, timeout=15)
        return "Connected: yes" in (r.stdout or "")
    except Exception:
        return False


def _entity_state(entity_id):
    """Current state string of an entity (None if unavailable)."""
    try:
        return (_core_req(f"/states/{entity_id}") or {}).get("state")
    except Exception:
        return None


def niimbot_diagnose():
    """Is a stale BlueZ link holding the printer hostage?

    Orphan signature = BlueZ reports a CONNECTED link while the hass-niimbot
    integration reports NOT connected. That is precisely "a link exists that the
    integration isn't using", which is what an orphan is.

    (An earlier version compared the battery sensor's `last_updated` age instead.
    That was wrong: `last_updated` only moves when the *value* changes, so a
    steady battery reading looked permanently stale and the watchdog cut healthy
    links every 90 s. `last_reported` would work but isn't exposed here.)
    """
    cfg = load_config()
    mac = cfg.get("niimbot_mac")
    ent = cfg.get("niimbot_conn_entity") or "binary_sensor.niimbot_8ae8d0_connection"
    connected = ble_connected(mac)
    ha_conn = _entity_state(ent)
    orphan = bool(connected and ha_conn == "off")
    return {"mac": mac, "ble_connected": connected, "ha_connected": ha_conn,
            "conn_entity": ent, "orphaned": orphan}


@app.get("/niimbot_diag")
def niimbot_diag():
    return jsonify(status="ok", **niimbot_diagnose())


def _watchdog():
    """Clear orphaned links proactively so the printer is ready before use.

    Requires the orphan condition on WATCHDOG_STREAK consecutive checks before
    acting, so a momentary blip in the connection sensor can never cause us to
    cut a healthy link (that mistake caused constant connect/disconnect churn).
    """
    streak = 0
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        try:
            d = niimbot_diagnose()
            if not d["orphaned"]:
                streak = 0
                continue
            streak += 1
            if streak < WATCHDOG_STREAK:
                print(f"[watchdog] possible orphan ({streak}/{WATCHDOG_STREAK}) "
                      f"- ble={d['ble_connected']} ha={d['ha_connected']}", flush=True)
                continue
            res = ble_disconnect(d["mac"])
            print(f"[watchdog] orphaned BLE link cleared (ble=connected, "
                  f"ha={d['ha_connected']}): {res}", flush=True)
            streak = 0
        except Exception as e:
            print(f"[watchdog] {e}", flush=True)


# ----------------------------------------------------------------------
# Deferred ("parked") Niimbot print -- see NIIMBOT_PENDING_TTL above
# ----------------------------------------------------------------------

_pending_lock = threading.Lock()
_pending = None          # {"img", "spec", "text", "oz", "created", "attempts"}


def _park_pending(img, spec, text, oz):
    """Hold a label that couldn't print, replacing any previous one."""
    global _pending
    with _pending_lock:
        replaced = _pending["text"] if _pending else None
        _pending = {"img": img, "spec": spec, "text": text, "oz": oz,
                    "created": time.time(), "attempts": 1}
    if replaced:
        print(f"[pending] replaced parked label '{replaced}' with '{text}'", flush=True)
    else:
        print(f"[pending] parked '{text}' -- will print when the B1 reconnects "
              f"(expires in {NIIMBOT_PENDING_TTL // 60} min)", flush=True)


def _pending_snapshot():
    with _pending_lock:
        if not _pending:
            return None
        return {"text": _pending["text"], "oz": _pending["oz"],
                "age_s": int(time.time() - _pending["created"]),
                "attempts": _pending["attempts"]}


def _pending_flusher():
    """Reprint a parked label as soon as the printer is actually reachable.

    Gated on the hass-niimbot connection entity rather than just retrying
    blindly: an unreachable B1 fails fast, so a blind retry loop would just
    churn the log and the BLE stack every few seconds for half an hour.
    """
    global _pending
    while True:
        time.sleep(NIIMBOT_PENDING_POLL)
        try:
            snap = _pending_snapshot()
            if not snap:
                continue

            if snap["age_s"] > NIIMBOT_PENDING_TTL:
                with _pending_lock:
                    _pending = None
                print(f"[pending] dropped '{snap['text']}' unprinted after "
                      f"{snap['age_s']}s -- printer never came back", flush=True)
                continue

            cfg = load_config()
            ent = (cfg.get("niimbot_conn_entity")
                   or "binary_sensor.niimbot_8ae8d0_connection")
            if _entity_state(ent) != "on":
                continue

            with _pending_lock:
                job = _pending
            if not job:
                continue
            print_niimbot(job["img"], job["spec"])
            with _pending_lock:
                # Only clear if it's still the same job (a newer press wins).
                if _pending is job:
                    _pending = None
            print(f"[pending] printed '{job['text']}' after waiting "
                  f"{snap['age_s']}s", flush=True)
        except Exception as e:
            with _pending_lock:
                if _pending:
                    _pending["attempts"] += 1
            print(f"[pending] retry failed: {e}", flush=True)


@app.get("/niimbot_pending")
def niimbot_pending():
    """Is a label waiting for the printer to wake up?"""
    snap = _pending_snapshot()
    return jsonify(status="ok", pending=bool(snap), ttl_s=NIIMBOT_PENDING_TTL,
                   **(snap or {}))


@app.post("/niimbot_pending_clear")
def niimbot_pending_clear():
    """Bin a parked label (dashboard button) -- e.g. it's no longer wanted."""
    global _pending
    snap = _pending_snapshot()
    with _pending_lock:
        _pending = None
    if snap:
        print(f"[pending] cleared '{snap['text']}' by request", flush=True)
    return jsonify(status="ok", cleared=bool(snap), **(snap or {}))


@app.post("/niimbot_unstick")
def niimbot_unstick():
    """Manually clear a stale BLE link (dashboard button / troubleshooting)."""
    mac = load_config().get("niimbot_mac")
    res = ble_disconnect(mac)
    print(f"Unstick {mac}: {res}", flush=True)
    return jsonify(status="ok", mac=mac, result=res)


def print_niimbot(img, spec, _retry=True):
    """Print a PIL image on the Niimbot B1 via HA's niimbot.print service.

    The whole rendered label is sent as ONE full-canvas `dlimg` element (a
    base64 PNG data-URI), so what prints is pixel-identical to the designer --
    imagespec just blits it. Requires the add-on's SUPERVISOR_TOKEN (granted by
    homeassistant_api) and a configured niimbot_device_id. Returns a label for
    logging; raises RuntimeError on missing config, propagates HTTP errors.
    """
    cfg = load_config()
    device_id = cfg.get("niimbot_device_id")
    if not device_id:
        raise RuntimeError("No niimbot_device_id set in the add-on options.")
    if not SUPERVISOR_TOKEN:
        raise RuntimeError(
            "Add-on lacks Home Assistant API access (SUPERVISOR_TOKEN missing) "
            "-- set 'homeassistant_api: true' in config.yaml.")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    W, H = img.width, img.height
    body = {
        "device_id": device_id,
        "width": W,
        "height": H,
        "density": int(spec.get("density", 3)),
        "dither": False,                              # image is already 1-bit
        "payload": [{
            "type": "dlimg", "x": 0, "y": 0, "xsize": W, "ysize": H,
            "mode": "stretch", "dither": False,
            "url": f"data:image/png;base64,{b64}",
        }],
    }
    req = urllib.request.Request(
        f"{CORE_API}/services/niimbot/print",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                 "Content-Type": "application/json"},
    )
    # BLE connect + print can take several seconds; the service call blocks until
    # the print finishes, so allow a generous timeout.
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except Exception as e:
        if not _retry:
            raise
        # Almost always an orphaned BlueZ link holding the printer's only
        # connection slot: clear it and try once more, so this self-heals
        # instead of needing a human with an SSH session.
        res = ble_disconnect(cfg.get("niimbot_mac"))
        print(f"Print failed ({e}); cleared stale BLE link: {res}; retrying",
              flush=True)
        time.sleep(3)
        return print_niimbot(img, spec, _retry=False)
    return f"Niimbot B1 ({device_id[:8]}…)"


@app.post("/print")
def do_print():
    now = datetime.now()
    oz = _parse_oz()
    # ?printer=niimbot prints the compact B1 milk label via HA's niimbot.print;
    # default (Brother) prints the full 62mm fridge label as before.
    printer = request.values.get("printer", "brother")
    try:
        if printer == "niimbot":
            img, header_text = build_niimbot_milk_label(now, oz)
            spec = {"density": 3}
            try:
                dest = print_niimbot(img, spec)
            except RuntimeError:
                raise            # misconfiguration -- parking would never succeed
            except Exception as e:
                # Printer asleep or out of range. Keep the label (rendered NOW,
                # so it carries the time you actually pressed the button) and
                # print it when the B1 comes back.
                _park_pending(img, spec, header_text, oz)
                return jsonify(status="pending", printed=header_text, oz=oz,
                               printer=printer, reason=str(e),
                               ttl_s=NIIMBOT_PENDING_TTL), 202
        else:
            img, header_text = build_label_image(now, oz)
            dest = print_image(img, load_config()["label"])
    except RuntimeError as e:
        return jsonify(status="error", error=str(e)), 503
    except Exception as e:
        return jsonify(status="error", error=str(e)), 502

    print(f"Printed '{header_text}'{f' ({oz:.1f} oz)' if oz is not None else ''} "
          f"to {dest} [{printer}]", flush=True)
    return jsonify(status="ok", printed=header_text, oz=oz, printer=printer, printer_ip=dest)


# ----------------------------------------------------------------------
# Custom-label designer: static UI, fonts, media, preview, print
# ----------------------------------------------------------------------

@app.get("/")
@app.get("/designer")
def designer():
    return send_from_directory(WEB_DIR, "designer.html")


@app.get("/api/printers")
def api_printers():
    return jsonify([{"id": pid, "name": p["name"], "px_per_mm": p["px_per_mm"]}
                    for pid, p in custom_render.PRINTERS.items()])


@app.get("/api/media")
def api_media():
    printer = request.args.get("printer", "brother")
    return jsonify(custom_render.media_table(printer))


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
                # Relative URL (no leading slash) so it resolves under HA's
                # ingress sub-path as well as on the direct :8099 port.
                rules.append(
                    f"@font-face{{font-family:'{family}';"
                    f"src:url('fonts/{family}/{style}');"
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

    printer = spec.get("printer", "brother")
    media = spec.get("media", custom_render.DEFAULT_MEDIA.get(printer, "62"))
    copies = max(1, int(spec.get("copies", 1)))
    send_one = (lambda: print_niimbot(img, spec)) if printer == "niimbot" \
        else (lambda: print_image(img, media))

    try:
        dest = send_one()                            # first copy
        for _ in range(copies - 1):
            send_one()
    except RuntimeError as e:
        return jsonify(status="error", error=str(e)), 503
    except Exception as e:
        return jsonify(status="error", error=str(e)), 502

    print(f"Printed custom label ({img.width}x{img.height}, {printer}/{media}, "
          f"{copies}x) to {dest}", flush=True)
    return jsonify(status="ok", printer=printer, printer_ip=dest, copies=copies,
                   size=[img.width, img.height])


if __name__ == "__main__":
    # Proactively keep the Niimbot's single BLE slot free, so a button press
    # prints immediately instead of paying for a failure + retry.
    threading.Thread(target=_watchdog, daemon=True).start()
    # Reprint any label that was parked because the B1 was asleep.
    threading.Thread(target=_pending_flusher, daemon=True).start()
    app.run(host="0.0.0.0", port=8099)
