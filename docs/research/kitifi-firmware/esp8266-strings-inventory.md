# ESP8266 firmware string/protocol inventory (KiTifi-ESP8266-LAN + Wireless)

Method: `strings -n 5` + targeted greps over the two bare app images, offsets
relative to each original `.bin`. Cross-referenced with `kitifi-controller-handoff.md`.

## Device-facing HTTP (board → controller)

All four handler paths present in both images (LAN offsets `0x5726e–0x573a0`;
Wireless `0x5dc09–0x5e0a1`):

| Route | Query | Notes |
|---|---|---|
| `/admin/api/portal/vendo_handler.php` | `host`,`token`,`version=v2.0`,`board`,`type` | Registration/heartbeat; LAN adds `type=lan` (`0x57330–0x5736d`) |
| `/admin/api/portal/coin_handler.php` | `id`,`token` | Coin-count report |
| `/admin/api/portal/pulse_handler.php` | `mac`,`token`,`version` | Pulse-type coin signal |
| `/admin/api/portal/stay_active.php` | — | Keepalive |
| Base URL | `http://kitifi.wifi/admin/api/portal/…` | mDNS-resolved controller |

**Shared token** `<redacted>` embedded in GET formats (LAN `0x572a3`,
`0x57335`; Wireless `0x5dc5c`, `0x5dfc1`) — matches the server-side finding
(masterKey slice, GET param → lands in logs).

## Board-local routes (controller → board)

- `/insertCoin` — enable coin slot (insert session phase 1 callback)
- `/cancelInsertCoin` — abort insert session

## Board-local config/admin API (setup UI)

- `/config.json`, `ap.json` — config documents
- `/admin/api/saveApConfig`, `/admin/api/saveSystemConfig`,
  `/admin/api/getSystemConfig`, `/admin/api/restart`, `/admin/api/forcerestart`,
  `/admin/api/update` (both variants)
- `/admin/api/scanSSID` (Wireless only) — WiFi scan for the setup portal

Config keys: `coinslot_pin`, `coinslot_set_pin`, `sys_ready_led_pin`,
`insert_coin_led_pin`, plus `ssid`, `password`, `rssi`, `server_address`, `open`.

## Variant differences

**LAN** (`359,520 B`, SHA-256 `275bc4ca…870e3`):
- Identifies as `KiTifi-ESP8266-LAN`, `type=lan`
- Arduino Ethernet library: `W5100::init: no chip :-(`, `EthernetServer::write`,
  `EthernetClient::write` (`0x56d94–0x56dd2`) → SPI-attached W5100/W5500 Ethernet
  controller (no WiFi stack active in this build)
- `Arduino/2.2.0` (`0x57925`) is the HTTP User-Agent, not the core version (the core is `2.2.2-dev(38a443e)`), SDK `compiled @ Jul 3 2019` (`0x4e551` area)

**Wireless** (`393,728 B`, SHA-256 `ef10af81…8239`):
- ESP8266WiFi stack (`ESP8266HTTPClient` `0x5393a`)
- Embedded AP-setup HTML from `0x5e180`: default SSID **`KiTifi Piso Wifi`**,
  hostname `kitifi.wifi`, SSID/password inputs, `scanSSID` endpoint
- Config documents: `ap.json`, `config.json`

Only common aligned 16-byte run: `0x0–0x100b` (headers); the two images are
otherwise independent builds.

## Notable absences

No MQTT, NTP, TLS certs, OTA URLs, EEPROM keys, `ArduinoJson` fingerprint
(targeted searches). SPIFFS markers absent (`spiffs_api.h` is a library string,
not an image).

## Coin-slot wiring hint

`coinslot_pin` / `coinslot_set_pin` naming plus `pulse_handler` imply a simple
GPIO pulse-counting interface: the acceptor's COIN line pulses N times per
denomination; the board counts pulses per session and reports the total. Exact
denomination mapping happens server-side via the `rates` tiers (server-side
billing — see `server-contract.md`).
