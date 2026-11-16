# KiTifi v2.2 — Reverse-Engineering Handoff

Reverse-engineered from the appliance image. Focus: (1) how the box controls the attached MikroTik router, (2) how it manages vendos/subvendos, (3) the lapses worth fixing. Every claim cites `file:line` so you can jump straight to the code.

## Where everything lives

| What | Path |
|---|---|
| Appliance image (rootfs, configs, SQL, shell scripts) | `<local-workdir>/kitifi-image/rootfs/` |
| Decrypted PHP tree (the real application) | `/tmp/kitifi-dec/` — **volatile**, wiped on reboot |
| REA findings | `kitifi-image/findings/findings.jsonl` |
| Core library (all MikroTik + license + crypto code) | `/tmp/kitifi-dec/admin/lib/kitifi.php` |
| MySQL schema + seed data | `rootfs/root/kitifi.sql` |
| Transport credentials | `rootfs/etc/rest_api.env` |

Re-deriving the decrypted tree if `/tmp` is gone: the shipped PHP is AES-256-CBC encrypted per file; each stub reads the key from `/dev/shm/.phpkey.cache`, which `rc.local:26` populates by running `/usr/bin/phpkey` (gate: `/etc/model` must contain `signed-by:kitifi_v2`; the 60-char key is embedded in the ELF, XOR-0x5A-obfuscated, written via overlapping `movabs` constants — decode by applying the writes in order, not sequentially). Key = `sha256(trim(key))`, IV = first 16 bytes of each file's base64 blob. `db_conn.php` uses the same 60-char string as MySQL root password.

## Mental model

An Armbian SBC runs LAMP + Redis. **The MikroTik does all actual traffic enforcement** (hotspot auth, uptime limits, profiles); the box is the brain: billing, vouchers, vendos, admin UI. Client devices hit the MikroTik hotspot → captive portal served from the MikroTik itself (HTML pushed there by the box) → portal PHP on the box does identity/billing and drives the router. Vendos are ESP8266 coin-acceptor boards (`tbl_vendo.board` default `esp8266`) that call HTTP endpoints on the box when coins drop.

Everything is dual-transport: `/etc/rest_api.env` `API_TYPE=rest_api|ros_api` selects RouterOS v7 REST vs legacy binary API for `mikrotikCurlRequest()`; SSH is always available regardless of that setting.

## Runtime decryption (read this first)

Every admin/portal PHP file ships as a stub that reads `/dev/shm/.phpkey.cache`, decrypts the remainder of itself (AES-256-CBC), and `eval("?>".$pt)`s it. Consequences you'll hit constantly:

- Eval'd code is **never opcache-cached** → every request pays full decrypt+parse. This is the #1 performance finding.
- Stack traces, line numbers, and debuggers see the stub, not the logic. Use the decrypted tree in `/tmp/kitifi-dec/` for reading.
- Any patch applied to `/var/www/html` gets overwritten by OTA (`ota_update.sh` unzips with the master key as password and deploys over the tree).

## MikroTik control layer

### Transports (4 ways the box talks to the router)

1. **REST (RouterOS v7)** — `mikrotikCurlRequest()` `kitifi.php:549-623`. `http://<gw>/rest<endpoint>` + Basic auth, static keep-alive curl handle, hard per-call deadline (`API_DEADLINE`, default 8s) with the "HANG-FIX" timeout set. TLS verification is disabled outright (`kitifi.php:604-605`).
2. **ROS API (legacy TCP 8728)** — same function, `kitifi.php:625-725`: hand-rolled sentence protocol (`_rosApi_*` helpers `kitifi.php:728-806`), persistent socket, login-once. Query operators are encoded inline in `.query` arrays as `#!` (AND), `#|` (OR), `#|||`/`#||` (NOT) — see usage at `mt_config_updater.php:12-13`. Replies parsed into rows (`!re`), traps into `['error'=>...]`.
3. **SSH (libssh2)** — `mtik_ssh_exec()` `kitifi.php:1520-1554`. Fresh connection+auth **per call** (no pooling). Used by all `mtik_*` user mutators, which push hand-built RouterOS one-liner scripts.
4. **Pushed artifacts** — the router also runs box-authored content: portal HTML files fetched onto the router via `/tool/fetch` (`home.php:98-107`), and the hotspot profile's `on-login`/`on-logout` scripts (`mt_config_updater.php:54-63`, `home.php:60-71`) — see below.

