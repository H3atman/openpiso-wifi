# Controller

The Controller MVP: one cgo-free Go binary, SQLite ledger, server-rendered admin
and Voucher portal, RouterOS v7 onboarding, Pause/resume, credit intents,
reconciliation, and Vendo pairing/protocol (issues #6–#9, #11 and #15).
Coin Windows and pricing remain #16.

## Build and run

Use the Go version in `go.mod`. On the development laptop:

```sh
export PATH="$HOME/.local/go/bin:$PATH"
make -C controller build
OPENPISO_ADMIN_PASSWORD='<choose an install-time password>' \
  controller/bin/openpiso \
  -data /path/to/private/controller-data \
  -listen 0.0.0.0:8080 \
  -admin-network 192.168.10.0/24 \
  -portal-url http://192.168.10.2:8080
```

`OPENPISO_ADMIN_PASSWORD` is required only when initializing a new ledger. It is
bcrypt-hashed, not retained as plaintext, and cannot change an existing password
by restarting with a different environment value. Later starts need no password
environment variable. The data directory contains `openpiso.db` and its WAL files;
keep the directory private and back it up as a SQLite database. The database uses
one connection with `journal_mode=WAL`, `synchronous=FULL`, and foreign keys on.
Do not store "remaining minutes": the Router is the only meter of used time.

Defaults are loopback-only HTTP and loopback-only admin access. Set the management
CIDR explicitly for a site, **excluding the Router's customer NAT address**. The
Controller uses the TCP peer address, not forwarded headers. Keep management
traffic on a trusted isolated network, or terminate HTTPS in front of the admin
page; the Controller's local HTTP listener does not itself provide HTTPS. Unpaid
Devices need only the portal through the Router walled garden.

Open `/admin/login`. Five failed logins block that source address for five
minutes (in-memory, reset on restart). Admin session cookies are HttpOnly and
SameSite=Strict. Browser cross-origin POSTs are refused. Explicit actions use POST;
`/admin/routes` lists the actual registration table and its authentication policy.
`/admin/state` is an authenticated, explicitly whitelisted readback; passwords and
Router API secrets are write-only.

## Router setup

Router setup (**Settings › Router setup**, `/admin`) connects a Router, then
Adopts the HotSpot servers the Controller manages
([ADR 0011](../docs/adr/0011-controller-manages-only-adopted-hotspot-servers.md)).
The Router needs RouterOS v7 and `www-ssl` enabled with a certificate whose IP/DNS SAN matches its HTTPS
address. The Controller does not guess which interface to convert into a
customer LAN. Supply an HTTPS origin, the Router admin username, and password.

1. The Controller reads the public certificate without sending credentials.
   Normal TLS verification remains enabled; for a private certificate the failed
   handshake exposes the certificate solely for inspection.
2. Compare the displayed SHA-256 fingerprint with `/certificate print` on the
   Router, then select **Fingerprint matches**. Nothing is provisioned before
   that authenticated POST.
3. The confirmed leaf certificate becomes a private trust anchor. Every REST
   request verifies the TLS chain, hostname, validity period, and exact pinned
   leaf; redirects are refused. Certificate replacement fails closed until an
   operator explicitly confirms the new certificate.
4. Connecting verifies v7, saves a Router backup `openpiso-YYYYMMDD-HHMMSS.backup`
   with the admin login (the limited user cannot), creates `openpiso-controller`
   with a random API secret in its own `read,write,test,api,rest-api` group and
   verifies it works. It changes no HotSpot server. No Router admin credentials are written to the ledger
   or returned in a page. They exist only in the in-memory pending confirmation
   and are discarded after successful onboarding/restart.
5. **Use existing** lists the HotSpot servers not yet Adopted, ticked. The review
   page names the two changes, then **Adopt** records the servers in the ledger
   and applies the baseline below. Servers left unticked, their server profiles
   and `hotspot/login.html` are never changed. A server profile shared with an
   unticked server is refused.

RouterOS permissions are coarse: this group can read/write configuration, but
cannot manage Router accounts/groups, reboot, or use SSH/FTP/console/Winbox.
Treat its secret and the Controller data directory as sensitive. Re-onboarding
the same Router reuses the API secret rather than rotating it.

Baseline resources:

- `openpiso-no-idle` and `openpiso-idle` HotSpot user profiles, shared-users=1,
  no MAC cookies, keepalive-timeout=none; Idle Pause period defaults to 2m and
  follows the saved site setting on onboarding and boot.
- Each Adopted server's server profile set to `login-by=http-pap` only and the
  Controller's `openpiso` HTML directory.
- One `openpiso-controller` walled-garden IP rule permitting TCP to the portal
  address and port. The walking skeleton requires an IP-literal portal origin.
- `openpiso/login.html`, containing only a redirect/link to `/portal` with the
  Device's escaped MAC and IP.

With nothing Adopted, boot only verifies RouterOS v7 and changes nothing; the
Dashboard says Router setup is not finished. A ledger from before Router setup
(a saved Router connection, no Adopted servers recorded) Adopts every server on
the Router at its next boot. Baseline verification runs on every boot with the limited credentials. Unchanged
resources are read, not rewritten; drift is repaired. A Router error does not
prevent admin HTTP from starting: inspect `/admin/state` for the error and the
still-pinned fingerprint. There is no automatic certificate trust reset.

## Vendo pairing and protocol

Flashing provisions a Vendo by MAC; its first signed report finishes pairing
([ADR 0005](../docs/adr/0005-vendo-serves-a-hotspot-server.md)). The flash script
uses `POST /admin/vendos/pair` with `Content-Type: application/x-www-form-urlencoded`
and one field, `mac` (a unicast six-byte MAC). Authenticate with an admin session
or HTTP Basic username `admin` and the admin password. Basic auth is accepted
only on this endpoint; management-network and cross-origin restrictions still
apply, and failed passwords share the admin login rate limit.

Success is HTTP 201 with JSON `{"id": 1, "secret": "<64 hex characters>",
"controller_url": "<configured portal URL>"}`. Invalid MAC/form is 400, missing or
invalid authentication is 401, network/Origin denial is 403, an already reserved
MAC is 409, password rate limiting is 429, and a storage failure is 500. The
Controller generates a random 32-byte secret and stores the Vendo as
`provisioned`, with empty IP and HotSpot server. No Router operation is needed.
The secret is returned once, never in list/page/state readback. If that response
is lost, unpair and provision again to generate a new identity/key.

The board must reach the Controller without source NAT. On its first signed
Report, the Controller verifies the HMAC, reads the Router HotSpot host by the
transport source IP, and requires its MAC to match the provisioned MAC and its
HotSpot server to be nonempty. A mismatch is rejected and audited without
changing the Vendo. The Controller pins the verified IP/server as `pending`,
installs the existing Router rules, then marks it `paired`. `/admin/vendos`
shows provisioned boards as "waiting for first contact" and displays DHCP lease
hostnames beside MACs when available. Hostnames grant no authority.

Pairing installs MAC-based IPv4 forwarding/input isolation and IPv6 drops before
creating a static DHCP lease and a MAC+IP HotSpot bypass. IPv4 permits DHCP to the
Router and TCP only from the fixed address to the Controller's portal origin;
other forwarded traffic and Router access are dropped, even if the source IP
changes. Rules precede existing accepts/FastTrack, and previous authenticated
access/connections for that address are cleared. The Controller is never a
traffic gateway. An existing dynamic lease for the MAC is made static; unrelated
static leases are not taken over. Without an existing lease, the Router must have
one DHCP server. Unpair removes only resources bearing that Vendo's ownership
comment, removing the bypass before removing isolation.

The first Ack is expected to be lost when pairing clears the connection carrying
that Report. Verified Router setup has its own 30-second deadline and continues
if the client disconnects. While unacked, the board sends a fresh Report with a
higher `rseq` every second; it does not replay one captured frame.

Interrupted Router pairing remains `pending`; retry a signed Report from the
pinned IP or unpair to clean up. Unpairing a `provisioned` Vendo needs no Router
cleanup. Unpair immediately marks the Vendo `unpairing`, disabling its messages
even if Router cleanup fails; retry the POST to finish. No background repair loop
is included in this ticket. Normal Controller boot does not remove paired rules.

Wire transport (all requests originate at the Vendo):

- `POST /vendo/messages`, `Content-Type: application/octet-stream`, exactly one
  signed frame in the body. GET does not process messages.
- Each frame is a one-byte tag plus big-endian unsigned 32-bit fields, followed
  by the full 32-byte HMAC-SHA256 over the message including the tag. The key is
  the Vendo's 32-byte secret. There is no JSON or separate signature header.
- Report (`0x01`) and Closed (`0x05`): `vendo_id, epoch, total, window_id,
  boot_nonce, rseq` (25 message bytes, 57 with HMAC).
- Open (`0x02`): `vendo_id, window_id, expires_ms, boot_nonce, rseq`
  (21 message bytes, 53 with HMAC). `expires_ms` is a duration from receipt,
  clamped by the board to 600000 ms.
- Ack (`0x03`): `vendo_id, epoch, total, boot_nonce, rseq`
  (21 message bytes, 53 with HMAC).
- Close (`0x04`): `vendo_id, window_id, boot_nonce, rseq`
  (17 message bytes, 49 with HMAC).
- A successful response is `application/octet-stream`: Ack first, optionally
  followed by an Open or Close for the Vendo's Coin Window. Frames have fixed
  lengths by tag; no length prefix or separator. Ack echoes the incoming frame's
  freshness pair; commands use the newest authenticated pair stored per Vendo.
- Report every 2 seconds, or every second without an Ack. The acceptor inhibits
  after 6 seconds without an Ack. A new nonce replaces the command echo; within
  a nonce `int32(rseq-newest_rseq)>0` advances it, matching the firmware model.
  Freshness is never a deduplication key.

A successful Ack is written only after the ledger transaction commits, with no
Router write on the already-paired report path (first contact performs pairing).
For each `(vendo_id, epoch)`, a positive
`int32(total-last_seen)` appends a durable coin delta and advances `last_seen`
in the same transaction. Duplicate/reordered totals add zero; each epoch keeps
its own baseline, so revisiting an old epoch cannot count it twice. Stale totals
and freshness are audited but authenticated reports still get their own Ack.
Bad frames, HMACs, types, unknown and unpaired Vendos are rejected and audited,
without changing last contact. No raw attacker payload or key is recorded.

A positive delta for a Coin Window issued to that Vendo adds to its live coin
count in the same transaction. Window 0 and unknown/wrong-Vendo tags remain
`unassigned_window` append-only audit, never bought time. `/admin/state` exposes
sanitized Vendos, totals and the most recent 1000 coin deltas/audit events, while
all events remain in the ledger. Firmware-owned Close/Closed vectors await #21;
their layouts are already defined above.

### Coin Windows and rates

Set `/admin/rates` with one `pesos=seconds` tier per line, starting at ₱1.
The highest eligible tier prices the **whole** window, rounded down to seconds:
`1=10` and `5=60` buy 40 seconds for ₱4, 60 for ₱5 and 72 for ₱6. Tiers must
increase in pesos without decreasing seconds per peso. Each window snapshots
its rates so later operator edits do not change accepted or late coins.
These are the site rates. A Vendo uses them until the Operator customizes the
Rates tab of its Edit dialog (`POST /admin/vendos/rates`, `id` and `action`:
`customize` copies the site tiers in, `save` sets `tiers` under the same rules,
`site` drops them). A window snapshots its Vendo's own tiers, or the site's.
A full line is `pesos=seconds,validity,pause limit,points`. Points are earned
per tier pesos with up to three decimals and stored in thousandths:
`1=10,86400,3,0.1` earns 0.1 Point per ₱1, rounded down over the whole window,
so a late coin adds only the difference. Vouchers and admin grants earn none.
Set the site Points table at `/admin/points` (Coins → Points table), one
`points=seconds,validity,pause limit` row per line: `5=1800,no limit,1` sells
30 minutes for 5 Points. Costs may have three decimals. An empty table offers
no redemptions, and it starts empty.
A Vendo uses the site Points table until the Operator customizes it in the
Points tab of its Edit dialog (`POST /admin/vendos/points`, the same `id` and
`action` as the Rates tab). Devices on the Vendo's HotSpot server redeem from
its table; with no serving Vendo, they redeem from the site's.
One pulse is ₱1; the acceptor must emit v pulses for a coin worth ₱v.

The Router-verified Device posts to `/portal/api/coins/open` without `vendo_id`
when selection is automatic. With one paired Vendo at the site, it serves every
network. Otherwise the Controller matches the Device host `server` against each
paired Vendo `hotspot_server`: one match selects automatically, several show a
picker limited to those matches, and zero shows `No Vendo serves this network`.
Only the picker posts `vendo_id`, and the Controller rejects IDs outside the
matches before reserving anything. A Coin Window keeps its reserved Vendo if
the Device moves VLAN. See ADR 0005.

Until flash-time pairing (#42), MAC+IP pairing learns `hotspot_server` from the
board host row if its MAC matches; otherwise the field stays empty. Operators
can change or clear it in the Vendo List (`/admin/vendos`) Edit dialog with the
Router-backed dropdown (`POST /admin/vendos/edit`, `id`, `name`, `location`, and
optional `hotspot_server`; a missing field keeps the server). Existing ledgers acquire
the field with an empty default on startup; there is no inferred default network.
On Routers with multiple DHCP servers, the board needs an existing DHCP lease.

The row's power button Deactivates or Reactivates a paired Vendo
(`POST /admin/vendos/active`, `id`, `active` 0 or 1; htmx gets the row back,
errors included, and a plain post redirects). A Deactivated Vendo stays paired
and keeps its secret, Router rules, Name and Sales; its reports are still
accepted, so a live Coin Window settles. Insert Coin never picks it: rule 1
still counts paired Vendos, so a lone paired Vendo serves only while active,
and server matching runs over active Vendos. When the Vendo that would serve is
Deactivated, Insert Coin answers HTTP 503 `The coin slot for this network is out
of service`. See the ADR 0005 amendment.

Opening binds one Vendo to its Time Credit, creating a zero-bought reservation
if needed.
Zero-bought reservations never become Router users (RouterOS's zero limit means
unlimited). A partial unique index reserves the Vendo until its final Closed,
including after grace settlement. A second request receives HTTP 409, Vendo busy.
The Portal shows the rate table and live burst count, re-reading `GET /portal/api/state` every second while the window is open.

While open, each response carries Open with the remaining 600000 ms safety cap
measured from the most recent new coin (initially the opening). Heartbeats and
resends do not renew it; new coins do, so continued insertion can keep a window
open. Done (`POST /portal/api/coins/done`, `window_id`), Router-observed Device
absence/replacement, or 60 seconds without a coin changes the command to Close.
Device departure is detected on the existing ~10-second Reconciler tick when
the Router no longer reports the original MAC/IP; lingering HotSpot hosts can
delay that signal, but the independent 60-second coin-idle close still applies.

Closed commits final coins, one tier-priced Top-up, Pause-count reset, append-only
audit and the shared absolute credit intent atomically, before Ack. It releases
the Vendo even when its delta is zero. Router projection runs asynchronously in
the existing Reconciler, woken by Closed; no Router write is made on the Ack path.
If Closed is missing for 5 seconds after Close, the Reconciler credits what is
recorded (on its next tick), but keeps sending Close and holds the Vendo busy.
Late coins buy `price(new whole total) - already credited` as another Top-up;
replays add nothing. Tapped Pause is preserved, since buying time is not Resume.
On boot the same Reconciler closes previously open windows before any new Open
command can be served, even if the Router is unavailable.

`/admin/coins` is an all-time coin sales/intake report per Vendo, separating
unassigned intake from time bought, with outstanding receipts and coin-total gap
warnings. The Reconciler compares every stored epoch high-water total with the
complete delta journal each tick (modulo uint32 wrap); `reconciliation.coin_gaps`
in `/admin/state` exposes discrepancies. Coin intake is counted once, not once
again for its audit. Period-filtered combined Voucher/coin reporting remains
part of the admin reporting ticket.

The E2E harness uses `e2e/simulated_vendo.py`. It also runs standalone without
Router credentials, reading the pairing key from a private hex file:

```sh
python3 -I controller/e2e/simulated_vendo.py \
  --url http://192.168.10.2:8080 --id 1 --key-file /private/vendo-key
```

The shared simulator supports Ack/Open/Close framing, the 6-second gate, cap,
and Closed after the 300 ms drain gap. E2E protocol cases deliberately assign
cumulative totals directly to represent replays, old epochs and unissued tags.
Its default CLI is a heartbeat-only Vendo; Coin Window scenarios use `insert()`.
## Portal page and JSON API

`GET /portal` serves the Default Portal, the M2 "Profile" layout. Its other
files are flat under `GET /portal/{file}` (one path segment): `core.js`,
`portal.css`, `custom.js`, `logo.svg`, `cover.svg` and `favicon.svg`, all in `web/portal`
and embedded in the binary. The page shows `/portal/logo` and `/portal/cover`:
the published Custom Portal's images, or the built-in SVGs. The page, stylesheet and script stay within a
40 KB code budget. Every `/portal` response carries the Portal CSP,
`default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`,
so the page has no inline styles or scripts. The Operator contract for a Custom
Portal (markers, actions, `data-portal-state` values, the `portal:state` event
and message codes) is [docs/portal-contract.md](../docs/portal-contract.md).
`core.js` does everything through `/portal/api/`:

- `GET /portal/api/state` is read-only: it never logs in, Rebinds or finishes a
  Pause. Remaining time comes from a Router read.
- `POST /portal/api/{coins/open, coins/done, redeem, points/redeem, connect,
  recover, pause, resume}` perform actions. Reconnecting after an Idle Pause, finishing an
  interrupted Pause, and Rebind happen only through `connect`.

Every response, errors included, is the same JSON state:

```json
{
  "time_credit": {"bought": 120, "remaining": 95, "usage_known": true,
    "valid_until": 1792800000, "expired": false, "paused": false,
    "pauses_left": 2, "pause_limit": 2, "resume_code": "ABCD2345", "online": true,
    "points_milli": 1100},
  "rebind": false,
  "rates": [{"pesos": 1, "seconds": 10, "validity_seconds": 2592000, "pause_limit": 3}],
  "points": [{"points_milli": 5000, "seconds": 1800, "validity_seconds": 86400, "pause_limit": 1}],
  "vendos": [{"id": 1, "rates": [{"pesos": 1, "seconds": 10, "validity_seconds": 2592000, "pause_limit": 3}]}],
  "coin_window": {"id": 3, "vendo_id": 1, "status": "open", "coins": 4,
    "credited_seconds": 40, "closes_at": 1792790060},
  "message": {"kind": "success", "code": "online", "text": "You are online."}
}
```

`time_credit` and `coin_window` are `null` when absent; `message` is `null` on a
quiet read. `kind` is `success`, `warning` or `error`; `code` is stable for
scripts, `text` is for people. `resume_code` is omitted while tapped-Paused.
`points_milli` is the Points balance in thousandths, and `points` the Points
table the Device can redeem from; see [Points](../docs/portal-contract.md#points).
`rebind: true` means the signed cookie owns a Time Credit on another Device and
reveals nothing else. Status codes are unchanged from the form-post Portal.

## Custom Portal

Open `/admin/portal` (Settings → Portal) to edit `index.html`, `portal.css` and
an optional `custom.js` in plain textareas, and to upload a cover and a logo.
"Download default" gives the Default Portal's three files from
`GET /admin/portal/default/{file}`. The ledger keeps three slots in its
`portal_versions` table: the draft, the published version, and the previous
published version. Each slot holds the three files and the `logo` and `cover`
WebP blobs (empty for the built-in images), so the images follow the draft
through Publish, Revert and Reset. The Default Portal stays embedded and is
never stored.

- `POST /admin/portal/draft` (`index_html`, `portal_css`, `custom_js`) runs the
  save check and stores a passing draft as the next version number. A failing
  draft is shown back with every failure and nothing is stored (400). The check
  refuses a file over 64 KB, any `http:`/`https:` URL or protocol-relative
  `//` URL (in HTML attributes, CSS `url()`/`@import` and JS strings), a page
  that loads `<script src="/portal/core.js">` other than exactly once, and a
  missing or duplicated required marker (listed in the
  [Portal contract](../docs/portal-contract.md#required-markers)).
- `POST /admin/portal/images/{logo,cover}` takes the raw WebP body that the
  admin page's script made: it crops the logo to 256 × 256 and the cover to
  1200 × 400 around the focus point, lowering WebP quality until it fits. The
  Controller checks only the magic bytes (`RIFF`…`WEBP`, else 415) and the cap
  (logo 20 KB, cover 100 KB, else 413), then saves a new draft version built on
  the current draft. Saving the text files keeps the draft's images.
  `GET /admin/portal/draft/{logo,cover}` shows the draft's image.
- `POST /admin/portal/publish` publishes the draft and keeps what was published
  as previous; 409 without a draft.
- `POST /admin/portal/revert` swaps the published and previous versions; 409
  without a previous one.
- `POST /admin/portal/reset` serves the Default Portal again. What was
  published becomes previous, so Revert undoes a Reset.
- `GET /admin/portal/preview?state={new|online|coin|paused|out|error}` is the
  Preview card's iframe: the draft (or else what the editor shows) as one
  document, with its stylesheet, `core.js` and `custom.js` inlined under nonces
  and `portal_preview.js` before `core.js`. That harness answers `core.js`'s
  fetches with the sample state and posts a report to the admin page: whether
  `core.js` rendered the state, the markers it knows, unknown markers, and JS
  errors. The iframe is `sandbox="allow-scripts"` without `allow-same-origin`,
  so Operator JS has an opaque origin and no admin session. Its CSP refuses
  every connection, form and inline code without the nonce, and allows images
  only from the Default Portal's own files and `data:` URLs, so the preview
  never reaches the Portal API or the Router. The sandbox can't send the admin
  session, so the draft's cover and logo (or the built-in ones) are inlined as
  `data:` URLs wherever the page names `/portal/logo` or `/portal/cover`: in an
  HTML attribute or a CSS `url()`.

`GET /portal`, `/portal/portal.css`, `/portal/custom.js`, `/portal/logo` and
`/portal/cover` serve the published version, or the embedded Default Portal
(whose `custom.js` is a comment, and whose images are `logo.svg` and
`cover.svg`). Images are `image/webp` with `nosniff`, under the Portal CSP. Every
other file, `core.js` included, is always embedded. A Custom Portal gets the
same Portal CSP as the Default Portal: the save check catches mistakes, and the
CSP is the security boundary. `/admin/state` shows `portal` with each slot's
`version` and `saved_at`, or `null` when empty.

## Vouchers and Device identity

Open `/admin/vouchers` to generate 1–1000 eight-character Vouchers with a time
value in seconds (1–31536000). Codes use a CSPRNG and exclude `I`, `O`, `0`, `1`.
Each Batch has a permanent authenticated printable URL; printing is entirely
local. Vouchers are site-local to this ledger, single-use, and show their used
status/time in the Batch view. `/admin/state` includes sanitized Batches,
Vouchers, Time Credits and append-only credit audits under `purchases`.

The portal accepts `POST /portal/api/redeem` with form field `code`. The Controller
looks up the request's actual source IP in the Router's HotSpot host table;
redirect MAC/IP parameters and forwarded headers are never trusted. **Route
HotSpot traffic to the Controller without source NAT, and provide a return route
from the Controller to the HotSpot LAN.** Masqueraded requests cannot redeem.
Router setup checks both per Adopted server: its one-click fix adds a
Controller-owned source-NAT exemption (comment `openpiso-controller-<server>`)
above the Operator's rules, and a missing return route is shown as manual steps.
The Router's redirect parameters are informational only.

Redemption atomically consumes the Voucher, creates or credits a Time Credit,
and appends an audit row before projecting its absolute bought time to the
Router. One random-password HotSpot user (`op-<Time Credit ID>`) belongs to each
Time Credit, using the site-selected Idle Pause profile with `shared-users=1`.
A signed random-token
HttpOnly, SameSite=Lax cookie identifies the Time Credit; its HMAC key persists
privately per installation. No cookie is needed to find an existing Time Credit
on its current Router-verified MAC/IP. On a new Device, the signed cookie offers
an automatic POST to `/portal/api/connect` (with a manual Connect fallback); reading
state never changes the Device binding or Router access.

Remaining time is always `bought − (committed HotSpot-user uptime + live
active-entry uptime)`, floored at zero; no usage or remaining-time counter is
stored. If Router projection fails after credit, bought time remains
saved. `POST /portal/api/connect` retries projection/login without consuming another
Voucher. Top-up reuses the existing Time Credit and HotSpot user, resets its
tapped-Pause count, and queues the new absolute bought-time target in #11's
credit-intent journal, in the same transaction as Voucher consumption and audit.
Raising the Router limit removes/re-logs the active session so RouterOS applies
the new countdown without resetting used uptime. A failed Router write leaves
the intent pending for boot/poll retry; retrying never buys the time twice.
Top-up also extends Validity from now in that same transaction. Rebind uses this
same projection journal.

Five invalid/unknown/used Voucher attempts block that Device MAC for five minutes
(in memory; restart clears it). A successful redemption resets the count.

## Resume Codes and Rebind

After purchase the portal shows an eight-character site-local Resume Code. Save
it privately: like the signed cookie, it proves ownership of the Time Credit.
Enter it using `POST /portal/api/recover` with form field `code` when a new browser or
captive-portal popup has no cookie. Five invalid/unknown guesses block that
Router-verified Device MAC for five minutes, independently of Voucher attempts
(in memory; restart clears it). Codes survive Controller restarts and are not
included in `/admin/state` or the E2E report's Portal state.

Cookie and Resume Code recovery use the same serialized Rebind flow: remove the
Time Credit's old active sessions, persist the new Device binding with an
append-only Rebind record and a superseding credit-intent revision, then use the
shared projection helper to log the new Device in. Newest Device wins. The
HotSpot user and its accumulated uptime are retained, so Rebind never buys time
or resets usage. If the new Device already holds another Time Credit, its
sessions are removed and its MAC/IP binding is cleared in the same ledger
transaction; its bought time and recovery credentials remain intact. Empty
MAC/IP means unbound, not expired. `/admin/state` exposes the sanitized Rebind
history under `purchases.rebinds`, including `via=displaced` records. Failed
Voucher attempts do not Rebind, even when accompanied by a valid cookie.

Login restricts the HotSpot user to the bound MAC and rechecks the host entry.
If an IP has multiple host entries, the Controller fails closed rather than
assuming `host-dead-time` proves the sender's MAC. If the host disappears before
login, no speculative login is attempted; reconnect through the WiFi and retry
Connect. The bought time and recovery credential remain saved.

The Rebind E2E functions switch the customer's actual HotSpot NIC (virtio ignores
runtime MAC edits), exercise Resume Code and signed-cookie recovery, and verify
old-Device denial and Router-metered remaining time. After each NIC return, the
fixture reads its bound DHCP address, refreshes that Device's source-policy rule,
and uses the current address for subsequent requests. Separate departure checks
use #6's `openpiso-idle` / `openpiso-no-idle` profiles selected by the lab fixture,
without depending on #8's admin settings. They record the elapsed session-end
bound and additional Router-metered usage when MAC changes without Rebind. The
Idle Pause check reads the profile's actual configured period; the no-idle check may wait
until the remaining bought time runs out. These checks require exclusive lab
access and are not run by PARALLEL implementers.

## Pause and Idle Pause

Open `/admin/pause` to set the Pause Limit (0–1000, default 3), enable/disable
Idle Pause (default off), and choose its idle period (1–86400 seconds, default
120). Saving updates both Router profiles and assigns the matching profile to
existing Time Credit users. Active Devices are re-logged in on this explicit
admin action so the new policy takes effect. Boot verifies the saved policy
without removing healthy active sessions. A failed Router projection leaves the
settings saved; retry Save to finish applying them.

`POST /portal/api/pause` takes a tapped Pause on the Router-verified Device, even
without a cookie. The durable tapped flag and count are recorded before session
removal; retrying a partial Pause does not consume another Pause. The portal
shows Pauses left and distinguishes **Tapped Pause** from **Idle Pause**.

A tapped Pause never resumes on a portal visit, a Router login-page redirect,
or through `/portal/api/connect`. `POST /portal/api/resume` requires the current Device
and its signed Time Credit cookie; `/portal/api/recover` accepts Resume Code proof.
A cookie-driven automatic Rebind preserves tapped Pause; only an explicit Resume
or Resume Code POST may clear it after confirmed Router login. Cookie-free
visits to a tapped Pause cannot mint an ownership cookie. Tapped state/counts
survive restarts. Top-up resets the count, but preserves the tapped flag until
explicit Resume; buying time does not silently resume a paused Device.

Idle Pause is enforced by Router `idle-timeout`, with `keepalive-timeout=none`.
When Idle Pause is enabled, an absent active session with time remaining and no
tapped flag reconnects through `POST /portal/api/connect`, which the Portal sends
automatically once per page load, without consuming a tapped Pause. Reading
state never logs a Device in. No Controller timer, used-time counter, or
remaining-time field is stored: the Router is still the only meter.

## Validity

Open `/admin/validity` to set a whole-number Validity in seconds, minutes, hours
or days (default 30 days; minimum 1 second, maximum 36500 days). The ledger stores
integer `validity_seconds`, never fractional days. The form chooses the largest
unit that divides the saved value exactly and previews when unused time expires
from the last Top-up. Short E2E scenarios use seconds through this same form,
without a fake clock.
A setting change applies to future purchases; it does not rewrite existing
deadlines. Every Voucher purchase sets the deadline from now, in the same
transaction as bought time, Pause reset, audit and credit intent. The portal
shows the deadline in UTC. A grant after the deadline is refused with 409 even
before the expiry sweep runs; it cannot revive forfeited time.

The existing Reconciler expires all due Time Credits, online or offline, on boot
and about every 10 seconds. It re-checks deadlines inside the expiry transaction,
so a committed Top-up extension cannot be overwritten by a stale deadline.
Retirement forfeits access to unused time, appends one `validity-expiry` audit,
and supersedes pending credit projections. Bought time and purchase history stay
intact; the Controller does not invent a used-time measurement at expiry.
Router session/user removal is idempotent and retried after Router failure or
Controller restart. `/admin/state` exposes `validity_seconds` and `validity` with
Unix-second deadlines, retirement timestamps and Router-removal completion.
An expired cookie cannot Connect or Resume/recreate the user. A purchase after
the deadline creates a new Time Credit with only the newly bought time.

Later purchase paths must call `ledger.ExtendValidity(tx, id)` in their credit
transaction, and choose a new Time Credit if the old one has expired. They must
preserve the shared `voucherMu` ordering and `projectRouterTimeCredit` expiry
guard. Expiry enforcement is a Controller responsibility: during a Controller
outage the Router continues enforcing its previously projected uptime limit;
Validity removal catches up on boot.

## Layout

- `cmd/openpiso`: flags, private ledger, boot verification, HTTP listener.
- `internal/controller`: HTTP routes, admin authentication, onboarding flow,
  `html/template` pages, whitelisted readback. Later portal/admin routes belong
  in the same route table so the route-list E2E policy covers them.
- `internal/ledger`: concrete SQLite ledger and its atomic storage operations.
- `internal/routeros`: the only production RouterOS REST speaker; TLS trust,
  limited API user and idempotent baseline. Extend it with domain operations,
  not a swappable mock Router interface.
- `internal/vendoprotocol`: signed binary Report/Open/Ack/Close/Closed codecs.
- `e2e/simulated_vendo.py`: Vendo-initiated protocol simulator and firmware vectors.
- `e2e/run.py`: stdlib Python harness driving the compiled binary through HTTP
  against the real CHR Router and Device. No internal unit-test seams.
- `e2e/reports/walking-skeleton.json`: full sanitized report, local and gitignored
  for debugging. The harness also writes `walking-skeleton.summary.json` for
  committing: result, step names, binary SHA-256, git commit, tree hash and dirty flag.
  Summaries converted from older reports have null git fields.

## Reconciliation and Controller outages

The Reconciler reads the Router active list on boot and about every 10 seconds.
It never resets healthy sessions just because the Controller restarted. Active
snapshots in `/admin/state` are transient; no used/remaining-time counter is
written to SQLite. The portal calculates remaining time from the Router's
committed user uptime plus live active uptime, and shows when time has run out.
The Router enforces the limit and redirects exhausted Devices to the portal.

Every Voucher credit also journals a pending absolute `limit-uptime` target in
its transaction. Projection applies that total, removes any old session to
commit its usage, then logs in the verified bound Device if time remains and
it is not tapped-Paused. A Router error or process kill leaves the intent pending;
boot/poll retries cannot credit the purchase twice. `/admin/intents` shows pending
and applied revisions, and `/admin/state` exposes sanitized `credit_intents` and
`reconciliation` readback. Error text excludes Router credentials. Interrupted
tapped Pauses are finished without automatically resuming them.

Later purchase paths must call `ledger.QueueCreditIntent(tx, id, boughtSeconds)`
in the same transaction as the bought-time change and audit. Coordinate mutations
and Router projections with `Server.voucherMu`. A new credit supersedes the old
absolute target; revision-checked completion cannot acknowledge a newer credit.
Existing pre-journal Time Credits receive one pending projection on upgrade.
Pause-count resets and Validity policy belong to Top-up/Validity, not this journal.

The Router-hosted `login.html` defaults to “Service temporarily unavailable” and
loads the Controller's non-cached `/portal/ready.js`. Only a successful script
load redirects to `/portal`; if the Controller is stopped, the fallback remains
visible with a retry link. JavaScript is required for this availability check;
no purchase or login credentials live on the Router page. Devices already online
continue under Router enforcement while the Controller is down.

## Admin Dashboard

Open `/admin/dashboard` (the first admin page) for a glanceable summary: a banner
linking to the Audit inbox when coin reports await review; four Sales tiles
(Today, This week from Monday, This month, This year: calendar period to date in
the Controller's local time), each linking to the Sales page for that period;
**Active Hotspot** (Time Credits with a live Router HotSpot session, "unknown"
when the Router cannot be read); **Customers with time left** (online plus paused
Time Credits, "—" when their state cannot be derived); and a System Status
section with Controller and Router cards. The page refreshes itself every 10
seconds through one HTMX poll (`/admin/fragments/dashboard`) rendering the same
data. Time Credits, grants (from a Time Credit's detail page), the Audit inbox,
Rebinds and Voucher Batches live on their own pages. All routes require the
management network and admin session. Add `format=json` for `sales`
(`today`, `week`, `month`, `year`), `active_hotspot` (null if unknown),
`customers_with_time_left` (null if unknown), `pending_audit`, and the
`controller` and `router` status blocks.

The Router card shows CPU % with a bar, memory and disk as "X MB free of Y MB"
with a bar, temperature in °C and uptime as "3d 4h 12m", read from RouterOS
`/system/resource` and `/system/health` on each refresh. A metric the Router does
not report (a CHR has no temperature sensor) shows "—" and is null in JSON. When
the Router cannot be read the card says "Router unreachable", Active Hotspot shows
"unknown", and Sales, Customers with time left and the Controller card still
render. The JSON `router` block holds `cpu_percent`, `memory_free_bytes`,
`memory_total_bytes`, `disk_free_bytes`, `disk_total_bytes`, `temperature_c`,
`uptime_seconds` (each null if missing) and `unreachable`.

The **Controller** card shows the machine running the Controller: CPU % with a
bar (the load since the previous poll, so "—" on the first reading after start),
"X MB free of Y MB" memory (`MemAvailable`) and disk (the filesystem holding the
`-data` directory, so the ledger database) each with a usage bar, temperature in
°C from the first readable kernel thermal zone, and machine uptime as "3d 4h
12m". Any metric the host cannot report shows "—". The `controller` JSON block
has `cpu_percent`, `memory_free_bytes`, `memory_total_bytes`, `disk_free_bytes`,
`disk_total_bytes`, `temperature_c` and `uptime_seconds`, each a number or null.
Reads are a few small `/proc` and `/sys` files and one `statfs` (Linux only, no
cgo), cheap enough for a 256 MB board at the 10-second poll.

Open `/admin/sales` for the Sales breakdown (Redeemed Vouchers, Coins received,
Total). `from` and `to` are `YYYY-MM-DD` local days in the Controller's system
timezone, both inclusive: the period is `[from 00:00, to+1 00:00)`. Defaults cover
the 1st of this month through today. Invalid or reversed dates return 400. Add
`format=json` for `voucher_pesos`, `redeemed_vouchers`, `unpriced_vouchers`,
`coin_pesos` and `total_pesos`. "Today" on the Dashboard starts at local midnight.

Voucher generation accepts optional `pesos` (0–1000000) as a permanent per-Voucher
face value. Existing Batches and new Batches with a blank price remain unpriced;
they appear in the redeemed count and unknown-price count, not invented Sales.
Voucher Sales are recognized at redemption, not when unsold paper is printed or
cash is exchanged. Coins received are the sum of durable positive Vendo deltas
(1 coin unit = 1 peso), including unassigned coins awaiting investigation. Replays,
audit copies and admin grants do not add to Sales.

`POST /admin/grants` takes `time_credit_id`, positive `seconds` (1–31536000),
`reason` (up to 500 bytes) and a unique `request_id` (up to 100 bytes). Browser
forms generate the ID; callers must reuse it for retries. Repeated IDs return
409 without buying more time. A grant appends an actor/reason/before/after/source
audit, resets the tapped-Pause count without clearing tapped Pause, and queues
the absolute Router limit in one transaction. If projection fails, the response
says the grant was recorded and projection is pending: do not grant again.
The existing journal retries without duplicating bought time. Grants extend
Validity in the same transaction (`ExtendValidity`); a retired Time Credit returns
409 and changes nothing, including any linked audit resolution. Grant to the
customer's new active Time Credit instead.

Every Vendo audit row stays `pending-review` across reads, polls and restarts.
`POST /admin/audit/resolve` with `audit_id`, `action=dismiss` and `reason` records
a dismissal without changing bought time. To compensate, include `audit_id` in
an admin grant: the grant and resolution commit together, with a link to the
purchase audit row. Re-resolving returns 409. Original Vendo evidence, purchase
audits and resolution rows are append-only. `/admin/state` exposes `audit_review`
with status, resolution timestamp, actor, reason and linked grant audit ID.

## Verification and CHR E2E harness

```sh
export PATH="$HOME/.local/go/bin:$PATH"
make -C controller check    # Go type checking and vet
make -C controller cross    # static Linux armv7 and arm64 binaries
make -C controller e2e      # build native binary and run the real-CHR scenario
```

Lab workflow (a full native run is ~23 min, ARMv7 ~26 min, one `--only` run 1.5–3.5 min):

- Each new issue adds its own `--only <name>` scenario (one `ONLY_SCENARIOS`
  entry in `e2e/run.py`) and iterates on that.
- Run the full `make -C controller e2e` once before the PR is merged.
- Run `make -C controller e2e-armv7` at milestones, or when a change touches
  performance or memory.
- Parallel runs queue on the lab lock (below) instead of failing.

For fast UI iteration against the same Router, see the dev lab in
[`lab/dev/README.md`](../lab/dev/README.md). It shares `chr-lab`: never run both at once.

The E2E harness **reconfigures the throwaway `chr-lab` and `chr-client` VMs**.
Use it only with exclusive access. It never operates on `piso-client`,
`piso-server`, or the `pisonet` network. Requirements:

- Existing CHR lab from `lab/chr/README.md`; `chr-lab` running and `chr-client`
  defined, with the management/HotSpot NICs described there.
- `virsh -c qemu:///system`, Python 3, `flock`, OpenSSL (for the non-v7 HTTPS
  fixture), and the gitignored `lab/chr/.env`. The loader prefers this checkout's
  file; when absent, it reads the main checkout's copy through Git's common
  directory. No credentials file is copied or symlinked into a worktree.
- Host address `192.168.122.1`, available TCP port 18080, and the lab HotSpot
  at `10.5.50.1`. Router/client management addresses are discovered from libvirt.
- Permission through libvirt to create a transient isolated network and hotplug
  a NIC on `chr-lab`. The harness creates `openpiso-e2e-routing` / `virbr-opiso7`
  (`10.77.7.1/24`, Router `10.77.7.2`) with host static return routes for
  `10.5.50.0/24`, `10.5.10.0/24` and `10.5.20.0/24`, and a lab-only source-NAT
  exemption to the Controller. These
  addresses/network names must be unused. This preserves real Device source IPs
  without sudo or trusting browser identity. The network/NIC/NAT rule are removed
  on exit; it does not modify `default`, `hotspot-lan`, or `pisonet`. The
  `--only onboarding` scenario deletes that exemption, adds a masquerade for
  `10.5.50.0/24` as the first NAT rule (comment `openpiso-e2e-masquerade`,
  removed on exit), shows a Device's Voucher refused through it, lets the
  Controller's checklist fix add its own exemption, and briefly disables the
  routing NIC to show a missing return route. RouterOS applies a NAT change to
  new connections only seconds later, so the step waits until the rule counts a
  Device fetch of `/portal/ready.js`.

Every CHR run (full or partial, native or ARM) cleans `chr-client` immediately
after it starts, before the VLAN prerequisite or any application scenario. It
removes leftover fixtures identified by `opnw-` or `openpiso-e2e-` names/comments:
routing rules and routes before tables, DHCP clients before their board MACVLANs
or VLANs, and harness DHCP options. DHCP clients are removed only on those
harness-owned interfaces; `mgmt` and its DHCP client are never touched. It then
enables `lan1` and releases/reacquires its Device lease using the same lease step
as Vendo unpair. The full report's `chr_client_cleanup.removed` lists each
successful deletion (resource path and original row), even if cleanup fails
partway; `chr_client_cleanup.device_lease` records the fresh lease. The summary
includes only `chr_client_cleanup_removed`, the removal count. End-of-run cleanup
remains in place. ARM `--only boot` does not access CHR and skips this step.

The entry takes an exclusive `flock` on
`/tmp/openpiso-chr-lab-<uid>.lock` before the `make e2e` build or any lab access,
shared by all this user's worktrees and native/ARM runs. A second run prints the
holder's PID and entry process once and queues; after `--lock-timeout` seconds
(default 2700, one full run plus margin) it exits with the same holder message. The lock is released on exit, including failure;
never delete the lock file to bypass a holder. Direct `run.py` invocations take
the same lock; `--build` builds the Controller while holding it.

The entry reports the loaded credentials file path (never its values) before
preflight. Before touching the Router, preflight refuses the dev lab's active
`openpiso-dev-routing` network or leftover routing NIC (`52:54:00:07:08:07`).
It prints the exact `python3 -I /path/to/checkout/lab/dev/dev.py down` command,
but never executes it. Agree with the dev lab's owner before running that
cleanup; the lock does not prevent someone starting the dev lab concurrently.

The refusal and worktree-credentials checks need no CHR lab:

```sh
python3 -I controller/e2e/safety_checks.py
```

They use PATH stubs for libvirt inspection and a scratch Git worktree with
synthetic credentials. They never connect to the Router or change lab state.
The held-lock check invokes `run.py --build --lock-timeout 1` with a failing
PATH-stubbed `go` to prove the queue gives up before compilation. The credentials check invokes `run.py` in
the scratch worktree and stops at a planted preflight conflict. Full E2E
verification is separate and requires exclusive lab access.

Each run creates a fresh CA and server certificate on the Router via its serial
console, then enables `www-ssl`. This fixture trusts the isolated lab to seed a
short-lived full-permission bootstrap account over HTTPS. Its password includes
`&`, `=`, `+`, `#`, spaces and a non-ASCII character; it is submitted to the
Controller as a real URL-encoded HTML form. Fixture inspection deliberately uses
lab-only unverified HTTPS, independently of the production client; the Controller
never disables TLS verification. The harness compares the Controller fingerprint
against Router certificate readback before confirming it.

The scenario covers boot, persisted admin password, login throttling, WAL/FULL,
authenticated POST policy and cross-origin rejection, limited API credentials,
baseline, second onboarding, boot with bootstrap admin deleted, drift repair,
a real Device redirect and walled-garden portal fetch, customer admin isolation,
Vendo pairing/unpairing, MAC-spoof isolation, per-Vendo keys, forged/unsigned/type
rejection, duplicate/lost-Ack/reordered reports, epoch and total wrap, replay after
restart, Closed accounting, certificate replacement refusal, printable Voucher
Batch generation, redemption and internet access, signed-cookie issuance, live
remaining time without a cookie, invalid/unknown/used rejection, per-Device rate
limiting, and concurrent redemption by two different Router-verified Device MACs
(exactly one winner). Protocol reports are driven by the simulated Vendo. Literal
firmware vectors validate the simulator; production Ack bytes are checked with
the newly paired key. Coin Window steps cover rate validation, a zero-bought
reservation without free Router access, concurrent Insert coin (one winner),
second-Device busy, live coins, dropped Ack/replayed reports, Done with a pulse
in flight, tiered Top-up, grace settlement and late whole-window repricing,
rate snapshots, boot recovery, real 60-second coin idle, Router-confirmed
Device departure and per-Vendo sales. The departure fixture disables the NIC
and lets the Router expire its host naturally using short fixture-only host and
keepalive timeouts. It restores the Router policy afterward; the report records
measured departure time and confirms that no manual host delete was used. These cases emit named report snapshots like all
other scenarios. VLAN auto-selection additionally covers the single-Vendo
site fallback, real host-server learning, one Vendo per VLAN, a network-limited
picker and forged-ID rejection, VLAN movement with a held Coin Window, admin
overrides and no-match refusal without a reservation. Its scripted topology
and teardown are documented in `lab/chr/README.md`.
Pause coverage includes tapped Pause until refused, signed-cookie resume,
refusal of silent/unauthenticated resume, Idle Pause with stable paused uptime
and cookie-free automatic resume, and profile changes.
The Device fetches with its HotSpot source
IP through a dedicated policy-routing table, not through its management NIC.

The Default Portal browser run (`e2e/browser_portal.py`) drives Chromium
(Playwright, Pixel 7 profile) as real Devices behind the HotSpot. It needs
Docker and no root. It builds `openpiso-e2e-browser:pw1.64.1`, which is
`mcr.microsoft.com/playwright/python:v1.64.1-noble` plus `pip install
playwright==1.64.1`. Each Device is one `--rm` container on a macvlan network
(`openpiso-e2e-hs`) over `virbr-hs`, with its own MAC and IP and a static
Router lease, so the Controller resolves it through the Router like a phone.
The container runs `e2e/browser_device.py` and asks the harness for simulated
Vendo coins over stdin, so the Vendo key stays in the harness process. It walks
new, Voucher error, online, paused → Resume, Coin Window → Done, Rebind to a
second Device, and out of time. It asserts the code budget, zero CSP violations
(plus a probe that an inline style is refused) and that the HttpOnly cookie
survives `fetch` POSTs. Containers, the network and the Router leases and hosts
are removed on exit, failures included. A third Device then opens a published
Custom Portal whose `custom.js` throws and whose inline script the CSP refuses,
and still redeems a Voucher through `core.js`. Then an Operator's browser on
the host network previews a good draft, one with a misspelled marker and one
whose `custom.js` throws, in every sample state, and asserts each report and that
no request went to `/portal/api/` or off the Controller. Last, the Operator's
browser crops and uploads a logo and a cover from generated PNGs, sees them in
the preview, and publishes. This is the only test of the in-browser WebP
conversion: it checks the sizes, the caps, the centred square crop and the
cover's focus point from decoded pixels. A fourth Device then gets exactly
those images. The Operator also names a Vendo and exercises the Sales shortcuts,
Vendo filter, search, page sizes, paging, reload and Back/Forward, without CSP
violations. Real Voucher redemptions provide more than 25 matching rows, and a
CSV downloaded from a later page is compared with all filtered JSON rows while
Total Sales retains its Vendo/date context. Screenshots of All, one Vendo, a
search and a later page join the Portal screenshots in
`e2e/reports/browser-portal/`; commit those with the summary.
The bootstrap account is removed on exit; `chr-client` stays running. Lab TLS and
baseline configuration remain; the next run recreates them repeatably.

The final certificate-replacement step intentionally leaves the disposable
Controller database pinned to the old certificate. The temporary database and
processes are removed at exit. This does not affect a separately running
Controller, but do not aim the harness at a live site.

To keep another report rather than replacing the committed artifact:

```sh
python3 -I controller/e2e/run.py \
  --binary "$PWD/controller/bin/openpiso" \
  --report /tmp/walking-skeleton.json
```

For a focused run, add `--only <name>`. Without `--report`, each native partial
run writes `<name>-only.json` under `e2e/reports/`:

- `--only pairing`: onboarding, then flash pairing.
- `--only vlan`: onboarding, then only VLAN auto-select.
- `--only time-running-out`: onboarding, then only the time-running-out exhaustion
  check, with a fresh 15-second Voucher redeemed by the real Device.
- `--only sales`: onboarding, then the Sales report HTTP scenario. Its committed
  evidence uses `--report e2e/reports/sales-export.json`.
- `--only browser`: onboarding, then the Custom Portal admin flows (save check,
  image upload refusals, Publish, Revert, Reset with their images, read by a
  Device) and the Portal/Operator browser run, including preview and Sales.
- `--only vendo-list`: onboarding, then the Vendo List: rename, Status with
  Last seen, the Edit dialog (Details over HTTP, then focus, Esc and Unpair
  in the Operator's browser), Rate tier Validity and Pause Limit, Points earn,
  carry and redeem, Sales columns and Coins Out (paired and unpaired), Online
  Devices per HotSpot server and Up since across a new boot nonce on two VLAN
  Vendos, and Deactivate/Reactivate on two VLAN Vendos.
- `--only boot` (requires `--armv7`): boot the ARM guest through cloud-init and
  verify SSH, without CHR; `armv7-boot.json`.

Each report has a matching `.summary.json` (for example,
`pairing-only.summary.json`). Full native runs keep `walking-skeleton.json`;
ARM runs keep `armv7.json` or `armv7-boot.json`. An explicit `--report` overrides
any default. Full reports stay gitignored; only their summaries are tracked.

Every CHR run records VLAN host evidence in its full report's `vlan_hosts`, but
leaves the committed research hosts file untouched by default. Add
`--write-vlan-hosts` to explicitly refresh
`docs/research/results/2026-10-29-chr-vlan-autoselect/hosts.json`.

These runs keep the existing pairing-only setup/cleanup pattern and stop before
unrelated purchase, recovery and certificate-replacement steps. Both the full
report and its `*.summary.json` name the partial run in `scenario` (`pairing-only`,
`vlan-only`, `time-running-out-only`, `sales-only`, `browser-only` or `armv7-boot`),
so a partial pass is not full-run evidence. `--only` also works with `--armv7`.

Report format version 1 contains UTC start/end, binary SHA-256, result, and named
steps. The full report and summary record `git_commit`, `git_tree` (`HEAD^{tree}`)
and `git_dirty`, captured when the run starts. The tree hash lets evidence be
matched after a squash merge. Commit only the summary; leave the full report local.
Each step records authenticated Controller/ledger state, selected Router
configuration, active/host/HotSpot-user state, and visible evidence. Future
purchase scenarios should add their public ledger readback to these snapshots,
not query SQLite as a testing side channel. Failures also write a report. Secrets
are never part of the selected snapshots, and known fixture secrets are redacted
again before writing. Do not commit raw Router exports or `.env`.

Failed lab runs (full or partial) also keep a `lab_snapshot` in the full local
report, never in the committed summary. It is captured before failure cleanup,
including nested scenario cleanup, and the first capture is retained. It records
chr-client's addresses, DHCP clients, routing rules, all routes (including
inactive ones), routing tables and MACVLAN interfaces, plus the HotSpot Router's
DHCP leases and host rows for the run's Device MACs. The shared Device MAC list
lives in `lab/chr/e2e_lab.py`. Each snapshot read records either `rows` or its
`error` and continues independently; snapshot errors never replace the run's
original failure. To check a failed read, temporarily change the snapshot's
`addresses` path in that module from `/ip/address` to `/invalid-snapshot-path`,
then induce a lab failure: `lab_snapshot.chr_client.addresses.error` records the
read error while the report's top-level `error` still records the run failure.
Restore the path after the check.

The real lab is v7.23.8. A tiny HTTPS fixture reporting RouterOS 6.49.18 covers
onboarding refusal before any Router write or persisted Controller change;
it is used only for this version-refusal case, not for purchase/enforcement tests.

## Install on Armbian / Raspberry Pi OS Lite

Copy the appropriate static binary and the `install/` directory onto the stock
board. No Go toolchain or database package is needed there. From that directory:

```sh
sudo sh install.sh --binary /path/to/openpiso-linux-armv7 \
  --portal-url http://192.168.10.2 \
  --admin-network 192.168.10.0/24
```

To install from a release instead of a local build, download the binary for the
board (`openpiso-linux-armv7`, `openpiso-linux-arm64` or `openpiso-linux-amd64`)
and `openpiso-install.tar.gz` from the latest release, unpack the archive (it
holds the `install/` directory), and run `install.sh` from it as above:

```sh
base=https://github.com/H3atman/openpiso-wifi/releases/latest/download
curl -fLO "$base/openpiso-linux-armv7"
curl -fLO "$base/openpiso-install.tar.gz"
tar -xzf openpiso-install.tar.gz
cd install
sudo sh install.sh --binary ../openpiso-linux-armv7 \
  --portal-url http://192.168.10.2 \
  --admin-network 192.168.10.0/24
```

Release binaries carry the Release key, so they can update themselves (see
[Updates](#updates)). A local build without `release.pub` cannot.

The service listens on `0.0.0.0:80` by default (change it with `--listen`)
without running as root: the unit grants `CAP_NET_BIND_SERVICE`. A portal URL
without a port means port 80.

The script prompts silently for the initial admin password and confirmation.
Automation can pipe it to `--password-stdin`; do not put a password in command
arguments or a shell history entry. Installation requires root, systemd,
`runuser` (util-linux), and curl or wget, all normally supplied by these images.
Choose the management CIDR to exclude the Router's customer NAT address.

The script installs `/usr/local/bin/openpiso`, creates an unprivileged `openpiso`
service user, and initializes the ledger privately using `-init-only` before
starting `openpiso.service`. The password is bcrypt-hashed through the existing
Ledger initializer; it is not retained in a file or passed to the service.
As with native first-run initialization, root can inspect the short-lived
initializer's environment. Reinstalling preserves the existing ledger and admin
password. Persistent data stays under `/var/lib/openpiso` (0700), with SQLite WAL
and FULL synchronization unchanged. `/etc/openpiso/openpiso.env` (0600) holds
only listener, portal URL, and management CIDR. Edit it and restart the service
to change network settings.

**System-wide logging change:** installation writes
`/etc/systemd/journald.conf.d/openpiso-journal.conf` with `Storage=persistent`,
`SystemMaxUse=32M`, `SyncIntervalSec=1m`, and `ForwardToSyslog=no`. It creates
`/var/log/journal`, restarts journald, and flushes the runtime journal to disk.
Logs survive reboots, the journal is capped at 32M, and writes are batched to
spare the SD card. Disabling syslog forwarding avoids a second rsyslog copy.
The previous OpenPiso volatile drop-in is removed on reinstall. This still
applies to **all services**, not only the Controller. Independent logging
consumers can have their own policies; review those on customized boards.
Use `journalctl -u openpiso` and `systemctl status openpiso` for diagnosis.

## Updates

The Controller updates from signed releases on
[GitHub Releases](https://github.com/H3atman/openpiso-wifi/releases); see
[ADR 0010](../docs/adr/0010-controller-updates-from-signed-releases.md). The
`openpiso` service never contacts the internet. A root helper, the same binary
run as `openpiso update`, does:

- Once a day `openpiso-update-check.timer` runs `openpiso update check`. It
  fetches `release.json` from the latest release, checks its Ed25519 signature
  against the Release key compiled into the binary, and records whether a
  strictly newer version exists in `/var/lib/openpiso-update/status.json`
  (root-owned). Nothing is installed.
- The admin page shows the running version and, when a newer release exists, an
  Update button. The button only drops a request file; `openpiso-update.path`
  sees it and runs `openpiso update apply`.
- Apply downloads the binary for the board, checks its size and SHA-256 against
  the signed manifest, stops `openpiso.service`, backs up the ledger, swaps the
  binary, restarts the service and checks `/healthz`.

Only a strictly newer vMAJOR.MINOR.PATCH is installed; an older or equal release
is refused even when correctly signed. Active Devices stay online under Router
enforcement during the restart; new logins and Coin Windows wait for it.

**Rollback.** If the new binary does not come up healthy, the helper restores the
previous binary and the ledger backup together, then restarts. Both are restored
because ledger migrations only move forward: the old binary cannot read a ledger
the new one migrated. Anything recorded between the backup and the rollback, a
window of seconds with the service stopped, is lost with it. The result is in
`status.json` and the helper's journal (`journalctl -u 'openpiso-update*'`).

**Update by hand.** Over SSH, `sudo openpiso update apply` runs the same apply
immediately, and `sudo openpiso update check` refreshes the status.

**Opt out.** Disable both the daily check and the admin button's trigger:

```sh
sudo systemctl disable --now openpiso-update-check.timer openpiso-update.path
```

`sudo openpiso update apply` still works afterwards; enable the units again to
restore the automatic check.

**Mirror or lab server.** Install (or reinstall, which keeps the ledger) with
`--release-url URL`, which sets `OPENPISO_RELEASE_URL` for the helper, to fetch
releases from another server laid out like GitHub Releases (`URL/latest/download/release.json`
and `URL/download/vX.Y.Z/openpiso-linux-<arch>`). The signature check does not
change, so a mirror can delay updates but cannot install a binary the Release
key did not sign.

### Cutting a release

Maintainers only. Bump nothing in code; the version is the release tag.

Every merge to `main` that touches `controller/` releases automatically. The
Release workflow bumps the latest release's patch number (`v0.1.0` → `v0.1.1`).
Label the PR `release:minor` or `release:major` before merging to bump that part
instead (`v0.2.0`, `v1.0.0`). Merges that don't touch `controller/` release
nothing.

To release a specific version by hand instead:

1. Make sure the commit you want to release is on `main`.
2. Tag it with a strict vMAJOR.MINOR.PATCH version, higher than the last
   release, and push the tag:

   ```sh
   git tag v1.2.3
   git push origin v1.2.3
   ```

Either way, watch the Release workflow in Actions. It refuses commits not on
`main`, vets, cross-compiles the three binaries, signs `release.json` with the
`RELEASE_SIGNING_KEY` secret, verifies the signature against the committed
`controller/release.pub`, packages `openpiso-install.tar.gz`, uploads everything
to a draft release and then publishes it as latest. Installed Controllers see it
at their next daily check, and the Operator applies it from the admin page.

`v1.2`, `1.2.3`, `v1.02.3` and pre-release suffixes do not trigger the workflow
or are rejected by it. The Release key is set up once with
`scripts/release-setup.sh`, which commits `controller/release.pub` and stores the
private seed as the secret. Keep an offline copy of the seed: if it is lost,
every installed Controller must be reinstalled by hand to trust a new key.

## ARM VM E2E run (256 MiB)

The ARM mode runs **the same `scenario()` in `e2e/run.py`**, not a separate smoke
scenario. New Voucher, Vendo, and Coin Window steps added there are automatically
included, including simulated coins once #15/#16 land. It boots a fresh transient
`openpiso-armv7-<random>` domain on libvirt's `default` network and talks directly
to the guest IP. There is no HTTP proxy or SSH port-forward that would erase the
Device source IP. Only the existing CHR fixture and this uniquely named guest
are touched; no sudo is used on the host.

Host prerequisites, in addition to the native CHR requirements:

- Python 3.11+, `qemu-system-arm`, `qemu-img`, libvirt ARM/TCG support,
  OpenSSH (`ssh`, `scp`, `ssh-keygen`), and an ISO tool: `xorrisofs` (preferred),
  `genisoimage`, or `mkisofs`.
- Permission to create/destroy transient ARM domains and attach them to libvirt's
  existing `default` network. Libvirt must be able to DAC-relabel the disposable
  disk under `/var/tmp`; restrictive host security policies may need an operator
  to authorize this path. No ARM UEFI firmware or qemu-user-static is required.
- Internet access to `cloud-images.ubuntu.com`, several GiB of temporary disk,
  guest reachability from the host and Router, and exclusive use of the CHR lab.

```sh
export PATH="$HOME/.local/go/bin:$PATH"
make -C controller check
make -C controller e2e-armv7  # cross-builds armv7, then runs the current full scenario
```

The runner fetches Canonical's stock Ubuntu 18.04 armhf cloud image at the
versioned `release-20230607` URL, plus its matching direct-boot kernel/initrd.
`e2e/arm_vm.py` pins literal SHA-256 hashes from that release's checksum manifests;
no `latest` URL, mutable checksum fetch, cached guest, or prebuilt private image
is used. Each download goes into its own fresh directory, separate from seed
inputs and scripts, and is verified before use. The root image is read-only
behind a fresh 4 GiB qcow2 overlay. Cloud-init supplies a new SSH key and disables
swap, without package upgrades. **Bionic is an isolated test fixture past standard
support, not a production OS recommendation**; boards should use supported
Armbian/Raspberry Pi OS releases.

Libvirt/QEMU caps guest RAM at 256 MiB with no balloon; the harness also asserts
armv7 execution, no swap, the unprivileged service identity, and persistent
journal entries with a 32M cap and one-minute sync interval. Installation uses the real install script. All later starts/restarts
use the installed systemd unit without the install password. ARM HTTP timeouts,
including simulated Vendo requests, default to 120 seconds for TCG/bcrypt
(`--http-timeout` overrides this); the Vendo's six-second acceptor/ACK gate is
unchanged.
Boot/cloud-init has a separate 900-second limit; pass `--arm-boot-timeout 1800`
to `python3 -I controller/e2e/run.py --armv7 --binary /path/to/armv7-binary`
to allow a slower host. The configured limit is recorded in the report.

The serial console is streamed over loopback TCP into a file owned by the runner
(libvirt's own log files are root-only) from domain creation. Ubuntu 20.04's
71 MiB initrd was tried first and hit an initramfs-unpack OOM panic at 256 MiB, so
the 48 MiB 18.04 initrd is pinned instead. `--only boot` (with `--armv7`) boots the
guest and verifies SSH without touching the CHR lab or installing the Controller;
under TCG this takes about 5.5 minutes. On failure its last 16 KiB
are included in the report (`execution.serial_console_tail`) and boot-timeout
error. The full log is copied alongside the report as `armv7.serial.log` (0600)
before temporary files are removed, with its path recorded in
`execution.serial_console_log`. Treat raw logs as local diagnostics, not a
committable artifact; inspect them for sensitive data before sharing.

`controller/e2e/reports/armv7.json` (or `armv7-boot.json` for boot-only) is the
full format version 1 report, local and gitignored for debugging. The harness also
writes `armv7.summary.json` (or `armv7-boot.summary.json`) for committing: result,
step names, binary SHA-256 and size in bytes, git commit and dirty flag, and
`execution` evidence including pinned guest/boot artifacts, guest RAM/swap, and
`execution.peak_controller_rss_kib`. Summaries converted from older reports have
null git fields. A RAM-only sampler reads
the Controller process's kernel `VmHWM` across service restarts, with a final
read before each stop. This is RSS of the Controller, not QEMU/guest memory or
cgroup page cache. The final maximum covers every step executed by the harness;
it must be positive and below the 64 MiB target. Failure reports retain available
resource evidence too. The transient guest, downloads, overlay, and SSH key are
removed at exit. Do not commit raw guest disk images or logs.

Until this target is actually run, neither ARM boot nor the memory target is
proven. Commit the generated summary only after inspecting the real serial run;
do not substitute native measurements or invent an ARM result.
