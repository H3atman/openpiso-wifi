# KiTifi-ESP8266-LAN: board behavior (decompiled-code walkthrough)

Target: `KiTifi-ESP8266-LAN.bin` (ESP8266, SPI-Ethernet, `type=lan`, `host=KiTifi-ESP8266-LAN`).
Method: static trace of the Ghidra corpus (`evidence/ghidra/KiTifi-ESP8266-LAN/funcs/`) plus raw-byte
verification against the `.bin` where the corpus is wrong (see "Corpus addressing caveat").
No tools other than file reads / byte lookups were used; nothing in the corpus was modified.

Confidence tags: **[decomp]** read straight from decompiled C; **[bytes]** decoded by hand from raw
Xtensa bytes (Ghidra did not decode it); **[inferred]** reasoned from library idiom, not directly proven.

---

## 0. Corpus addressing caveat (read this first)

The Ghidra project flat-mapped the whole file at `0x40200000 + file_offset`. The real ESP8266 image
(`flash-layout.md`) has a second image header at file `0x1000` with 5 segments:

| Seg | Runtime addr | Size | File offset | Ghidra sees it at |
|---|---|---|---|---|
| 0 | 0x40201010 (IROM, all app+SDK code) | 0x4e7cc | 0x1010 | 0x40201010 (correct) |
| 1 | 0x40100000 (IRAM) | 0x102 | 0x4f7e4 | 0x4024f7e4 (wrong) |
| 2 | 0x40100104 (IRAM, **ISR + IRAM code**) | 0x6c45 | 0x4f8ee | 0x4024f8ee (wrong, mostly undecoded/garbage) |
| 3 | 0x3ffe8000 (DRAM .data/.rodata) | 0x5fc | 0x5653b | 0x4025653b (wrong) |
| 4 | 0x3ffe8640 (DRAM .data/.rodata) | 0x1118 | 0x56b3f | 0x40256b3f (wrong) |

Consequences:
- All app strings in `strings.tsv`/`addr2str.tsv` at `0x40256d1a..0x40257c..` are really **DRAM at
  `0x3ffe8640 + (G - 0x40256b3f)`** (e.g. `/insertCoin` G=`0x4025738c` -> runtime `0x3ffe8e8d`). Decompiled
  code reaches them via literal-pool entries `DAT_402xxxxx` (the pool word holds the runtime address), which
  is why plain greps for the string address find nothing. I resolved pools by scanning the binary for the
  runtime address as a little-endian word; every `DAT_` citation below was resolved that way, and the
  string text shown in `DAT_xxxx/*"..."*/` form is from that resolution.
- Functions at `0x4024f9xx..0x402563dc` in `functions.txt` are mis-based IRAM code/data, not real
  functions. The **coin ISR lives there** and had to be hand-decoded (section 3.2).
- Globals are in BSS/DATA at `0x3ffef2xx..0x3ffef8xx` (runtime). I cite them by runtime address.

---

## 1. Function map (app layer, all in IROM)

| Function | Role |
|---|---|
| `FUN_4020d6c8` | Arduino `main` loop trampoline: calls `setup()` (`FUN_40209290`) once, then `loop()` (`FUN_402090a8`) forever |
| `FUN_40209290` | `setup()` |
| `FUN_402090a8` | `loop()` (session state machine + heartbeats + `server.handleClient()` via `PTR_LAB_402090a4`=`0x40206bb8`) |
| `FUN_40207e6c` | load `config.json` |
| `FUN_40207f5c` | write file (`"w"`), used for `config.json` |
| `FUN_40208708` | register / heartbeat: `vendo_handler.php` (+ auto-bind) |
| `FUN_402086a8` | `stay_active.php` |
| `FUN_402084c4` | `coin_handler.php` (running total) |
| `FUN_402085e8` | `pulse_handler.php` (burst delta) |
| `FUN_40206368` | `/insertCoin` handler |
| `FUN_4020642c` | `/cancelInsertCoin` handler |
| `FUN_40206240` | start / re-arm coin session |
| `FUN_40206aac` | end coin session |
| `FUN_40207ff0` | `/admin/api/saveApConfig` |
| `FUN_402081c4` | `/admin/api/saveSystemConfig` |
| `FUN_402064bc` | `/admin/api/getSystemConfig` |
| `FUN_402067d0` | `/admin/api/restart` |
| `FUN_40206720` | `/admin/api/forcerestart` |
| `FUN_402068a4` / `FUN_402069ac` | `/admin/api/update` upload callback / completion handler (OTA) |
| `FUN_40204d2c`, `LAB_40204d88` | `/` handler (bind page / redirect) |
| `FUN_40204e0c` | `onNotFound` (CORS preflight / 404) |
| `FUN_4020614c` | add permissive CORS headers |
| `FUN_4020a8dc` | `ESP.restart()` |
| ISR at runtime `0x4010011c` | coin pulse counter (hand-decoded) |

