# KiTifi-ESP32-LAN — decompiled behavior deep dive

MCU: ESP32 (Xtensa LX6, dual-core). Network: external **W5x00 SPI Ethernet**
chip driven by the Arduino Ethernet library. Build: Arduino core 2.0.11 / IDF
4.4.5, Jun 12 2023. Evidence: Ghidra 12.1.2 decompilation under
`evidence/ghidra/KiTifi-ESP32-LAN/funcs/`, cross-referenced with the
address→string map (`addr2str.tsv`) and the controller contract
(`../kitifi-controller-handoff.md`, `server-contract.md`).

All addresses below are the Ghidra function/data addresses for this image.

## Runtime at a glance

```
boot
 └─ FUN_400da7c0  load /config.json  → server_address, GPIO pins, setup flag
 └─ bring up W5x00 Ethernet (Arduino Ethernet lib)
 └─ register + heartbeat loop
        FUN_400dac40  vendo_handler.php   (presence / registration)
        FUN_400da944  stay_active.php     (keepalive)
 main poll loop: FUN_400db82c (ticked continuously)
   ├─ not in session → periodic heartbeat/keepalive only
   └─ in session (server called /insertCoin):
         coin ISR FUN_400d2a2c counts pulses on coinslot_pin
         after 130 ms quiet → report:
           FUN_400daaec  coin_handler.php   (coin total)
           FUN_400da9c0  pulse_handler.php  (raw pulse count)
         /cancelInsertCoin or timeout → FUN_400d8f10 finalize, close session
```

## 1. Configuration load — `FUN_400da7c0`

Logs `"Loading system configuration"`, opens `/config.json`, JSON-parses it, and
reads these keys into global state (GPIO numbers are **data-driven**, not
hardcoded):

| JSON key | Stored to | Meaning |
|---|---|---|
| `server_address` | `DAT_400d02b4` | controller base host |
| `coinslot_pin` | `*DAT_400d03b8` | GPIO that receives acceptor pulses |
| `coinslot_set_pin` | `*DAT_400d033c` | GPIO that enables/gates the acceptor |
| `sys_ready_led_pin` | (LED) | "system ready" indicator |
| `insert_coin_led_pin` | `*DAT_400d03c4` | "insert coin" indicator |
| `setup` | `*DAT_400d0350` | setup-mode flag |
| (connection state) | `*DAT_400d02ac` | 1 = controller reachable/connected |

The board also serves `/config.json` and a local admin API (`/admin/api/update`)
for provisioning — it is both an HTTP client (to the controller) and a small
HTTP server (for the controller/setup UI).

## 2. Coin acceptance — ISR `FUN_400d2a2c` + debounce

The coin acceptor is a **pulse device**: each coin emits N pulses on
`coinslot_pin` (1 pulse ≈ 1 peso unit, denomination mapping happens server-side
via rate tiers — see `server-contract.md`). The ISR only runs while the slot is
enabled (`*DAT_400d0028 != 0`, set when a session is active):

```c
if (*DAT_400d0028 != 0) {       // slot enabled for this session
    *DAT_400d002c += 1;         // coin-value accumulator
    *DAT_400d0030  = 1;         // coin-pending flag
    *DAT_400d0034 += 1;         // raw pulse counter
    *DAT_400d0038  = 1;         // pulse-pending flag
    *DAT_400d003c  = now + 0x82; // arm 130 ms quiet-window timer
}
```

`0x82` = **130 ms**. A coin produces a burst of pulses; the board waits for
130 ms of silence after the last edge before treating the burst as complete and
reporting it. This is a standard coin-pulse grouping/debounce.

## 3. Main dispatch loop — `FUN_400db82c`

Polled continuously. Gated by the session flag `*DAT_400d056c`:

- **No active session**: if connected (`*DAT_400d02ac == 1`) and the keepalive
  timer has elapsed, it pings the controller (`FUN_400dec98`). No coin handling.
- **Active session** (server has called `/insertCoin`):
  1. **Timeout/cancel check** — session deadline is `*DAT_400d0348`; a cancel
     request sets `DAT_400d03d8`. Either one jumps to finalize
     (`LAB_400db858` → `FUN_400d8f10`) which reports the final state and clears
     the session.
  2. **Coin report** — when the pulse-pending flag is set and the 130 ms window
     has passed (`*DAT_400d003c < now`), it snapshots the accumulator, subtracts
     the reported amount, clears the pending flag, and calls:
     - `FUN_400da944` (stay_active) once at session start (`DAT_400d03f8` latch),
     - `FUN_400daaec` → **coin_handler.php** with the running coin total,
     - `FUN_400da9c0` → **pulse_handler.php** with the raw pulse count.
  3. **Heartbeats** — while connected it also fires `FUN_400dac40`
     (vendo_handler) and `FUN_400da944` (stay_active) on their own interval
     timers (`DAT_400d0570/0574`, `DAT_400d0578/057c`).