Config: `rootfs/etc/rest_api.env` (gateway defaults `192.168.88.1`, user `admin`, plaintext password, port/proto). Note `mtik_ssh_exec` reads the **same** REST creds for SSH.

### The architectural split worth knowing

Reads and config writes go over REST/ROS-API (`mikrotikCurlRequest`), but **all hotspot-user mutations are SSH one-liners** (`mtik_*`), because they need multi-step atomicity (find → modify → kick active → re-login) that a single REST call can't express. This split is why the box opens SSH to the router with admin credentials even in REST mode.

### User lifecycle on the router (hotspot)

Model: `user` = client MAC without colons. Time budget = RouterOS `limit-uptime` on the hotspot user. Expiry/status = the user entry's `comment="expiry|status"`. The router is the source of truth for remaining time; `tbl_user` on the box mirrors it.

| Operation | Where | Mechanism |
|---|---|---|
| Create user | `mtik_add_user` `kitifi.php:1556` | SSH `/ip hotspot user add name=… password=… limit-uptime=… comment=…` |
| Delete user | `mtik_delete_user` `kitifi.php:1586` | SSH script: find user + active session, remove both; returns sentinel strings `DEL_OK` / `DEL_NONE` / `ERR` that PHP string-matches |
| Update | `mtik_update_user` `kitifi.php:1646` | REST `user/set` |
| Add paid time | `mtik_add_time` `kitifi.php:1706` | **The core billing op.** Single SSH script: read old `limit-uptime`, concatenate (`$old + $add`), `set` new limit+password+profile, remove any active session, then `/ip hotspot active login` the client back in under its MAC. Distinguishes EXTENDED (existing user) vs NEW. |
| Random-MAC repair | `mtik_fix_active` `kitifi.php:1822` | SSH script: find active session for user with `mac-address != current`, capture old MAC, remove it, re-login under the new MAC. Emits `REMOVED`/`LOGIN_FAIL`/`ERR`. |
| Login by MAC | `mtik_login_user_mac` `kitifi.php:1678` | SSH `active login` — currently dead code (callers commented out) |

Self-cleaning on the router: the hotspot profile's `on-logout` script (installed at `mt_config_updater.php:55`, `home.php:60-71`) compares the user's accrued `uptime` against `limit-uptime` and **removes the hotspot user outright** when exceeded — the router garbage-collects its own user table.

Client-side identity resolution and random-MAC detection live in the portal (`/tmp/kitifi-dec/index.php:150-200` Redis `user_map_mac:<mac>` → `tbl_user.mac` JSON array scan → auto-create). Detection of a changed MAC is **cookie-dependent** (`$_COOKIE['randomMac']`, `status`+`activeMac` mismatch, `index.php:397-437`) — see lapses.

### Background workers

| Worker | Cadence | What it does |
|---|---|---|
| `hotspot-updater` systemd unit | continuous | `hotspot_updater.sh` branches on `API_TYPE`: REST → curl loop polling the router every 1s; ROS → `exec php mt_json.php`, a persistent worker doing 3 prints (active/user/host) over one connection (`mt_json.php:25-45`). Feeds Redis keys `active`, `user`, `host_hotspot_data`. |
| `hotspot_expired.php` | cron, every minute | `tbl_user` rows with `expiry < now` → zero the row, `mtik_delete_user()`, drop Redis key (`hotspot_expired.php:35-56`). |
| `pppoe_expired.php` | cron, hourly | `/ppp/secret/print`, parse `comment="expiry|orig_profile"`, secrets expired >5 days → demote profile to `expired_profile` (`pppoe_expired.php:20-32`). Grace period before demotion. |
| `mt_config_updater.php` | cron, every minute | Re-asserts kitifi.wifi DNS entry, walled-garden for the box IP, firewall allow, and the profile on-login/on-logout scripts — self-healing router config. |
| `license_checker.php` | cron, */10 + boot | Cloud license check — see Licensing. |