Libraries recognized: Stoffregen/Arduino `Ethernet` (W5100/W5200/W5500 autodetect, strings `W5100::init: no chip :-(`
at `0x40256d94`), **ArduinoHttpClient** (not ESP8266HTTPClient: `" HTTP/1.1"`, `"Host: "`, `"User-Agent"`,
`Arduino/2.2.0` (HTTP User-Agent, not the core version; the core is `2.2.2-dev(38a443e)`), `"HTTP/*.* "` status matcher `FUN_4020a56c`), a web server for the local admin routes (the string `ESP8266WebServer` is not in the image; the exact class is unverified), Arduino_JSON-style parser
(`null`/`Infinity` literals, `FUN_402073bc` int getter), SPIFFS (`spiffs_api.h` `0x4024f590`).

---

## 2. Boot / setup flow  (`FUN_4020d6c8` -> `FUN_40209290`)

[decomp] `FUN_40209290` in order:

1. `FUN_4020b200(Serial, 0x1c200, 0x1c, 0, 1)` = `Serial.begin(115200, 8N1)`.
2. `FUN_402176ac(DAT_40207e4c=0x3ffef8a8)` = `SPIFFS.begin()` (vtable `begin`). **If it fails, the rest of
   `setup()` is skipped** (whole body is inside `if (iVar1 != 0)`), so the board is a brick with no server.
3. `FUN_40207e6c` loads config: prints `Loading system configuration` (`0x40257191`), opens `config.json`
   (`0x402571ae`, mode `"r"`, no leading slash), parses JSON, then reads, via `FUN_402073bc`
   (int getter: string -> atoi-like, returns 0 if the key is absent):

   | JSON key (string addr) | Stored in (runtime) | Pool |
   |---|---|---|
   | `server_address` (`0x402571ba`) | String `0x3ffef39c` | `DAT_40204d7c` |
   | `coinslot_pin` (`0x402571c9`) | int `0x3ffef3bc` | `DAT_40206498` |
   | `coinslot_set_pin` (`0x402571d6`) | int `0x3ffef3b8` | `DAT_40206220` |
   | `sys_ready_led_pin` (`0x402571e7`) | int `0x3ffef3b0` | `DAT_402064a4` |
   | `insert_coin_led_pin` (`0x402571f9`) | int `0x3ffef3b4` | `DAT_40206238` |
   | `setup` (`0x4025720d`) | int `0x3ffef3ac` | `DAT_40204d78` (1 = bound/configured) |

   Storage = **SPIFFS file `config.json`**, not EEPROM.
4. GPIO modes [decomp] (`FUN_4020dd74` = `pinMode`):
   - `pinMode(coinslot_pin, 2)` = **INPUT_PULLUP**
   - `pinMode(insert_coin_led_pin, 1)`, `pinMode(sys_ready_led_pin, 1)`, `pinMode(coinslot_set_pin, 1)` = OUTPUT
   - Pin value 255 (config `-1`) is a no-op in the core (`pinMode` only acts for pin < 0x10 / ==0x10).
5. `FUN_40209738(WiFi, 0)` = `WiFi.mode(WIFI_OFF)`, then `*DAT_40202090 = 0` which sets the
   **Ethernet chip-select variable to GPIO0** (variable at `0x3ffe85dc`, `.data` initial value 15, read by
   the W5x00 `init` in `FUN_402020a4` as `ss_pin`) [decomp + bytes].
6. MAC: `FUN_4020998c` -> `FUN_4024cba8(0, buf)` = `wifi_get_macaddr(STATION_IF)`. The Ethernet MAC **is the ESP8266
   station MAC**; the same call formatted `%02X:%02X:...` (`FUN_402099ac`, fmt `0x402578af`) fills the `mac` String
   (`0x3ffef3d8`).
7. **DHCP**: `FUN_40203df8(Ethernet, mac, 60000, 4000)` = `Ethernet.begin(mac, timeout=60 s, respTimeout=4 s)`;
   inside it `FUN_40203d88` is the DHCP client. **No static-IP path exists** in the app (no static strings, no
   `Ethernet.begin(mac, ip, ...)`). On success: IP is read back (`FUN_4020b660` String of localIP -> `0x3ffef3cc`),
   host String `0x3ffef3c0 = "KiTifi-ESP8266-LAN"` (`DAT_4020922c`, G `0x40257379`) and the "network ready" flag
   `0x3ffef291` (`DAT_4020907c`) = 1.
