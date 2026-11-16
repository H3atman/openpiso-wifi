# WiFi5-Soft architecture and client-time lifecycle (24.10.4, static)

Read this before designing client-time semantics (pause, resume, idle, extend, top-up) for the controller, or before running spec cases T07–T12 against the `piso-server` VM. It maps how WiFi5-Soft holds a paid session from purchase to expiry. Identity and cookies live in [`wifi5soft-client-identity-spec.md`](wifi5soft-client-identity-spec.md); the Q1 answer (paid time follows the ID) lives in [`hotspot-identity-lab.md`](hotspot-identity-lab.md).

**Authority.** Everything here is **observed static** or **derived** from the full View8 decompilation of all 34 modules (`<local-workdir>/w5s/decomp/native/dec/`, pseudo-source recovered from V8 10.2 bytecode, not original JavaScript). Runtime behavior is **not-run**. The two duration-burn gates were checked against raw bytecode ([Appendix A](#appendix-a-burn-gate-polarity-bytecode)); the monitor cadence was checked in the native addon's machine code. Open questions sit in [§9](#9-unknowns).

**Reading the decompile.** `Scope[1][N]` is a module-level variable; `a0…` are arguments. View8 prints `delete obj[key]` reversed, as `delete key[obj]` (e.g. `delete "duration"[record]` deletes `record.duration`). Anchors are `module:line` in `<module>.native.dec.txt`; `disasm @N` is a bytecode offset in `<local-workdir>/w5s/decomp/native/firewall.disasm.native.txt`.

## 1. Process architecture

One Node 18.17.1 process (`<local-workdir>/w5s/root/soft/index.o`) requires all 33 modules in-process and spawns `nft`, `tc`, `arping` and `ping` as children. A native addon, `soft-<arch>.node`, links libnftables and supplies the nft monitor (index:67).

**The nftables rule table is the session table.** A client is *online* exactly when an accept rule exists whose comment is `<code>-<handle>`. Every accounting decision derives from a 1 s snapshot of those rules.

Boot order (`index` → `httpserver` → `firewall`):

1. `index` creates the 35-directory `/mnt/wifi5/` tree (index:390) and reads `idbase-rule` / `idbase-key`. A custom `idKey` moves the session, client-data and voucher-cookie paths under `/<idKey>/` subdirectories (httpserver:340-370).
2. `httpserver` recovers state: `getAllClientInfo`, then `getSessionList`, which reads every file in `sessionpath` into `sessionList` and keeps a file only when its JSON `code` equals its filename, else unlinks it (httpserver:425-456, 9387-9513). It then reads vendos, vouchers and sales.
3. `firewall.setup(sessionList, uniqueCode, nftable_input)` (index:461 → firewall:1875) stores the parked-session map and installs the **60 s autopause sweep**.
4. `httpserver.start(firewall)` serves `GET /client`, a Socket.IO app whose action names are random per-install aliases from `keys` (keys:10-26), plus `/admin`. `dns-server.start(autologin, getActivate)` (index:522) handles portal DNS.
5. Two 1 s loops start:
   - **JS heartbeat** `setInterval(…, 1000)` (index:608, body index:1126-1132): `pppoe.counter()` (PPPoE uptime +1), `moviewatcher` (drops ad-watch entries older than 120 s), `android_rental_counter()`.
   - **nft monitor** `nftable_monitor_string(cb)` (index:611): a native loop that sleeps `timespec{1,0}` between polls (every `nanosleep` call site in `soft-x64.node` loads `tv_sec=1, tv_nsec=0`). The callback parses rule comments and calls `firewall.counter(snapshot)` (index:613-952). **One `counter()` call ≈ one second**, which is why the ledger counts `duration` in seconds and subtracts 1 per call.
6. `netwatcher` pings the IP in `/mnt/wifi5/netwatcher/config` every 10 s and maintains `global.internet`, restarting the pinger after 10 s on failure (netwatcher:12-99). The same config supplies `global.stoptimer`.

### Module map

Each role is **derived**; the anchor is the evidence it rests on.

| Module | Role | Anchor |
|---|---|---|
| index | boot orchestrator; 1 s heartbeat; nft monitor → `firewall.counter` | index:390, 608, 952 |
| init | `/mnt/wifi5` tree, `idbase` config | init:190, index:23-48 |
| httpserver | portal HTTP + Socket.IO; voucher login, payments, credit, status | httpserver:7986 |
| main | admin socket: vouchers, sessions, credit, `setid`, `pausable` config | main:3680, 9972 |
| firewall | session ledger; nft allow/remove; both accounting loops; idle | firewall:1875, 2568, 2763 |
| dhcp / variables | lease file → `ip_list` / `cIfname` maps → `getmac` (+12 h lookup expiry) | dhcp:19-33, variables:11-15 |
| host | MAC → bridge interface (`getbrIfname`) | host:106 |
| dns-server | portal DNS (`native-dns`, uses dhcp) | dns-server:10-23 |
| dns | DNS helper (`flush`/`check`/`list`); role not traced further | dns:25-39 |
| hostapd | WiFi radio config (`getWireless`, `parseChannels`) | hostapd:18, 247 |
| tc | game-port QoS: nft `inet gaming game-priority` set + tc filters; not per-client shaping | tc:105, 240 |
| fwtools | admin whitelist/blacklist nft sets | fwtools:84, 140 |
| netwatcher | WAN watchdog → `global.internet` | netwatcher:40-59 |
| client-io | license and cloud telemetry (5-day license staleness) | client-io:305-325 |
| reseller | reseller voucher minting into the shared voucher list | reseller:1226-1288 |
| spin-wheel | promo wheel; voucher-type prizes grant time via httpserver | httpserver:4453 |
| onoff | GPIO library; httpserver drives the vendo **relay** pin | httpserver:11173-11177 |
| pppoe / pppoe-server / radius / radius-server / billing-server | separate PPPoE account system | pppoe:37-54 |
| android-rental | separate device-rental ledger (`idle: 60`, own duration) | android-rental:925 |
| system | sysupgrade; eload crypto (`crypto-js`) | system:36-74 |
| tools | shared exec, fs, log and random helpers | tools:16 |
| keys | random socket-event aliases | keys:10-26 |
| global-path | every persistence path, in one object | global-path:8 |
| cron | bundled cron-expression **validator**, not a scheduler | cron:41 |
| remote, ngrok, sys-info, chat, ip-converter | cloud link, tunnel, stats, chat, IP math | remote:9-15, ngrok:31 |

## 2. The session record and its three maps

Firewall holds one record object per paid session and moves it between three maps:

| Map | Key | Holds | Meaning |
|---|---|---|---|
| `Scope[1][9]` *live* | `code` | `tx`/`rx` deltas, `uptime`, `idle`, nft `handle`, `mac`, `address` | an nft rule exists (online) |
| `Scope[1][10]` *attached* | lowercase MAC | the record | the session is bound to the MAC currently using it |
| `Scope[1][14]` *parked* | `code` | the record (shared object with httpserver's `sessionList`) | every known session, including offline ones; the wallet `getVoucher(code)` reads |

The record fields are: `code` (`"UNQCODE"+id`, or `"IX"+MAC`), `type` (`"pausable"` or `"consumable"`), `duration` (seconds; pausable), `expiration` (epoch ms; consumable), `validity` (epoch-ms cap on a pausable voucher), `pauselimit`, `pausetime`, `forceDisconnect`, `oldDuration`, `logindate`, `lastactive`, `start_date`, `address`, `mac`, `upload`/`download` rate limits (default `"2M"`, firewall:3138), `max_data_limit`, `total_upload_data`/`total_download_data`, `notsametype`.

It is persisted as JSON at `/mnt/wifi5/sessionCookie/<code>` (`/mnt/wifi5/session/<idKey>/<code>` with a custom key). `saveSession` writes live records every 60 `counter()` calls (firewall:2628-2632, 2655-2666). Pause, idle, extend and resume paths each write immediately.

**Detach on rule loss.** When a session's rule is missing from a snapshot, `sessionCounter` persists the record, puts it in *parked*, and deletes it from *attached* and *live* (firewall:2948-2956; idle path 2920-2927). An offline session therefore exists only in *parked* and on disk, keyed by `code`. It re-attaches on return through `getVoucher(code)` → `addDataList` (the T07 path), which assigns the record wholesale to `Scope[1][10][mac]` (firewall:366-375).

## 3. Client-time lifecycle

### 3.1 States

| State | Maps | Pausable `duration` | Consumable `expiration` |
|---|---|---|---|
| **Active** | live + attached + parked | −1 per second while WAN is up, or always when `stoptimer` is off | wall clock |
| **Paused (force)**: explicit pause | parked (+ attached until next snapshot) | frozen | wall clock (cannot pause) |
| **Paused (idle)**: idle reap or walked away | parked | frozen if `autopause` off; −60 per minute if on | wall clock |
| **Dead** | none; file unlinked | ≤ 0, `validity` passed, or data limit hit | `expiration` passed, or data limit hit |

The client status shows `"Connected"` when a live session exists and `"Paused"` when a pausable record exists without one (httpserver:7553-7567). It does not distinguish force from idle pauses.

### 3.2 New time

Every source ends in a record; "extend or new" means `ExtendTime` when the MAC has an attached record, otherwise `allowClient`, building `code` as `"UNQCODE"+id` or `"IX"+MAC` (httpserver:4503-4534).

| Source | Path | Effect |
|---|---|---|
| Prepaid voucher | admin (main:9972-10039) or reseller (reseller:1226-1288) mints into `/mnt/wifi5/vouchers/list.json`; client `trylogin` → `allowVoucher` (httpserver:6428-6563) | new record; voucher `type==1` → pausable, else consumable (main:10006-10011) |
| Vendo credit (coins) | coin credit reported per vendo (`lastinsertedvendo`, httpserver:813-843) → `ratecalculator` at the vendo's rate (httpserver:5825-5890) | extend or new |
| Online payment | success handlers (httpserver:2511-2783) | duration product: extend or new; voucher product: mints a voucher |
| Spin-wheel voucher prize | `loginextend` (httpserver:4453-4534) | extend or new |
| Admin grant | main:10123-10222 writes the record directly to `sessionpath + code` | new record |

`notsametype` blocks extending with a different type ("pausable cannot extend via consumable", httpserver:5842-5856). PPPoE/RADIUS and android-rental keep separate ledgers and never touch this one.

### 3.3 Accounting: two loops

1. **`counter(snapshot)`** runs every ≈1 s (firewall:2568-2654) and iterates *attached* records:
   - adds the live `tx`/`rx` to the totals and destroys the session at `max_data_limit` (2590-2606);
   - **pausable**: destroys it when `validity <= now`. Otherwise, if `duration > 0` **and** the code is *live* **and** (`global.internet` or not `global.stoptimer`), `duration -= 1`; at `duration <= 0` it destroys it with "Voucher expired" (2607-2637). Polarity is verified in bytecode (disasm @348 skips the `Dec` at @370 when the code is not live). **Pausable time burns only while online**, and freezes during a WAN outage when `stoptimer` is set;
   - **consumable**: destroys it when `expiration - now <= 1` (2644-2646);
   - finally calls `sessionCounter(snapshot)`, the idle logic in §3.4 (2649-2654).
2. **Autopause sweep**, every 60 s (`setup`, firewall:1883-1968), iterates *parked* records:
   - **pausable**, not live, not `forceDisconnect`, with `global.pausable.autopause` on: `duration -= 60`, then persist; at `≤ 0` it deletes the record and unlinks the file (1899-1959). Polarity is verified in bytecode (disasm @89 and @95 jump past the `SubSmi [60]` at @101 when the code is live or `forceDisconnect` is set);
   - **consumable**, not live, `expiration <= now`: logs "Voucher expired" and deletes the record (1960-1968).

**What `autopause` means in effect**: with it on, a client that *drifts* offline keeps paying at the online rate, and only an *explicit* pause stops the clock. With it off, every pause is free. Config: `global.pausable = {autopause, autolimit}`, loaded from `/mnt/wifi5/pausable-rule` (httpserver:487-504); the admin reads and writes it via `GET`/`SET` (main:3680-3700).

### 3.4 Pause

| Trigger | Mechanism | `forceDisconnect` | Offline burn | Pause-limit effect | How it comes back |
|---|---|---|---|---|---|
| **Explicit**: client logout or admin disconnect | `disconnectClient(mac, address, true, cb, a4)` (firewall:794-882) | set true (819) | never | `a4` true: `pauselimit -= 1`; at `< 0`, converted to consumable (821-853) | explicit resume only: autologin attaches but will not re-admit (firewall:370); `reconn` skips it (httpserver:9746) |
| **Idle**: 120 s of zero traffic and failed arping | `sessionCounter` (firewall:2763-2960) | unchanged (false) | only with `autopause` | with `autolimit`: `pauselimit -= 1`; at `≤ 0`, converted to consumable (2884-2910) | automatic: `reconn` → `allowClientResume` when the device next talks to the portal (httpserver:9746-9750) |

Explicit-pause details:

- **Pausable only.** A consumable record gets "Duration type unable to pause." (879-882).
- **`pausetime` is a cooldown, not a pause timestamp.** A repeat pause within 60 s is refused with "Please try again after mm:ss" (811-814, 859, 872-877).
- **Converting to consumable** sets `expiration = now + duration*1000`, keeps `oldDuration`, and deletes `duration` and `pauselimit`. The client is told "The pause limit has been exceeded, and the duration type has been changed to consumable." From then on the remaining time runs on the wall clock.
- **It logs** "Connection disconnect by user. Remaining duration: …" (firewall:900-932).

Idle details:

- **The idle counter.** Each live session starts with `idle = 120` (2789-2799). A snapshot with `tx === 0` decrements it; any upload traffic resets it to 120 (2838-2840, 2933). **Idle is traffic-based**: 120 consecutive seconds with no upload bytes.
- **Probing.** At `idle` 25 and 12 an `arping -w 10 -c 10 <address>` is spawned, and a "Unicast reply from" resets idle to 120 (2935-2947, 2974-2985). A phone that is asleep but associated survives; a departed one does not.
- **The reap at `idle <= 0`.** It deletes the nft rule by handle, removes tc state, logs "Idle Timeout. Remaining duration: …", persists and detaches (2841-2928). **The remaining duration is preserved**, subject to the autopause sweep.

### 3.5 Resume

- **`allowClientResume`** requires an attached record whose `address` matches. When the record is alive (`duration > 0` or `expiration > now`) it re-adds the nft rule, clears `forceDisconnect`, sets `lastactive` and persists. Otherwise it deletes and unlinks with "Connection expired" (firewall:600-741). A live session answers "Device already active" (607-611).
- **`consumableresume`** is the consumable counterpart, called from a client socket action (httpserver:7277). `reconn` chooses it for consumable records (httpserver:9756).
- **A MAC change** goes through the cookie → `code` → `getVoucher(code)` → `addDataList` path, rebinding `mac` and `address` (httpserver:9229ff; T07). It does not modify `duration` or `expiration`.

### 3.6 Extend

`ExtendTime(amount, mac, address, dataLimit?, addData?, cb)` (firewall:132-197; exported at firewall:58):

- requires an attached record whose `address` matches;
- **pausable**: `duration += amount` (seconds); **otherwise** `expiration += amount * 1000`;
- if `dataLimit` is given, `addData` truthy adds it to `max_data_limit`; otherwise it replaces it;
- writes `sessionpath + code` and answers `cb` with the new values. The reported "pre" values are read after the mutation, so they equal the new values.

Callers: vendo credit, payment success, spin-wheel prize, voucher-code redemption (httpserver:5825-5890, 6034-6098, 2831-2869, 4503-4534). Admin "extend" bypasses `ExtendTime` and mutates records directly (main:10934-11017, 11324-11475).

### 3.7 Expiry and eject

`destroySession(record, mac, reason)` (firewall:2678-2701) ends every death in `counter()`:

1. `removeClientFirewall` deletes the rule whose comment matches `code` (firewall:2084-2245).
2. Its callback deletes the record from *parked*, *live* and *attached*, unlinks the file, and calls `logVoucher`.

The reasons logged are "Voucher expired", "Voucher validation expired" and "Voucher has exceeded its <size> data limit". The sweep's deaths delete and unlink directly; no rule exists for them to remove. Admin kick uses `clientRemove` (main:10673-10743, 11150-11202).

## 4. What the client sees

The status payload is built HTTP-side from `firewall.getInfo` (httpserver:7484-7606):

| Group | Fields |
|---|---|
| Always present | `now`, `mac`, `address`, `credit`, `internet`, `resduration` (credit converted to time at the vendo rate; `"00:00:00"` when credit is zero) |
| When available | `point`, `id`, `interface`, `lastinsertedvendo` |
| Subscription data | `code`, `duration`, `type`, `dlimit`, `ulimit`, `pauselimit`, `validity`, `max_data_limit`, `total_upload_data`, `total_download_data`, `start_date`; plus `expiration` for non-pausable records |
| Live session | `uptime`, `TXRate`, `RXRate`, `download`, `upload`, `status: "Connected"` |
| Pausable record, no live session | `status: "Paused"` |
| No subscription data | `trial` (optional) |

## 5. Timers

| Timer | Cadence | Anchor | Job |
|---|---|---|---|
| nft monitor → `counter()` + `sessionCounter()` | ≈1 s (native `nanosleep{1,0}`) | index:611, 952 | burn, expiry, data limit, idle |
| JS heartbeat | 1 s | index:608, 1126 | PPPoE uptime, ad-watch prune, rental counter |
| autopause sweep | 60 s | firewall:1883 | offline pausable burn, parked consumable expiry |
| live-record save | every 60 `counter()` calls | firewall:2571, 2628 | persist active sessions |
| idle arping | at idle 25 and 12 | firewall:2935 | aliveness probe |
| netwatcher ping | 10 s | netwatcher:51-64 | `global.internet` |
| time-file sync | 10 s | index:98, 129-138 | writes `/mnt/wifi5/time` |
| license check | 30 s | client-io:40, 305-325 | `SysError` on stale license |
| client-io jobs | 10 min and 60 s | client-io:194-195 | cloud / sub-vendo; not traced |

## 6. Boundaries

- **Per-router state.** The ledger, cookies, vouchers and `pausable-rule` are local files with no sync. A cookie decrypts on any WiFi5-Soft box (the AES passphrase is fixed), so it produces the same `code` everywhere, but time bought at site A does not exist at site B. There is no home-site concept.
- **Parallel ledgers.** PPPoE/RADIUS (`pppoe`, `billing-server`, `/mnt/wifi5/pppoe/`) and android-rental keep their own time and never touch the portal ledger.

## 7. Spec implications

- **T07** (paid-time continuity): the burn loops, the pause paths and `ExtendTime` all operate on the code-keyed record. A MAC change rebinds `mac` and `address` only. Static support is complete; the runtime oracles remain not-run. **A T07 run must pin `pausable-rule`**: with `autopause` on, an idle-reaped client keeps burning, which looks like loss but is the configured policy.
- **Idle cases**: the trigger is zero *upload* bytes plus failed arping, not elapsed time since last request. A synthetic client must truly go silent (or leave L2) to trigger it.
- **Explicit pause**: expect `forceDisconnect` to block autologin. Without an explicit resume, the next portal visit shows "Paused".
- **WAN outage**: with `stoptimer` set, live pausable sessions freeze while `global.internet` is false. A characterization case can cover this.

## 8. Design translation for openpiso-wifi

- **Two time types are worth copying.** Pausable (pay for presence) and consumable (wall-clock pass) are a real product split. The **pause-limit → consumable** conversion is a cheap anti-abuse rule: unlimited free parking is impossible.
- **Make the pause policy explicit and per-site.** WiFi5-Soft's `autopause` flag hides a big policy: whether drifting offline costs money. Name both modes plainly in admin UI, persist the choice, and log flips.
- **Separate force-pause from idle-pause on the customer screen.** WiFi5-Soft shows "Paused" for both, yet only one needs a button press to resume.
- **Idle = no traffic + failed ARP probe** is a good, cheap aliveness check. 120 s is aggressive for phones with the screen off; the arping probe softens it. Keep the probe; tune the window.
- **Key the ledger by the token.** WiFi5-Soft keys *attached* records by MAC and only *parked* ones by code, a seam that the `addDataList` rebinding papers over. Key everything by the entitlement token and treat the MAC as a binding.
- **Live state = enforcement state.** Deriving "online" from the firewall rule table (not from HTTP activity) keeps billing and enforcement consistent. Copy this shape with RouterOS `/ip hotspot active`.

## 9. Unknowns

1. **The force-paused autologin branch**: the `else` of the gate at firewall:370 was not traced line by line. The derived outcome (attached, no rule, "Paused") rests on the gate plus `reconn` at httpserver:9746.
2. **The monitor's real period**: one second plus nft list time. Under load the effective `counter()` rate, and so the burn rate, could drop below 1 Hz. That would *under*-charge, since duration falls once per call.
3. **The coin-pulse input path**: GPIO is used for the relay output (httpserver:11173). How coin pulses reach `credit` (local GPIO watch versus networked sub-vendo) was not traced.
4. **Clock discipline**: all timestamps use `Date.now()`. Only the `/mnt/wifi5/time` file sync was observed, no NTP handling.
5. **Parked records past `validity`**: these are not destroyed until re-attached, because `counter()` only iterates *attached* records.
6. **Two `client-io` intervals** (10 min and 60 s): their jobs were not traced.

## Appendix A: burn-gate polarity (bytecode)

These are the two decisions this document leans on most. Both are quoted verbatim from `<local-workdir>/w5s/decomp/native/firewall.disasm.native.txt`. Context slot `[9]` is `Scope[1][9]`, the *live* map.

`counter()`, the live burn. The code is looked up in *live*; when it is absent, `JumpIfToBooleanFalse` at @348 skips to @380, past the `Dec` at @374:

```
11165 S> 0x28d6528e7b78 @  336 : 17 09             LdaImmutableCurrentContextSlot [9]
11189 E> 0x28d6528e7b7d @  341 : 2d f6 0a 1c       GetNamedProperty r4, [10], [28]
11183 E> 0x28d6528e7b81 @  345 : 2f f4 45          GetKeyedProperty r6, [69]
         0x28d6528e7b84 @  348 : 97 20             JumpIfToBooleanFalse [32] (0x28d6528e7ba4 @ 380)
...
11261 S> 0x28d6528e7b9a @  370 : 2d f6 16 42       GetNamedProperty r4, [22], [66]
         0x28d6528e7b9e @  374 : 52 4d             Dec [77]
11274 E> 0x28d6528e7ba0 @  376 : 32 f6 16 4e       SetNamedProperty r4, [22], [78]
```

The 60 s sweep, the offline burn. When the code is live (@89) or `forceDisconnect` is set (@95), it jumps to @552, past the `SubSmi [60]` at @101:

```
22738 S> 0x28d6528ebe6d @   77 : 17 09             LdaImmutableCurrentContextSlot [9]
22763 E> 0x28d6528ebe72 @   82 : 2d f7 06 0c       GetNamedProperty r3, [6], [12]
22757 E> 0x28d6528ebe76 @   86 : 2f ed 0e          GetKeyedProperty r13, [14]
         0x28d6528ebe79 @   89 : 94 28             JumpIfToBooleanTrueConstant [40] (0x28d6528ec048 @ 552)
22778 E> 0x28d6528ebe7b @   91 : 2d f7 07 10       GetNamedProperty r3, [7], [16]
         0x28d6528ebe7f @   95 : 94 29             JumpIfToBooleanTrueConstant [41] (0x28d6528ec048 @ 552)
22883 S> 0x28d6528ebe81 @   97 : 2d f7 08 12       GetNamedProperty r3, [8], [18]
         0x28d6528ebe85 @  101 : 46 3c 14          SubSmi [60], [20]
```

Native monitor cadence: every `nanosleep` call site in `<local-workdir>/w5s/root/soft/build/soft-x64.node` (at 0xde0a, 0xdea7, 0x127d7 and 0x13986) is preceded by `movq $0x1, tv_sec` and `movq $0x0, tv_nsec`.
