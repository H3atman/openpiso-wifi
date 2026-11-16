# ESP8266 subvendo: KiTifi LAN board + JuanFi parity, improvements, and firmware design

Status: research draft. Scope is the ESP8266 subvendo (Vendo board) firmware. The Controller (Go + SQLite, agreed by the user) is context only and does not run on the board. Items marked `[Open]` or `[Inference]` are unverified; the `[Open]` ones are listed under Open questions.

## TL;DR

- **Scope:** this doc plans the Vendo board firmware. Controller material is kept short and marked as context.
- **Chip status:** the ESP8266EX datasheet v7.1 (2025.11) is marked "NOT RECOMMENDED FOR NEW DESIGNS" and points to ESP8684 (`https://documentation.espressif.com/0a-esp8266ex_datasheet_en.pdf`). Pick a successor (ESP8684/ESP32-C2 or ESP32-C3) or accept ESP8266 for v1 (candidate ticket 10).
- **Firmware platform:** C++ on the ESP8266 Arduino core (3.1.2) with PlatformIO. Budget about 50 KB of heap + data once WiFi station mode is up (datasheet §3.1.3). No TLS on the board: use HMAC-signed messages on the LAN (Q3).
- **Keep from KiTifi:** the Vendo is a dumb pulse counter that holds no prices. The Controller prices coins, and the acceptor is inhibited unless a Coin Window is open.
- **Keep from JuanFi (Controller/portal context):** a QR purchase tied to the client MAC and approved by the owner. The shipped JuanFi config has this flow off (`qrCodeVoucherPurchase = false`). QR expiry and one-time use are OUR proposal, not JuanFi behavior: `juanfi-architecture.md:142` says the QR payload is unsigned with no expiry, and `:151` calls expiry a proposal.
- **Replace in both:** the shared static token, unauthenticated control endpoints, unsigned OTA, read-modify-write counters, and the third-party QR call that leaks voucher codes. JuanFi's admin API also has plaintext secrets in config readback and backups, and state-changing GETs (Q2).
- **Coin acceptor:** the CH-926 manual gives a programmable pulse count per coin (up to 50), pulse widths of 20/50/100 ms, and a warning against a pull-up on the signal line. The gap between pulses and the output type are not in the manual, so they must be measured on the bench. JuanFi's default LAN pins (GPIO2, GPIO15, GPIO0) are all strapping pins, so avoid them.
- **Lost coin (revised design):** the Controller opens a window `open{window_id, expires, hmac}`. The board unblocks the SET pin only while a window is open AND the Controller is reachable. If the Controller is unreachable, the slot is blocked, so the acceptor rejects and returns the coin instead of losing it. This is the biggest improvement over both systems. The board reports a cumulative `total` with `epoch` and `window_id`, and the Controller credits the delta. The cumulative total is the idempotency key, so there is no per-message seq and no queue.
- **Residual risk:** power loss mid-burst before the flash save. The ESP8266 has no documented brownout interrupt, so an external supervisor or bulk capacitor is needed `[Inference]`; bench test.
- **Gaps in our current plan:** firmware, acceptor characterization, pin decision, chip choice, secrets handling, and special-character round trips are not yet tracked. Candidate tickets are in Q6.

## Scope and evidence rules

- Read first: `docs/research/kitifi-firmware/board-behavior-esp8266-lan.md` (the summary used here is from the earlier session), `docs/research/juanfi-architecture.md`, `docs/research/kitifi-firmware/SUMMARY.md`, `docs/research/kitifi-firmware/server-contract.md`, `docs/research/kitifi-firmware/board-behavior-esp32-lan.md`, `GLOSSARY.md` and `docs/adr/0001` to `0003`, which exist only on PR #4 (branch `docs/device-controller-planning`, head `65e5e61`) and not on this branch, and GitHub issues #5 to #16.
- Terms follow `GLOSSARY.md`: Controller, Router, Vendo, Coin Window, Device, Entitlement, Voucher, Top-up, Pause, Idle Pause, Pause Limit, Validity, Resume Code, Rebind.
- `[Inference]` marks a conclusion that the cited sources do not state directly.
- `[Open]` marks an item that remains unverified. These are also listed at the end.
- JuanFi board citation bases (used in 1d and Q2):
  - `analysis/` = `/mnt/local-disk-d/Projects/random/juanfi-bin/release-4.4/release/LanBased/ESP8622/analysis/`
  - `strings:N` = `analysis/firmware/strings_ascii.txt:N`
  - `sc:N` = `analysis/webroot/admin/system-config.html` byte N
  - `admin.js` = `analysis/webroot/admin/js/admin.js`

## Q1. Parity: KiTifi ESP8266 LAN board and JuanFi, mapped to our design

### 1a. KiTifi ESP8266 LAN board, feature by feature

