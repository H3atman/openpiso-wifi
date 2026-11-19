# Architecture

How openpiso-wifi fits together, as a map. This document names the components,
the invariants that bind them, and the flows that cross them — and points to the
authoritative doc for each. It does not redefine terms ([`GLOSSARY.md`](GLOSSARY.md)),
re-argue decisions ([`docs/adr/`](docs/adr/)), restate evidence
([`docs/research/`](docs/research/)), or list acceptance criteria
([the issues](https://github.com/H3atman/openpiso-wifi/issues)). Capitalized terms
(Controller, Router, Device, Time Credit, Voucher, Coin Window, Pause, Idle Pause,
Pause Limit, Validity, Resume Code, Rebind, Top-up, Vendo, Release, Release key)
are the glossary's;
use them verbatim in code, commits, and prompts.

## The spine

One invariant explains most of the design: **the Router is the only meter of
used time, and the ledger records only time bought.** Remaining time is
`bought − used`, where `used` is the Router's accumulated HotSpot uptime. The
Controller never runs a billing countdown; it reads usage from the Router. See
[ADR 0003](docs/adr/0003-router-is-sole-meter.md).

Three decisions frame it:
- **The Router enforces; the Controller stays off the traffic path** — no
  SBC-as-gateway mode. [ADR 0001](docs/adr/0001-mikrotik-enforces-controller-off-path.md)
- **The Controller is one static Go binary** (armv7 + arm64, cgo-free SQLite,
  ~under 64 MB RSS, Orange Pi Zero 256 MB target). [ADR 0002](docs/adr/0002-controller-in-go.md)
- **The Portal is Operator-editable static files over a JSON API** — every rule
  stays behind the API; Operators restyle HTML/CSS around `data-portal`
  markers. [ADR 0004](docs/adr/0004-portal-is-operator-editable-static-files-over-json-api.md)

## Components and who owns what

```
customer Device ──joins HotSpot──▶ Router (MikroTik RouterOS v7)
      │  (walled-garden redirect)        │ enforces + meters access
      ▼                                   ▲
   Controller portal ◀──REST/HTTPS────────┘  (controller-only API user)
      │  owns ledger + portal + admin
      ▼
   Vendo(s) ──signed coin reports──▶ Controller

   Update helper (root, same binary) ──HTTPS──▶ github.com Releases
      │  verifies signed release, swaps binary, rolls back
      ▼
   Controller (restarted)
```

| Owner | Holds / does | System of record for |
|---|---|---|
| **Controller** (Go, SBC) | Ledger (SQLite/WAL), portal, admin, Router client, coin intake | Time Credits, Vouchers, Vendos, Coin Windows, time bought, site settings |
| **Router** (RouterOS v7) | HotSpot enforcement + metering; one HotSpot user per Time Credit | Live sessions, used time (`limit-uptime` accumulated uptime) |
| **Device** | A MAC/IP presenting now; may change for the same customer | Nothing — it is a transient binding, not identity |
| **Vendo** | Coin-accepting unit; reports coins over a signed channel | Nothing authoritative — the Controller credits coins |
| **Update helper** (root, `openpiso update`) | Daily Release check, Operator-requested apply, rollback | Update status (`/var/lib/openpiso-update/status.json`) |

The update helper is the only outbound internet dependency: it reaches
`github.com` (or a configured mirror) to fetch Releases. The Controller process
itself still talks only to the Router, its Vendos, and the Devices it serves.

The Controller reaches the Router only through a **Router client** seam (all
RouterOS REST calls: baseline provision/verify, Time Credit user/profile setup,
active-session login/removal, used-time reads, Vendo rules, version check). The
Router's `login.html` does nothing but redirect to the Controller portal with
the Device MAC and IP.

## Identity: Time Credit vs Device (the crux)

Paid time belongs to a **Time Credit**, held in the ledger independently of any
Device. The HotSpot username is a stable Controller identity code for the
Time Credit — **not** the client MAC. A per-install HMAC key signs a random-token
cookie that identifies the Time Credit in the browser; a short **Resume Code** is
the fallback when the cookie is lost (captive-portal browsers often don't share
cookies). Moving a Time Credit to a new Device is a **Rebind**: at most one
Device active at a time, newest wins. Fuller treatment:
[`docs/research/hotspot-identity-lab.md`](docs/research/hotspot-identity-lab.md).

Why identity can't ride on the Router's own cookie — forced by the CHR labs
([cookie/MAC](docs/research/results/2026-10-16-chr-cookie-mac/results.md),
[session-control](docs/research/results/2026-10-16-chr-session-control/results.md)):
- RouterOS `login-by=cookie` is **MAC-bound**: after a MAC change the new Device
  sees the login page; the old active session keeps charging until removed.
- `shared-users=1` **rejects** a second login rather than taking over. So the
  Controller must remove the old active entry, then `active login` the new
  MAC/IP (which requires an existing host-table entry).
- `limit-uptime` is **cumulative** per user across sessions and MACs, so carrying
  a Time Credit across a Rebind preserves its used time.

## Enforcement mapping

| Ledger concept | Router projection |
|---|---|
| Time Credit | one HotSpot user, random Controller-held password, `shared-users=1` |
| time bought (absolute) | `limit-uptime` set to the absolute total |
| used time | Router's accumulated uptime for that user |
| Idle Pause configured | HotSpot profile with `idle-timeout` (idle tail uncharged) |
| HotSpot profile P of a coin Top-up's Vendo | the user on `openpiso-P-idle`/`openpiso-P-no-idle`, an allowlisted copy of P with `shared-users=1` and the site `idle-timeout` ([#93](https://github.com/H3atman/openpiso-wifi/issues/93)) |
| active Device | entry in `/ip hotspot active` |

RouterOS attribute/behavior detail lives in
[`docs/research/mikrotik-controller.md`](docs/research/mikrotik-controller.md).
RADIUS/PPPoE is deferred behind an `Aaa` seam (not in the MVP).

## Flows

Each flow: trigger → Router action → ledger effect. Acceptance criteria are in
the linked issue.

- **Voucher online** ([#7](https://github.com/H3atman/openpiso-wifi/issues/7)):
  portal accepts a single-use code → atomically mark it used and credit its time
  to the cookie-identified Time Credit (or create one) → set `limit-uptime`,
  `active login` the Device. Concurrent redemption of one Voucher has exactly one
  winner.
- **Coin Window** ([#16](https://github.com/H3atman/openpiso-wifi/issues/16),
  protocol [#15](https://github.com/H3atman/openpiso-wifi/issues/15)): customer
  opens a window reserving one Vendo for one Time Credit (second customer gets
  "Vendo busy") → acceptor enabled only while the window is open and an ack is
  recent → cumulative-total reports add only new coins to the window → Controller
  sends `close` on Done, ~60 s idle, or Device departure; `expires_ms` is only a
  600000 ms safety cap → Vendo blocks the acceptor and answers `closed` with the
  final total → apply rate tiers and credit **one Top-up**. The window
  snapshots its Vendo's rate tiers, or the site's when the Vendo has none
  ([#95](https://github.com/H3atman/openpiso-wifi/issues/95)). Coins belong to the
  window open at their first pulse; tag 0 goes to audit, and late coins for a
  credited window become a further Top-up priced over the whole window. Windows
  left open at restart are closed the same way.
- **Pause / Idle Pause** ([#8](https://github.com/H3atman/openpiso-wifi/issues/8)):
  tapped Pause removes the active session and increments the Time Credit's Pause
  count, refused at the **Pause Limit**; Idle Pause is Router-enforced after a
  site-set idle period, preserves remaining time, and never consumes the Pause
  Limit. Resume logs the Device back in if time remains.
- **Rebind / Resume Code** ([#10](https://github.com/H3atman/openpiso-wifi/issues/10)):
  a known Time Credit arriving on a new Device (via cookie or Resume Code) →
  remove the old session first, then `active login` the new one → record the
  Rebind. Resume-code guesses are rate-limited per Device.
- **Top-up** ([#9](https://github.com/H3atman/openpiso-wifi/issues/9)):
  credit time atomically → raise `limit-uptime` to the new absolute total, reset
  the Pause count, extend **Validity** from now → if online, remove and re-login
  the active session — a session's countdown is fixed at login, so raising the
  limit alone doesn't extend it (CHR lab L7). A coin Top-up takes Validity
  and Pause Limit from the Rate tier in its Coin Window's snapshot; Vouchers use
  their batch snapshot ([ADR 0008](docs/adr/0008-voucher-batch-carries-purchase-terms.md)),
  and admin grants use the site values; no Top-up shortens Validity. Coins also earn
  the tier's Points, in thousandths and journaled with the Top-up; Points outlive
  Validity by moving to the replacement Time Credit, and follow a Rebind
  ([ADR 0006](docs/adr/0006-rate-tier-sets-validity-pause-limit-and-points.md), [#96](https://github.com/H3atman/openpiso-wifi/issues/96)).
  Redeeming a Points table row on the Portal is a Top-up of bought time with
  the row's Validity and Pause Limit; it is never Sales and keeps the HotSpot
  profile. On an expired Time Credit it takes the replacement path, carries the
  Points, then deducts ([#97](https://github.com/H3atman/openpiso-wifi/issues/97)).
  The row comes from the Points table of the Vendo serving the Device's HotSpot
  server, or the site's when none does ([#98](https://github.com/H3atman/openpiso-wifi/issues/98)).
- **Reconcile / outage** ([#11](https://github.com/H3atman/openpiso-wifi/issues/11)):
  poll `/ip hotspot active` ~every 10 s and on boot (no `on-logout` scripts).
  Active Devices stay online under Router enforcement through a Controller
  restart; new logins/redemptions/Rebinds fail and the portal shows a temporary
  -unavailability state. After restart, reconcile `used` from Router uptime so
  time is neither lost nor invented.
- **Controller update** ([ADR 0010](docs/adr/0010-controller-updates-from-signed-releases.md)):
  a daily timer runs the root helper, which fetches the latest `release.json`,
  verifies its Ed25519 signature against the compiled-in Release key, and
  records whether a strictly newer Release exists → the admin page offers
  Update, which only drops a request file → the helper downloads and
  hash-checks the binary, stops the service, backs up the ledger, swaps the
  binary, restarts, and checks `/healthz`; on failure it restores binary and
  ledger together. No Router action; active Devices ride out the restart as in
  any outage.

**Validity** ([#12](https://github.com/H3atman/openpiso-wifi/issues/12)) defaults
to 30 days from the last Top-up; expiry forfeits remaining time and removes the
Router user. **Admin oversight** ([#13](https://github.com/H3atman/openpiso-wifi/issues/13))
is read-only reporting; grants are Top-ups, deductions are out of scope.

## Vendo layer

A **Vendo** is a networked coin unit. The Controller pairs it by MAC, generates a
per-Vendo 32-byte secret, and configures a static lease + HotSpot bypass +
controller-only firewall. Every message is HMAC-SHA256 signed over a type tag
and big-endian `u32` fields; every request starts at the Vendo, with `open`, `ack`,
and `close` riding on report responses. Reports carry a cumulative total per
epoch; the Controller records only its positive serial delta from the last total,
so repeats add 0; the total is the idempotency key. Per-boot `boot_nonce` and
`rseq` prove freshness. The Vendo reports every 2 s (1 s while unacked); the
acceptor is enabled only during a Coin Window
and while an ack arrived within 6 s. See the [Q5 design](docs/research/esp8266-lan-juanfi-qr-parity-and-controller-design.md#design-window-gated-cumulative-total),
[#15](https://github.com/H3atman/openpiso-wifi/issues/15), and
[#16](https://github.com/H3atman/openpiso-wifi/issues/16).

A Vendo serves one HotSpot server. Insert Coin reserves the Vendo on the
Device's HotSpot server, with no default Vendo. Pairing happens when the board is
flashed and finishes on its first signed report. See
[ADR 0005](docs/adr/0005-vendo-serves-a-hotspot-server.md),
[research](docs/research/vendo-auto-select-by-vlan.md), and
[#41](https://github.com/H3atman/openpiso-wifi/issues/41) to
[#43](https://github.com/H3atman/openpiso-wifi/issues/43).

The ESP8266 reference firmware and host simulation live in `firmware/subvendo/`;
its [test plan](firmware/subvendo/TEST-PLAN.md) and Q5 predate `close`/`closed`,
whose board-side update and remaining bench work are tracked in
[#21](https://github.com/H3atman/openpiso-wifi/issues/21). The firmware follows
the reverse-engineered **KiTifi** coin-board family (four ESP8266/ESP32 boards).
It adopts GPIO pulse counting (1 pulse = 1 unit, server-side peso→time mapping)
while rejecting a shared hardcoded GET token, unauthenticated local control, and
unsigned OTA:
- Protocol contract, server-side: [`docs/research/kitifi-controller-handoff.md`](docs/research/kitifi-controller-handoff.md) (committed).
- Firmware deep-dives: `kitifi-subvendo-bins/rea-findings/research/board-behavior-*.md`
  — **local only, gitignored** (ships with the binaries, not the repo).

openpiso-wifi's Vendo channel differs deliberately: per-Vendo secret (not a fleet
token), signed cumulative-total reports, and a Controller-reserved Coin Window
with explicit `close`/`closed` instead of the board's open insert-coin callback.

## Prior art: borrow / avoid

Two reverse-engineered systems inform the design without being a base:
- **JuanFi** ([`docs/research/juanfi-architecture.md`](docs/research/juanfi-architecture.md))
  — borrow `limit-uptime` metering, coin-window busy/timeout crediting, rate
  tiers, refund vouchers; avoid its one-box ESP design, non-atomic flat files,
  MAC-owned Time Credit, and RouterOS binary API (openpiso uses REST/HTTPS).
- **WiFi5-Soft** ([`docs/research/wifi5soft-architecture-client-time.md`](docs/research/wifi5soft-architecture-client-time.md),
  [`docs/research/wifi5soft-client-identity-spec.md`](docs/research/wifi5soft-client-identity-spec.md))
  — borrow the code-keyed identity pattern (time follows the Time Credit, not the
  MAC) and explicit pausable-vs-consumable semantics; avoid its weak fixed-key
  cookie and its billing ticker. openpiso's MVP prices only pausable used time
  ([#3](https://github.com/H3atman/openpiso-wifi/issues/3) tracks extensions).

## Build order

Walking skeleton first ([#6](https://github.com/H3atman/openpiso-wifi/issues/6)):
Go binary + SQLite ledger + Router onboarding/baseline + E2E harness. Then
the dependency chain: Voucher (#7) → Pause (#8) → Top-up (#9), with Rebind (#10),
Reconciler (#11), Validity (#12), admin (#13) off those; Vendo pairing (#15) and
Coin Window (#16) land last. The armv7/256 MB run + install script (#14) gates on
the skeleton. E2E drives external portal/admin/Vendo interfaces against a real
CHR Router, with Linux containers as customer Devices and Vendos, on a throwaway
Docker lab per run — never a fake Router
([ADR 0011](docs/adr/0011-e2e-runs-on-a-docker-lab-per-run.md)).

## Open questions

Unverified items that could move the design live in the research docs' own
"still open" sections — real-phone captive-portal cookie behavior, `idle-timeout`
combined with `keepalive-timeout`, lowering a live limit, long outages on real
hardware, and Orange Pi Zero memory headroom. Check those sections before relying
on any behavior the CHR labs did not isolate.
