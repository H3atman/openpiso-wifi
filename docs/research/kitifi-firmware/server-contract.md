# Server-side vendo protocol contract (what board firmware must implement)

Source: `kitifi-controller-handoff.md` (reverse-engineered from the decrypted PHP tree at
`/tmp/kitifi-dec/admin/api/portal/` and `kitifi.sql`). This is the controller
half of the protocol; the board half is confirmed against firmware strings
(`esp32-strings-inventory.md`, `esp8266-strings-inventory.md`).

## Endpoint summary (device-facing, on the controller)

| Endpoint | Params | Auth | Semantics |
|---|---|---|---|
| `GET /admin/api/portal/vendo_handler.php` | `host` (board MAC), `token`, `version` | shared static token | Registration + heartbeat. Upserts `tbl_vendo` by MAC, refreshes `last_seen`, records `board`/`version`. |
| `GET /admin/api/portal/stay_active.php` | — | (session) | Keep hotspot session alive. |
| `GET /admin/api/portal/coin_handler.php` | `id` (user), `token` | shared static token | **Absolute set** of `tbl_user.coins` for caller-supplied `id`. No MikroTik call. |
| `GET /admin/api/portal/pulse_handler.php` | `mac`, `token`, `version` | shared static token | Coin-drop signal for `type='pulse'` vendos: logs event, bumps `cs_date_active`. No billing. |
| `GET /admin/api/portal/insert_coin_handler.php` | (user id, vendo id) | session/token | **Phase 1** of insert session (see state machine). |
| `GET /admin/api/portal/done_insert_coin_handler.php` | (session) | session/token | **Phase 2**: bills the collected coins. |
| `POST /admin/api/portal/index.php` action=`addcoins` | node + coin data | **per-board** `tbl_vendo.token` | Generic node coin action (the only per-board-token endpoint). |

Shared static token: `<redacted>` (= `masterKey[31:47]`), sent as a **GET**
param — it lands in access logs. Confirmed embedded verbatim in all board
firmwares analyzed here.

## Server → board callback

During phase 1 the controller calls the board back at
`http://<board-ip>/insertCoin` (route confirmed in firmware strings) to enable
the coin slot; `/cancelInsertCoin` aborts. The board's address comes from the
`tbl_vendo.ip` maintained by heartbeats.

## Two-phase insert-coin session (state on `tbl_vendo`)

1. **Start** (`insert_coin_handler.php`): resolve node by `type='coins'` +
   hotspot server, check `activation` (license seat), call board `/insertCoin`,
   set `cs_user` = paying user, `cs_active=1`.
2. **Coins drop** → board counts pulses/coins locally; the running total is
   pushed to the server via `coin_handler.php` (`id`=user) so the portal can
   display it.
3. **Finish** (board signals done → `done_insert_coin_handler.php`):
   `calculateAddTime()` maps coins→time/points, `mtik_add_time()` bumps the
   RouterOS `limit-uptime` and re-logs the client in, zeroes `tbl_user.coins`,
   adds points/pause-limit, writes `tbl_sales` + `sales_report`, increments
   vendo sales counters, clears `cs_user`/`cs_active`.

   > **Correction (firmware static analysis):** the firmware does not signal
   > "done". Its 45 s session timer is re-armed on every coin; on timeout it
   > zeroes its total and sends no HTTP call. See `SUMMARY.md:74-86` and
   > `docs/research/esp8266-lan-juanfi-qr-parity-and-controller-design.md` (1e).

## Billing model

`calculateAddTime($coin,$id)` (`kitifi.php:24-73`) walks the vendo's `rates`
JSON **greedily** (largest `amount` ≤ remaining, repeat). Default tiers:
₱1→12min, ₱5→90min+4pts, ₱10→4h+9pts, ₱20→10h+18pts, ₱30→24h+25pts.
**The board does not hold rates** — it only counts coins and reports; the server
is the sole billing authority. Remainders that fit no tier are discarded.

## Coin acceptor hardware types

- `type='coins'` — acceptor with a credit/coin line (ICT-style pulse or serial):
  counted on the board, reported via `coin_handler`.
- `type='pulse'` — dumb pulse output: every pulse is a `pulse_handler` GET.
  `tbl_vendo.type` selects which handler the server expects.

## Licensing / activation

`tbl_vendo.activation` is the per-vendo license seat. The main license JSON's
`credits` integer caps total activated vendos. Boards on inactive vendos get
rejected in phase 1 (`insert_coin_handler` checks activation).

## Timing assumptions

- `last_seen` refreshed on every `vendo_handler` heartbeat; no explicit timeout
  enforcement found server-side beyond display staleness.
- No idempotency guard on phase 2: a stale completion can pay the wrong wallet
  (known server-side race, handoff item 13).