| KiTifi behavior (evidence) | Our design (where decided) | Verdict |
|---|---|---|
| Coin counting is pulse counting, 1 pulse = 1 unit. The board has no denomination table. Peso conversion is server-side with greedy `calculateAddTime` tiers (`server-contract.md:47-51`; `SUMMARY.md:64-71`). | Same split. The Vendo reports counts only. The Controller maps counts to coin types and applies Coin Window pricing (#16, #5). | Keep |
| ISR counts every RISING edge while a session is active. There is no software debounce. A 130 ms quiet timer groups the burst (`board-behavior-esp32-lan.md:59-66`; `board-behavior-esp8266-lan.md` §3.2). | The ISR records edges, and grouping happens outside the ISR. The quiet window is measured, not copied from KiTifi (Q4, candidate ticket 3). | Change |
| Two separate counters: an absolute coin total (`coin_handler`) and a raw pulse count (`pulse_handler`). Both are sent, and the server picks one by `tbl_vendo.type` (`SUMMARY.md:57-59`; `server-contract.md:53-58`). | One cumulative `total` per Vendo with an `epoch`, sent as an HMAC-signed report. The Controller decides what it means (#5 Vendo protocol; candidate ticket 1). | Change |
| Coin total is an absolute set (`coin_handler.php` sets `tbl_user.coins`) (`server-contract.md:14`), and the board subtracts what it reported (`board-behavior-esp32-lan.md:83-86`, §3 step 2). | The total is cumulative and monotonic per epoch. The Controller credits `total - last_seen(vendo, epoch)`. No "set" operation changes a balance (candidate ticket 1). | Change |
| Session begins with `/insertCoin` and ends with `/cancelInsertCoin` or a timer. The default timer is 45 s (`board-behavior-esp8266-lan.md` §5; `board-behavior-esp32-lan.md:129-136`). | Coin Window opened by the Controller (`open{window_id, expires, hmac}`). The SET pin is enabled only while a window is open AND the Controller is reachable. Window length: 60 s in #5, 45 s in KiTifi, 30 s in JuanFi (1d). | Keep (duration: `[Open]`) |
| The acceptor is enabled through the SET pin. HIGH means enabled on the LAN build, GPIO15/D8 (`board-behavior-esp8266-lan.md` §3.1, §9). | Same control role. The polarity must be set per acceptor. GPIO15 is a strapping pin (Q4), and JuanFi's LAN defaults also use strapping pins (1d). | Keep, with a pin change |
| Heartbeat `vendo_handler.php` about every 180 s. `stay_active.php` about every 10 s (`board-behavior-esp8266-lan.md` §5). The board reboots on any heartbeat failure (§8 S5). | Pairing and signed heartbeat (#15). A failed heartbeat must not reboot the board. It blocks the slot (Controller unreachable) and retries with backoff. | Change |
| `stay_active.php` keeps a hotspot session alive. | Not needed. The Router enforces time through `limit-uptime`, and the Controller is off the traffic path (ADR 0001; ADR 0003). | Drop |
| `/insertCoin` has no authentication (`board-behavior-esp8266-lan.md` §4). | Window commands are HMAC-signed with a per-Vendo secret (#5 Vendo protocol). | Change |
| Fleet token `<redacted>` in a GET query string (`SUMMARY.md:88-93`; `board-behavior-esp8266-lan.md` §8). | Per-Vendo secret. Nothing secret goes in a URL. | Drop |
| OTA over plain HTTP with no signature (`board-behavior-esp8266-lan.md` §7; `SUMMARY.md:99`). | Signed firmware updates (candidate ticket 7). | Change |
| `/config.json` provisioning with a local admin API (`board-behavior-esp32-lan.md` §1). | Pairing (#15). Provisioning must not expose a local admin API without auth. | Change |

### 1b. JuanFi QR purchase (Controller/portal context)

This is portal and Controller behavior, not board firmware. It is kept as context for the Controller side.

| JuanFi behavior (evidence) | Our design | Verdict |
|---|---|---|
| The login page shows a "Purchase Via QR" button (`mikrotik-template/login.html:228`). The button is driven by `qrCodeVoucherPurchase`, which is `false` in the shipped config (`mikrotik-template/assets/js/config.js:63`; `core.js:145-147`). So this flow is off in the release. | Keep the purchase concept. Ship it behind a setting, and do not ship it on by default without a test. | Keep (off by default) |
| The payload is `juanfi://purchasevoucher?mac=<mac>&ip=<ip>`, built in `login.html:490`, then `new QRCode(..., JSON.stringify(purchaseInfo))` at `login.html:492`. `JSON.stringify` on a string adds literal double quotes to the QR contents. | Payload is a plain URI or a token. No JSON wrapping. A purchase request is identified by a server-issued ID, not by MAC and IP. | Change |
| The client polls `/data/<mac>.txt` every 1 s (`core.js:306-320`). On success it runs `newLogin()` after 3 s. The board has no purchase or approval route and no `/data/<mac>.txt` writer in its strings (`strings:3109-3138` grep). Approval most likely happens off-board, in JuanFi Manager, which is closed source (`juanfi-architecture.md` §10, §11). Unproven. | Purchase request row in the Controller ledger with states (pending, approved, expired, used). The portal polls our API for the state. Approval is an admin action in the Controller (#13). | Change |
| The QR payload is unsigned and has no expiry (`juanfi-architecture.md:142`). Expiry is only a proposal (`:151`). | Our proposal: about 5 min expiry, one-time use, and owner approval. These are NOT JuanFi behavior. Voucher redemption is #7. | Change (our proposal) |
| The default voucher length is 4 (`sc:15649`, `sc:15681`). The character set was not recovered. | Voucher generation uses a CSPRNG with enough entropy, and redemption is rate limited (#7). A 4-character default is short, whatever the charset `[Inference]`. | Change |
| The admin page `webroot/admin/voucher-generate.html:227` sends the voucher code to `api.qrserver.com`. | Render QR codes locally (candidate ticket 6). | Drop |
| `macAsVoucherCode = false` (`config.js:61`). The MAC is not used as a code. | Not planned. | Keep (no MAC codes) |
| The portal's MAC and IP come from the client, and the QR binds to them. | A QR is a bearer for the purchase. Bind it to the Device and expire it. Anyone who gets the text could try to use it. Mitigate with owner approval and a short expiry `[Inference]`. | Change |

### 1c. Gaps where neither design covers the need (Controller/Router context)

- Pause and Idle Pause. The WiFi5-Soft design uses a 120 s traffic check plus an `arping` probe, and an autopause sweep every 60 s (`wifi5soft-architecture-client-time.md` §3.3, §3.4). The CHR lab says `idle-timeout` does not charge the idle tail (E8/L6), and `keepalive-timeout` charges the departure tail (E11/L8). So our Idle Pause profile needs `idle-timeout` (#5 "two profiles", #8).
- Top-up on an active session. A live `limit-uptime` change does not affect the active session (L7/C1). Top-up has to be applied at the next login, or with a re-login, which is #9's job.
- Rebind. `remove-then-login` works in the CHR lab. The RouterOS cookie login is bound to the MAC (E1/C1). This is #10.

### 1d. JuanFi board findings (from webroot + REPORT.md)

Board facts, with citations (bases in "Scope and evidence rules"):

| Area | Finding |
|---|---|
| Default pins | LAN UI defaults: coin GPIO2, set GPIO15, insert GPIO3, LAN CS GPIO0 (`admin.js:531-539, 588, 432-442`). GPIO0, 2, and 15 are all strapping pins. The sample `system.data` stores pins 12/13/0 (not 12/13/14). **Pin precedence (static analysis, verified):** the loader `FUN_4020b140` reads `/admin/config/system.data`. File pins are used only when field 40 (`lanModeOverride`, global `0x3fff07dc`) is non-zero, and every admin UI save sets it to 1 (`admin.js:114`). Otherwise the hardcoded pins apply: coin 2, set 15, button 3, LEDs 255 (`0x4020b7b2..0x4020b7da`, log string "ESP8622 pins loaded"). The sample file has no field 40, so its pins are ignored. |
| Coin wait | Defaults to 30 s, with auto-use of the voucher on expiry (`analysis/webroot/admin/config/system.data` field 7; `sc:9625`; `strings:3059`). No quiet window was found. |
| Pulse limits | `pulseToBlock` defaults to 20 and logs "Unusual coinslot pulse detected" (`sc:10771`; `admin.js:640-644, 1336`). There is no audit queue. |
| Pulses per coin | Single-coin pulse count defaults to 1, with multicoin/single modes (`sc:9275, 9331, 9567`). There is no per-coin pulse table. |
| Abuse rule | 3 strikes / 30 min (`system.data` fields 10-11; `sc:9946`). |
| Restart | Deferred during activity. Auto-restart is off by default (`admin.js:1048-1054, 623`). |
| Logs | Kept in RAM and cleared on restart. A hidden `persistLogs` flag carries a flash-life warning (`sc:21770, 17230, 17378`; `strings:2994, 3012`). |
| Updates | An FS update resets promo rates (`sc:25341, 25452`). |
| ISR | Static analysis (Ghidra, Xtensa LE32; addresses are flash VAs), verified unless tagged. The ISR at `0x4010010c` is in IRAM, a leaf function, writes volatile state (`memw`), and is registered RISING with INPUT_PULLUP. The `loop()` consumer `FUN_4020e348` reads, re-reads, subtracts and stores the pulse count with no interrupt masking (`0x4020e709..0x4020e730`; no `rsil` anywhere in the binary), so a lost-update window exists. The pulse flag is cleared at `0x4020e792`, after Serial/String/log work, so a pulse in that span is stranded until the next pulse `[Inference from control flow]`. A fixed 700 ms settle starts at first sight and is not re-armed per pulse, so a long coin can split across batches. In single-coin mode the ISR assigns a constant, so a split coin could be credited twice `[Inference]`. `pulseToBlock` is 20. |
| `/testInsertCoin` | The handler `FUN_40203e4c` is behind the `X-TOKEN` check `FUN_40203cf8`. The token is compared to the admin/operator tokens set by `/validateLogin` from a client-chosen `randomToken`, or to config `apiKey`. A failure returns 401. It simulates pulses: `count += coin`, with no `coinMultiplier` and no range check, only while coin-accepting; the loop then consumes the count like real pulses. The handler sets no CORS headers; only the OPTIONS preflight does (`Allow-Origin: *`, `Allow-Credentials: false`). |
| Routes | No purchase or approval route on the board (route strings `0x3ffe9b85..0x3ffe9beb`). Outbound calls are only e-load (`api-eload.juansystems.com`, `loadcentral`) and the MikroTik API. "Approval happens off-board" stays unproven but is consistent with this. |
| Defaults | `admin/admin` and MikroTik `10.0.10.1` with `pisonet/abc123` (`system.data` fields 4-6, 8-9; `strings:3160-3161`). Confirmed. |

Corrections to earlier docs:
- `REPORT.md` says the login uses MD5. It does not: MD5 is only for the e-Load hash (`admin.js:1661`).
- `pisonet/abc123` are literals in the firmware (`strings:3160-3161`), which contradicts `REPORT.md:67`.
- The S3 host is navigation and download only (`admin.js:1860, 1864`).
- The jsDelivr `pako` script is loaded at runtime with no SRI (`admin.js:1618`).
- `REPORT.md:104` says the portal loads `api.qrserver.com`. The admin page does (`voucher-generate.html:227`).
- `juanfi-architecture.md:76` describes a "done" route that does not exist.
- `juanfi-architecture.md` §8 does not support a `random(1000,9999)` voucher range. That claim is removed here.

### 1e. KiTifi board, static analysis comparison

From `KiTifi-ESP8266-LAN.bin` (Ghidra, Xtensa LE32; flash VAs). Verified unless tagged.

- The ISR at `0x4010011c` is IRAM. It increments two volatile counters and re-arms a 130 ms quiet deadline on every pulse. That is the good pattern.
- The consumer `FUN_402090a8` has the same unmasked snapshot-then-subtract as JuanFi, but the window is smaller (3 instructions) and the flag is cleared right after.
- Session end: `millis() + 45000` is re-armed per coin. On timeout `FUN_40206aac` zeroes the total, with no "done" HTTP call. If the last `coin_handler.php` GET failed, those coins are lost permanently.
- `SUMMARY.md:74-86` matches the firmware. `server-contract.md:39-43` ("board signals done") does not; a correction note is added there.

## Q2. Improvements: weaknesses in both architectures and concrete fixes

### Board-side

| Area | Weakness (evidence) | Concrete fix |
|---|---|---|
| Security: board to controller | One fleet token in a GET query string, sent in cleartext (`SUMMARY.md:88-93`; `board-behavior-esp32-lan.md:146-155`). Anyone on the path can replay `coin_handler?...&coins=N&token=...` and credit coins. | Per-Vendo secret. Sign each report with HMAC over `vendo_id`, `epoch`, `total`, `window_id`, sent in a POST body. A replayed report credits 0 because `total` is cumulative. No TLS on the board (Q3: the default TLS buffers are heavy against a 50 KB budget). |
| Security: local control | `/insertCoin` unauthenticated. The local admin token is probably the constant `307163591` (`board-behavior-esp8266-lan.md` §8 S2). | The board accepts window commands only when signed by the Controller. No local admin token. Local admin is off unless a setup button is pressed. |
| Security: OTA | Unsigned OTA over HTTP, token carried as a multipart field name (`SUMMARY.md:99`). JuanFi OTA at `/admin/updateMainBin` is guarded only by `X-TOKEN`, with no signature (`admin.js:987-990`). | Signed firmware images. Verify before flashing. Version pinned by the Controller (candidate ticket 7). |
| Security: credentials | Wireless board: unauthenticated `saveApConfig` and `scanSSID`, and WiFi credentials in cleartext config (`board-behavior-esp8266-wireless.md:95-101`, "Security notes"). | Pairing creates a secret stored in flash, not in a readable config file. Admin endpoints need auth. |
| Security: JuanFi defaults | `admin/admin` and MikroTik `10.0.10.1` `pisonet/abc123` are shipped defaults (`system.data` fields 4-6, 8-9; `strings:3160-3161`). Confirmed. | Force a password change at first setup (#14 install script). |
| Security: JuanFi admin token | The admin API uses `X-TOKEN` (`admin.js:16-23`), and the login is plain HTTP (`analysis/webroot/admin/login.html` byte 2023). The token is generated with `Math.random()` (`login.html` byte 1831). | Server-generated random token from a CSPRNG. Do not log in over plain HTTP where a secret crosses the wire. |
| Security: JuanFi secret exposure | Config readback returns passwords and the API key (`admin.js:451, 459-514`). Backups hold plaintext secrets (`strings:3001, 3063-3064`). | Never return secrets on readback (write-only fields). Exclude secrets from backups or encrypt them (candidate ticket 8). |
| Correctness: JuanFi request encoding | `createParam` uses `encodeURI`, so `& = + #` corrupt request bodies (`admin.js:263-272`). | Use `encodeURIComponent` or a JSON body. Round-trip test for special characters (candidate ticket 9). |
| Security: JuanFi state-changing GET | `resetStatistic` is a GET (`admin.js:279`). | State changes use POST with auth (candidate ticket 8). |
| Security: JuanFi `/testInsertCoin` | `/testInsertCoin` is behind the `X-TOKEN` check (401 on failure), but the token can be minted from a client-chosen `randomToken` at `/validateLogin` (1d). The handler adds `count += coin` with no `coinMultiplier` and no range check. Only the OPTIONS preflight sets CORS headers (`Allow-Origin: *`, `Allow-Credentials: false`). | Remove it, or require a signed command with a bounded argument and the same multiplier and cap as real pulses. |
| Security: JuanFi pin import | A `.board` JSON file fills the pin fields without validation (`admin.js:1575-1607`). | Validate pin numbers against an allowlist that excludes strapping pins. |
| Platform: JuanFi | Arduino core 2.2.2-dev with a 2019 SDK (`REPORT.md:50, 103`). | Build on the current Arduino core (3.1.2) with a pinned toolchain (Q3). |
| Reliability: Vendo | Reboot on any heartbeat failure, no retries (`board-behavior-esp8266-lan.md` §8 S5, S8). JuanFi defers restarts during activity and has auto-restart off by default (`admin.js:1048-1054, 623`). | Retry with backoff. Reboot only on watchdog faults, and never while a Coin Window is open. |
| Reliability: race on coin counts | JuanFi: the ISR is a safe IRAM leaf, but the `loop()` consumer does an unmasked read, re-read, subtract and store, so a lost-update window exists (1d, static analysis). KiTifi's consumer `FUN_402090a8` has the same unmasked snapshot-then-subtract, with a smaller window (3 instructions) and the flag cleared right after (see 1e). KiTifi has the same shape on the server, `mtik_add_time` RMW race (handoff #12). | The ISR only increments a `volatile` cumulative counter. The Controller computes deltas. No RMW on a balance anywhere. |
| Reliability: lost coin | A coin counted while the Controller is unreachable is lost if the Vendo reboots (`board-behavior-esp8266-lan.md` §3.3, §8 S8). | Block the slot while the Controller is unreachable, and checkpoint `{epoch, total}` to RTC and flash per burst (Q5; candidate ticket 2). |
| Reliability: pulse anomalies | JuanFi logs "Unusual coinslot pulse detected" at `pulseToBlock` = 20 and has no audit queue (1d). | Tag pulses with `window_id`. Pulses on a closed window go to the audit queue (candidate ticket 4). |
| Reliability: contract drift | `server-contract.md:39-43` says the board signals "done" to `done_insert_coin_handler.php`. The firmware never does (`SUMMARY.md:74-86`). | Treat the contract as wrong on phase 2. The Controller closes the window, and the firmware is never assumed to send a "done". |
| Reliability: updates | A JuanFi FS update resets promo rates (`sc:25341, 25452`). | Updates preserve config and rates (candidate ticket 7). |
| Reliability: logs | JuanFi logs live in RAM and clear on restart. A hidden `persistLogs` flag has a flash-life warning (`sc:21770, 17230, 17378`; `strings:2994, 3012`). | Keep logs in RAM. Persist only the coin checkpoint, per burst, in a ring (Q5). |
| UX: abuse | A 3-strikes, 30-minute ban (`juanfi-architecture.md` §5; `system.data` fields 10-11; `sc:9946`). The rule is undocumented to the user. | Show the rule and the remaining time in the portal. Admin can lift it (#13). |

Lessons from the board analysis (1d, 1e), for the subvendo firmware:
- Keep the leaf IRAM ISR.
- Never read-and-reset the counter without masking interrupts. A cumulative total avoids reset entirely.
- Re-arm the quiet window on every pulse.
- Never zero the total until the Controller acknowledges it.
- Use one pin source, and fail loudly on bad config.
- If a test-coin route exists, it needs real auth, a bounded argument, and the same multiplier and cap as real pulses.

### Controller-side (context)

- Session races: insert-session check-then-act (handoff #13), on-logout race (#15), and no idempotency guard on phase 2 (`server-contract.md:70-71`). Fix: one open Coin Window per Vendo, enforced by a DB unique constraint.
- Router offline: Vouchers cannot be applied. Fix: intent journal and Reconciler replay (candidate ticket 5; #11).
- JuanFi ledger: flat files in SPIFFS, no transactions (`juanfi-architecture.md` §4-§5). Fix: SQLite WAL with `synchronous=FULL` for money tables.
- QR UX: QR contents are JSON-quoted (`login.html:492`) and the 1 s poll hits the Router. Fix: plain URI, and poll the Controller with backoff.
- Ops: no ledger trail that explains a balance change, and a single install script is needed on armv7 (#14). Fix: append-only audit table (Q5).

## Q3. Firmware language and platform

### Chip status

The ESP8266EX datasheet v7.1 (2025.11) is marked "NOT RECOMMENDED FOR NEW DESIGNS" and points to ESP8684 (`https://documentation.espressif.com/0a-esp8266ex_datasheet_en.pdf`). Both KiTifi and JuanFi boards are ESP8266, so v1 can accept it, but a new design should decide on a successor: ESP8684/ESP32-C2 or ESP32-C3. The successors were not evaluated here `[Open]` (candidate ticket 10).

### RAM budget

- The datasheet states: "when ... working under the Station mode and connects to the router, the maximum programmable space accessible in Heap + Data section is around 50 kB" (§3.1.3, PDF p.13).
- The ~160 KB figure is total SRAM (96 KB data + 64 KB instruction), from the memory map wiki (`https://github.com/esp8266/esp8266-wiki/wiki/Memory-Map`). It is not the application budget.
- Budget about 50 KB and confirm on hardware with `ESP.getFreeHeap()`.

### Toolchain options

| Option | Facts | Fit |
|---|---|---|
| C++ on the Arduino core | `https://github.com/esp8266/Arduino`, 3.1.2, active, bundles NONOS SDK. | Recommended |
| NONOS SDK directly | Frozen since Dec 2019, critical fixes only (`https://github.com/espressif/ESP8266_NONOS_SDK`). | No |
| RTOS SDK | Plans an esp-idf migration (`https://github.com/espressif/ESP8266_RTOS_SDK`). | No |
| TinyGo | The `d1mini` machine API has GPIO/UART/I2C/SPI/ADC/PWM and no WiFi. `espat` needs a separate AT-firmware chip (`https://tinygo.org/docs/reference/microcontrollers/machine/d1mini/`, `https://github.com/tinygo-org/drivers/tree/release/espat`). | No (no on-chip WiFi) |
| MicroPython | "Total RAM available: 96KB (part of it reserved for system)" (`https://docs.micropython.org/en/latest/esp8266/general.html`). ISRs can't allocate (`https://docs.micropython.org/en/latest/reference/isr_rules.html`). | No (tight against 50 KB) `[Inference]` |

Recommendation: C++ on the Arduino core with PlatformIO.

Build check (done): the reference core (`firmware/subvendo/lib/subvendo_core/`) builds with the core 3.1.2 xtensa GCC 10.3 under `-Wall -Wextra -Werror` (`pio run -e core_target`). Core code size is about 2.2 KB of text, with 0 B data/bss. The toolchain is pinned in `firmware/subvendo/platformio.ini`.

Provenance (verified by string scans of the firmware images; the `.bin` files are not in the repo):
- JuanFi v4.4 LanBased/ESP8622 (`/mnt/local-disk-d/Projects/random/juanfi-bin/release-4.4/release/LanBased/ESP8622/bins/JuanFi-FlashFile1.bin`, machine-local): `analysis/firmware/strings_ascii.txt:2849` has `2.2.2-dev(38a443e)` and `:2466` has `SDK ver: %s compiled @ Jul  3 2019 15:53:05`. `EthernetWebServer v1.6.0`, `W5500` and `SPIFFS` are present; `LittleFS` is absent.
- KiTifi `KiTifi-ESP8266-LAN.bin` and `KiTifi-ESP8266-Wireless.bin` (gitignored `kitifi-subvendo-bins/`): the same `2.2.2-dev(38a443e)` and `SDK ver ... Jul  3 2019`. The LAN image also has `W5100::init: no chip :-(`, `spiffs_api.h` and `bearssl`.
- KiTifi ESP32 boards: `KiTifi-ESP32-LAN.bin` is Arduino-ESP32 core 2.0.11 on ESP-IDF v4.4.5. `KiTifi-LAN8720.bin` is Arduino-ESP32 2.0.9 on ESP-IDF v4.4.4.
- Both vendors build their ESP8266 boards on the Arduino ESP8266 core (2.2.2-dev, NONOS SDK, 2019, SPIFFS). This supports our choice of the same framework. We move to core 3.1.2 and LittleFS, because SPIFFS is deprecated. `Arduino/2.2.0` seen in the KiTifi strings is an HTTP User-Agent, not the core version.

### ISR and WiFi rules (Arduino core reference)

Source: `https://arduino-esp8266.readthedocs.io/en/latest/reference.html`.

- Mark the handler `IRAM_ATTR` (replaces the older `ICACHE_RAM_ATTR` used in earlier notes).
- Do not call `delay()` or `yield()` in an ISR. Work longer than 1 ms causes instability. Blocking interrupts destabilizes WiFi. No heap use in an ISR.
- `attachInterrupt` works on any GPIO except GPIO16.
- Calling `analogRead()` too often stops WiFi.
- The docs make no claim that WiFi delays GPIO ISRs. Latency under WiFi load is still unmeasured `[Open]`.

### Message authentication: HMAC, no TLS

- BearSSL ships in the core: `tools/sdk/include/bearssl/bearssl_hmac.h` and `bearssl_hash.h` (`br_sha256_vtable`). The API is `br_hmac_key_init` / `init` / `update` / `out` (`https://bearssl.org/apidoc/bearssl__hmac_8h.html`).
- TLS defaults are `setBufferSizes(16384, 512)`, about 16.7 KB of RX buffer (`WiFiClientSecureBearSSL.cpp:86, 181-185`). That is heavy against a 50 KB budget.
- Recommendation: HMAC-signed messages without TLS on the LAN.

## Q4. Coin acceptor pulse signal

### CH-926 facts (manual, read in this session)

Source: the Adafruit CDN manual image, committed at `image-ref/ch926-multicoin-manual.jpg` (1280x919). `[Open]` The CDN URL is not recorded in this doc. Record the URL before filing.

- Supply: DC +12 V ±10%, working current 65 mA ±5%. Signal output: pulse. Speed listed as ≤0.6 s. Accuracy 99.5%.
- Coins: 1 to 6 coin types at once. Coin diameter 15 to 32 mm, thickness 1.2 to 3.8 mm.
- Pulses per coin: programmed in setup, "P = output pulses per coin, quantity limit 50". Set by holding Add+Minus for about 3 s, then the setup routine. Setup is stored at power-off.
- Sensitivity: "F = accuracy 1-30 (1 most accurate, 5-10 typical)". Sample at least 20 coins during setup.
- Pulse width switch: fast 20 ms, medium 50 ms, slow 100 ms.
- Output polarity: two-way switch, NO or NC.
- FAQ: "Check if there is pull-up resistor with signal wire (not allowed)."
- Wiring: red +12 V, white COIN, black GND, gray Counter. The 4-pin socket is DC12V, COIN, GND, COUNTER.

Notes:
- Pulses per coin are programmed. This means denomination is encoded in the count, so two coin types must not share a count. `[Inference]`
- The manual gives pulse width as a switch, not a measured waveform. The gap between pulses and the idle level are not stated. `[Open]`
- The FAQ conflicts with the common MCU practice of `INPUT_PULLUP`. Which one is right depends on the output stage (open-collector to GND needs a pull-up; push-pull does not). Measure with a multimeter. `[Open]`

### Other acceptors

- ALLAN datasheet: not found. `[Open]`
- A third-party ESP32 coin project (`saidtuncc/reflex-game-machine`, README.md lines 71 and 115) wires a CH-926 to GPIO34, which is listed as "Interrupt on RISING edge" and debounced in software with `COIN_DEBOUNCE_MS = 100`. This is a single-edge debounce, not a burst grouping. GPIO34 is input-only on ESP32 `[Inference from ESP32 datasheet; not fetched]`, so it has no internal pull-up. This project's README is secondary evidence.
- Another README (from an earlier session, URL not recorded) gives a 300 ms aggregation window and a 10 kΩ series resistor. Re-verify before citing.

### ESP8266 firmware practice

- ISR rules are in Q3: `IRAM_ATTR`, no `delay()`/`yield()`, no heap, keep it to a timestamp and a counter.
- Strapping pins on ESP8266: GPIO0 (flash mode if low), GPIO2 (must be high at boot), GPIO15 (must be low at boot to boot from flash). The KiTifi LAN build uses GPIO2 as COIN and GPIO15 as SET (`board-behavior-esp8266-lan.md` §9). JuanFi's LAN UI defaults use GPIO2 (coin), GPIO15 (set), and GPIO0 (LAN CS) (1d). A pull or drive on any of these can change boot behavior. Keep GPIO0, GPIO2, and GPIO15 free of acceptor lines `[Inference]`. JuanFi's sample config stores 12/13/0 (but those are ignored unless field 40 is set, 1d), so use 12/13 or other non-strapping GPIOs, and settle it in the bench ticket.
- ESP32 PCNT (only relevant if the successor chip is ESP32 family): a hardware pulse counter with a glitch filter and watch points, which lets the counter run without ISR load. Whether PCNT counts while the CPU is busy is not stated in the page used `[Open]`.
- Quiet window: KiTifi uses 130 ms (`board-behavior-esp32-lan.md:64`). This is safe only if the gap between pulses inside one coin is less than 130 ms and the gap between coins is more. The CH-926 slow setting is 100 ms pulse width, so the inter-pulse gap at slow speed may be near 130 ms. `[Inference]` Measure it. JuanFi has no quiet window at all (1d).
- Wi-Fi interference on ESP8266 ISR latency: no documented claim either way; bench test `[Open]`.

### Design

1. Count rising edges (or falling, per NO/NC) in an `IRAM_ATTR` ISR. The ISR touches only a `volatile` cumulative `total` and a last-edge timestamp.
2. Group bursts in the main loop. A burst ends after a quiet window longer than the measured inter-pulse gap. Make the window configurable, with the default set by bench measurement (ticket 3).
3. The SET pin gates the acceptor (Q5): enabled only while a window is open AND the Controller is reachable. Edges that arrive anyway are still counted into `total` and tagged with the `window_id` in force, so the Controller can audit them. They never auto-credit.
4. Set the pull-up or series resistor from the bench measurement, not from either source alone.
5. On ESP32, use PCNT with the glitch filter. On ESP8266, use a software filter in the ISR that drops edges closer than a few milliseconds `[Inference]`.

## Q5. Lost coin and lost credit: failure modes and robust design

### Design: window-gated cumulative total

Board side (this is the subject of the doc):

- The Controller opens a window: `open{window_id, expires, hmac}`. The board unblocks SET only while a window is open AND the Controller is reachable. If the Controller is unreachable, the board blocks the slot, so the acceptor rejects and returns coins instead of losing them. This is the biggest improvement over both systems.
- The ISR (`IRAM_ATTR`, `volatile` counter, timestamp only) increments a cumulative `total`.
- At the end of a burst, the board saves `{epoch, total}` to RTC memory (a cache with a CRC) and to a CRC-checked flash record ring/log. By default it does not write per pulse (per-burst vs per-pulse is compared in "Simulation results").
- The board reports `{vendo_id, epoch, total, window_id, boot_nonce, rseq, hmac}`. The Controller credits `delta = (int32)(total - last_seen(vendo, epoch))` when `delta > 0`, else 0. The same report twice credits 0. A missing ack means the board resends the latest total, so no queue is needed. The cumulative total is the idempotency key. `boot_nonce` (random per boot, not persisted) and `rseq` (per-boot counter from 0) are not idempotency keys: they only prove freshness. `open` and `ack` echo `boot_nonce` and `rseq`. The board accepts an `ack` only for this boot's nonce, and an `open` only if it echoes this boot's nonce and one of the last 3 reports it sent.
- HMAC-SHA256 with the per-Vendo 32-byte secret, full 32-byte tag, over fixed big-endian layouts. The first byte is a type tag (domain separation); all fields are `u32`:
  - `report` = `0x01 | vendo_id | epoch | total | window_id | boot_nonce | rseq` (25 B)
  - `open` = `0x02 | vendo_id | window_id | expires_ms | boot_nonce | rseq` (21 B)
  - `ack` = `0x03 | vendo_id | epoch | total | boot_nonce | rseq` (21 B)
  - Test vectors for a Go Controller are at the end of `firmware/subvendo/sim/out/report.md`.
- After a reboot, the board restores the record (RTC or flash) with the highest `seq`. If nothing valid survives (first boot, reflash, wipe, every record corrupt), the epoch comes from the hardware RNG (`random32()`) with `total = 0`, and the Controller starts a new baseline without double-counting. A random 32-bit epoch collides with a known one with probability 2^-32. Counter wrap needs no new epoch (serial arithmetic).
- A coin belongs to the window open at its first pulse (the ISR keeps the first-edge time). Pulses whose first pulse is after the window closed are tagged 0 and go to the audit queue; they are never auto-credited. This refines F7: only coins whose first pulse is after close go to audit.
- The board ignores a new `open` until its latest total is acked, nothing is unsaved, and at least 300 ms (placeholder) has passed since the last window ended. The Controller resends `open` every second, so the board needs no queue. The 300 ms is the longest delay from inhibit to the first pulse of an already accepted coin, to be measured on the CH-926 bench.
- `expires` is a duration in ms from receipt (`millis()`), clamped to 10 min. A duplicate `open` never extends it. This answers open question 10 as a proposal.
- "Reachable" means an authenticated `ack` for the last report sent arrived within 6 s. The board sends a heartbeat report every 2 s (1 s while unacked), so three missed heartbeats block the slot. No ack since boot means unreachable.
- No reboot while a Coin Window is open (JuanFi defers restarts during activity, 1d).
- Residual risk: power loss mid-burst before the flash save. There is no brownout interrupt, so an external supervisor or bulk capacitor is needed. Size the hold-up time against the flash timings below `[Inference]`; bench test.

Controller side (context):

- A credit intent row is written in the same SQLite transaction as the ledger, then the Router is written with an absolute `limit-uptime` computed from the ledger. Absolute writes are safe to retry (#5). The Reconciler replays pending intents (#11).
- Audit table: append-only. Every change to a balance writes a row with actor, reason, before, after, and a link to the report. Pulses on a closed window land here.
- Reconciliation: each hour (or on Reconciler tick), compare each Vendo's last-seen `total` per epoch with the sum of credited deltas. Any gap is raised to admin (#13).
- SQLite: `journal_mode=WAL`, `synchronous=FULL` for the money tables. The SQLite docs say `synchronous=NORMAL` in WAL mode can roll back committed transactions on power loss (`https://www.sqlite.org/pragma.html#pragma_synchronous`, `https://www.sqlite.org/wal.html`). WAL needs the database and its `-shm` file on the same host. Both URLs were summarized in the earlier session.

### Failure modes

| # | Failure | Effect today | Mitigation |
|---|---|---|---|
| F1 | Report lost on the network, or Controller unreachable | KiTifi: count never reaches the Controller, no retry. | No window opens while the Controller is unreachable, and an open window's SET pin is blocked, so the acceptor rejects and returns coins. If a report is lost, the board resends the latest `total`. No queue. |
| F2 | Ack lost | Vendo resends. A naive server credits twice. | The Controller credits `total - last_seen(vendo, epoch)`. The same report twice credits 0. |
| F3 | Board reboots | Count resets to 0, so the Vendo re-reports from zero and the Controller either double-credits or drops. | Restore `{epoch, total}` from the RTC cache, falling back to the flash ring. If both fail, a random `epoch` and `total = 0`, and the Controller starts a new baseline. No reboot while a Coin Window is open. |
| F4 | Power loss mid-burst before the flash save | A counted coin is lost. | Residual risk. Needs an external supervisor or bulk capacitor sized for the flash timings. There is no brownout interrupt `[Inference]`. Bench test. |
| F5 | Controller crashes after credit, before Router write | Balance says yes, Router says no. | Context: credit intent written in the same transaction as the ledger, then Router apply, then mark applied. Reconciler replays pending intents (#11). |
| F6 | Router write fails | Same as F5. | Context: intent stays pending and is visible to admin. Retry with backoff. |
| F7 | Late pulses after the Coin Window closes | Credit to the wrong user, or silently dropped. | A burst is tagged by the window open at its first pulse. Those whose first pulse is after close (tag 0) go to the audit queue with status `pending-review` and never credit automatically (candidate ticket 4). |
| F8 | Two users in one window | KiTifi check-then-act race (handoff #13). | Context: DB unique constraint, one open Coin Window per Vendo. |
| F9 | Pulse miscount (EMI, merged pulses) | Wrong amount. | Record raw burst counts. Compare with the coin type table (Controller). Discrepancies are audited. The ISR drops edges closer than a few milliseconds `[Inference]`. |
| F10 | Clock jump or no RTC clock | Time-based logic breaks. | Ordering and idempotency use `epoch` and the cumulative `total`, not time. Time matters only for window expiry. The board has no wall clock, so it counts `expires` as a duration from receipt using `millis()`, and compares with `(int32)(a - b)`. The `wrap` sim scenario passes. |
| F11 | Flash wear from persisting every coin | Chip wears out. | Persist per burst, not per pulse, into a record ring so one sector erase covers many bursts. W25Q32JV endurance is 100K cycles per sector (below), which is not a limit at coin-slot rates `[Inference]`. |

### Simulation results

Source: `firmware/subvendo/TEST-PLAN.md` (decisions D1-D14, failure list) and `firmware/subvendo/sim/out/report.md` (numbers below).

- **What was simulated:** 21 scenarios x 500 seeds (the `wear` scenario uses one), each under both checkpoint policies. The real core (`firmware/subvendo/lib/subvendo_core/`) runs against fakes for clock, flash, RTC, slot gate, transport and HMAC, with a minimal Controller model. It is not hardware: ISR timing, WiFi load, supply decay and flash bit rot are bench items.
- **Invariants:** I1 (credited <= accepted), I2 (accepted + spurious = credited + audit + residual loss), I3 (no coin counted while blocked; slot never open too long after the last ack), I4 (forged messages never credit or open a window), I5 (restore never goes below a reported total in the same epoch) all pass in every scenario under both policies, with 0 unexplained pulses. I6 (flash wear) is reported, not pass/fail.
- **Flaws found** (the first version failed three invariants; each is fixed and was re-checked by removing the fix in a scratch copy):
  - F-A, epoch collision when every record is lost: the epoch is now `random32()` from a HAL seam (hardware RNG) when restore fails.
  - F-B, a delta spanning two windows credited to the later one: tag by first pulse, and ignore `open` until saved, acked and 300 ms after the last window.
  - F-C, stale `open` and replayed `ack` across a reboot: `boot_nonce` and `rseq` echoed in `open` and `ack`, plus the last-3-reports recency check on `open`.
- **Per-burst (default) vs per-pulse**, from `report.md`:

| metric | per-burst | per-pulse |
|---|---|---|
| pulses lost to residual F4 (summed over cut scenarios) | 60294 | 11849 |
| pulses lost per losing cut | 4.38 | 1.02 |
| pulses of an accepted coin arriving while the board is down | 10045 | 67543 |
| `wear` scenario: flash records per coin | 1.00 | 4.36 |
| `wear` scenario: erases per burst | 0.0077 | 0.0339 |
| `wear` scenario: years to 100K erases at 300 coins/day (2-sector ring) | 232 | 54 |

  - Caveat: pulses that arrive while the board is down dominate total loss, and per-pulse does not reduce them (67543 vs 10045 in the table). Per-pulse only shrinks the F4 part, and the sim does not model whether a flash write in the middle of a pulse train makes the ISR miss pulses.
  - Recommendation: keep per-burst as the default until the bench test `isr_loopback` under the `flash` load (continuous erase and program of one sector; `firmware/subvendo/bench/README.md`, section 4) shows whether pulses are missed during flash writes. Per-pulse puts flash writes in the middle of pulse trains.
- **Run it:** `make -C firmware/subvendo/sim run`. Reports are `firmware/subvendo/sim/out/report.md` and `report.json`.

### RTC memory on ESP8266 (primary sources)

From the ESP8266 Arduino core:

- User RTC memory is 512 bytes after a 256-byte system area, accessed in 4-byte blocks (`Esp.cpp:142-146, 176-192`).
- It is retained across deep sleep (`RTCUserMemory.ino:5-9`), but "might be lost after power cycling".
- There is no primary statement on retention across a software or watchdog reset. Bench test `[Open]`.
- eboot uses the first 128 bytes (32 blocks) when the Updater is used (`Esp.cpp:167-169`). Put the checkpoint at block offset 32 or higher if OTA is used.
- Treat RTC as a cache with a CRC. The flash ring is the durable copy.

### Flash on ESP8266

- W25Q32JV: at least 100K program/erase cycles per sector, 20-year retention, 4 KB sectors, page program 0.4 ms typical / 3 ms max, 4 KB sector erase 45 ms typical / 400 ms max (`https://www.winbond.com/resource-files/w25q32jv%20revg%2003272018%20plus.pdf`). Confirm that the actual module uses this part `[Open]`.
- LittleFS does dynamic best-effort wear leveling and is power-loss resilient at the metadata-commit level (`https://github.com/littlefs-project/littlefs/blob/master/DESIGN.md`). The core docs call SPIFFS deprecated, and LittleFS has a 4K minimum per file (`https://arduino-esp8266.readthedocs.io/en/latest/filesystem.html`).
- A raw CRC-checked record ring avoids the 4K-per-file cost and keeps write timing predictable. LittleFS is the fallback `[Inference]`.
- The sector erase time (up to 400 ms) is what the hold-up capacitor must outlast in the worst case `[Inference]`.

### Brown-out

- There is no documented brownout interrupt on the ESP8266.
- VDD can be read through `system_get_vdd33` (±0.2 V, needs init byte 107 = 0xFF; datasheet §4.9).
- Use an external supervisor if needed `[Inference]`.

## Q6. Synthesis: best-of-both target architecture

```
Vendo board (ESP8266, C++ / Arduino core)
+----------------------------------------------------------------------------+
|  coin acceptor pulse                                                       |
|        |                                                                   |
|        v                                                                   |
|  ISR (IRAM_ATTR; volatile total + timestamp only)                          |
|        |                                                                   |
|        v                                                                   |
|  burst grouper (main loop, quiet window)                                   |
|        |                                                                   |
|        v                                                                   |
|  checkpoint at burst end: RTC cache {epoch,total,CRC} + flash record ring  |
|        |                                                                   |
|        v                                                                   |
|  reporter -- {vendo_id, epoch, total, window_id, nonce, rseq, hmac} --+    |
|                                                              |             |
|  window gate ----> SET pin (enabled only while a window is   |             |
|        ^            open AND the Controller is reachable)    |             |
+--------|-----------------------------------------------------|-------------+
         | open{window_id, expires, hmac}                       v
         |                                                      |
+--------+------------------------------------------------------+-------------+
| Controller (Go + SQLite, context)                                          |
| credits total - last_seen(vendo, epoch); audits pulses on closed windows;  |
| writes absolute limit-uptime to the Router; ack to the board               |
+----------------------------------------------------------------------------+
```

The Router (MikroTik RouterOS 7) is the sole meter (ADR 0003), and the captive Portal (RFC 8908/8910) reads state from the Controller. Both are outside this doc's scope. Portal discovery uses RFC 8910 (DHCP option 114 or RA type 37) and RFC 8908 (`application/captive+json`) (`https://www.rfc-editor.org/rfc/rfc8908`, `https://www.rfc-editor.org/rfc/rfc8910`).

### What to keep, change, and drop from each

- Keep from KiTifi: dumb Vendo, server-side pricing, Coin Window, inhibit by default.
- Keep from JuanFi: deferred restart during activity, a coin-wait timeout, and (as Controller context) QR purchase with owner approval. Expiry and one-time use are our additions.
- Change both: auth (per-Vendo HMAC), counters (cumulative per epoch), storage (RTC + flash ring on the board; SQLite WAL on the Controller), pricing (no RMW), pins (no strapping pins).
- Drop both: fleet token in a URL, unsigned OTA, secrets in readback and backups, state-changing GETs, third-party QR, a Vendo that reboots on heartbeat failure.

### Controller language (context)

The user agreed on Go + SQLite for the Controller. It is not on the board. Target: Orange Pi Zero, armv7, 256 MB RAM (ADR 0002). `[Open]` The 256 MB variant was not confirmed from a vendor page; the Orange Pi wiki fetch failed with `ECONNREFUSED`.

| Criterion | Go | PHP |
|---|---|---|
| Deploy as one artifact | Yes. Build with `GOARM=7` (`https://go.dev/wiki/GoArm`). | No. Needs PHP runtime, a web server, and PHP-FPM. |
| SQLite | cgo-free `modernc.org/sqlite` (BSD-3, pushed 2026-10-15, GitHub API). Larger binary and more RAM than cgo SQLite `[Inference; measure on armv7]`. | Built in (PDO SQLite). |
| Timers and concurrency | Goroutines and `time.Timer` fit Coin Windows and the Reconciler. | Request-scoped. Timers need cron or a daemon. |
| Memory on 256 MB | Set `GOMEMLIMIT` (`https://go.dev/doc/gc-guide`). Idle RSS not measured `[Open]`. | FPM pool sized for 256 MB. Not measured `[Open]`. |
| Support window | Not checked. | PHP 8.4 active until 31 Dec 2026 (`https://www.php.net/supported-versions.php`). |
| Libraries | `go-routeros/routeros` (MIT, v3.0.1, 2025-02-16). `skip2/go-qrcode` (MIT, pushed 2024-03-01; check maintenance). REST needs only `net/http` `[Inference]`. | Not checked. |

Recommendation: Go, using `modernc.org/sqlite`, a single binary, and `GOMEMLIMIT` set for 256 MB. Before committing, run the armv7 256 MB test already in #14 and measure binary size, idle RSS, and peak RSS under Vendo load.

### Candidate GitHub tickets

These are checked against #5 to #16 (and #3). Each is new, or extends an existing ticket in a way that needs its own acceptance criteria. Before filing, check whether #7, #11, #13, #14, #15, #16 already cover the item. Labels: `needs-triage` unless noted.

1. **Subvendo report protocol: cumulative total, epoch, window_id, HMAC**
   - Description: The board sends `{vendo_id, epoch, total, window_id, boot_nonce, rseq, hmac}` using a per-Vendo secret. `open` and `ack` echo `boot_nonce` and `rseq`. HMAC-SHA256 over the binary layouts in Q5 (tag byte, big-endian u32 fields); the Go Controller must reproduce the vectors at the end of `firmware/subvendo/sim/out/report.md`. The Controller credits `(int32)(total - last_seen(vendo, epoch))` when positive. The cumulative total is the idempotency key, so there is no per-message seq. A missing ack means the board resends the latest total. A coin is credited to the window tagged on its report. This is the protocol part of #5, implemented by #15, and it is separate from pricing in #16. It merges the earlier report-sequencing ticket. A reference implementation of the board side and a Controller model exist in `firmware/subvendo/`.
   - Acceptance criteria:
     - The same report sent twice credits 0 the second time.
     - A dropped ack causes a resend of the latest total that credits nothing twice.
     - A bad HMAC or wrong type tag is rejected and audited.
     - A `total` lower than `last_seen` in the same epoch (serial compare) credits 0 and is audited.
     - A new `epoch` with `total = 0` starts a new baseline with no double-count.
     - The Controller echoes the newest `(boot_nonce, rseq)` in `open` and `ack`, and the canonical vectors match.
     - An E2E test using the simulated Vendo from #15 covers all of these.

2. **Subvendo firmware: ISR, burst grouping, checkpoint, window gate**
   - Description: Firmware counts edges in an `IRAM_ATTR` ISR, groups bursts with a configurable quiet window set from bench data, and checkpoints `{epoch, total}` to RTC (CRC cache, block offset 32 or higher if OTA is used) and a CRC-checked flash ring at burst end (per-burst default; per-pulse is configurable, decided by the bench). It blocks the SET pin unless a window is open AND the Controller is reachable (ack within 6 s, heartbeat every 2 s). It ignores a new `open` until the latest total is saved and acked and the 300 ms window gap has passed. It tags a coin by the window open at its first pulse, and takes `expires` as a duration from receipt clamped to 10 min. It never reboots while a Coin Window is open (`canReboot()`). The reference core and the fault-injection simulation exist in `firmware/subvendo/`; this ticket ports them to the Arduino layer and runs the bench tests. Firmware is out of scope in #5, so it needs its own ticket.
   - Acceptance criteria:
     - Power-cycle at N points after a burst-end save produces no double credit and no lost coin.
     - With the Controller unreachable, the slot is blocked and the acceptor returns the coin.
     - A failed restore (RTC wiped, flash corrupt or wiped) yields a random `epoch` from the hardware RNG, `total = 0`, and a clean Controller baseline.
     - The sim invariants I1-I5 still pass against the Arduino-layer build, and the bench results in `firmware/subvendo/bench/README.md` are recorded.
     - The quiet window is set by config, not by a constant.
     - The ISR has no blocking calls and no heap use (checked in review).
     - No reboot is triggered while a window is open.
     - Power loss mid-burst before the flash save is measured and the loss window is documented.
   - Label: `ready-for-human` (needs hardware).

3. **CH-926 bench characterization and pin decision**
   - Description: Measure pulse width, inter-pulse gap, idle level, and output type (NO/NC, open-collector or push-pull) at fast, medium, and slow settings, for programmed counts of 1, 2, and 5 pulses. Use the data to choose the pull-up or series resistor and the quiet window. This resolves the FAQ conflict with `INPUT_PULLUP`. Choose pins that avoid GPIO0, GPIO2, and GPIO15, including JuanFi's defaults.
   - Acceptance criteria:
     - A measurement table is committed under `docs/research/`.
     - The pin map avoids GPIO0, GPIO2, and GPIO15 and is recorded in an ADR if pins or wiring change.
     - Coin types have counts that do not collide.
     - ISR latency with WiFi active is measured.
   - Label: `ready-for-human`.

4. **Late and unmatched pulses go to an audit queue (Controller side)**
   - Description: Pulses tagged with a closed `window_id`, or with a count that matches no programmed coin type, are written to an append-only audit table with status `pending-review`. They never credit a balance without an admin action. Extends #16 (Coin Window) and #13 (admin oversight).
   - Acceptance criteria:
     - A report tagged with a closed window lands in the audit table, not in a balance.
     - An unknown count creates an audit row.
     - Admin can see per-Vendo totals and resolve a row, and the resolution is recorded.
   - Note: check whether #13 already covers this before filing.

5. **Credit intent journal and idempotent Router apply (Controller; context)**
   - Description: The Controller writes "credit N to Entitlement E" in one SQLite transaction (`synchronous=FULL`) before calling the Router. The Router write sets `limit-uptime` to an absolute value derived from the ledger, so retries are safe. The Reconciler (#11) replays pending intents. Extends ADR 0003 and #9.
   - Acceptance criteria:
     - An E2E test that kills the Controller after the ledger commit and before the Router write yields exactly one effective limit after restart.
     - A Router error leaves the intent pending and visible in admin.
     - No code path does read-modify-write on `limit-uptime`.

6. **Render voucher and purchase QR codes locally (Controller)**
   - Description: The Controller renders QR codes with a Go library (`skip2/go-qrcode` is MIT, last pushed 2024-03-01, so check maintenance first). Voucher codes never reach a third-party host. This avoids the pattern in `voucher-generate.html:227`.
   - Acceptance criteria:
     - With outbound network blocked, the voucher page renders a QR.
     - The QR decodes to the expected payload.
   - Note: check #7 first.

7. **Signed firmware updates**
   - Description: The Controller publishes a version and a signature. The board verifies before flashing and rejects unsigned images. Replaces the unsigned OTA in KiTifi (`SUMMARY.md:99`) and JuanFi (`admin.js:987-990`). Not in #14, which covers the Controller install script.
   - Acceptance criteria:
     - An unsigned image is rejected.
     - A signed image with a wrong version is rejected.
     - Rollback to the prior image works after a failed boot.
     - An update preserves config and rates (JuanFi's FS update resets promo rates, `sc:25341, 25452`).
   - Label: `needs-triage`.

8. **No secrets in config readback or backups, and no state-changing GETs**
   - Description: The board's admin API never returns passwords or API keys on readback, backups exclude or encrypt secrets, and every state change uses an authenticated POST. Motivated by JuanFi's readback (`admin.js:451, 459-514`), plaintext backups (`strings:3001, 3063-3064`), and `resetStatistic` as a GET (`admin.js:279`).
   - Acceptance criteria:
     - A config readback contains no secret values.
     - A backup file contains no plaintext secret.
     - No GET route changes state (checked by a route list in the test).

9. **Round-trip tests for special characters in values**
   - Description: Values with `& = + #` and non-ASCII characters survive save and readback. Motivated by JuanFi's `createParam` using `encodeURI` (`admin.js:263-272`).
   - Acceptance criteria:
     - An E2E test saves a password containing `& = + #` and reads back the same value.
     - The test output is committed as a repeatable artifact.

10. **Chip choice for v1: ESP8266 (NRND) vs ESP8684 / ESP32-C3**
    - Description: The ESP8266EX is marked "NOT RECOMMENDED FOR NEW DESIGNS" and points to ESP8684. Decide whether v1 accepts ESP8266 or moves to ESP8684/ESP32-C2 or ESP32-C3, considering the 50 KB RAM budget, RTC retention, flash, GPIO, and toolchain.
    - Acceptance criteria:
      - A decision is recorded in an ADR with RAM, RTC, GPIO, and toolchain facts for the chosen chip.
      - If a successor is chosen, the open items on RTC reset retention and ISR latency are re-asked for that chip.
    - Label: `needs-triage`.

Optional, check #3 first: a purchase request with owner approval (JuanFi pattern) as a feature of the portal. If #3 already has it, skip.

## Open questions

1. ALLAN acceptor: datasheet not found. Need pulse width, count, polarity, and inhibit spec.
2. CH-926: inter-pulse gap, idle level, and output stage at each speed setting. Needed for the quiet window and pull-up choice.
3. **Answered:** the CH-926 manual is stored as `image-ref/ch926-multicoin-manual.jpg`. The CDN URL is still not recorded.
4. ESP8266 RTC user memory: does it survive a software or watchdog reset? There is no primary statement. Bench sketch `rtc_retention` (`firmware/subvendo/bench/README.md`, section 2).
5. ESP8266 WiFi-induced ISR latency: no documented claim either way; not measured. Bench sketch `isr_loopback` (`firmware/subvendo/bench/README.md`, section 4).
6. Brown-out hold-up: how long must the supply hold up for a flash save (page program up to 3 ms, sector erase up to 400 ms)? Does the module need an external supervisor or bulk capacitor? Bench sketch `flash_ring` (`firmware/subvendo/bench/README.md`, section 3).
7. Does the actual module use the W25Q32JV flash part? The endurance numbers assume it. Bench sketches `flash_id` and `flash_ring` (`firmware/subvendo/bench/README.md`, sections 1 and 3).
8. Chip choice for v1: ESP8266 (NRND) vs ESP8684/ESP32-C2 vs ESP32-C3 (candidate ticket 10).
9. Coin Window value: 60 s (#5), 45 s (KiTifi), 30 s (JuanFi). Confirm in #16.
10. **Answered (proposal):** `expires` is a duration in ms from receipt, counted with `millis()` and clamped to 10 min (TEST-PLAN D1).
11. **Answered:** the file pins win only when field 40 (`lanModeOverride`) is non-zero, which every admin UI save sets. Otherwise the hardcoded pins (2/15/3) apply (1d).
12. **Answered:** `/testInsertCoin` is behind `X-TOKEN`, but the token can be minted from a client-chosen `randomToken`. It has no range check or multiplier (1d).
13. **Answered:** the JuanFi ISR is a safe IRAM leaf, but the `loop()` consumer has an unmasked read-modify-write and a stranded-pulse window (1d).
14. **Answered (in part):** the board has no purchase or approval route (route strings `0x3ffe9b85..0x3ffe9beb`). Approval off-board is consistent with this but still unproven.
15. ESP32 PCNT: whether counting continues while the CPU is busy is not stated in the page used (only if the successor is ESP32 family).
16. ESP32 GPIO34 internal pull-up (input-only): inferred from the ESP32 datasheet, not fetched.
17. Controller context: Orange Pi Zero RAM variants (vendor wiki fetch failed with `ECONNREFUSED`; ADR 0002 assumes 256 MB), Go memory and binary size on armv7 with `modernc.org/sqlite`, and RouterOS transport (REST or binary API; JuanFi uses the binary API, the lab uses REST).
18. Does #7 cover QR rendering? Does #3 cover QR purchase or owner approval?
19. **Answered:** KiTifi phase 2 contract conflict: `server-contract.md:39-43` vs `SUMMARY.md:74-86`. The firmware never sends "done" (1e), so `SUMMARY.md` is right; a correction note is added to `server-contract.md`.
20. Git state: `kitifi-handoff.md` and `wifi5soft-client-identity-spec.md` have moved under `docs/research/` (#19) and are now `kitifi-controller-handoff.md` and `wifi5soft-client-identity-spec.md`. `SUMMARY.md` and the other KiTifi write-ups use the new name.
21. PR #4 branch lacks `juanfi-architecture.md` and `wifi5soft-architecture-client-time.md`, which are on `main` (commit `23ae382`). Cited paths here use `main`.
22. Per-burst vs per-pulse checkpoint: decided by the bench (does a flash write in a pulse train make the ISR miss pulses; `isr_loopback`, `flash` load). Default stays per-burst.
23. Window gap value (placeholder 300 ms): from the CH-926 bench, the longest pulse train after inhibit.
24. JuanFi LAN W5500 CS pin is unknown. The bench preset guesses GPIO0 (`firmware/subvendo/bench/common/bench_pins.h`).

## Sources

Repo files read or cited (line numbers as read; others by section):
- `docs/research/kitifi-firmware/board-behavior-esp32-lan.md` lines 51-66, 68-91, 97-106, 129-136, 146-155.
- `docs/research/kitifi-firmware/SUMMARY.md` lines 14-19, 20-22, 57-59, 64-71, 74-86, 88-103.
- `docs/research/kitifi-firmware/server-contract.md` lines 14, 39-43, 47-51, 53-58, 70-71.
- `docs/research/kitifi-firmware/board-behavior-esp8266-lan.md` §3.1, §3.2, §3.3, §4, §5, §7, §8 (S2, S5, S8), §9 (from earlier session).
- `docs/research/kitifi-firmware/board-behavior-esp8266-wireless.md` lines 87-104 (Security notes).
- `docs/research/juanfi-architecture.md` §4, §5, §8, §10, §11, and lines 76, 142, 151.
- `docs/research/wifi5soft-architecture-client-time.md` §3.3, §3.4 (from earlier session).
- `docs/research/results/2026-10-16-chr-session-control/results.md` E5, E6, E8, E11, L6, L7/C1, L8 (from earlier session).
- `docs/research/results/2026-10-16-chr-cookie-mac/results.md` E1 (referenced from `hotspot-identity-lab.md`).
- `docs/adr/0001-*.md`, `0002-*.md`, `0003-*.md` on PR #4 branch `docs/device-controller-planning` (from earlier session).
- GitHub issues #5 to #16 and #3 via `gh issue view` (from earlier session).

JuanFi release files (`/mnt/local-disk-d/Projects/random/juanfi-bin/release-4.4/`):
- `mikrotik-template/login.html` lines 228, 412, 490, 492 (QR button, QR container, payload, `JSON.stringify` wrapper).
- `mikrotik-template/assets/js/core.js` lines 145-147 (QR button shown), 306-320 (1 s poll of `/data/<mac>.txt`, `newLogin()` after 3 s).
- `mikrotik-template/assets/js/config.js` lines 61 (`macAsVoucherCode = false`) and 63 (`qrCodeVoucherPurchase = false`).
- `release/LanBased/ESP8622/analysis/REPORT.md` lines 50, 67, 103, 104.
- `release/LanBased/ESP8622/analysis/webroot/admin/voucher-generate.html` line 227 (voucher code sent to `api.qrserver.com`).
- `release/LanBased/ESP8622/analysis/webroot/admin/js/admin.js` lines 16-23, 263-272, 279, 432-442, 451, 459-514, 531-539, 588, 623, 640-644, 987-990, 1048-1054, 1336, 1575-1607, 1618, 1661, 1860, 1864.
- `release/LanBased/ESP8622/analysis/webroot/admin/system-config.html` bytes 9275, 9331, 9567, 9625, 9946, 10771, 15649, 15681, 17230, 17378, 21770, 25341, 25452.
- `release/LanBased/ESP8622/analysis/webroot/admin/login.html` bytes 1831, 2023.
- `release/LanBased/ESP8622/analysis/webroot/admin/config/system.data` fields 4-11.
- `release/LanBased/ESP8622/analysis/firmware/strings_ascii.txt` lines 2883-2891, 2994, 3001, 3012, 3059, 3063-3064, 3091-3100, 3109-3138, 3160-3161, 3210.

Static analysis and simulation:
- Method: Ghidra 12 headless on ELF-wrapped firmware segments (Xtensa LE32), for `JuanFi-FlashFile1.bin` and `KiTifi-ESP8266-LAN.bin`. Scratch projects and decompiler output are in `/tmp/subvendo-re/` (machine-local, not in the repo). Addresses in 1d and 1e are flash VAs.
- `firmware/subvendo/TEST-PLAN.md` (decisions, failure list, invariants).
- `firmware/subvendo/sim/out/report.md` and `report.json` (simulation results, canonical message vectors).
- `firmware/subvendo/bench/README.md` (bench procedure for open questions 4 to 7).

Other local and web sources:
- `image-ref/ch926-multicoin-manual.jpg` (CH-926 manual image, read this session).
- `https://documentation.espressif.com/0a-esp8266ex_datasheet_en.pdf` (ESP8266EX datasheet v7.1, 2025.11: NRND notice, §3.1.3 PDF p.13 heap, §4.9 `system_get_vdd33`).
- `https://documentation.espressif.com/esp8266_datasheet_en.pdf` (ESP8266 strapping pins; read via the Read tool, pages 9-12, in the earlier session).
- `https://github.com/esp8266/esp8266-wiki/wiki/Memory-Map` (96 KB data + 64 KB instruction SRAM).
- `https://github.com/esp8266/Arduino` (core 3.1.2): `cores/esp8266/Esp.cpp:142-146, 167-169, 176-192`; RTC example `RTCUserMemory.ino:5-9`; `WiFiClientSecureBearSSL.cpp:86, 181-185`; `tools/sdk/include/bearssl/bearssl_hmac.h`, `bearssl_hash.h`.
- `https://bearssl.org/apidoc/bearssl__hmac_8h.html` (HMAC API).
- `https://github.com/espressif/ESP8266_NONOS_SDK` (frozen since Dec 2019).
- `https://github.com/espressif/ESP8266_RTOS_SDK` (esp-idf migration plan).
- `https://tinygo.org/docs/reference/microcontrollers/machine/d1mini/` and `https://github.com/tinygo-org/drivers/tree/release/espat` (TinyGo d1mini API, espat).
- `https://docs.micropython.org/en/latest/esp8266/general.html` and `https://docs.micropython.org/en/latest/reference/isr_rules.html` (MicroPython RAM, ISR rules).
- `https://arduino-esp8266.readthedocs.io/en/latest/reference.html` (ISR rules, `attachInterrupt`, `analogRead`).
- `https://arduino-esp8266.readthedocs.io/en/latest/filesystem.html` (SPIFFS deprecated, LittleFS 4K minimum per file).
- `https://www.winbond.com/resource-files/w25q32jv%20revg%2003272018%20plus.pdf` (W25Q32JV endurance, retention, program and erase timings).
- `https://github.com/littlefs-project/littlefs/blob/master/DESIGN.md` (LittleFS wear leveling and power-loss resilience).
- `https://github.com/saidtuncc/reflex-game-machine/blob/main/README.md` lines 71 and 115 (CH-926 on GPIO34, 100 ms coin debounce; read via `gh api`, branch assumed `main`; secondary source).
- GitHub API, earlier session: `go-routeros/routeros` (MIT, pushed 2025-02-16, release v3.0.1 published 2025-02-16); `skip2/go-qrcode` (MIT, pushed 2024-03-01); `modernc-org/sqlite` (BSD-3-Clause, pushed 2026-10-15); `layeh/radius` (MPL-2.0, not used).
- `https://www.php.net/supported-versions.php` (PHP support windows; cited from the earlier session, not re-fetched).
- `https://go.dev/wiki/GoArm` and `https://go.dev/doc/gc-guide` (GOARM and GOMEMLIMIT; cited from the earlier session, not re-fetched).
- `https://www.sqlite.org/pragma.html#pragma_synchronous` and `https://www.sqlite.org/wal.html` (durability and WAL same-host requirement; cited from the earlier session, not re-fetched).
- `https://www.rfc-editor.org/rfc/rfc8908` and `https://www.rfc-editor.org/rfc/rfc8910` (captive portal API and DHCP/RA discovery; cited from the earlier session, not re-fetched).
- `https://help.mikrotik.com/docs/display/ROS/REST+API` (RouterOS REST verbs and timeout; cited from the earlier session).