8. `0x3ffef3a8 = FUN_4020c22c(0x124ef1c7, 0xa4c67fff)`: the **admin token** (section 8, finding S2).
9. LED init: `digitalWrite(sys_ready_led, LOW)` (the compare `*0x3ffef420 == *0x3ffe85cc` is a never-written
   polarity variable vs const 1; always false -> LEDs are active-HIGH).
10. Branch on `setup` (`0x3ffef3ac`):
    - **`setup == 0` (unbound):** `0x3ffef291` forced to 0, a different catch-all handler is installed
      (`PTR_PTR_40209230`, not decoded). No ISR, no `/insertCoin`.
    - **`setup == 1` and network ready (bound):** all LEDs and SET driven LOW, then
      `FUN_4020e11c(coinslot_pin, ISR=0x4010011c, mode=1)` = **`attachInterrupt(coinslot_pin, isr, RISING)`**
      (`FUN_4020e058` writes int-type `(mode&0xf)<<7` into the GPIO pin reg; mode 1 = positive edge; it also
      checks the ISR address is in IRAM, `"ISR not in IRAM!"` `0x40257960`). Registers `/insertCoin`
      (`FUN_40206368`) and `/cancelInsertCoin` (`FUN_4020642c`), and `onNotFound` = `FUN_40204e0c`.
11. Unconditionally registers (both modes): `/admin/api/saveApConfig`, `saveSystemConfig`, `getSystemConfig`,
    `restart`, `/` (`LAB_40204d88`), `forcerestart`, and `POST /admin/api/update` (`FUN_40206e80(..., method=3 POST,
    fn=FUN_402069ac, uploadFn=FUN_402068a4)`). Then `FUN_40204318` = `server.begin()` (port 80) and **`FUN_40208708()`
    once** (first registration / auto-bind).

---

## 3. Coin acceptance mechanism

### 3.1 What the pins do

- **COIN line (`coinslot_pin`)**: INPUT_PULLUP, interrupt on **RISING** edge. With an open-collector acceptor
  that idles high and pulls low during a pulse, the board counts the **trailing edge of each pulse**.
- **SET line (`coinslot_set_pin`)**: plain digital OUTPUT. `FUN_40206240` (session start) does
  `digitalWrite(SET, HIGH); delay(100)`; `FUN_40206aac` (session end) does `digitalWrite(SET, LOW)`. So SET is
  an **acceptor enable**, asserted for the session only (the board does not read it).
- **insert_coin_led**: HIGH at the end of `FUN_40206240`, LOW in `FUN_40206aac`. It tracks "session active".
- **sys_ready_led**: set LOW at boot, set HIGH only when a `vendo_handler.php` request returns HTTP 200
  (`FUN_40208708`). It is never cleared afterwards (a later outage just reboots the board).

### 3.2 The ISR [bytes]  (runtime `0x4010011c`, file offset `0x4f906`)

Hand-decoded Xtensa (call0 ABI). Pool at `0x40100104`: `{0x3ffef3f1, 0x3ffef41c, 0x3ffef404, 0x3ffef410, 0x3ffef400, 0x3ffef3e8}`.

```c
// ICACHE_RAM_ATTR
void coinISR() {
  if (sessionActive /*u8 0x3ffef3f1*/) {
    pulseTotal   += 1;            // 0x3ffef41c  (feeds coin_handler)
    pulseSeen     = 1;            // 0x3ffef404
    burstTotal   += 1;            // 0x3ffef410  (feeds pulse_handler)
    burstSeen     = 1;            // 0x3ffef400
    lastPulseDeadline = millis() + 130;   // 0x3ffef3e8  (call0 -> 0x40100314 = millis)
  }
}
```

**No software debounce; every RISING edge is one count.** The `+130 ms` timestamp is the only timing the ISR
records. Pulses arriving while `sessionActive == 0` are discarded.

**1 pulse = 1 coin unit.** The board has no denomination table: a PHP 5 coin that emits 5 pulses is simply
`+5`. Peso-to-time conversion is entirely server-side (`calculateAddTime`).

### 3.3 Turning pulses into reports (`FUN_402090a8`, session branch) [decomp]

