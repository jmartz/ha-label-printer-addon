# Breast Milk / Fridge Label Printer — Home Assistant add-on

Press a button (or use your voice) → Home Assistant composes a date/time +
expiration label and prints it. The add-on runs entirely on your HA box and
talks straight to the printers, so no PC needs to be on.

It now drives **two printers** from **three buttons plus voice**:

- **Brother QL-820NWB** — networked thermal label printer in the kitchen
  (TCP 9100, `brother_ql`, no driver). The "always there" printer.
- **Niimbot B1** — battery/BLE portable printer at the pump station
  (via the `hass-niimbot` integration). The "grab it and go" printer.

## Who prints where

| Trigger | Kind | Prints on | Behind the scenes |
|---|---|---|---|
| **"Refrigerator Button"** (LUMI Zigbee) | single-press | **Brother QL** | `zha_event` → `rest_command.print_milk_label` |
| **"Breast Milk Printer Button"** (Aqara Zigbee) | single-press | **Niimbot B1** | `zha_event` → `rest_command.print_milk_label_niimbot` |
| **M5Stack Dial** (ESPHome knob) | dial oz, then press | **Brother QL** | direct `POST /print?oz=<n>` to the add-on |
| **Voice** (HA Assist / Gemini) | "print 2 labels at 4 oz" | **Brother QL** | intent → `print_milk_labels` event → copies + oz |

> The original **Tapo S200D** button was the first trigger; it has been
> retired in favour of the push-based Zigbee buttons (no ~2–5 s poll lag, and
> no phantom-print risk from restored button state). The S200D device still
> exists in HA but no longer has a print automation.

## Architecture

```
  [LUMI Zigbee btn] ─┐                         ┌─ TCP 9100 ─> [Brother QL-820NWB]
  [M5Stack Dial] ────┼─> [Home Assistant] ─> [THIS ADD-ON: Flask + brother_ql]
  [Voice / Assist] ──┘         │  rest_command / POST /print
                               │
  [Aqara Zigbee btn] ──────────┘─> [hass-niimbot integration] ─ BLE ─> [Niimbot B1]
```

Both Zigbee buttons join through a **SONOFF ZBDongle-E** coordinator (ZHA).
The Niimbot is reached over Bluetooth (the HA host's adapter, with an ESPHome
BT proxy for range); the add-on parks a Niimbot label if the printer is asleep
and reprints it on reconnect, so a press is never lost.

## The `rest_command` endpoints (in `configuration.yaml`)

| Service | HTTP | Result |
|---|---|---|
| `rest_command.print_milk_label` | `POST /print` | Brother: universal fridge/milk label, no amount |
| `rest_command.print_milk_label_oz` | `POST /print?oz={{oz}}` | Brother: label with "Amount: N oz" filled in |
| `rest_command.print_milk_label_niimbot` | `POST /print?printer=niimbot` | Niimbot B1 label |

There is also a **web-based label Designer** served by the add-on as an HA
ingress sidebar panel ("Label Designer") for one-off custom labels — drag/drop
text, QR, barcodes, symbols; exact on-screen preview; prints to the Brother.

## One-time setup (summary)

The add-on is installed from a **GitHub add-on repository**
(`github.com/jmartz/ha-label-printer-addon`), so it survives HA backup
restores automatically. To (re)install:

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add the repo URL.
2. Install **"Breast Milk Label Printer"**, open **Configuration**, set
   `printer_ip` (the Brother QL, e.g. `192.168.10.120`) and `label` (`62` for
   62 mm DK-4205). Save, **Start**, enable **Start on boot** + **Watchdog**.
3. Quick test: `curl -X POST http://<HA_IP>:8099/print` → a label prints.

### Adding a button trigger (Zigbee)
1. **Settings → Devices & Services → ZHA → Add device**, then long-press the
   new button (~5 s) to join.
2. Add an automation: trigger **`zha_event`** with `device_ieee: <button IEEE>`
   and `command: single`; action = the `rest_command` for the printer you want
   (`print_milk_label` for Brother, `print_milk_label_niimbot` for Niimbot).
3. Reload automations (Developer Tools → YAML → Reload Automations, or restart
   Core). Press the button — a label prints.

### Voice
The `Voice: print breast milk label(s)` automation exposes an Assist intent:
say e.g. *"print two labels at 4 ounces."* It parses the count (1–10) and
optional oz, guards absurd amounts, and loops the Brother print. Works with the
HA Voice PE hardware (wake word "Okay Nabu") or any Assist entry point.

## Notes
- **Self-healing IP (Brother):** if the printer's DHCP address changes, the
  add-on rescans the LAN for the QL-820NWB, prints, and caches the new IP. A
  DHCP reservation avoids even that one slow scan.
- **Niimbot power:** charge the B1 over **USB-A** — its USB-C port often
  negotiates to 0 W and drains while "plugged in." On adequate power it stays
  awake and instantly printable; otherwise a wake-tap re-advertises it.
- **Timezone:** the label uses the host clock passed to the add-on; confirm the
  HA system timezone so am/pm is correct.
- **Changing the label design:** edit `build_label_image()` in
  `label_render.py` (fridge/milk label) and restart the add-on.
