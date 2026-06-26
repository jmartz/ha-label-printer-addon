# Breast Milk Label Printer — Home Assistant add-on

Press a Tapo S200D button → Home Assistant prints a date/time + expiration
label to your networked Brother QL-820NWB. The add-on runs entirely on your
HA box and talks straight to the printer, so your PC doesn't need to be on.

## Architecture

```
[Tapo S200D]──868MHz──>[Tapo H100 hub]
                              │  (HACS Tapo integration polls the hub)
                              v
                     [Home Assistant automation]
                              │  rest_command: POST /print
                              v
              [This add-on: Flask + brother_ql]──TCP 9100──>[QL-820NWB]
```

## One-time setup

### A. Install the add-on on your HA box
1. Get the files onto HA. Install the **"Samba share"** add-on (or
   **"Advanced SSH & Web Terminal"**), then copy this whole `label_printer`
   folder into the `/addons` share. Result: `/addons/label_printer/`.
2. **Settings → Add-ons → Add-on Store**, click the **⋮** menu →
   **Check for updates**, then reload. A **Local add-ons** section appears
   with **"Breast Milk Label Printer."**
3. Open it → **Install** (first build takes a few minutes).
4. **Configuration** tab → set `printer_ip` (currently `192.168.10.83`) and
   `label` (`62` for your 62mm DK-4205 roll). Save.
5. **Start** the add-on. Check the **Log** tab for `Running on
   http://0.0.0.0:8099`. Enable **Start on boot** and **Watchdog**.
6. Quick test from any computer on the LAN (replace with the HA IP):
   ```
   curl -X POST http://<HA_IP>:8099/print
   ```
   A label should print.

### B. Expose the S200D button to HA
The official TP-Link integration can't see button presses. Use the community
integration:
1. Install **HACS** if you don't have it.
2. In HACS, add/install **"Tapo Controller"**
   (`petretiandrea/home-assistant-tapo-p100`) and restart HA.
3. Add the integration and sign in / point it at your **H100 hub**. Your
   **S200D** shows up as a device that reports button-press and dial events.
   (Note: it **polls**, so expect a ~2–5s delay; double-click isn't supported.)

### C. Wire up the automation
1. Add the `rest_command` from `home-assistant-snippets.yaml` to your
   `configuration.yaml` (use your **HA box's** LAN IP), and restart HA.
2. Create the automation (see the snippet). Easiest: **Settings → Automations
   → Create → Add Trigger → Device →** pick the S200D → **Pressed**, then
   **Add Action → Call service → `rest_command.print_milk_label`**.
3. Press the button. A label prints.

## Notes
- **Self-healing IP:** if the printer's DHCP address changes, the add-on
  detects the unreachable IP, rescans the LAN for the QL printer, prints, and
  caches the new IP in `/data/last_ip.txt`. Set a DHCP reservation if you
  want to avoid even that one slow scan.
- **Timezone:** the label uses the container clock. HA passes the host TZ to
  add-ons; confirm your HA system timezone is correct so am/pm is right.
- **Changing the label design** later: edit `print_server.py`'s
  `build_label_image()` and restart the add-on (Supervisor rebuilds it).