State vars (runtime): `A=0x3ffef3f1` session active, `B=0x3ffef3f0` armed, `C=0x3ffef3e4` cancel-request,
`D=0x3ffef3e5` stay-active-sent, `deadline=0x3ffef3ec`, `snap=0x3ffef418`, `coins=0x3ffef414` (running total),
`userId=0x3ffef3f4` (String), `timerMs=0x3ffe85c8` (.data default **45000**).

Each `loop()` while `A==1`:
1. if `millis() > deadline` -> set `C=1`; if `C` -> `FUN_40206aac()` (end session), `B=0`, `C=0`.
2. else if `B==1`:
   - **if `pulseSeen>=1` (coin path)**: first time per session (`D==0`) call `stay_active.php`; then
     `snap = pulseTotal; pulseTotal -= snap; pulseSeen = 0; B = 0; FUN_402084c4()`.
     `FUN_402084c4` sees `B==0`, so it does `coins += snap` then `FUN_40206240()` (**re-arm**: SET HIGH,
     `delay(100)`, `snap=0`, `B=1`, `A=1`, `deadline = millis()+timerMs`, insert LED HIGH), and finally sends the
     report with `coins` (the **running total**).
   - **else (pulse path)**: if `burstSeen>0 && lastPulseDeadline < millis()` (130 ms of silence):
     `snapB(0x3ffef40c) = burstTotal; burstTotal -= snapB; burstSeen = 0; FUN_402085e8()` (pulse report).

Notable behaviours:
- There is **no "wait for end of pulse train" before `coin_handler`**. The first loop pass after the first pulse
  reports whatever has been counted (often 1 of N). The re-arm `delay(100)` plus the HTTP round trip let the
  remaining pulses accumulate (ISR keeps running), and the next pass reports the larger cumulative total. So a
  single coin typically produces **several `coin_handler` calls with increasing absolute totals**
  (e.g. `coins=1`, then `coins=5`). The server's absolute-set semantics make this converge.
- Every accepted batch also **re-arms the inactivity timer** (sliding window of `timer` seconds).
- Because `coins` is a running total kept on the board and re-sent in full, a failed report (the board ignores
  the HTTP status) only heals when a later pulse triggers another cumulative report.
- `pulseTotal/pulseSeen/burstTotal/burstSeen` are **not cleared at session start or end** (`FUN_40206240`
  only zeroes `snap`; `FUN_40206aac` zeroes `coins`, `A`, `D`). Pulses counted just before a session ends can
  therefore be reported to the next session's user id [inferred; small window].

---

## 4. Insert-coin session