Provisioning one-shot: `applyprerequisites` (`home.php:26-107`) pushes all portal HTML onto the router's hotspot html directory via `/tool/fetch`, sets `login-by=http-pap,mac-cookie`, installs on-logout, adds walled-garden + firewall entries, restarts hotspot-updater.

## Vendo / subvendo management

### Data model (`kitifi.sql`)

- **`tbl_vendo`** (`kitifi.sql:176-203`) — one row per physical vendo board: `type` (coins/pulse), `board` (esp8266), `ip`/`mac`, `last_seen`, sales counters, and the two JSON rule tables:
  - `rates` — coin→time tiers: `[{"id","pause_limit","amount","time","expiry","points"}, …]` (default: ₱1→12min, ₱5→90min+4pts, ₱10→4h+9pts, ₱20→10h+18pts, ₱30→24h+25pts)
  - `points` — loyalty points→time tiers `[{"id","pause_limit","points","time","expiry"}, …]`
  - `activation` (0/1) — **this is the per-vendo license seat**; `token` — board auth credential.
- **`tbl_voucher`** (`kitifi.sql:211-226`) — voucher codes with `seller_id` FK: vouchers belong to sellers.
- **`tbl_user`** (`kitifi.sql:146-166`) — per-client billing mirror: `user` (MAC), `coins`, `points`, `expiry`, `mac` (JSON array of known MACs), `pause_limit`.

### Coin event → time on the router (two-phase insert session)

The device-facing handlers live in `/tmp/kitifi-dec/admin/api/portal/` — **not** `/tmp/kitifi-dec/portal/`, which contains only redirect stubs. Three of them authenticate the board with one shared static token, compared against a GET param: `<redacted>` (= masterKey[31:47]).

- `vendo_handler.php:10-15` — node self-registration/presence: upsert `tbl_vendo` by MAC, refresh `last_seen`/board/version. No billing.
- `coin_handler.php:3-20` — sets `tbl_user.coins` for a caller-supplied `id` (absolute set, not increment; MAC only checked non-empty, never bound to that user). No MikroTik call.
- `pulse_handler.php:5-34` — coin-drop signal: logs the event and bumps `cs_date_active`. No balance or router change.

Actual billing is a two-phase session:

1. **Start** — `insert_coin_handler.php` resolves the node (`type='coins'` + hotspot server), checks activation, tells the board's `/insertCoin` URL to accept coins, and marks `cs_user`/`cs_active=1` on the vendo row.
2. **Finish** — `done_insert_coin_handler.php:55-125` runs `calculateAddTime()` → `mtik_add_time()` (the SSH one-liner that bumps `limit-uptime` and re-logs the client in), zeroes the user's coins, adds points/pause-limit, writes `tbl_sales` + `sales_report`, increments vendo sales counters, and clears the session state.

`calculateAddTime($coin,$id)` (`kitifi.php:24-73`) walks the `rates` tiers greedily — repeatedly the largest `amount` ≤ remaining coins — summing `time` and `points`; expiry/pause-limit come from the highest denomination used. Worked example (default rates): **37 coins → 30+5+1+1 → 93,240s (25h54m) + 29 points**. Remainders that fit no tier are discarded; there's no optimization across alternative combinations.

A generic node action `addcoins` also exists in `admin/api/portal/index.php:375-407`, which — unlike the three above — compares against the node row's own `token` column.

### Sub-licensing = the `credits` integer; a "seller" is just a vendo row

There is no separate per-vendo license mechanism and no seller account system. A **seller is a `tbl_vendo` row with `type='voucher'`** (`addseller` inserts it with `activation=1` and `token=0`, `home.php:2137-2172`); "seller" screens are admin pages (`home=seller`, `menu.php:286`), vouchers reference sellers via `tbl_voucher.seller_id`, and sales attribution flows through `tbl_sales.seller_id` / `sales_report (seller_id,date)`. The main license JSON carries `credits`; `home.php:18` reads it, and:

