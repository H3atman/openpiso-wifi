# KiTifi subvendo firmware — how the four bins work

Cross-board synthesis of the four reverse-engineered vendo-board images. This is
the map: one wire protocol, one coin model, four hardware wrappers. For any
board's internals read its own deep-dive; for the controller half read the
contract. This doc does not restate either.

> Provenance: these write-ups cite a Ghidra decompilation corpus (`evidence/`,
> `artifacts/`) and `kitifi-controller-handoff.md`. The corpus is the local analysis kit that
> ships with the firmware binaries (gitignored, not in this repo); `kitifi-controller-handoff.md`
> is at `docs/research/kitifi-controller-handoff.md`. The corpus paths below are where the analysis was done, not
> links that resolve from `docs/`.

| Read for | Doc |
|---|---|
| ESP8266 LAN board internals (fullest trace; ISR + session machine) | [`board-behavior-esp8266-lan.md`](board-behavior-esp8266-lan.md) |
| ESP8266 Wireless board (WiFi + AP portal delta) | [`board-behavior-esp8266-wireless.md`](board-behavior-esp8266-wireless.md) |
| ESP32 LAN board (W5x00 SPI Ethernet) | [`board-behavior-esp32-lan.md`](board-behavior-esp32-lan.md) |
| ESP32 LAN8720 board (RMII PHY delta) | [`board-behavior-lan8720.md`](board-behavior-lan8720.md) |
| Controller-side protocol + billing | [`server-contract.md`](server-contract.md) |
| Flash/partition/segment maps | [`flash-layout.md`](flash-layout.md) |
| Raw string/endpoint inventories | [`esp8266-strings-inventory.md`](esp8266-strings-inventory.md), [`esp32-strings-inventory.md`](esp32-strings-inventory.md) |

## The one design

All four boards are the **same application** compiled four ways. The board is a
**dumb pulse counter with an HTTP client**: it counts coin pulses on a GPIO,
reports running totals to the controller, and holds no rates and no money logic.
The controller is the sole billing authority ([`server-contract.md`](server-contract.md)).
What differs between images is only the **network wrapper** — the layer below the
socket — and the **default coin-pin wiring**.

| Image | MCU | Network wrapper | Default coin / set pin |
|---|---|---|---|
| ESP8266-LAN | ESP8266 | W5x00 **SPI Ethernet** (Arduino Ethernet lib) | D4 / D8 (SPI bus occupies D5–D7) |
| ESP8266-Wireless | ESP8266 | **WiFi station** + SoftAP setup portal | D6 / D7 (bus free) |
| ESP32-LAN | ESP32 | W5x00 **SPI Ethernet** (Arduino Ethernet lib) | from `/config.json` |
| LAN8720 | ESP32 | internal EMAC + **LAN8720 RMII PHY** (esp_eth) | from `/config.json` |

The **D6/D7 wiring diagram matches the Wireless board only** — the LAN builds
need those pins for the Ethernet SPI bus. All pins are data-driven from
`/config.json` (keys `coinslot_pin`, `coinslot_set_pin`, `sys_ready_led_pin`,
`insert_coin_led_pin`); the table shows each build's shipped default.

## The wire protocol (identical on all four)

The board is an HTTP **client** of the controller and a small HTTP **server** for
it. Four outbound GETs, two inbound callbacks, one shared token — byte-identical
across images.

Outbound (board → controller), each with `&token=<redacted>` appended:
- `vendo_handler.php` — registration + heartbeat (board MAC, IP, version, type).
- `coin_handler.php?id=` — running **coin total** for the paying user.
- `pulse_handler.php?mac=` — raw **pulse burst** count.
- `stay_active.php` — keepalive.

The same firmware fires **both** `coin_handler` and `pulse_handler`; the
controller's `tbl_vendo.type` (`coins` vs `pulse`) decides which it honors
([`server-contract.md`](server-contract.md)).

Inbound (controller → board): `/insertCoin` opens a session and enables the
acceptor; `/cancelInsertCoin` aborts it.

## Coin acceptance (identical model)

Pulse counting with a quiet-window debounce. While a session is active, each
rising edge on `coinslot_pin` increments two counters (coin accumulator + raw
pulse count) and arms a **~130 ms** quiet timer; a burst is complete after
130 ms of silence. **1 pulse = 1 unit** — the board has no denomination table, so
a ₱5 coin that emits 5 pulses is just `+5`. Peso→time conversion is entirely
server-side via `calculateAddTime` greedy rate tiers ([`server-contract.md`](server-contract.md)).
Instruction-level ISR trace: [`board-behavior-esp8266-lan.md`](board-behavior-esp8266-lan.md) §3.

## Session: who drives phase 2 (correction to the contract)

The insert-coin session is **entirely controller-driven**. The controller calls
`/insertCoin` (phase 1), the board counts and reports cumulative totals, and the
session ends on the board by **inactivity timeout or `/cancelInsertCoin`** only.

**The firmware never signals "done."** There is no `done_insert_coin_handler.php`
call and no final-total message anywhere in any image. Phase 2 billing is driven
by the controller reading the totals the board already pushed via `coin_handler`.
[`server-contract.md`](server-contract.md) §"Two-phase insert-coin session" still
describes phase 2 as "board signals done →" — that is the inferred server-side
view; **the firmware is ground truth and contradicts it.** Treat the board as
fire-and-forget: it reports running totals and forgets them.

## Security posture (shared across all four)

The weaknesses are in the shared application, so every board carries them:
- **One hardcoded fleet token** `<redacted>` in a GET query param over
  cleartext HTTP — sniffable, logged, identical on every board. Anyone with it
  can call `coin_handler.php` to set any user's coin balance.
- **Unauthenticated local control**: `/insertCoin`, `/cancelInsertCoin`, and the
  admin/config endpoints (`saveApConfig`, `saveSystemConfig`, OTA `update`) take
  no real auth; the local admin token is likely a **constant** (`307163591`, from
  a buggy `random()` with a negative max — see
  [`board-behavior-esp8266-lan.md`](board-behavior-esp8266-lan.md) §8 S2).
- **Unsigned OTA** over plain HTTP; the token rides as a multipart field name.

These are the anti-patterns the openpiso-wifi Vendo channel is designed to avoid
(per-Vendo secret, signed requests, sequence-deduped reports) — see the repo's
`ARCHITECTURE.md` "Vendo layer".

## Open items

Runtime-only confirmations noted in the board docs: the constant-token and
unauthenticated-reboot findings (ESP8266-LAN §8 S2/S5), the exact W5x00 variant,
and the hand-decoded ESP8266 ISR (re-decode under a correct IRAM-based import if
instruction-level proof is needed).
