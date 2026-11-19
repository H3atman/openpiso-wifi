# MikroTik Piso-WiFi Controller: Research and Recommendation

**Snapshot:** 2026-10-16. Read-only research from primary sources. The open-piso-wifi repo was cloned and read at revision [`24f1ab0`](https://github.com/haliknihudas666/open-piso-wifi/tree/24f1ab078cea247343504416360d1d2f76ff13eb), but its code was not run. Nothing here was tested on a router. Claims marked **unverified** need a lab test.

## Decisions since this research

Terms follow [`GLOSSARY.md`](../../GLOSSARY.md). Where the text below disagrees with this list, this list wins; superseded passages are marked inline.

- **MikroTik at every site; no SBC-only/nftables mode.** The Controller is off the traffic path, on a management port/VLAN, and reachable by unpaid Devices only via the walled garden. [ADR 0001](../adr/0001-mikrotik-enforces-controller-off-path.md)
- **Go, not Rust.** One static binary for armv7 + arm64. Hardware floor is an Orange Pi Zero 256 MB (also Orange Pi One/PC, RPi 3/4), target under ~64 MB RSS and SQLite WAL. The installer configures a host-wide persistent journal capped at 32M with one-minute batched writes. [ADR 0002](../adr/0002-controller-in-go.md)
- **The Router is the only meter of used time.** The ledger records time bought; the Controller sets an absolute limit on Top-up and reconciles from the Router after outages. [ADR 0003](../adr/0003-router-is-sole-meter.md)
- **Every coin unit is an ESP8266 Vendo** on the network (no Controller GPIO coin slot). Coins credit through a Coin Window with sequence-numbered idempotent reports, per-Vendo secrets, and signed reports. ESP hardware details are still in progress.
- **Pricing:** pausable used time (`limit-uptime`); rate tiers apply to the Coin Window total; Vouchers carry time (8-char unambiguous codes, site-scoped Voucher Batches, atomic redemption). Other pricing models: [issue #3](https://github.com/H3atman/openpiso-wifi/issues/3).
- **Pauses:** the Pause Limit counts only customer-tapped Pauses and resets on Top-up. Idle Pause is a separate site setting and never counts.
- **One Device per Entitlement;** newest wins via Rebind. Ownership proof is a Controller-signed token cookie, with a rate-limited Resume Code fallback. No customer accounts in the MVP.
- **Validity:** remaining time is forfeited after the site-set period from the last Top-up (default 30 days). Stored as whole seconds; the admin form accepts a whole number of seconds, minutes, hours or days.
- **Router integration:** RouterOS v7 only, REST over HTTPS. The Controller provisions the Router baseline idempotently and re-checks on boot. The portal lives on the Controller. Logouts are detected by polling the active list (~10 s), with no on-logout scripts. Admins can only grant time (no mid-session limit lowering).
- **MVP scope:** vouchers first (portal, REST login, Pause/resume, Rebind), then coins. **Deferred:** PPPoE/RADIUS, cloud sync, cross-site balances, multi-admin.
- **Admin and UI:** local web admin on the Controller (single install-time password, management network only, rate-limited). Portal and admin are Go `html/template`, server-rendered, minimal vanilla JS, no JS build step.
- **Testing:** CHR lab (one CHR Router, one CHR client) with the Controller native on a laptop, an armv7 VM capped at 256 MB, a simulated Vendo, and an E2E scenario that produces a repeatable artifact.
- **Install:** Go binary + systemd unit via install script on stock Armbian / Raspberry Pi OS Lite. Custom images later.

## TL;DR

- **open-piso-wifi is a reference, not a base.** It enforces access with Linux/OpenWrt `nftables`. Its controller modes are OpenWrt SSH, local Linux, and simulation. Its README lists MikroTik as "seeking test reports". [README L20-26](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/README.md#L20-L26), [router-controller.ts L8-27](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/daemon/services/router-controller.ts#L8-L27), [README L502-508](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/README.md#L502-L508)
- **KiTifi's architecture is the right model:** an SBC owns billing and MikroTik enforces access. Its code isn't worth copying, though: it has races, a shared token, and broad SSH credentials. [`kitifi-controller-handoff.md` L18-45, L117-139](kitifi-controller-handoff.md)
- **Let RouterOS enforce sessions; keep the ledger local.** Phase 1 should use local HotSpot users with `limit-uptime` and byte limits, provisioned over REST/HTTPS. No RADIUS, no cloud. **Decided:** the Router is the only meter of used time ([ADR 0003](../adr/0003-router-is-sole-meter.md)). [HotSpot](https://help.mikrotik.com/docs/display/ROS/Hotspot+-+Captive+portal), [REST API](https://help.mikrotik.com/docs/display/ROS/REST+API)
- **Don't write your own RADIUS server for the MVP.** Use FreeRADIUS with SQL + `rlm_sqlcounter` once PPPoE arrives (**deferred**; PPPoE/RADIUS is out of the MVP). If you must stay in Rust, `radius-tokio` 0.1.0 (May 2026) is the only crate that fits on paper. It is pre-1.0 and unaudited. [FreeRADIUS sqlcounter](https://www.freeradius.org/documentation/freeradius-server/4.0.0/reference/raddb/mods-available/sqlcounter.html), [radius-tokio](https://github.com/ogital-net/radius-tokio)
- **Rust works, but speed is not a reason to pick it.** *Superseded: Go was chosen, see [ADR 0002](../adr/0002-controller-in-go.md) and section 8.* The workload is I/O plus correctness: router API calls, SQLite, GPIO. Pick Rust for one static ARM binary and strict types. Watch licenses: `mikrotik-rs` 0.9.0 is **AGPL-3.0-only**. Calling REST through `reqwest` avoids that dependency. [mikrotik-rs](https://github.com/ferrohd/mikrotik-rs), [reqwest](https://docs.rs/reqwest/latest/reqwest/)
- **A Cloudflare Worker cannot be the RADIUS server.** Workers have no UDP listener and no arbitrary inbound TCP. Spectrum does UDP, but it is an Enterprise add-on and only a proxy. [Workers TCP sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/), [Spectrum](https://developers.cloudflare.com/spectrum/)
- **Cloud = mirror plus remote admin, never in the payment path.** (Cloud sync is **deferred** past the MVP.) Use HTTPS push from a durable local outbox to a Worker with D1. 50 sites with a 30 s heartbeat is ≈4.38M requests/month. That is over the Free plan but inside the $5 Paid plan. Batching brings it down a lot. [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/)
- **Don't run the controller in a RouterOS container by default.** Enabling it needs device-mode with physical confirmation, and MikroTik recommends fast external storage. A separate SBC is simpler, and is now the decided design ([ADR 0001](../adr/0001-mikrotik-enforces-controller-off-path.md)). [Container](https://help.mikrotik.com/docs/display/ROS/Container)

## 1. open-piso-wifi compared with the repo material

### What open-piso-wifi is

- **Stack:** Next.js/React portal, Node/TypeScript daemon (Express, GPIO, SSH to OpenWrt), SQLite via Prisma, optional Supabase sync. MIT license. The repo was created 2026-10-13, so there is no maintenance track record yet. [portal package.json](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/portal/package.json#L1-L24), [daemon package.json](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/daemon/package.json#L1-L25), [LICENSE](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/LICENSE), [repo metadata](https://api.github.com/repos/haliknihudas666/open-piso-wifi)
- **Enforcement:** `nftables` sets with kernel timeouts on a Pi gateway or OpenWrt router. With MikroTik, the MikroTik only works as a dumb AP/bridge. [README L20-26](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/README.md#L20-L26), [MULTI_ARCHITECTURE_GUIDE L31-40](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/docs/MULTI_ARCHITECTURE_GUIDE.md#L31-L40)
- **Vouchers:** the code reads a voucher, checks `isUsed`, then updates it in a separate call. Redemption is not atomic. [connect/route.ts L80-116](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/portal/app/api/connect/route.ts#L80-L116)
- **Coins and sessions:** pulses are converted to minutes. The code writes a sale and session to SQLite, then asks the daemon to authorize the MAC. These are two separate state changes. [connect/route.ts L379-502](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/portal/app/api/connect/route.ts#L379-L502)
- **Cloud sync:** the offline queue is an in-memory array capped at 150 entries, so a restart loses it. [supabase-sync.ts L37-44, L122-143](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/daemon/services/supabase-sync.ts#L37-L44)
- **Security smell:** the satellite daemon ships a default shared secret in its source. [daemon/index.ts L38-67](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/daemon/index.ts#L38-L67)

**Reuse:** the portal UX, coin pulse/inhibit pattern, voucher data model ideas, and ESP32 satellite concept. **Don't reuse:** the router adapter, the non-atomic redemption, or the RAM queue.

### KiTifi and WiFi5-Soft

- **KiTifi** is the closest architecture. An Armbian SBC handles billing and vouchers, and MikroTik enforces HotSpot. It talks to the router over REST, the binary API, SSH, and router scripts. Its handoff documents non-atomic coin sessions, read-modify-write races on paid time, and a completion handler that doesn't re-check the payer. Use it as a list of what to avoid. [`kitifi-controller-handoff.md` L18-45, L76-100, L134-139](kitifi-controller-handoff.md)
- **WiFi5-Soft** is a static reverse-engineering spec, not a controller design. No runtime verification was done. Its value is the identity model: current MAC, cookie-derived ID, and paid-entitlement owner are separate concepts. ID continuity after MAC rotation does not prove paid time carried over. [`wifi5soft-client-identity-spec.md` L3-11, L34-48, L70-89, L122-136](wifi5soft-client-identity-spec.md)

## 2. MikroTik control plane

| Option | Use it for | Limits / failure mode |
|---|---|---|
| **REST** (`/rest`, v7.1beta4+, HTTPS via `www-ssl`) | Default for CRUD: HotSpot users, profiles, PPP secrets | Basic Auth, so never use plain HTTP. One resource per create. No continuous `monitor`. Long commands time out at 60 s. All values come back as strings. [REST API](https://help.mikrotik.com/docs/display/ROS/REST+API) |
| **Binary API** (TCP 8728, API-SSL 8729) | `listen` event streams (login/logout changes) | You handle framing, `!trap`/`!fatal`, and reconnects. API-SSL without a certificate uses anonymous DH. [API](https://help.mikrotik.com/docs/display/ROS/API) |
| **RADIUS** (HotSpot + PPP/PPPoE) | Shared AAA and accounting, needed once PPPoE subscribers exist | Local users shadow RADIUS users with the same name. [RADIUS](https://help.mikrotik.com/docs/display/ROS/RADIUS), [PPP AAA](https://help.mikrotik.com/docs/display/ROS/PPP+AAA) |
| **HotSpot profiles / walled garden / HTML dir** | Local enforcement and portal hosting | IPv4-centric. Test captive-portal detection on real phones. [HotSpot](https://help.mikrotik.com/docs/display/ROS/Hotspot+-+Captive+portal) |
| **Scripts / Netwatch / Scheduler** | Small local reactions and watchdogs | Not a billing database. [Netwatch](https://help.mikrotik.com/docs/display/ROS/Netwatch), [Scheduler](https://help.mikrotik.com/docs/display/ROS/Scheduler) |

### RADIUS attributes that matter

| Attribute | What RouterOS documents |
|---|---|
| `Mikrotik-Rate-Limit` | Access-Accept and CoA. Takes precedence over Ascend/WISPr rate attributes. |
| `Session-Timeout`, `Idle-Timeout` | Access-Accept and CoA. |
| `Acct-Interim-Interval` | HotSpot honours it only with `radius-interim-update=received`. PPP has its own `interim-update`. RFC 2869 says it MUST NOT be below 60 s and SHOULD NOT be below 600 s. |
| `Mikrotik-Recv-Limit` / `Mikrotik-Xmit-Limit` | Access-Accept and CoA byte limits. |
| `Mikrotik-Total-Limit` | Appears only in the vendor table (14988, type 17; gigawords type 18). **HotSpot behaviour unverified.** |

Sources: [RADIUS](https://help.mikrotik.com/docs/display/ROS/RADIUS), [RFC 2869 §5.16](https://www.rfc-editor.org/rfc/rfc2869#section-5.16)

**CoA and Disconnect:** configure with `/radius incoming`. It is disabled by default, and the default port is **1700**, not the RFC 5176 port 3799, so match them explicitly. RouterOS does not support PoD. CoA can't change an active session's IP, pool, or routes; disconnect the session first. [RADIUS](https://help.mikrotik.com/docs/display/ROS/RADIUS), [RFC 5176](https://www.rfc-editor.org/rfc/rfc5176)

### When the controller or RADIUS is offline

- **Controller offline (API/REST):** new provisioning stops. Sessions that already exist keep running under the limits RouterOS already holds. This is an inference from the docs. **Verify it on the target version.** The design relies on it: logged-in Devices continue under Router enforcement, while new logins, coins, Vouchers, and Rebind fail ([ADR 0003](../adr/0003-router-is-sole-meter.md)). [HotSpot](https://help.mikrotik.com/docs/display/ROS/Hotspot+-+Captive+portal)
- **RADIUS offline:** existing sessions continue. New RADIUS logins can't be assumed to work. The default `timeout` of 1100 ms is a resend interval, not a total deadline. The docs do **not** say a RADIUS timeout falls back to local users. `accounting-backup` exists, but durable buffering of accounting during an outage is **unverified**. [RADIUS](https://help.mikrotik.com/docs/display/ROS/RADIUS)
- **WAN offline:** a controller on the LAN still sells and provisions. Customers just have no upstream.

## 3. Coin acceptor hardware

**Decided:** every coin unit is an ESP8266 Vendo on the network; the Controller has no GPIO coin slot, and the "Pi GPIO" and Orange Pi GPIO points below are prior art only. Vendos join the HotSpot WiFi with an ip-binding bypass matched on MAC+IP, a static DHCP lease, and a firewall rule restricting them to reach only the Controller. Each Vendo has a per-Vendo secret and signs its reports. ESP hardware details are still in progress.

- **Pattern:** a 12 V multi-coin acceptor sends a pulse train into a GPIO pin, either on the Pi or on an ESP32 satellite. An inhibit line blocks the slot. The daemon debounces and aggregates the pulses. [README L130-163](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/README.md#L130-L163), [daemon README L7-19](https://github.com/haliknihudas666/open-piso-wifi/blob/24f1ab078cea247343504416360d1d2f76ff13eb/daemon/README.md#L7-L19)
- **Electrical:** Pi GPIO is 3.3 V logic. open-piso-wifi says a resistor is enough and no optocoupler is needed. Don't trust that. Use an optoisolator or level interface rated for the exact acceptor's output, and bench-test it. [Raspberry Pi GPIO](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio)
- **Orange Pi** support in open-piso-wifi is **unverified**; the project is still looking for testers. (Orange Pi is our Controller floor, not a coin-input host; see [ADR 0002](../adr/0002-controller-in-go.md).)
- **Recommended flow** (now realised as the Coin Window):
  1. Open a Coin Window: one Vendo locked to one Entitlement for ~60 s.
  2. Capture the pulse train on the Vendo.
  3. Persist the coin event with an idempotency key (sequence-numbered, signed report).
  4. Apply time to RouterOS (rate tier applied to the Coin Window total).
  5. Mark the event applied and inhibit the acceptor.

  Default to inhibited after boot, and whenever no Coin Window is open. Each Vendo has its own secret.

## 4. RADIUS: FreeRADIUS or Rust

**Deferred:** PPPoE/RADIUS is out of the MVP. This section is kept as research for when it returns.

### FreeRADIUS

The SQL module supports SQLite and PostgreSQL. `rlm_sqlcounter` enforces accumulated time and data quotas from `radacct`. It doesn't write accounting itself; the SQL module does. FreeRADIUS covers Access, Accounting, CoA, and Disconnect. Its RAM footprint on an ARM SBC is **unverified**, so measure it. [SQL module](https://www.freeradius.org/documentation/freeradius-server/4.0.0/reference/raddb/mods-available/sql.html), [sqlcounter](https://www.freeradius.org/documentation/freeradius-server/4.0.0/reference/raddb/mods-available/sqlcounter.html), [CoA](https://www.freeradius.org/documentation/freeradius-server/4.0.0/howto/protocols/radius/coa.html)

### Rust RADIUS crates

| Crate | Version / date / downloads | Fit |
|---|---|---|
| [`radius-tokio`](https://crates.io/crates/radius-tokio/0.1.0) | 0.1.0, 2026-05-27, ~1k | Best on paper: async server, RFC 2866 accounting, RFC 5176 CoA/Disconnect originator, opt-in MikroTik dictionary. Pre-1.0, MSRV 1.83. Production maturity **unverified**. [repo](https://github.com/ogital-net/radius-tokio) |
| [`radius`](https://crates.io/crates/radius/0.4.0) | 0.4.0, 2024-01-15, ~125k | Server/client plus packet library. Accounting and CoA flows are **unverified**. [repo](https://github.com/moznion/radius-rs) |
| [`radius-rust`](https://crates.io/crates/radius-rust/0.4.4) | 0.4.4, 2024-09-29, ~12k | Sync API. Its README says it does **not validate Message-Authenticator**, which disqualifies it for a server. [repo](https://github.com/MikhailMS/rust-radius) |
| [`radius-parser`](https://crates.io/crates/radius-parser/0.5.0) | 0.5.0, 2021-09-13 | Parser only. |
| `rad` | — | Not RADIUS (it's a Ceph librados binding). |

**Verdict:** a narrow Rust RADIUS server is realistic. It would cover your auth methods, Start/Interim/Stop accounting, MikroTik attributes, and CoA/Disconnect. It is still a security-sensitive protocol surface: authenticators, shared secrets, retransmits, duplicate accounting, and malformed packets. Rough guess (**unverified**): a few weeks for a lab prototype on top of `radius-tokio`, then more for hardening. Doing that before you have paying sites is a bad trade. **Use FreeRADIUS; keep the controller behind an `Aaa` interface so you can swap it later.**

## 5. Rust feasibility

**Superseded:** the Controller is written in Go ([ADR 0002](../adr/0002-controller-in-go.md)). This section is kept as the original evidence; see section 8 for the Go comparison.

- **RouterOS API crates:** `mikrotik-rs` 0.9.0 / `mikrotik-proto` 0.2.1 (2026-09-09, ~155k downloads) are an active async binary-API client, but the license is **AGPL-3.0-only**. The older `mikrotik_api` 0.3.0 is GPL-3.0, has had no commits since 2022, and its query support is incomplete. For REST you don't need a crate: `reqwest` + `serde` is enough. [mikrotik-rs](https://github.com/ferrohd/mikrotik-rs), [mikrotik-api-rs](https://github.com/Nic0w/mikrotik-api-rs), [reqwest](https://docs.rs/reqwest/latest/reqwest/)
- **Storage:** `rusqlite` with the `bundled` feature is the simplest choice for a single-process appliance. `sqlx` brings an async runtime and more machinery than you need. [rusqlite](https://docs.rs/rusqlite/latest/rusqlite/), [sqlx](https://docs.rs/sqlx/latest/sqlx/)
- **Cross-compiling:** `aarch64-unknown-linux-musl` and armv7 musl targets work with `cross` (Docker/Podman) or `cargo-zigbuild`. Check native dependencies (SQLite, TLS) separately; prefer rustls. [platform support](https://doc.rust-lang.org/rustc/platform-support.html), [cross](https://github.com/cross-rs/cross), [cargo-zigbuild](https://github.com/rust-cross/cargo-zigbuild)
- **RouterOS container:** supports arm, arm64, and x86 (EN7562CT is arm32v5 only). It is off by default and needs device-mode with physical confirmation. MikroTik recommends external storage (≥100 MB/s, 10K IOPS), and image pulls need a lot of free RAM. It's possible, but it's a poor default. [Container](https://help.mikrotik.com/docs/display/ROS/Container)
- **Rust vs Go vs TypeScript:** nothing here is CPU-bound. Rust earns its place through a single static binary, strict types, and long uptime on a cheap SBC. Go would be just as defensible and has less library friction. TypeScript is fine for the portal UI. Pick Rust because you want to maintain Rust, not for speed. **Decided: Go** (see ADR 0002).

## 6. Cloudflare online layer

**Deferred:** cloud sync and cross-site balances are out of the MVP. This section is kept as research.

### Workers as RADIUS? No.

`connect()` is outbound TCP only. Inbound TCP from external sources isn't supported, and there is no UDP bind/listen API. Spectrum proxies TCP/UDP, but custom apps need Enterprise plus a paid add-on, and it only forwards to a RADIUS server you still run. RADIUS stays on the LAN. [Workers TCP sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/), [Spectrum](https://developers.cloudflare.com/spectrum/)

### Sync options (all outbound, so all work behind CGNAT)

| Option | Verdict |
|---|---|
| **HTTPS push → Worker + D1** | Start here. Batch events, and poll for remote commands. |
| **Outbound WebSocket → Durable Object** | Only if command latency matters. You **must** use the Hibernation API; plain `accept()` bills duration while connected. Deploys drop sockets, so clients must reconnect. [DO WebSockets](https://developers.cloudflare.com/durable-objects/best-practices/websockets/) |
| **Cloudflare Tunnel** | For remote access to the local admin UI. Not for telemetry, and not a UDP/RADIUS path. [Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/) |

### Cost: 50 sites, 30 s heartbeat, 2,000 events/day (unbatched, 30-day month)

| Item | Math | Result |
|---|---|---:|
| Heartbeats per site per day | 86,400 / 30 | 2,880 |
| Heartbeats, all sites, per day | 2,880 × 50 | 144,000 |
| Worker requests per month | 144,000 × 30 + 2,000 × 30 | **4,380,000** |
| D1 writes per month (1 per request) | — | 4,380,000 |

- **Free plan:** 144k requests/day exceeds the 100k/day cap, and 146k writes/day exceeds D1's 100k/day.
- **Paid ($5/month):** 43.8% of the 10M included requests and well under the 50M included D1 writes. CPU at an assumed 5 ms/request is 21.9M CPU-ms, under the 30M included. **The 5 ms is an assumption.**
- **DO WebSocket:** incoming messages bill at 20:1, so ≈219k request-equivalents.
- **Non-hibernating DO trap:** 50 × 0.128 GB × 86,400 s × 30 ≈ 16.6M GB-s. That is ≈**$202/month** of duration overage, so use hibernation.
- **Queues:** optional. 4.38M records × 3 operations ≈ $4.86/month; events only fits the free allowance.

Batching, for example one heartbeat per 5 minutes plus a batched event push, brings even the Free plan into range.

Sources: [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/), [DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/), [Queues pricing](https://developers.cloudflare.com/queues/platform/pricing/)

### Offline-first vouchers

Two offline sites holding the same code can both redeem it; no cloud can prevent that. To handle it:

- **Scope Voucher Batches per site.** Each site's database is the authority for its own batches. (Decided.)
- **Claim atomically.** (Decided.) Use `UPDATE … WHERE used = 0` plus a unique constraint.
- **Sync idempotently.** Send events with stable IDs, and flag duplicates for review instead of auto-resolving them.
- **Cross-site vouchers must be redeemed online, or reserved to a site** before distribution.

## 7. Recommended architecture

Original proposal (Rust, GPIO coin input). Now: Go, every coin unit is an ESP8266 Vendo, and the Controller is on a management port/VLAN. The `Aaa` and `Outbox` branches are deferred.

```text
Coin acceptor ─ opto ─ GPIO / ESP32 ─┐
                                      v
Phone ─ HotSpot walled garden ─> Controller (Rust, SQLite)  ── REST/HTTPS ──> MikroTik HotSpot (enforces limits)
                                      │
                                      ├─ Aaa seam ─> FreeRADIUS (later, PPPoE) <── RADIUS/UDP ── MikroTik PPPoE
                                      └─ durable outbox ── HTTPS ──> Worker + D1 (mirror, dashboard)
```

- **MikroTik** owns live sessions, enforces limits, and is the only meter of used time ([ADR 0003](../adr/0003-router-is-sole-meter.md)). The Controller uses a dedicated least-privilege account that only the Controller's management IP can reach.
- **The Controller** owns the ledger of time bought: Vouchers, coin events, sales, Entitlements, and (later) the outbox. It does not track time used; RouterOS uptime is read back and reconciled after outages.
- **Seams:** `RouterOs` (REST first), `Ledger` (atomic and idempotent), `CoinInput` (now the Vendo protocol), `Aaa` (deferred), `Outbox` (deferred). Keep it to those; skip anything more until it's needed.

### Things that will bite you

- **MAC randomization, and captive assistants not sharing cookies with the browser.** The MAC is not the account.
- **"Database committed" is not "router applied".** Use an operation ID and reconcile on startup.
- **Local users shadow RADIUS users** with the same name.
- **CoA port 1700 vs 3799.** `Mikrotik-Total-Limit` is unverified.
- **RADIUS outage behaviour is not fully documented.** Lab-test it.
- **12 V into 3.3 V GPIO.**
- **The Cloudflare Free plan breaks without batching.** A non-hibernating DO is expensive.
- **AGPL `mikrotik-rs`** if you ever plan to distribute closed.

## MVP phases

**Revised:** the MVP is vouchers first (portal, REST login, Pause/resume, Rebind), then coins via Vendos. Phases 4 (PPPoE/RADIUS) and 5 (cloud) are deferred. The phase 1 spike is largely covered by the CHR lab results below. Original list:

1. **RouterOS spike.** On a disposable router, test REST/HTTPS, HotSpot users and profiles, walled garden, expiry, and controller restart/disconnect behaviour on real phones.
2. **Local vouchers.** Atomic redemption, SQLite ledger, provisioning, startup reconciliation, minimal admin UI. No coins, no cloud, no RADIUS.
3. **Coins.** Bench-test the acceptor and interface first. Cover pulses, inhibit, power loss, idempotent credit, and customer binding.
4. **PPPoE + FreeRADIUS.** Cover Access-Accept attributes, interim accounting, sqlcounter quotas, CoA/Disconnect, and RADIUS-outage tests.
5. **Cloud.** Worker + D1 fed by a batched durable outbox, plus expiring authenticated commands. Add a DO or Tunnel only when there's a measured need.

E2E checks before release: double redemption, duplicate coin event, controller restart, WAN loss and recovery, router reboot, RADIUS outage, CoA/Disconnect.

## Sources

**Repo:** [wifi5soft-client-identity-spec.md](wifi5soft-client-identity-spec.md), [kitifi-controller-handoff.md](kitifi-controller-handoff.md), [open-piso-wifi @ 24f1ab0](https://github.com/haliknihudas666/open-piso-wifi/tree/24f1ab078cea247343504416360d1d2f76ff13eb)

**MikroTik:** [API](https://help.mikrotik.com/docs/display/ROS/API), [REST API](https://help.mikrotik.com/docs/display/ROS/REST+API), [RADIUS](https://help.mikrotik.com/docs/display/ROS/RADIUS), [PPP AAA](https://help.mikrotik.com/docs/display/ROS/PPP+AAA), [HotSpot](https://help.mikrotik.com/docs/display/ROS/Hotspot+-+Captive+portal), [Netwatch](https://help.mikrotik.com/docs/display/ROS/Netwatch), [Scheduler](https://help.mikrotik.com/docs/display/ROS/Scheduler), [Container](https://help.mikrotik.com/docs/display/ROS/Container)

**RFCs:** [2865](https://www.rfc-editor.org/rfc/rfc2865), [2866](https://www.rfc-editor.org/rfc/rfc2866), [2869](https://www.rfc-editor.org/rfc/rfc2869), [5176](https://www.rfc-editor.org/rfc/rfc5176)

**FreeRADIUS:** [RADIUS how-to](https://www.freeradius.org/documentation/freeradius-server/4.0.0/howto/protocols/radius/), [CoA](https://www.freeradius.org/documentation/freeradius-server/4.0.0/howto/protocols/radius/coa.html), [sql](https://www.freeradius.org/documentation/freeradius-server/4.0.0/reference/raddb/mods-available/sql.html), [sqlcounter](https://www.freeradius.org/documentation/freeradius-server/4.0.0/reference/raddb/mods-available/sqlcounter.html)

**Rust:** [radius-tokio](https://github.com/ogital-net/radius-tokio), [radius](https://github.com/moznion/radius-rs), [radius-rust](https://github.com/MikhailMS/rust-radius), [radius-parser](https://github.com/rusticata/radius-parser), [mikrotik-rs](https://github.com/ferrohd/mikrotik-rs), [mikrotik-api-rs](https://github.com/Nic0w/mikrotik-api-rs), [reqwest](https://docs.rs/reqwest/latest/reqwest/), [rusqlite](https://docs.rs/rusqlite/latest/rusqlite/), [sqlx](https://docs.rs/sqlx/latest/sqlx/), [cross](https://github.com/cross-rs/cross), [cargo-zigbuild](https://github.com/rust-cross/cargo-zigbuild), [platform support](https://doc.rust-lang.org/rustc/platform-support.html)

**Cloudflare:** [Workers TCP sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/), [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [DO WebSockets](https://developers.cloudflare.com/durable-objects/best-practices/websockets/), [DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/), [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/), [Queues pricing](https://developers.cloudflare.com/queues/platform/pricing/), [Spectrum](https://developers.cloudflare.com/spectrum/), [Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)

**Hardware:** [Raspberry Pi GPIO](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio)

## 8. Follow-up decisions (2026-10-16)

**Decided then:** FreeRADIUS behind an `Aaa` seam; local-first, Cloudflare only for account syncing. **Update:** the MVP order was revised to vouchers first, then coins; RADIUS, cloud sync, and cross-site balances are deferred (see "Decisions since this research").

### Go instead of Rust

**Decided: Go** ([ADR 0002](../adr/0002-controller-in-go.md)).

| Need | Go | Rust |
|---|---|---|
| RouterOS binary API | `go-routeros/routeros` v3.0.1 (2025-02-16), **MIT**, `listen` + TLS. [repo](https://github.com/go-routeros/routeros), [v3.0.1](https://github.com/go-routeros/routeros/releases/tag/v3.0.1) | `mikrotik-rs` 0.9.0, **AGPL-3.0-only** |
| SQLite on arm64 / armv7 | `modernc.org/sqlite` is pure Go, so `GOOS=linux GOARCH=arm64` builds without a C toolchain. [docs](https://pkg.go.dev/modernc.org/sqlite) | `rusqlite` `bundled` needs a C cross-compiler (handled by `cross`/zigbuild) |
| RADIUS library | `layeh.com/radius`, MPL-2.0. It has a server, accounting helpers, `radius-dict-gen`, and a `vendors/mikrotik` package. Last commit 2023-12-13. Turnkey RFC 5176 CoA/Disconnect is **unverified**; the packet codes exist. [repo](https://github.com/layeh/radius) | `radius-tokio` 0.1.0 (2026-05), pre-1.0 |
| Existing RADIUS server to learn from | ToughRADIUS (Go, built on a layeh fork). Production use is **unverified**. [repo](https://github.com/talkincode/toughradius) | none found |

**Verdict:** Go has lower friction for this controller. It has an MIT RouterOS client, cgo-free SQLite cross-builds, and an older but proven-shape RADIUS library with a MikroTik dictionary. If a custom RADIUS server ever replaces FreeRADIUS, Go on `layeh/radius` is the safer bet than Rust today. Rust is still fine if you'd rather maintain Rust; you would use REST via `reqwest` to avoid the AGPL.

### Local-first, cloud only for account syncing

**Deferred:** cloud sync and cross-site balances are out of the MVP; no customer accounts either. Kept as research.

This works, and it is simpler than the original plan. What changes:

- **Every site runs fully without the cloud.** That covers sales, vouchers, coins, RADIUS, and enforcement. The cloud holds the cross-site account directory and a copy of each site's ledger.
- **No heartbeat.** Each site syncs when it has changes, plus a slow check-in. "Last seen" comes from the timestamp of the last sync, so monitoring becomes coarse, which is acceptable.
- **The real problem is shared balances.** If one account's balance can be spent at two sites while they're offline, it can be overspent. Pick one rule:
  1. **Home-site ownership:** an account's balance lives at one site, and other sites see it read-only. Simplest.
  2. **Append-only ledger plus per-site offline allowance:** each site may spend at most N offline. Reconcile on sync.
  3. **Cross-site spending only while online.**
- Profile fields (name, PPPoE plan) can use last-writer-wins. Money must never be synced as a "balance" field; sync it as ledger entries.

**Cost, revised.** Assume the same 50 sites and 2,000 events/day total, with a 5-minute batched sync that piggybacks pulls of account changes:

| Item | Math | Result |
|---|---|---:|
| Sync requests per day (upper bound) | 50 × 288 | 14,400 |
| Sync requests per month | 14,400 × 30 | ≈432,000 |
| D1 row writes per day | 2,000 events + account updates | ≈2,000–3,000 |

Both stay under the **Free plan** caps (100k requests/day, 100k D1 writes/day). Durable Objects, Queues, and Tunnel are not needed. Measure Worker CPU against the Free plan's 10 ms limit; move to the $5 plan if a batch exceeds it. [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/)

### Using the WiFi5-Soft identity approach

Take the idea, not the implementation. WiFi5-Soft re-associates a browser cookie ID with the newly seen MAC. Its cookie is AES-encrypted with a **fixed passphrase hardcoded in the firmware** (`88932009YYE`). That makes the cookie forgeable by anyone who reads the firmware. The spec also says it is unknown whether paid time actually follows the ID. [`wifi5soft-client-identity-spec.md` L42-48, L70-82](wifi5soft-client-identity-spec.md)

Do this instead (**decided**, with no customer accounts in the MVP):

- Make the Entitlement owner a Controller-side Entitlement row, never the MAC or Device.
- Give the browser a random token that the Controller signs (HMAC with a per-install key), stored as a cookie. On a new MAC, the portal sends the token, the Controller performs a Rebind of the HotSpot login to the new MAC, and it logs the transfer. One Device per Entitlement; newest wins.
- Captive-portal assistants (iOS/Android) often have their own cookie jar, so the token can be missing. Give customers a short **Resume Code** shown after payment as a fallback; guesses are rate-limited.
- RouterOS HotSpot has built-in `login-by=cookie`. **Resolved:** it is MAC-bound (E1/C1 below), so it cannot be the Rebind mechanism.

### Session control: pause, idle, resume, MAC rebind, top-up

**Settled** (formerly a draft pending grilling; see "Decisions since this research"). This entry refines the identity bullets above and grill topics 1–3 in [`hotspot-identity-lab.md`](hotspot-identity-lab.md#grill-session-once-q2-has-data) (identity model, cookie strategy, Resume Code); it does not replace them.

**Principle:** the Controller decides; the Router meters. The HotSpot user is a stable Entitlement identity code, not the MAC. The Controller's ledger records time bought, Pause Limit use, and Validity, and sets the Router's `limit-uptime` as an absolute total on Top-up ([ADR 0003](../adr/0003-router-is-sole-meter.md)). The Controller runs no time ticker.

| Controller action | RouterOS operation | Status |
|---|---|---|
| Pause | `/ip hotspot active remove` for the user | Confirmed (L1, [L5](results/2026-10-16-chr-session-control/results.md)): REST `active remove` freezes the balance |
| Idle Pause | Auto-pause (site setting, on/off + idle period; never counts toward the Pause Limit): controller sets `idle-timeout` (e.g. 2m) on the HotSpot server or user profile. Router logout is a pause because the balance freezes | Confirmed ([L6](results/2026-10-16-chr-session-control/results.md)): the idle tail is not charged; `$cause=idle timeout`. Preferred over keepalive |
| Resume | Remove any stale active entry for the user, then `/ip hotspot active login` with the current MAC+IP | Confirmed ([L5](results/2026-10-16-chr-session-control/results.md)): remove first, then login; the host must be in the host table |
| MAC change (Rebind) | Controller identifies the Device by its own signed token cookie or Resume Code, then resumes on the new MAC; the balance carries over via E5. This is KiTifi's `mtik_fix_active`, keyed on code instead of MAC | Confirmed ([L5](results/2026-10-16-chr-session-control/results.md)): balance carries over exactly; login before remove fails (`no more sessions are allowed`) |
| Top-up | Ledger += time bought (also resets the Pause Limit and Validity); set `limit-uptime` to the absolute total; one request per user at a time | Changed ([L7](results/2026-10-16-chr-session-control/results.md)): a live `limit-uptime` change does not affect the active session. Top-up = set limit, then remove + `active login` if active |
| Pause Limit / Validity | Controller-side, as in WiFi5-Soft. Per Entitlement; only customer-tapped Pauses count, reset on Top-up; at the limit, tapped Pause stops working (Idle Pause unaffected). Validity forfeits remaining time N days after the last Top-up (default 30) | Decided |
| Departed client | Short `keepalive-timeout` and/or explicit remove | Confirmed ([L8](results/2026-10-16-chr-session-control/results.md)): `keepalive-timeout` detects departure but charges the tail (22s here). Prefer `idle-timeout` (uncharged); keepalive optional/longer |

**Lab basis** ([`results.md`](results/2026-10-16-chr-cookie-mac/results.md)):

- **Balance freezes when no active entry exists.** In L1, user uptime stayed `1m` from logout through the MAC change ([`L1/2-after-logout-user.json`](results/2026-10-16-chr-cookie-mac/L1/2-after-logout-user.json), [`L1/3-after-change-user.json`](results/2026-10-16-chr-cookie-mac/L1/3-after-change-user.json)). Between L3 and L4, uptime grew only while a session was active.
- **`limit-uptime` is cumulative per user** across sessions and MACs (E5).
- **An active session does not follow a MAC change** and keeps charging until removed (E3).
- **`shared-users=1` rejects a second login** (E6), so the old entry must be removed before the rebind.
- **RouterOS cookie login is MAC-bound** (E1/C1), so it cannot be the rebind mechanism.

**Taken from prior art:**

- **KiTifi** ([`kitifi-controller-handoff.md`](kitifi-controller-handoff.md)): the router ops pattern of remove active, then `active login`, done as one step per user (`mtik_fix_active`, `mtik_add_time`). Avoid: user = MAC; cookie-dependent detection (lapse #16); the `limit-uptime` read-modify-write race (lapse #12).
- **WiFi5-Soft** (decomp at `<local-workdir>/w5s/decomp/native/dec/`):
  - Code-based identity (`UNQCODE` + cookie id) with MAC/IP rewrite on rebind (`httpserver.native.dec.txt:9259-9289`).
  - Separate manual-pause and idle-pause limits (`firewall.native.dec.txt:809-877`, `2873-2910`).
  - Top-up adds to remaining time and restores the pause limit (`firewall.native.dec.txt:132-198`).
  - Validity deadline kept separate from remaining time (`firewall.native.dec.txt:1083-1095`).
  - Avoid: its own 1s ticker; idle detection on upload delta only, with 120s hardcoded (`firewall.native.dec.txt:2786-2847`); the `autopause` job draining paused balances after an idle timeout (`firewall.native.dec.txt:1875-1910`); unlocked top-up writes (`firewall.native.dec.txt:132-198`).

**L5–L8 results** ([`results.md`](results/2026-10-16-chr-session-control/results.md)):

- **L5:** remove then `active login` rebinds a new MAC under `shared-users=1`; the balance carries over exactly.
- **L6:** `idle-timeout` logs out after the timeout and the idle tail is not charged.
- **L7:** raising `limit-uptime` mid-session does not change the active session's `session-time-left`; top-up needs remove + `active login`. Lowering mid-session (L7b) is untested, and no longer needed: admins can only grant time in the MVP.
- **L8:** `keepalive-timeout` logs out a departed client but charges the departure tail.
- **`$cause` in `on-logout`:** `admin reset` (controller remove), `idle timeout`, `session timeout`, `keepalive timeout`. This would let the controller count manual and idle pauses separately. **Decided otherwise:** no on-logout scripts; the Controller polls the active list (~10 s) to detect logouts, and only customer-tapped Pauses, which the Controller itself issues, count toward the Pause Limit.
- **Resolved:** L7b (not needed, see L7); `on-logout` reliability and script -> HTTP callback vs polling (polling chosen).
- **Still open:** `idle-timeout` combined with keepalive.

## 9. Testing whether Devices reach the Portal un-NATed (2026-11-19)

Router setup's checklist item 8. Probed on the CHR lab over REST with the admin login:

- `POST /rest/ping` with `{"address": "<Controller IP>", "src-address": "<server's own address>", "count": "2"}` works. Each row is one packet: a reply carries `time` and `ttl`; a lost one carries `"status": "timeout"`; a `src-address` the Router does not own gives `"status": "could not make socket"`.
- From the HotSpot address, with no route back from the Controller to the HotSpot network, the ping timed out; without `src-address` it succeeded. So the ping proves the return route.
- Router-originated packets pass source NAT too: a masquerade that catches HotSpot-to-Controller traffic also masquerades the ping, which then succeeds. A ping alone does not prove Devices arrive with their own address.

**Chosen mechanism:** item 8 is met when the Controller-owned exemption (`chain=srcnat action=accept src-address=<server network> dst-address=<Controller IP>`, comment `openpiso-controller-<server>`) comes before every enabled static source NAT rule of the Operator's, *and* the ping from the server's address answers. The fix adds the exemption, or moves it above the Operator's rules, and edits nothing else. With the exemption in place the ping is not masqueraded, so a ping that still fails means no route back: manual steps. The fallback (reading NAT rules for a matching masquerade and comparing the first Device's source address) was not needed.

Confirmed by the E2E (`--only onboarding`, RouterOS 7.23.8 CHR): the Controller's limited user (policy `read,write,test,api,rest-api`) may call `/ping`; `POST /ip/firewall/nat/move` with `numbers` and `destination` moves the exemption; a masquerade first in the NAT list makes the Portal refuse a Device's Voucher, and after the fix the same Device gets online with the masquerade still there. Two RouterOS behaviours to know: a NAT change applies to new connections only seconds later (a new rule reads `invalid=true` until then), and Router pings reuse one ICMP id, so a ping masqueraded just before the exemption applied keeps riding that connection (and succeeding) while pings repeat within `icmp-timeout` (10 s on the lab). So right after the fix, item 8 can read met although the Controller has no route back, until the checklist goes unread for longer than `icmp-timeout`; the E2E drops those ICMP connections before testing a missing route.