### Start: `GET /insertCoin?coins=<int>&id=<str>&timer=<sec>` (`FUN_40206368`, G `0x4025738c`)
[decomp]
1. `coins` -> `0x3ffef414` (seed of the running total; the controller passes the user's current coin balance).
2. `id` -> `0x3ffef3f4` (kept as a String, later pasted raw into the `coin_handler` URL).
3. `timer * 1000` -> `0x3ffe85c8` (`timerMs`; default 45000 if never called).
4. `FUN_40206240()` starts the session (SET HIGH, `delay(100)`, flags `A=B=1`, deadline = now + timerMs,
   insert LED HIGH).
5. `FUN_4020614c()` adds CORS headers; replies `200 application/json {"status":"true"}`.

Registered **only when bound and network-ready**. **No authentication and no check that a session is already
running** (calling it again just reseeds the counters).

### Cancel: `GET /cancelInsertCoin` (`FUN_4020642c`)
Sets `C=1` and `coins=0`, replies `{"status":"true"}`. The next `loop()` runs `FUN_40206aac`.

### End (`FUN_40206aac`) [decomp]
`D=0`, `coins=0`, `A=0`, `SET=LOW`, insert LED LOW. Triggered by (a) inactivity timeout (`millis() > deadline`,
deadline is renewed on every report), or (b) `/cancelInsertCoin`.

**The board never reports "done".** There is no call to `done_insert_coin_handler.php` and no final-total
message; the only strings for outbound URLs are the four in section 5. The "board signals done" step in
`server-contract.md` must be driven by the portal/controller side (reading `tbl_user.coins` that the board
kept updated), not by the firmware. The board also has no concept of "time granted"; it only counts pulses.

---

## 5. Reporting to the controller

Transport: **ArduinoHttpClient over `EthernetClient`, plain HTTP, port 80, HTTP/1.1 `GET`**, headers
`Host`, `User-Agent: Arduino/2.2.0` (`0x40257925`), `Connection: close`. Host = `server_address` String
(`FUN_4020a274(http, client 0x3ffef36c, host, 0x50)`) or the literal `kitifi.wifi` (`FUN_4020a220`, G
`0x4025736d`). Status read by `FUN_4020a56c` (default response timeout 30 s per ArduinoHttpClient). Response
bodies are ignored except in the registration flow. **No retries anywhere.**

| Call | Function | Query (exact) |
|---|---|---|
| register/heartbeat | `FUN_40208708` | `/admin/api/portal/vendo_handler.php?host=KiTifi-ESP8266-LAN&mac=<STA MAC>&ip=<local IP>&token=<redacted>&version=v2.0&board=<board str, "esp8266" [inferred]>&type=lan` |
| coin total | `FUN_402084c4` | `/admin/api/portal/coin_handler.php?id=<userId>&mac=<MAC>&coins=<running total>&token=<redacted>` |
| pulse burst | `FUN_402085e8` | `/admin/api/portal/pulse_handler.php?mac=<MAC>&coins=<pulses in burst>&token=<redacted>` |
| keepalive | `FUN_402086a8` | `/admin/api/portal/stay_active.php` (**no query string at all**) |

Strings: `0x4025726e`/`0x40257295`/`0x4025729b`/`0x402572a3` (coin), `0x402572bb` (pulse), `0x402572e4` (stay),
`0x40257306`/`0x40257330`/`0x40257335`/`0x40257356`/`0x4025735b`/`0x40257363` (vendo), runtime literal pool
entries `DAT_402084ac..402084bc`, `DAT_402085e4`, `DAT_402086a4`, `DAT_402086e8..40208700`.

Both coin paths fire for every pulse burst: the **same firmware sends `coin_handler` (absolute user total) and
`pulse_handler` (burst delta)**; the controller's `tbl_vendo.type` decides which one it honors. Deltas vs the
existing contract doc: `host` is the constant board name, the MAC is in `mac`; `coin_handler` additionally
carries `mac` and `coins`; `pulse_handler` carries `coins` and **no `version`**; `vendo_handler` additionally
carries `ip`, `board`, `type=lan`.

### Cadence (`FUN_402090a8`, idle branch; only when bound, network-ready, and `A==0`) [decomp]
- Once at end of `setup()` (`FUN_40208708`).
- Then `vendo_handler` when `millis() > lastVendo(0x3ffef28c) + 0x2bf20` = **180 000 ms (3 min)**. (`lastVendo`
  starts at 0 and the boot call does not update it, so the first loop heartbeat comes about 3 min after boot.)
- Otherwise `stay_active` when `millis() > lastStay(0x3ffef288) + 10000` = **every 10 s** while idle.
- Neither runs during a coin session. `stay_active` is called once more at the first coin of a session.

### Controller base URL resolution
1. **Stored `server_address`** (config.json). After a successful bind this is the controller IP returned by the
   controller.
2. **Fallback: DNS name `kitifi.wifi`** (resolved via the DHCP-provided DNS; the controller is expected to
   answer it). Not mDNS; ArduinoHttpClient just calls `client.connect("kitifi.wifi", 80)`.

### Registration / auto-bind logic (`FUN_40208708`) [decomp]
- **Bound (`setup==1`)**: request to `server_address`. HTTP 200 -> `digitalWrite(sys_ready_led, HIGH)`, done.
  Anything else -> retry the same URL against `kitifi.wifi`. If that returns 200, parse the JSON body, take its
  `"ip"` field, rewrite `config.json` as `{"server_address":"<ip>","coinslot_pin":"<current>",...,"setup":"1"}`,
  `delay(5000)`, **reboot**. If it fails, `delay(1000)`, **reboot**. So any missed heartbeat (including a 30 s
  timeout) reboots the board; a changed controller IP is self-healed through `kitifi.wifi`.
- **Unbound (`setup!=1`)**: request to `kitifi.wifi` once (at the end of `setup()`). On 200, read `"ip"` from
  the JSON body and write `config.json` with `server_address=<ip>` and the **default pins `coinslot_pin="2"`,
  `coinslot_set_pin="15"`, `sys_ready_led_pin="-1"`, `insert_coin_led_pin="-1"`, `setup="1"`**
  (strings `0x40257257` and `DAT_40207fe0/e4/e8`), then reboot. On failure the board stays unbound serving only
  the config API (the unbound loop does nothing but `handleClient`).
- So the controller's `vendo_handler.php` reply must be JSON containing `"ip"` for zero-touch binding.

---

## 6. Connectivity (Ethernet)

- Chip: Stoffregen `Ethernet` library `W5100::init` (`FUN_402020a4`) autodetects, in order, chip codes
  `0x34` (W5200, `FUN_40201edc`), `0x37` (W5500, `FUN_40201e74`), `FUN_40201fbc`/`FUN_40201f48`/`FUN_40202008`
  (W5100-family variants; code `0x3d`=61 is the W5100S-style path [inferred]). Failure prints `[ETG] ` + `W5100::init: no chip :-(` on
  UART (G `0x40256d94`). So **W5500/W5100/W5100S/W5200 class**; the exact chip is a runtime detection.
- Bus: hardware **HSPI** via `FUN_40209da4` = `SPI.begin()` (muxes GPIO14 SCK, GPIO12 MISO, GPIO13 MOSI). **CS =
  GPIO0** (set in `setup()`, section 2.5).
- Addressing: **DHCP only** (60 s timeout, 4 s response timeout). No static fallback. If DHCP fails on a bound
  board, `0x3ffef291` stays 0 and `loop()` reboots after `0x2bf1f` (~180 s) of uptime.
- IP registration: the board pushes its IP in `vendo_handler.php?...&ip=`; the controller stores it and uses it
  for the `/insertCoin` callback.
- WiFi radio is switched off (`WiFi.mode(0)`), no STA/AP behavior in this build (the "AP config" endpoint name is
  legacy from the Wireless build).

---

## 7. Local HTTP server + config API (port 80)

All handlers add `Access-Control-Allow-Origin: *`, `-Max-Age: 10000`, `-Allow-Methods: PUT,POST,GET,OPTIONS`,
`-Allow-Headers: *`, `-Allow-Credentials: false` (`FUN_4020614c`). `onNotFound` (`FUN_40204e0c`) answers
`OPTIONS` preflight with 204 and everything else with 404. Auth model: a numeric `token` request parameter
compared to `String(0x3ffef3a8)` (`FUN_4020117c` = `!String::equals`), or no auth at all (marked below).

| Route (G addr) | Handler | Auth | Behavior |
|---|---|---|---|
| `/` (`0x4025736d`-adj) | `LAB_40204d88` | none | bound: `text/html` `<script>window.location.replace('http://<server_address>/')</script>` (`0x40256f19`/`0x40256f42`); unbound: `FUN_40204d2c` -> `Go to KiTifi Admin to bind this node.` (`0x40256ee9`) |
| `/insertCoin` (`0x4025738c`) | `FUN_40206368` | **none** | see section 4 (bound only) |
| `/cancelInsertCoin` (`0x40257398`) | `FUN_4020642c` | **none** | see section 4 (bound only) |
| `/admin/api/saveApConfig` (`0x402573aa`) | `FUN_40207ff0` | **none** | arg `ip` -> writes `config.json` `{"server_address":"<ip>","coinslot_pin":"2","coinslot_set_pin":"15","sys_ready_led_pin":"-1","insert_coin_led_pin":"-1","setup":"1"}`, replies `{"status":"true"}`, `delay(1000)`, reboot. **Resets pins to defaults.** No session check |
| `/admin/api/saveSystemConfig` (`0x402573c2`) | `FUN_402081c4` | token; also refused while `A==1` | args `server_address`, `coinslot_pin`, `coinslot_set_pin`, `sys_ready_led_pin`, `insert_coin_led_pin` spliced **unescaped** into `config.json` (keeps current `setup`), reply true, `delay(1000)`, reboot |
| `/admin/api/getSystemConfig` (`0x402573de`) | `FUN_402064bc` | token | `{ "status":"true","uptime":"<s>","server_address":...,"coinslot_pin":...,"coinslot_set_pin":...,"sys_ready_led_pin":...,"insert_coin_led_pin":...,"board":"<b>","type":"lan"}`; bad token -> `{"status":"false","message":"Not Authorized!"}` (`0x40256d6e`) |
| `/admin/api/restart` (`0x402573f9`) | `FUN_402067d0` | token; refused while `A==1` (`"busy"`, `0x40256d7e`) | reply true, `delay(1000)`, reboot |
| `/admin/api/forcerestart` (`0x4025740c`) | `FUN_40206720` | token | same, ignores session |
| `POST /admin/api/update` (`0x40257424`) | `FUN_402068a4` (upload cb), `FUN_402069ac` (final) | token in upload cb only | **OTA, see below** |

Config fields (config.json): `server_address`, `coinslot_pin`, `coinslot_set_pin`, `sys_ready_led_pin`,
`insert_coin_led_pin`, `setup`. No WiFi credentials, no `ssid`/`password` keys in this LAN build (the inventory's
`ssid/password/rssi/open` keys belong to the Wireless build).

### OTA (`/admin/api/update`) [decomp]
- It is a real HTTP multipart OTA. `FUN_402068a4` is the `HTTPUpload` callback:
  - `UPLOAD_FILE_START`: compares **`upload.name` (the multipart form-field name) to `String(token)`**. Mismatch ->
    JSON `Not Authorized!` and no `Update.begin`. Match -> `Update.begin(freeSketchSpace-0x1000 aligned, U_FLASH)`
    (`FUN_4020aa84`, `FUN_4020be60`).
  - `WRITE`: `Update.write(buf, currentSize)`; `END`: `Update.end(true)`; `ABORTED`: `Update.end(false)`.
- `FUN_402069ac` (final handler) tests Update's error byte: no error -> `{"status":"true","message":"Firmware Updated
  Successfully!"}` (`0x40256d49`), `delay(100)`, `FUN_4020269c`, **reboot**; error -> `Upload has error` /
  `Error uploading firmware!` (`0x40256d2f`).
- No signature, hash, version or rollback check. Plain HTTP.

---

## 8. Security notes

| # | Finding | Evidence |
|---|---|---|
| S1 | **Hardcoded shared controller token** `<redacted>` baked into all four device URLs and sent as a **GET** param over plain HTTP. Anyone who sniffs one request (or reads firmware) can call `coin_handler.php` (absolute set of any user's coins) and `pulse_handler.php`. | strings `0x402572a3`, `0x40257335`; used in `FUN_402084c4`, `FUN_402085e8`, `FUN_40208708` |
| S2 | **Local admin token is very likely a compile-time constant (`307163591` = `0x124ef1c7`), same on every board.** Setup computes `random(min=0x124ef1c7, max=0xa4c67fff)` (`FUN_4020c22c`, Arduino `random(min,max)`); the max is negative as a signed int, and the function does `if (min >= max) return min` (bytes `37 a2 07` = `bge a2,a3`), so the result is deterministic. Pool words verified at `0x40209288`/`0x4020928c`, and the `l32r` loads at `0x40209391`/`0x40209394` feed a3/a2. Token is compared as decimal text. **Confirm at runtime with `getSystemConfig?token=307163591`.** | `FUN_40209290`, `FUN_4020c22c`, `DAT_4020647c` |
| S3 | `saveApConfig` is **unauthenticated**, CORS `*`, GET-able, and works even mid-session: any LAN host, or any web page the operator visits (drive-by `<img src>`), can repoint `server_address` to a rogue controller, reset pins to defaults and reboot the board. | `FUN_40207ff0` (no token read) |
| S4 | `/insertCoin` and `/cancelInsertCoin` are **unauthenticated**: any LAN host can enable the acceptor, choose the `id` the coins are credited to, seed `coins=<big>` (reported as the absolute total on the next pulse) and choose `timer`. `id` is pasted into the controller URL **without URL-encoding** (parameter injection). | `FUN_40206368`, `FUN_402084c4` |
| S5 | OTA has **no authenticity check** beyond the constant token (S2) carried as a multipart field name; any LAN host with the token can flash arbitrary firmware. The completion handler does not re-check the token: a POST that never started `Update` falls to the "success" branch and **reboots the board** (likely unauthenticated reboot/DoS) [inferred; confirm at runtime]. | `FUN_402068a4`, `FUN_402069ac` |
| S6 | `saveSystemConfig` splices request args **unescaped** into JSON (config corruption / key injection such as forging `setup`), though token-gated. | `FUN_402081c4` |
| S7 | Everything is **cleartext HTTP**; admin tokens travel in query strings. | all handlers |
| S8 | Reliability/abuse: no retries, report responses ignored, **any heartbeat failure reboots the board**, a 30 s controller stall during a session blocks the loop (and the coin ISR still counts, but nothing is reported until the call returns). Final totals are not persisted; a failed last report is never resent. | `FUN_40208708`, `FUN_402084c4`, `FUN_402090a8` |
| S9 | Stale counters across sessions (section 3.3) can credit pulses to the next session's `id`. | `FUN_40206240`, `FUN_40206aac` |

---

## 9. Pin map

| Function | Config key | Default (auto-bind / saveApConfig) | Arduino GPIO / label | Mode | Where used |
|---|---|---|---|---|---|
| Coin pulse in | `coinslot_pin` | `"2"` | GPIO2 / D4 | INPUT_PULLUP, `attachInterrupt(RISING)` | `FUN_40209290`, ISR `0x4010011c` |
| Acceptor enable (SET) | `coinslot_set_pin` | `"15"` | GPIO15 / D8 | OUTPUT, HIGH during session | `FUN_40206240` / `FUN_40206aac` |
| System-ready LED | `sys_ready_led_pin` | `"-1"` (disabled) | n/a | OUTPUT, HIGH after first 200 from `vendo_handler` | `FUN_40208708` |
| Insert-coin LED | `insert_coin_led_pin` | `"-1"` (disabled) | n/a | OUTPUT, HIGH = session active | `FUN_40206240` / `FUN_40206aac` |
| Ethernet CS | hard-coded | n/a | **GPIO0 / D3** | OUTPUT | `FUN_40209290`, `FUN_402020a4` |
| Ethernet SCK | n/a | n/a | GPIO14 / D5 | HSPI | `FUN_40209da4` |
| Ethernet MISO | n/a | n/a | GPIO12 / D6 | HSPI | `FUN_40209da4` |
| Ethernet MOSI | n/a | n/a | GPIO13 / D7 | HSPI | `FUN_40209da4` |
| UART0 debug | n/a | n/a | TX GPIO1 | 115200 8N1 | `FUN_4020b200` |

**Wiring conflict to note:** the wiring diagram's COIN -> D6 and SET -> D7 cannot apply to this LAN build as is:
D5/D6/D7 are the hardware SPI bus to the W5x00. The LAN firmware's own defaults are **COIN = D4 (GPIO2),
SET = D8 (GPIO15)**; the pins are configurable via `config.json`, but D6/D7 would collide with MISO/MOSI. The
D6/D7 diagram is presumably the Wireless build.

---

## 10. State machine

```
              +--------------------------------------------------------------+
 power-on --> | BOOT: Serial, SPIFFS.begin, load config.json, pinMode,       |
              | WiFi off, CS=GPIO0, Ethernet.begin(DHCP, 60 s)               |
              +--------------------------------------------------------------+
                  | SPIFFS fail          | DHCP fail                | DHCP ok
                  v                      v                          v
               DEAD (no server)    NO-NET: loop only        setup==0 ?   setup==1 ?
                                   handleClient; if bound              |            |
                                   reboot at ~180 s            UNBOUND  |            | attachInterrupt(RISING),
                                                                 |      |            | register /insertCoin etc.
              UNBOUND: serve config API only; one auto-bind       |      |            v
              try to kitifi.wifi at boot; 200 -> write config  <--+      |      REGISTER: vendo_handler (stored
              (ip + default pins, setup=1) -> reboot                     |      server_address, 200 -> sys LED on)
                                                                         |        non-200 -> try kitifi.wifi
                                                                         |        200 -> rewrite cfg+reboot; else reboot
                                                                         v
                                   IDLE (A=0): vendo_handler every 180 s, stay_active every 10 s
                                        | GET /insertCoin?coins&id&timer   ^
                                        v                                  | timeout (no coin for timer s)
                                   SESSION ARMED (A=1,B=1): SET HIGH,      | or /cancelInsertCoin
                                   insert LED, deadline=now+timer          |
                                        | ISR: pulses counted (RISING)     |
                                        v                                  |
                                   COIN PATH: first pulse -> stay_active (once), coin_handler(id,mac,total) ;
                                   re-arm (delay 100, SET HIGH, deadline renewed) ; burst of 130 ms silence ->
                                   pulse_handler(mac, burst) ---------------------------------------------+
```

End-to-end timeline: boot -> DHCP -> `vendo_handler` registration (IP, MAC, board) -> idle heartbeat
(180 s / 10 s) -> controller calls `/insertCoin` -> SET HIGH + LED -> coin pulses counted in ISR ->
`coin_handler` (cumulative total) and `pulse_handler` (burst) -> session ends on inactivity/cancel (no done
message) -> server converts the stored `tbl_user.coins` to time in its own phase 2.

---

## 11. Open items (not proven statically)

- Runtime confirmation of S2 (constant token) and S5 (unauthenticated reboot via `/update`).
- Exact W5x00 variant; code `0x3d` (61) mapping is by library convention.
- What the unbound-mode catch-all handler `PTR_PTR_40209230` (vtable `0x40217e34`) serves.
- `board=` literal: pool `0x4020962c` points at the `esp8266` string (`0x40257436`) but the static constructor
  that fills `0x3ffef384` is not in the decoded set.
- The ISR and pool decode in section 3.2 were done by hand from bytes; re-decode in a correct IRAM-based Ghidra
  import (segments from section 0) if exact instruction-level proof is needed.
