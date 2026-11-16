# KiTifi-ESP8266-Wireless — board behavior

Target: `KiTifi-ESP8266-Wireless.bin` (ESP8266, WiFi station + AP setup portal).
Method: static trace of `evidence/ghidra/KiTifi-ESP8266-Wireless/funcs/` +
`addr2str.tsv`, cross-referenced with the controller contract and the
companion board docs. The automated deep-dive agent for this board was blocked
by a safeguard; this doc was written directly.

**Same application firmware family as `KiTifi-ESP8266-LAN`.** The coin
acceptance, insert-coin session, the four `*_handler.php` GET reports, the
shared token, the local admin API, and the OTA path are the same design —
read `board-behavior-esp8266-lan.md` for the fully traced logic. This file
records only what the Wireless build does **differently**: it reaches the
controller over WiFi (station mode) and ships an on-device **AP setup portal**
for first-time provisioning instead of relying on DHCP over SPI-Ethernet.

## Confirmed identical to ESP8266-LAN (from strings + corpus)

- **Four outbound endpoints**, byte-identical paths and the same static token:
  - `/admin/api/portal/vendo_handler.php?host=…` (`0x4025dfef`, full URL literal
    `http://kitifi.wifi/admin/api/portal/vendo_handler.php?host=`)
  - `/admin/api/portal/coin_handler.php?id=` (`0x4025dc74`)
  - `/admin/api/portal/pulse_handler.php?mac=` (`0x4025dc2b`)
  - `/admin/api/portal/stay_active.php` (`0x4025dc09`)
  - Shared token `<redacted>` embedded verbatim and appended as `&token=`.
- **Insert-coin session** driven by the controller: `/insertCoin` (`0x4025e095`)
  and `/cancelInsertCoin`, board-local, same seed-total/id/timer semantics.
- **Config document** `config.json` (`0x4025df46`), same keys:
  `coinslot_pin`, `coinslot_set_pin`, `sys_ready_led_pin`, `insert_coin_led_pin`,
  `server_address` — plus the WiFi keys below.
- **Coin model**: pulse-counting on `coinslot_pin`, 1 pulse = 1 unit, server-side
  denomination/time mapping. Same coin_handler(running total) + pulse_handler(burst).

## Difference 1 — default GPIO pins (this is the board the wiring diagram matches)

The Wireless image embeds its default pin config verbatim at `0x4025ff03`:

```json
{"coinslot_pin":"12","coinslot_set_pin":"13","sys_ready_led_pin":"0","insert_coin_led_pin":"2"}
```

| Function | GPIO | NodeMCU label |
|---|---|---|
| Coin pulse in (`coinslot_pin`) | GPIO12 | **D6** |
| Acceptor enable (`coinslot_set_pin`) | GPIO13 | **D7** |
| System-ready LED (`sys_ready_led_pin`) | GPIO0 | D3 |
| Insert-coin LED (`insert_coin_led_pin`) | GPIO2 | D4 |

Because there is **no SPI-Ethernet chip** on this build, the SPI bus
(GPIO12/13/14 = D6/D7/D5) is free, so COIN/SET can use **D6/D7**. This resolves
the wiring-diagram question from the LAN analysis: the **D6 = coin / D7 = set**
wiring belongs to the **Wireless** board, not the LAN build (the LAN build is
forced to D4/D8 because D5/D6/D7 are its W5x00 SPI bus).

## Difference 2 — connectivity: WiFi station, not Ethernet

- Full ESP8266 WiFi/SoftAP SDK stack present (`ieee80211.c`, `wpa`, `softAP`;
  the WiFi-mode routine is e.g. `FUN_402308f4`, which brings up STA and/or AP
  interfaces and logs `mode : …` / `add if` / `softAP`). The Arduino
  `ESP8266HTTPClient` client is used (inventory `0x5393a`), vs the LAN build's
  ArduinoHttpClient-over-EthernetClient.
- The board joins an existing WiFi network as a **station** using stored
  credentials, then talks to the controller at `server_address` (fallback DNS
  name `kitifi.wifi`) over plain HTTP — same outbound protocol as LAN.
- Extra config keys for WiFi (from the strings inventory): `ssid`, `password`,
  `rssi`, `open`, stored in `config.json` / `ap.json`.

## Difference 3 — AP setup portal (first-run provisioning)

When unprovisioned, the board runs its own SoftAP and serves a setup web page
so an installer can hand it WiFi credentials and the controller address without
a serial console. Evidence:

- Embedded HTML (`evidence/wireless-ap-setup.html`, extracted): title
  **"KiTifi - AP Setup"**, default SSID placeholder/value **"KiT Piso Wifi"**
  (`0x4025f1b2`), SSID + password inputs, a controller-IP field.
- Client-side JS calls:
  - `scanSSID()` → `POST /admin/api/scanSSID` (`0x4025f9d6`, endpoint string
    `0x4025e10f`) — scans for nearby APs and populates the SSID picker.
  - on save → `POST /admin/api/saveApConfig` (`0x4025f657`, endpoint
    `0x4025e0c0`) — writes `ssid`/`password`/`server_address` to config and
    reboots into station mode.
- `/admin/api/scanSSID` exists **only** in this build (the LAN build has no WiFi
  to scan). `/admin/api/saveApConfig` exists in both, but here it is the real
  provisioning entry point (in the LAN build it only repoints `server_address`).

## Security notes (carry over from ESP8266-LAN, plus WiFi-specific)

All the LAN findings apply — shared hardcoded controller token sent as a GET
param over cleartext HTTP (S1), likely-constant local admin token (S2),
unauthenticated `/insertCoin`/`/cancelInsertCoin` with caller-chosen `id` and
seed total (S4), unsigned OTA over plain HTTP (S5), no retries / reboot-on-error
(S8). WiFi-specific additions:

- **`/admin/api/scanSSID` and `/admin/api/saveApConfig` are unauthenticated**
  (CORS `*`, like the rest of the admin API). Any station on the AP — or, once
  joined, any host on the LAN — can rewrite the stored SSID/password and
  controller address and reboot the board onto an attacker-chosen network or
  controller.
- WiFi credentials are stored in cleartext in `config.json`/`ap.json` on SPIFFS
  and are returned by the config API.
- The SoftAP setup portal has no admin password of its own; whoever reaches the
  AP during provisioning controls the board's binding.

## Net

Functionally the Wireless board is the ESP8266 vendo firmware with the network
layer swapped from W5x00 SPI-Ethernet to WiFi-station, plus a SoftAP
provisioning portal. Everything the controller sees on the wire — the four GET
handlers, the token, the insert-coin session, the pulse-count coin model — is
identical to the other three boards.