- Add/gate counting: `$total_vendo` counts **every** activated `tbl_vendo` row, `type`-agnostic (`home.php:626`).
- Activate/add gates: `if ($mt_credits <= $total_vendo) → "Not enough credits!"` at `home.php:1716-1717` (activatevendo) and `home.php:2142-2143` (addseller).
- Auto-demote on load: `home.php:1333-1343` — if `credits < $total_vendo`, page load runs `UPDATE tbl_vendo SET activation='0' WHERE activation='1' LIMIT $credit_remain` — **no `type` filter, no ORDER BY**: it can deactivate voucher sellers, not just coin nodes, and which rows lose their seat is up to the storage engine.
- `deactivatevendo` (`home.php:1729-1753`) deletes the vendo's `tbl_sales` rows but not `sales_report` or vouchers; `deletevendo` (`home.php:2044-2057`) drops only the `tbl_vendo` row — sales/voucher history outlives the node (no FKs anywhere).

## Licensing (context for credits)

Local check only: `is_license_valid()` `kitifi.php:1230-1370` — rebuilds `json_encode([license_type, mac, serial, license, credits, expiry, device_model, device_arch, email])`, compares `hash_hmac('sha256', $unsigned, SECRET_KEY)` with `hash_equals` against the file's `signature`. `SECRET_KEY` is a compile-time constant **hardcoded in the image** (`kitifi.php:1233`) — the same 60-char master string as the PHP-encryption key and MySQL root password. Cloud activation (`license_checker.php` → `api.kitifi.com/cloud_activate.php`) is the issuer/revoker: a cloud `status:false` deletes the license file; anything else (including network failure) leaves it in place. Admin UI gates on the result (`admin/index.php:65`); the client portal does not.

## Lapses and improvements

### Security

