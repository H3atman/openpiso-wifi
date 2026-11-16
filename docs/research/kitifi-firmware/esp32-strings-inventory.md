# ESP32 firmware string/protocol inventory (KiTifi-ESP32-LAN + KiTifi-LAN8720)

Method: `strings -n 5` over the carved app0 partitions, cross-referenced with the
server-side contract (`kitifi-controller-handoff.md`). Offsets are relative to each carved
`*-app.bin` (= flash offset 0x10000).

## Server-facing routes (the board is an HTTP *client* of the controller)

| Route | Query params | Evidence (LAN / LAN8720 offsets) |
|---|---|---|
| `/admin/api/portal/stay_active.php` | — | LAN `+0x92f` / LAN8720 `+0x1402` |
| `pulse_handler.php?mac=` | `&token=…&version=` | LAN `+0x951`, `+0x982` / LAN8720 `+0x1424`, `+0x1455` |
| `coin_handler.php?id=` | `&token=…` | LAN `+0x99a`, `+0x9f6` / LAN8720 `+0x1470`, `+0x16f9` |
| `vendo_handler.php?host=` | `&token=…&version=` | LAN `+0x9c7` / LAN8720 `+0x17d0` |
| Base host | `http://kitifi.wifi` (mDNS name) | LAN `+0xa2e` / LAN8720 `+0x1731` (full URL literal) |

**Shared static token** `<redacted>` is embedded verbatim in both firmwares
(matches the server-side finding: it is `masterKey[31:47]`, sent as a GET param).

## Local board routes (the board is an HTTP *server* for the controller)

| Route | Purpose | Evidence |
|---|---|---|
| `/insertCoin` | Server→board callback enabling the coin slot (phase 1 of insert session) | LAN `+0xa56` / LAN8720 `+0x17dc` |
| `/cancelInsertCoin` | Server→board: abort the insert session | LAN `+0xa62` / LAN8720 `+0x17dc` area |
| `/config.json` | Board configuration document | LAN `+0x8xx` / LAN8720 `+0x15xx` |
| `/admin/api/update` | Local management/OTA-ish route | LAN string table |
| `index.html` | Landing page name (LAN8720) | LAN8720 `+0x1613` |

## Config keys (JSON) — board-side settings

`coinslot_pin`, `coinslot_set_pin`, `sys_ready_led_pin`, `insert_coin_led_pin`,
plus `/config.json`. These are *names*, not numeric GPIO values — pins are
data-driven, configured at runtime (loaded from `/config.json` or NVS).

## Presence / registration

`vendo_handler.php?host=<mac-ish id>&token=…&version=` is the registration +
heartbeat call (server upserts `tbl_vendo` by MAC, refreshes `last_seen`).
`stay_active.php` keeps the hotspot session alive.

## What distinguishes LAN8720 from ESP32-LAN

- **Both are wired Ethernet, via different hardware paths:**
  - **ESP32-LAN** = ESP32 + external **W5x00 SPI** Ethernet chip, driven by the
    Arduino Ethernet library: `W5100::init: no chip :-(` (`0x3f40014d`),
    `EthernetClient::write`/`EthernetServer::write` (`0x3f400166`, `0x3f40018b`),
    and `EthernetWebServer::_parseForm` (`0x3f400600`). Same approach as the
    ESP8266-LAN board. Reports `"type":"lan"`.
  - **LAN8720** = ESP32 internal EMAC + **LAN8720 RMII PHY** via the native
    ESP-IDF `esp_eth` stack: `esp_eth_mac_new_esp32`, `lan87xx_*`,
    `esp_eth_phy_new_lan87xx` (`+0x59d3–0x7212`). ESP32-LAN has none of these.
  - (Correction: an earlier draft guessed ESP32-LAN was WiFi. The Ethernet
    library strings, found once the address→string map was built, show it's wired.)
- Different builds: LAN = Arduino core 2.0.11 / IDF 4.4.5, built Jun 12 2023 16:41:45,
  ELF SHA-256 `f5b8d630…40dc`; LAN8720 = core 2.0.9 / IDF 4.4.4, built Apr 20 2023
  00:53:55, ELF SHA-256 `679f44d2…a4ab` (app_desc at `+0x20`).
- Bootloaders differ (SHA-256 `d88065d2…e098` vs `f660002e…7600`), both 28,672 B.
- Board identifiers: `KiTifi-ESP32-LAN` (`+0xa45`), `KiTifi-LAN8720` / `lan8720`
  (`+0x1790`, `+0x18a3`).

## Absences (negative findings)

- No MQTT, no W5500, no NTP literals, no ICT/NCC coin-acceptor protocol names,
  no TLS certificates, no literal OTA download URL.
- No valid gzip payload (LAN8720's `1f 8b` at `+0x5acce` does not inflate).
- LAN embeds a bind-node message + small redirect `<script>` whose target URL is
  assembled from `http://` + a runtime value.
