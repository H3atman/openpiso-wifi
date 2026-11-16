# KiTifi-LAN8720 — decompiled behavior deep dive

MCU: ESP32. Network: **ESP32 internal EMAC + LAN8720 RMII PHY** via the native
ESP-IDF `esp_eth` stack (no SPI Ethernet chip). Build: Arduino core 2.0.9 / IDF
4.4.4, Apr 20 2023. Evidence under
`evidence/ghidra/KiTifi-LAN8720/funcs/`.

**This is the same application firmware as KiTifi-ESP32-LAN**, recompiled for the
LAN8720 PHY network path. The coin-acceptance logic, session state machine,
reporting URLs, and shared token are byte-for-byte equivalent; only the
Ethernet driver and function/data addresses differ. Read
`board-behavior-esp32-lan.md` for the full prose; this file records the
LAN8720-specific addresses and the one real difference (connectivity).

## Confirmed-identical behavior (different addresses)

| Role | ESP32-LAN | LAN8720 |
|---|---|---|
| config loader (`/config.json`) | `FUN_400da7c0` | `FUN_400d61d8` |
| coin_handler.php reporter | `FUN_400daaec` | `FUN_400d36d8` |
| pulse_handler.php reporter | `FUN_400da9c0` | `FUN_400d33dc` |
| vendo_handler.php (register/heartbeat) | `FUN_400dac40` | `FUN_400d56f0` |
| stay_active.php (keepalive) | `FUN_400da944` | `FUN_400d3324` |
| `/insertCoin` handler | `FUN_400d8dc0` | `FUN_400d63f8` |
| coin persist/update | `FUN_400d8390` | `FUN_400d3690` |

### coin_handler — `FUN_400d36d8` (verified identical shape)
Same guarded accumulate then GET:
```c
if (*DAT_400d00dc == 0) {               // not already pending
    *DAT_400d00ec += *DAT_400d00d8;     // running coin total += this coin's value
    FUN_400d3690();                      // persist
}
// itoa(total,buf,10); build:
//   http://<server_address>/admin/api/portal/coin_handler.php?id=<id>&...&coins=<total>&token=<redacted>
```
The URL is assembled by string concatenation from the literals
`http://`, `/admin/api/portal/coin_handler.php?`, `&coins=`,
`&token=<redacted>` — the **same static token** as every other board.

### config loader — `FUN_400d61d8` (verified identical keys)
Logs `"Loading system configuration"`, opens `/config.json`, reads
`server_address`, `coinslot_pin`, `coinslot_set_pin`, `sys_ready_led_pin`,
`insert_coin_led_pin`, `setup` — same key set, same data-driven GPIO model as
ESP32-LAN.

## The one real difference — connectivity

- **LAN8720** uses the ESP32's **internal Ethernet MAC (EMAC)** wired to an
  external **LAN8720 RMII PHY**, driven by ESP-IDF's native `esp_eth` stack.
  Supporting symbols in this image: `esp_eth_mac_new_esp32`,
  `esp_eth_phy_new_lan87xx`, `lan87xx_*`, EMAC/RMII init
  (`evidence` funcs `FUN_400edcf4`, `FUN_400ede90`, `FUN_400ee398`,
  `FUN_400f073c`, …). None of the Arduino `Ethernet`/`W5100` library strings
  that ESP32-LAN carries are present here.
- **ESP32-LAN**, by contrast, uses an external **W5x00 SPI** Ethernet chip via
  the Arduino Ethernet library.

Both end up as wired-Ethernet HTTP clients of the controller; the difference is
purely the PHY/driver layer below the socket API. Everything above the socket —
coin counting, the insert-coin session, the four `*_handler.php` GET calls, and
the token — is the same.

## Everything else

Coin acceptance (pulse ISR + ~130 ms debounce), the `FUN_400db82c`-equivalent
poll loop, the two-phase `/insertCoin` → `/cancelInsertCoin` session, the
heartbeat/keepalive cadence, and the security observations (one shared token in
a GET param, plain HTTP, server-side coin→money mapping) all carry over
unchanged from `board-behavior-esp32-lan.md`.
