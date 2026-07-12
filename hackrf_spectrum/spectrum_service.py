#!/usr/bin/env python3
"""HackRF One spectrum sweep -> live waterfall PNG + MQTT status for Home Assistant.

Runs inside a HA add-on that has `usb: true`, so it can actually OPEN the HackRF
(unlike the Terminal/SSH add-on, which has usb:false and only enumerates it).

Loop: continuously sweep the currently-selected band with hackrf_sweep, render a
line plot + rolling waterfall to <www_dir>/spectrum.png (served by the existing
`camera.spectrum_waterfall` local_file entity), and publish sdr/spectrum/status
for the Spectrum-dashboard sensors. The selected band comes from HA over MQTT
(sdr/spectrum/request), published by the input_select / "sweep now" button.
"""
import json
import os
import re
import subprocess
import threading
import time

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import gridspec
import paho.mqtt.client as mqtt


def log(*a):
    print("[spectrum]", *a, flush=True)


# ---------------------------------------------------------------- config
def load_config():
    cfg = {
        "mqtt_host": "core-mosquitto",
        "mqtt_port": 1883,
        "mqtt_user": "mqtt_sdr",
        "mqtt_pass": "sdr_mqtt_2026",
        "www_dir": "/homeassistant/www/sdr",
        "default_band": "Airband (118-137 MHz)",
        "bin_width_hz": 100000,
        "sweeps_per_batch": 30,
        "sweep_interval_s": 1,
        "waterfall_rows": 120,
        "waterfall_cols": 600,
        "lna_gain": 32,
        "vga_gain": 40,
        "flash_firmware": False,
        "diagnostic": False,
    }
    try:
        with open("/data/options.json") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v not in (None, "")})
    except FileNotFoundError:
        pass
    for k in ("mqtt_port", "bin_width_hz", "sweeps_per_batch", "sweep_interval_s",
              "waterfall_rows", "waterfall_cols", "lna_gain", "vga_gain"):
        cfg[k] = int(cfg[k])
    return cfg


CFG = load_config()


def resolve_www(preferred):
    """The HA config dir may be mounted at /homeassistant or /config depending on
    the `map` scheme. Find the first candidate whose config-root exists & is
    writable; all of these resolve to the same physical /config/www/sdr that the
    camera reads."""
    for c in [preferred, "/homeassistant/www/sdr", "/config/www/sdr", "/share/sdr"]:
        root = os.path.dirname(os.path.dirname(c))  # the config root (.../ )
        if os.path.isdir(root):
            try:
                os.makedirs(c, exist_ok=True)
                t = os.path.join(c, ".wtest")
                open(t, "w").close()
                os.remove(t)
                return c
            except OSError:
                continue
    return preferred


WWW_DIR = resolve_www(CFG["www_dir"])


