#!/usr/bin/env python3
"""FM broadcast radio via RTL-SDR for Home Assistant.

One 171 kHz FM MPX capture feeds BOTH audio and RDS (redsea --feed-through):
  rtl_fm -M fm -s 171k -f FREQ | redsea -u -r 171k -e
     stdout (MPX)  -> ffmpeg (lowpass 15k -> MP3) -> Icecast :8000/radio.mp3
     stderr (RDS)  -> parse -> MQTT radio/fm/*  (+ HA MQTT discovery sensors)
Icecast always drains the audio branch, so RDS keeps flowing even with no listener.
Change station: publish the MHz (e.g. "106.7") to radio/fm/set_frequency.
Needs usb:true AND the RTL free (stop the rtl_433 add-on first — one tuner, one job).
"""
import json
import os
import subprocess
import threading
import time

import paho.mqtt.client as mqtt


def log(*a):
    print("[fm]", *a, flush=True)


def load_config():
    cfg = {"frequency_mhz": 102.7, "gain": 30, "mqtt_host": "core-mosquitto",
           "mqtt_port": 1883, "mqtt_user": "mqtt_sdr", "mqtt_pass": "sdr_mqtt_2026",
           "audio_bitrate": "128k", "http_port": 8000}
    try:
        with open("/data/options.json") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v not in (None, "")})
    except FileNotFoundError:
        pass
    cfg["mqtt_port"] = int(cfg["mqtt_port"])
    cfg["gain"] = int(cfg["gain"])
    cfg["http_port"] = int(cfg["http_port"])
    cfg["frequency_mhz"] = float(cfg["frequency_mhz"])
    return cfg


CFG = load_config()
state = {"freq": CFG["frequency_mhz"], "procs": [], "rds": {}, "lock": threading.RLock()}


def start_icecast():
    try:
        p = subprocess.Popen(["icecast2", "-c", "/app/icecast.xml"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        time.sleep(2)
        log("icecast started (pid %s)" % p.pid)
        return p
    except Exception as e:  # noqa: BLE001
        log("icecast failed to start:", e)
        return None


def build_pipeline():
    freq = f"{state['freq']}M"
    rtl = subprocess.Popen(
        ["rtl_fm", "-M", "fm", "-l", "0", "-A", "std", "-p", "0", "-s", "171k",
         "-g", str(CFG["gain"]), "-F", "9", "-f", freq],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    redsea = subprocess.Popen(
        ["redsea", "-u", "-r", "171k", "-e"],
        stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    rtl.stdout.close()
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", "171000", "-ac", "1", "-i", "-",
         "-af", "lowpass=15000", "-ar", "48000", "-ac", "1",
         "-c:a", "libmp3lame", "-b:a", CFG["audio_bitrate"],
         "-content_type", "audio/mpeg", "-f", "mp3",
         f"icecast://source:hackme@localhost:{CFG['http_port']}/radio.mp3"],
        stdin=redsea.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    redsea.stdout.close()
    return [rtl, redsea, ff], redsea.stderr


def rds_reader(client, stderr):
    for raw in iter(stderr.readline, b""):
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("{"):
            continue
        try:
            g = json.loads(line)
        except json.JSONDecodeError:
            continue
        changed = False
        for k in ("ps", "radiotext", "callsign", "pi", "prog_type"):
            v = g.get(k)
            if v not in (None, "") and str(v) != str(state["rds"].get(k)):
                state["rds"][k] = str(v).strip()
                client.publish(f"radio/fm/{k}", state["rds"][k], retain=True)
                changed = True
        if changed:
            client.publish("radio/fm/state", json.dumps(state["rds"]), retain=True)


def stop_pipeline():
    with state["lock"]:
        for p in state["procs"]:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        for p in state["procs"]:
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        state["procs"] = []


def start_pipeline(client):
    with state["lock"]:
        state["rds"] = {}
        procs, rds_err = build_pipeline()
        state["procs"] = procs
    threading.Thread(target=rds_reader, args=(client, rds_err), daemon=True).start()
    client.publish("radio/fm/frequency", str(state["freq"]), retain=True)
    for k in ("ps", "radiotext", "callsign", "prog_type"):
        client.publish(f"radio/fm/{k}", "", retain=True)
    log("tuned %.1f MHz; audio at :%d/radio.mp3" % (state["freq"], CFG["http_port"]))


DISCOVERY = {
    "ps": ("FM Station", "mdi:radio", None),
    "radiotext": ("FM Now Playing", "mdi:music", None),
    "callsign": ("FM Callsign", "mdi:identifier", None),
    "frequency": ("FM Frequency", "mdi:sine-wave", "MHz"),
}


def publish_discovery(client):
    dev = {"identifiers": ["fm_radio_rtlsdr"], "name": "FM Radio (RTL-SDR)",
           "model": "RTL-SDR Blog V4", "manufacturer": "redsea"}
    for key, (name, icon, unit) in DISCOVERY.items():
        cfg = {"name": name, "unique_id": f"fm_radio_{key}",
               "state_topic": f"radio/fm/{key}", "icon": icon, "device": dev}
        if unit:
            cfg["unit_of_measurement"] = unit
        client.publish(f"homeassistant/sensor/fm_radio_{key}/config",
                       json.dumps(cfg), retain=True)


def on_connect(client, userdata, flags, rc, *a):
    log("MQTT connected rc", rc)
    publish_discovery(client)
    client.subscribe("radio/fm/set_frequency")


def on_message(client, userdata, msg):
    try:
        fq = float(msg.payload.decode().strip())
    except ValueError:
        return
    if 87.0 <= fq <= 108.5 and abs(fq - state["freq"]) > 0.01:
        log("change station ->", fq)
        with state["lock"]:
            state["freq"] = fq
            stop_pipeline()
            start_pipeline(client)


def main():
    log("starting; %.1f MHz gain %d" % (CFG["frequency_mhz"], CFG["gain"]))
    start_icecast()
    client = mqtt.Client()
    client.username_pw_set(CFG["mqtt_user"], CFG["mqtt_pass"])
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(CFG["mqtt_host"], CFG["mqtt_port"], 60)
            break
        except Exception as e:  # noqa: BLE001
            log("mqtt connect retry:", e)
            time.sleep(5)
    client.loop_start()
    start_pipeline(client)
    while True:
        time.sleep(10)
        with state["lock"]:
            dead = (not state["procs"]) or any(p.poll() is not None for p in state["procs"])
        if dead:
            log("pipeline down; restarting")
            with state["lock"]:
                stop_pipeline()
                start_pipeline(client)
        client.publish("radio/fm/heartbeat", "ON", retain=False)


if __name__ == "__main__":
    main()