## 4. Reporting to the controller (board = HTTP client)

Every report is a plain HTTP **GET** with the shared static token appended.

### coin_handler — `FUN_400daaec`
Accumulates then reports. The guarded accumulate is:
```c
if (*DAT_400d0344 == 0) {                 // not already pending
    *DAT_400d03dc += *DAT_400d0340;       // running coin total += this coin's value
    FUN_400d8390();                        // persist / update state
}
// itoa(total,buf,10); then GET:
//   /admin/api/portal/coin_handler.php?id=<id>&mac=<mac>&coins=<total>&token=<redacted>
```

### pulse_handler — `FUN_400da9c0`
```c
// itoa(pulsecount,buf,10); then GET:
//   /admin/api/portal/pulse_handler.php?mac=<mac>&coins=<n>&token=<redacted>
```

### vendo_handler — `FUN_400dac40` (registration + heartbeat)
`/admin/api/portal/vendo_handler.php?host=<id>&token=…&version=…` — server
upserts `tbl_vendo` by MAC and refreshes `last_seen`. Reports `"type":"lan"`.

### stay_active — `FUN_400da944` (keepalive)
`/admin/api/portal/stay_active.php` — keeps the current hotspot session alive.

**Token:** `<redacted>` is embedded verbatim and appended as a `&token=`
GET param on every call. Server-side this is `masterKey[31:47]` and lands in
request logs — see the security notes in `server-contract.md`.

## 5. Insert-coin session (controller → board)

The controller drives a two-phase session via board-local HTTP routes:

- **`/insertCoin`** → handler `FUN_400d8dc0`. Parses the request body for
  `coins`, `id`, `timer`, `status`; stores the coin total (`*DAT_400d03dc`), sets
  the session timer `PTR_DAT_400d034c = timer * 1000` (ms), records the session
  `id` (`DAT_400d03f0`), enables the coin slot, and replies
  `application/json` 200. This arms the acceptor so the ISR will start counting.
- **`/cancelInsertCoin`** → aborts the session; the main loop's cancel branch
  finalizes and disables the slot.
- Natural expiry: when `now > *DAT_400d0348` the loop finalizes the same way.

## 6. Connectivity — W5x00 SPI Ethernet

Wired Ethernet through an external W5x00 chip on SPI, via the Arduino Ethernet
library (not WiFi). Supporting strings in this image: `W5100::init: no chip :-(`,
`EthernetClient::write`, `EthernetServer::write`, `EthernetWebServer::_parseForm`.
The controller is reached by `server_address` from config (mDNS name
`kitifi.wifi` is also embedded as a fallback base URL).

## 7. Security observations

- Single hardcoded bearer token shared across the whole fleet, sent as a GET
  query param (logged server-side, no per-device secret, no rotation).
- Plain HTTP, no TLS. Coin/pulse counts and session control travel in cleartext
  on the LAN.
- The coin→money mapping is entirely server-side; a board that can reach the
  controller can replay `coin_handler.php?...&coins=N&token=…` to credit
  arbitrary coins (the token is the only gate). This matches the server-side
  findings in `kitifi-controller-handoff.md`.

## Function index (this image)

| Address | Role |
|---|---|
| `FUN_400da7c0` | load `/config.json` (server_address, pins, setup) |
| `FUN_400d2a2c` | coin-slot ISR (pulse count + 130 ms debounce arm) |
| `FUN_400db82c` | main poll loop / handler dispatch |
| `FUN_400d8dc0` | `/insertCoin` request handler |
| `FUN_400daaec` | coin_handler.php reporter (coin total) |
| `FUN_400da9c0` | pulse_handler.php reporter (raw pulses) |
| `FUN_400dac40` | vendo_handler.php (registration/heartbeat) |
| `FUN_400da944` | stay_active.php (keepalive) |
| `FUN_400d8f10` | session finalize |
| `FUN_400d8390` | persist/update coin state |
| `FUN_40089180` | itoa (int→decimal string, base arg) |