# ---------------------------------------------------------------- band parsing
def band_range_mhz(label):
    """'Airband (118-137 MHz)' -> (118.0, 137.0)."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", label or "")
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if hi > lo:
            return lo, hi
    return 118.0, 137.0


# ---------------------------------------------------------------- hackrf
def hackrf_info():
    try:
        out = subprocess.run(["hackrf_info"], capture_output=True, text=True, timeout=20)
        txt = (out.stdout or "") + (out.stderr or "")
        return ("Serial number" in txt or "Part ID" in txt), txt
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def run_sweep(lo_mhz, hi_mhz):
    # Run a BATCH of sweeps in one hackrf_sweep process (-N) instead of spawning a
    # fresh one-shot every few seconds. Rapidly opening/closing the HackRF's
    # streaming (the old `-1` per call) wedges its USB after ~10-15 cycles; one
    # process doing N sweeps cuts that churn ~Nx. We max-hold across the batch so
    # brief signals still show up.
    lo = int(lo_mhz)
    hi = int(max(hi_mhz, lo_mhz + 1))
    cmd = ["hackrf_sweep", "-f", f"{lo}:{hi}", "-w", str(CFG["bin_width_hz"]),
           "-l", str(CFG["lna_gain"]), "-g", str(CFG["vga_gain"]),
           "-N", str(CFG["sweeps_per_batch"])]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    bins = {}
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            hz_low = float(parts[2])
            binw = float(parts[4])
            vals = [float(x) for x in parts[6:]]
        except ValueError:
            continue
        for i, v in enumerate(vals):
            f = round(hz_low + (i + 0.5) * binw)
            if f in bins:
                if v > bins[f]:
                    bins[f] = v
            else:
                bins[f] = v
    if not bins:
        raise RuntimeError("hackrf_sweep produced no data: " + (out.stderr or "")[:300])
    freqs = np.asarray(sorted(bins), dtype=float)
    powers = np.asarray([bins[round(f)] for f in freqs], dtype=float)
    return freqs, powers


# ---------------------------------------------------------------- rendering
_wf = None
_wf_band = None


def render(freqs, powers, band, lo_mhz, hi_mhz, stats):
    global _wf, _wf_band
    cols, rows = CFG["waterfall_cols"], CFG["waterfall_rows"]
    grid = np.linspace(lo_mhz * 1e6, hi_mhz * 1e6, cols)
    row = np.interp(grid, freqs, powers)
    if _wf is None or _wf_band != band or _wf.shape[1] != cols:
        _wf = np.full((rows, cols), np.nan)
        _wf_band = band
    _wf = np.roll(_wf, 1, axis=0)
    _wf[0, :] = row

    fig = plt.figure(figsize=(8, 6), facecolor="#0d1117")
    gs = gridspec.GridSpec(2, 1, height_ratios=[1, 2], hspace=0.28)
    ax1, ax2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    for ax in (ax1, ax2):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#c9d1d9", labelsize=8)
        for s in ax.spines.values():
            s.set_color("#30363d")
    ax1.plot(freqs / 1e6, powers, color="#58a6ff", lw=0.7)
    ax1.set_title(
        f"{band}   peak {stats['peak_freq_mhz']} MHz @ {stats['peak_dbm']} dB   "
        f"noise {stats['noise_floor_dbm']} dB",
        color="#c9d1d9", fontsize=9)
    ax1.set_ylabel("dB", color="#c9d1d9", fontsize=8)
    ax1.set_xlim(lo_mhz, hi_mhz)
    ax1.grid(color="#21262d", lw=0.4)
    ax2.imshow(_wf, aspect="auto", cmap="viridis",
               extent=[lo_mhz, hi_mhz, 0, rows], origin="upper")
    ax2.set_xlabel("Frequency (MHz)", color="#c9d1d9", fontsize=8)
    ax2.set_ylabel("time (top = newest)", color="#c9d1d9", fontsize=8)

    os.makedirs(WWW_DIR, exist_ok=True)
    tmp = os.path.join(WWW_DIR, ".spectrum.tmp.png")
    fig.savefig(tmp, dpi=90, facecolor="#0d1117", bbox_inches="tight")
    plt.close(fig)
    os.replace(tmp, os.path.join(WWW_DIR, "spectrum.png"))


# ---------------------------------------------------------------- mqtt + loop
state = {"band": CFG["default_band"], "lock": threading.Lock()}


def pub(client, topic, payload, retain=True):
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    client.publish(topic, payload, retain=retain)


def do_sweep(client):
    band = state["band"]
    lo, hi = band_range_mhz(band)
    with state["lock"]:
        # Note: we do NOT publish a "running:true" status with null numerics before
        # the sweep -- that blanked the peak/level/noise sensors to unknown between
        # sweeps. Instead we only publish real stats after a good sweep, and on
        # error we leave the last good values in place.
        try:
            freqs, powers = run_sweep(lo, hi)
            pk = int(np.argmax(powers))
            stats = {"running": True, "band": band,
                     "peak_freq_mhz": round(float(freqs[pk]) / 1e6, 3),
                     "peak_dbm": round(float(powers[pk]), 1),
                     "noise_floor_dbm": round(float(np.percentile(powers, 20)), 1)}
            render(freqs, powers, band, lo, hi, stats)
            pub(client, "sdr/spectrum/status", stats)
            pub(client, "sdr/spectrum/image", "/local/sdr/spectrum.png")
            log(f"swept {band}: peak {stats['peak_freq_mhz']}MHz "
                f"{stats['peak_dbm']}dB noise {stats['noise_floor_dbm']}dB")
        except Exception as e:  # noqa: BLE001
            log("sweep error:", e)


def on_connect(client, userdata, flags, rc, *a):
    log("MQTT connected rc", rc)
    client.subscribe("sdr/spectrum/request")
    pub(client, "sdr/spectrum/status", {"running": False, "band": state["band"],
        "peak_freq_mhz": None, "peak_dbm": None, "noise_floor_dbm": None})


def on_message(client, userdata, msg):
    try:
        band = json.loads(msg.payload.decode()).get("band")
        if band:
            state["band"] = band
            log("band ->", band)
    except Exception as e:  # noqa: BLE001
        log("bad request:", e)


FW_URL = ("https://github.com/greatscottgadgets/hackrf/releases/download/"
          "v2024.02.1/hackrf-2024.02.1.tar.xz")


def do_flash():
    """One-shot: download the 2024.02.1 release, flash hackrf_one_usb.bin via
    hackrf_spiflash (control-transfer path = reliable even though streaming isn't),
    then exit. Enabled by the flash_firmware option; turn it back off after."""
    import urllib.request
    import io
    import tarfile
    import glob
    log("=== FIRMWARE FLASH MODE ===")
    ok, txt = hackrf_info()
    log("before:", "opened" if ok else "NOT opened")
    for line in txt.splitlines():
        log("  " + line)
    try:
        log("downloading", FW_URL)
        data = urllib.request.urlopen(FW_URL, timeout=180).read()
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:xz")
        tf.extractall("/tmp/fw")
        bins = glob.glob("/tmp/fw/**/hackrf_one_usb.bin", recursive=True)
        if not bins:
            log("ERROR: hackrf_one_usb.bin not found in archive")
            return
        binp = bins[0]
        log("flashing", binp)
        r = subprocess.run(["hackrf_spiflash", "-w", binp],
                           capture_output=True, text=True, timeout=240)
        log("spiflash rc", r.returncode)
        for line in (r.stdout + r.stderr).splitlines():
            log("  " + line)
        log("=== FLASH COMPLETE — POWER-CYCLE THE HACKRF, then set flash_firmware=false ===")
        time.sleep(3)
        ok2, txt2 = hackrf_info()
        log("after (pre power-cycle):", "opened" if ok2 else "not opened")
        for line in txt2.splitlines():
            log("  " + line)
    except Exception as e:  # noqa: BLE001
        log("FLASH ERROR:", e)


def find_hackrf_devnode():
    import glob
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            vid = open(d + "/idVendor").read().strip()
            pid = open(d + "/idProduct").read().strip()
            if vid == "1d50" and pid == "6089":
                busnum = int(open(d + "/busnum").read().strip())
                devnum = int(open(d + "/devnum").read().strip())
                return f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
        except OSError:
            continue
    return None


def usb_reset():
    """Electrically reset the HackRF's USB (like a re-plug) via USBDEVFS_RESET."""
    import fcntl
    node = find_hackrf_devnode()
    if not node:
        log("  usb_reset: device node not found")
        return False
    usbdevfs_reset = (ord('U') << 8) | 20  # _IO('U', 20)
    try:
        fd = os.open(node, os.O_WRONLY)
        try:
            fcntl.ioctl(fd, usbdevfs_reset, 0)
        finally:
            os.close(fd)
        log("  usb_reset: OK on " + node)
        return True
    except OSError as e:
        log("  usb_reset: failed " + str(e))
        return False


def transfer_test(rate):
    n = rate * 3
    r = subprocess.run(
        ["hackrf_transfer", "-r", "/dev/null", "-f", "100000000",
         "-s", str(rate), "-n", str(n), "-l", "24", "-g", "16"],
        capture_output=True, text=True, timeout=25)
    got = [ln for ln in (r.stdout + r.stderr).splitlines() if "MiB/second" in ln]
    return got[-1].strip() if got else "FAIL (no throughput)"


def do_diagnostic():
    """Can a USB reset un-wedge the HackRF's RX streaming? Reset it, then test
    hackrf_transfer. If streaming works AFTER a reset, we can auto-recover in
    software (reset-on-stall) instead of needing a physical power-cycle."""
    log("=== HACKRF DIAGNOSTIC v2: USB-reset recovery ===")
    ok, txt = hackrf_info()
    log("  before: " + ("opens" if ok else "NOT opening"))
    log("--- baseline transfer @ 8 Msps (pre-reset) ---")
    log("  " + transfer_test(8000000))
    log("--- USB RESET ---")
    usb_reset()
    time.sleep(6)  # let it re-enumerate
    ok2, _ = hackrf_info()
    log("  after reset: " + ("opens" if ok2 else "NOT opening"))
    for rate in (8000000, 4000000, 2000000):
        log(f"--- transfer @ {rate//1000000} Msps (post-reset) ---")
        log("  " + transfer_test(rate))
    log("=== DIAGNOSTIC DONE (set diagnostic=false) ===")


def main():
    log("starting; WWW_DIR =", WWW_DIR, "| MQTT", CFG["mqtt_host"])
    if CFG.get("diagnostic"):
        do_diagnostic()
        while True:
            time.sleep(3600)
    if CFG.get("flash_firmware"):
        do_flash()
        while True:  # stay alive so the add-on doesn't crash-loop; do nothing
            time.sleep(3600)
    ok, txt = hackrf_info()
    log("hackrf_info:", "OPENED OK" if ok else "FAILED TO OPEN")
    for line in txt.splitlines():
        log("  " + line)

    client = mqtt.Client()
    client.username_pw_set(CFG["mqtt_user"], CFG["mqtt_pass"])
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(CFG["mqtt_host"], CFG["mqtt_port"], 60)
            break
        except Exception as e:  # noqa: BLE001
            log("MQTT connect retry:", e)
            time.sleep(5)
    client.loop_start()

    def heartbeat():
        while True:
            client.publish("sdr/spectrum/heartbeat", "ON", retain=False)
            time.sleep(30)
    threading.Thread(target=heartbeat, daemon=True).start()

    while True:
        do_sweep(client)
        time.sleep(CFG["sweep_interval_s"])


if __name__ == "__main__":
    main()