1. **License forgeable by design** — hardcoded HMAC key (`kitifi.php:1233`) ships in every image ⇒ anyone with the image can mint a passing license with arbitrary `credits`/`expiry`. Fix: Ed25519 signature, private key only on the issuance server; verify with public key on-box.
2. **One master secret, five jobs** — PHP encryption, MySQL root password, OTA zip password, vendo token slice, HMAC key are all the same string. Leak once, pwn everything. Fix: derive independent per-purpose keys (HKDF) or separate secrets.
3. **`/dev/shm/.phpkey.cache` mode 644** (`rc.local:27`) — any local user/process can read the master key from RAM.
4. **TLS verification disabled** (`kitifi.php:604-605`) and REST creds plaintext in `/etc/rest_api.env`.
5. **Vendo endpoints authenticated by a static shared token** (`<redacted>`, a slice of the master key) over **GET** — it lands in access logs and router logs (`vendo_handler.php:10-15`, `coin_handler.php:3-9`, `pulse_handler.php:5-11`). Per-board tokens exist in the schema (`tbl_vendo.token`, used only by the `addcoins` action at `admin/api/portal/index.php:375-407`) but the three device handlers don't use them.
6. **SQL injection surface in admin actions** — `addseller`/`editseller` (`home.php:2146-2151,2164`), `editvendo` (`:1882`), `deletevendo` (`:2051`) interpolate request values directly into SQL strings. Other handlers use prepared statements; these don't.
7. **Admin default `admin/admin`** (tty1 banner `rc.local:8-19`, `admin/api/login.php:53`), password storage via trivial additive cipher (`encrypt()`/`decrypt()`, `kitifi.php` — `ord($c)+ord($k)` then base64; seeded value `mZWfoZ8=` decodes to `admin`), no rate limiting on login.
8. **Broad sudoers for www-data** (`rootfs/etc/sudoers.d/kitifi`): NOPASSWD systemctl/reboot/shutdown/script/* — web RCE is instantly root RCE.
9. **Router credential reuse** — SSH uses the REST admin creds (`kitifi.php:1522-1524`); the router account is full admin.
10. **Auto-demote `UPDATE … LIMIT` without type filter or ORDER BY** (`home.php:1333-1343`) — can strip activation from voucher sellers arbitrarily and nondeterministically.
11. **Vendo table missing the indexes its queries need** — no `server`, `activation`, or `type` index on `tbl_vendo` (server-based node resolution scans); `tbl_voucher.code` is neither indexed nor unique despite redemption lookups (`kitifi.sql:275-287`).

### Correctness / race conditions

12. **`mtik_add_time` read-modify-write is not concurrency-safe** — two coin drops arriving together both read the same old `limit-uptime`; the second `set` can overwrite the first's added time. The concatenation happens inside one RouterOS script, but two concurrent SSH scripts still race. Same race between a coin drop and `mtik_update_user` via REST.
13. **Insert session is check-then-act, not atomic** — `insert_coin_handler.php:91-97,134-140,192-205` reads `cs_active`, later writes it back; concurrent requests can both pass the check and claim the machine. And `done_insert_coin_handler.php:9-16` applies MikroTik time **without verifying the paying user is still the node's `cs_user`** — no idempotency guard, so a stale completion pays the wrong wallet.
14. **`coin_handler.php:11-20` trusts caller-supplied user `id`** — MAC is only checked non-empty, never bound to the target user; the coin balance is set (absolute, not incremented) on whatever `id` the request names. This endpoint, on the shared static token, is a coin-balance write primitive.
15. **on-logout self-delete races payment** — the router deletes the hotspot user when `uptime >= limit-uptime` (`mt_config_updater.php:55`); a coin drop landing after logout-but-before-delete recreates via the NEW branch, but one landing mid-window can pay a user that the script then removes.
16. **Random-MAC time loss is cookie-dependent** — detection relies on `$_COOKIE['randomMac']` / `status`+`activeMac` (`index.php:397-437`); clear cookies + new MAC ⇒ lookup falls through to a fresh user, old time stranded. Also, uptime accrues against the old session until `mtik_fix_active` runs.

### Performance (the "slow admin UI" causes, ordered by impact)

17. **Eval tax** — encrypted-stub `eval()` defeats opcache entirely (see Runtime decryption). Every page re-decrypts ~100KB+ of PHP. Fix options: opcache-friendly loader (decrypt to real files at boot with correct mtime), or ship plaintext behind ACLs.
18. **Cache bypass** — `hotspot.php:1705+` member tables have Redis reads **commented out**, replaced by live `/ip/hotspot/user/print` + `active/print` + `ssh_get_hotspot_hosts()` per page render — the worker already maintains exactly those Redis keys.
19. **Pagination in PHP, not SQL** — `SELECT *` (no LIMIT) + `array_slice($data,$start,$len)` (`hotspot.php:1843`). With N thousand users every table view hauls everything.
20. **O(n²) row loops** — `in_array($name, array_column($active,'user'))` inside `foreach` (`hotspot.php` member/voucher tables). Flip to a keyed map first.
21. **Fresh SSH per call** — `mtik_ssh_exec` connects+auths every time (`kitifi.php:1527`), and `ssh_get_hotspot_hosts` (`kitifi.php:2013`) does so per page load. Pool it like the REST/ROS handles already are, or move to ROS API entirely.
22. **mpm_prefork + MaxRequestWorkers 150** on a small SBC with PHP under Apache; opcache.jit off. Schema gaps compound it: `tbl_vendo` without `server`/`activation`/`type` indexes, `tbl_voucher.code` unindexed.

## Quick-win patch order

1. Re-enable the Redis reads in `hotspot.php` member tables (code is already there, commented).
2. Keyed-map lookups replacing `in_array(array_column())` loops.
3. SQL LIMIT pagination; drop `array_slice`.
4. Cache or pool `ssh_get_hotspot_hosts()`.
5. Boot-time decrypt-to-file to make opcache effective (biggest single win, medium effort).

## Open items for a rebuild

- Single transport (ROS API or REST) with one pooled client; SSH only for provisioning.
- Per-device secrets; asymmetric license verification; actual on-box expiry enforcement.
- Redis as the read model for all router state (the worker already exists).
- Vendo auth per-board (unique random tokens — the `tbl_vendo.token` column already exists and one code path already uses it; extend it to all handlers, move off GET).
- The insert-coin session as a real atomic state machine (`UPDATE … WHERE cs_active=0` compare-and-set instead of check-then-act), with completion verifying `cs_user`.
- Indexes to match access patterns: `tbl_vendo(server,activation,type)`, unique `tbl_voucher.code`.

---
*Sources: decrypted PHP (`/tmp/kitifi-dec/`), image rootfs + `kitifi.sql`, crontab, rc.local. Subvendo subsystem mapped by Scout recon (handler-by-handler, schema-index-verified).*
