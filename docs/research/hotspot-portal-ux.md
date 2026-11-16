# Simple hotspot portal: reference implementations and what applies to ours

Status: research, 2026-10-28. Scope: the customer portal served by the Controller
(`/portal*`), compared with JuanFi, MikroTik HotSpot, openNDS, CoovaChilli,
pfSense/OPNsense, Omada and the captive-portal RFCs. No code was changed. Terms
follow [`GLOSSARY.md`](../../GLOSSARY.md).

Labels: **[V]** verified against the cited primary source on 2026-10-28;
**[I]** inference; **[Open]** no first-party source found.

## TL;DR

- **Best-fit reference: JuanFi MikroTik template 4.3, for the customer flow only.**
  Its actions (Insert Coin → pesos and time counted → Done, Voucher, rates,
  Remaining Time, Extend Time, Pause) are exactly the piso-WiFi vocabulary our
  customers already know. Its implementation (browser-storage identity, phone
  calling the board directly, jQuery/Bootstrap/media bundle, public
  `/data/<MAC>.txt` lookups) does not fit our ADRs and should not be copied.
- No OS vendor documents how the sign-in sheet (CNA) handles cookies, JS, popups
  or auto-close. Our own phone lab is the best evidence we have. Design for:
  no popups, no cookie reliance, server-rendered, works after the sheet closes.
- Our portal's **logic is ahead of the references** (Router-verified identity,
  POST-only Rebind, Resume Code). Its **presentation is behind**: unstyled HTML,
  durations in raw seconds, UTC expiry, developer copy, coin errors that leave
  the portal.
- Top changes, all inside the ADRs: format durations and local time; inline
  `<style>` with a status-first layout; render coin errors inside the portal;
  keep Time Credit context on error renders; customer copy. RFC 8908 is not worth
  it yet (needs a public TLS certificate; few clients act on it).

## 1. Constraints from our design

Any portal change must keep these (from the repo, not external sources):

- **Router is the sole meter** ([ADR 0003](../adr/0003-router-is-sole-meter.md),
  `ARCHITECTURE.md:13-19`). Remaining time = bought minus Router uptime. A
  client-side countdown is acceptable only as a visual reseeded from a Router
  read on every render; it must never be a source of truth.
- **Controller off the traffic path** ([ADR 0001](../adr/0001-mikrotik-enforces-controller-off-path.md)).
  `portal.html` is standalone: no layout, no `/admin/static`
  (`controller/internal/controller/web.go:42-47`). Customer assets would need a
  new non-`/admin` route; inline CSS avoids that.
- **Identity**: signed cookie `openpiso_time_credit` first
  (`portal.go:76-111`), then a Device binding matching MAC **and** IP
  (`ledger/vouchers.go:188-190`), resolved from the Router host table by
  `RemoteAddr`. The `mac=`/`ip=` query from the Router `login.html` is ignored
  on purpose (`routeros/routeros.go:407-410`).
- **Only POST moves a Time Credit.** GET never Rebinds; the Rebind form
  auto-submits (`portal.html:5-6`).
- **GET /portal is not read-only**: it can auto-resume an Idle Pause via login
  (`portal.go:191-195`). Any future polling/status endpoint must be a separate,
  side-effect-free read.
- **Resume Code is withheld during tapped Pause on purpose**
  (`pauses.go:280-285`). Do not "fix" this by always showing it.
- Phone lab ([results](results/2026-10-18-phones-plain/results.md)): iOS sheet
  drops cookies between sheets, Android keeps them, neither shares with the main
  browser; an iPhone with a new private MAC needs the Resume Code.

## 2. Our current portal

All in `controller/internal/controller/`; routes at `controller.go:84-96`.

| Flow | Code | What the customer sees |
|---|---|---|
| Router redirect | `routeros/routeros.go:407-410` | `login.html` loads `/portal/ready.js`, then `location.replace("/portal")`. |
| GET /portal | `portal.go:113-200` | Unknown Device: "Controller is ready.", Resume Code form, Voucher form, rates, Insert coin. Bound Device: time bought, expiry, remaining (seconds), Resume Code, Pauses left, Connect/Pause. |
| Redeem | `vouchers.go:65-158` | Errors render an empty view, so a customer with time sees a new-user page (`:102,128,153`). |
| Connect | `portal.go:202-292` | "You are online." |
| Pause / Resume | `pauses.go:304-376` | "Tapped Pause / Idle Pause" wording. |
| Recover | `rebind.go:444-500` | Resume Code → Rebind + login. |
| Coin Window | `coin_window.go:13-47,106-159` | Errors are bare `http.Error` text pages; the customer leaves the portal. While open, the page `location.replace`s itself every 1 s (`portal.html:22`). |

Gaps observed in the code:

1. No styling at all, though `web.go:42` says "inline styles".
2. Every duration is raw seconds (`portal.html:7,9,18,20`).
3. Expiry is UTC (`pauses.go:261`); admin already moved to local time (#32).
4. Developer copy: "Controller is ready." (`portal.html:3`), "Tapped Pause /
   Idle Pause", "Vendo {{.ID}}" (no Vendo name field in `ledger/vendo.go`).
5. Coin errors leave the portal (item above).
6. Error renders drop Time Credit context (redeem, pause, connect).
7. Out-of-time copy mentions Vouchers only (`portal.go:40`,
   `ledger/validity.go:129`).
8. Resume Code is one inline line (`portal.html:10`); the Recover and Voucher
   inputs look identical (`portal.html:15-16`) and are easy to confuse.
9. Flat list, no primary action; Recover/Voucher/coins show even when online.
10. Insert coin renders with an empty Vendo list (`portal.html:19`).
11. Coin polling reloads the full document each second, re-running Router calls.

## 3. Reference implementations

### 3.1 JuanFi MikroTik template 4.3 — chosen reference

Pinned at commit `90f6142c74724eb3bdbc7c1d726327a802e2bc90` (2024-01-07).

- **[V]** `login.html`: INSERT COIN, PROMO RATES, VOUCHER/CONNECT; the coin modal
  shows amount, time and Done.
  [source](https://github.com/ivanalayan15/JuanFi/blob/90f6142c74724eb3bdbc7c1d726327a802e2bc90/mikrotik-template/4.3/login.html)
- **[V]** `status.html`: Remaining Time, Expiration Time, EXTEND TIME, PAUSE TIME;
  an inline per-second countdown that never resyncs with the router.
  [source](https://github.com/ivanalayan15/JuanFi/blob/90f6142c74724eb3bdbc7c1d726327a802e2bc90/mikrotik-template/4.3/status.html)
- **[V]** `assets/js/core.js`: Pause stores remaining-time HTML in localStorage
  then logs out; the phone polls the board `/checkCoin` cross-origin every 1 s
  with no timeout or in-flight guard; identity falls back localStorage → cookies;
  auto-lookup of `/data/<MAC>.txt`.
  [source](https://github.com/ivanalayan15/JuanFi/blob/90f6142c74724eb3bdbc7c1d726327a802e2bc90/mikrotik-template/4.3/assets/js/core.js)
- **[V]** The bundle pulls jQuery, Bootstrap, Popper, pako, QR, audio, e-load, and
  Facebook chat on the status page.
- **[V]** License file is Apache-2.0 though the README says MIT.
  [LICENSE](https://github.com/ivanalayan15/JuanFi/blob/90f6142c74724eb3bdbc7c1d726327a802e2bc90/LICENSE)

**Why this one** [I]: it is the de facto piso-WiFi UX in the Philippines, so its
labels and order are what customers expect, and its flow maps 1:1 onto our
Coin Window, Voucher, Pause and remaining-time features. Everything that makes
it fragile (client-side identity and countdown, direct board calls, heavy
bundle) is exactly what our architecture already replaced. Take the screens and
words; keep our server.

WiFi5Soft: vendor site returned 403; portal source not verified. Use
[wifi5soft-architecture-client-time.md](wifi5soft-architecture-client-time.md)
(static analysis) only.

### 3.2 MikroTik HotSpot

Sources: [Hotspot customisation](https://help.mikrotik.com/docs/display/ROS/Hotspot+customisation),
[HotSpot – Captive portal](https://help.mikrotik.com/docs/spaces/ROS/pages/56459266/HotSpot+-+Captive+portal).

- **[V]** Pages `login`, `alogin`, `status`, `logout`, `error`; a request for `/`
  returns `rstatus` when logged in and `rlogin` when not — a stable way back.
- **[V]** The status window opens via JS `open(...)` in `alogin.html`; MikroTik
  notes ad popups "might be blocked by browsers popup blockers".
- **[V]** `login-by=cookie` (default lifetime 3 d) and `mac-cookie`, a
  server-side record keyed by MAC "designed to improve accessibility for
  smartphones".
- **[V]** RouterOS 7.3+ serves an RFC 8908 `api.json` (`seconds-remaining`,
  `can-extend-session`) via DHCP option 114, only when a DNS name **and** valid
  TLS certificate are configured.

### 3.3 openNDS / Nodogsplash

Sources: [FAS](https://opennds.readthedocs.io/en/stable/fas.html),
[How it works](https://opennds.readthedocs.io/en/stable/howitworks.html),
[Nodogsplash](https://nodogsplash.readthedocs.io/en/latest/).

- **[V]** "Many client CPD processes will automatically close the landing page as
  soon as Internet access is detected." Only first-party statement on auto-close
  found (and it is about other vendors' OSes).
- **[V]** An authenticated client revisiting the portal is redirected with
  `status=authenticated`.
- **[V]** When a session expires, mobile OSes usually just report "no internet"
  until Wi-Fi is toggled; desktops re-probe about every 300 s. Their advice:
  session timeouts longer than a typical visit. **Relevant to piso time-based
  expiry** — the customer will often not see the portal again on their own.
- **[V]** RFC 8908 API since v9.5.0, but "very few client devices support this
  API" (Sep 2022).
- Nodogsplash is legacy (v3.3.3-beta, splash + BinAuth).

### 3.4 CoovaChilli

- **[V]** JSON/JSONP interface (`/json/status` on `:3990`) and `ChilliLibrary.js`
  polling every 30 s by default. A JS-polling status page that assumes the page
  stays open. [source](https://coova.github.io/CoovaChilli/JSON)

### 3.5 pfSense / OPNsense

- **[V]** pfSense: the logout popup "may not work for most clients" because
  "most browsers block pop up windows".
  [source](https://docs.netgate.com/pfsense/en/latest/captiveportal/configuration.html)
- **[V]** OPNsense implements RFC 8910/8908 with a publicly trusted cert; keeps
  forced redirection for compatibility; warns iOS may have redirection issues on
  redirect-only networks; reconnecting within the timeout resumes without login.
  [source](https://docs.opnsense.org/manual/captiveportal.html)

### 3.6 Omada / UniFi

- **[V]** Omada sends a 302 to the external portal with `clientMac`, `apMac`,
  `ssidName`, `redirectUrl`, etc.
  [source](https://www.tp-link.com/us/support/faq/3231/)
- **[Open]** UniFi docs returned a bot challenge; nothing verified.

## 4. Standards and OS behaviour

- **[V] RFC 8910**: DHCPv4 114 / DHCPv6 103 / RA 37 carry the API URI; SHOULD
  NOT be an IP literal; responses SHOULD carry `Cache-Control: private`.
  [rfc8910](https://www.rfc-editor.org/rfc/rfc8910.txt)
- **[V] RFC 8908**: HTTPS, `application/captive+json`, required `captive`;
  optional `user-portal-url` (TLS), `can-extend-session`, `seconds-remaining`.
  [rfc8908](https://www.rfc-editor.org/rfc/rfc8908.txt)
- **[V] RFC 8952**: solutions MUST NOT require forged DNS/HTTP responses; client
  MUST validate the TLS cert before calling the API.
  [rfc8952](https://www.rfc-editor.org/rfc/rfc8952.txt)
- **[V] Apple**: probes on join; iOS 14+ supports option 114 and the API (TLS).
  Interception after expiry "can interfere with pages… in Safari".
  [source](https://developer.apple.com/news/?id=q78sq5rv)
- **[V] Android**: probes plain HTTP (e.g. `connectivitycheck.gstatic.com`) since
  5.0; Android 11 adds option 114; `venue-info-url` appears as a notification.
  [source](https://developer.android.com/about/versions/11/features/captive-portal)
- **[V] Windows**: NCSI probes `msftconnecttest.com`; sign-in opens the default
  browser, not a sandboxed sheet.
  [source](https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/internet-explorer-edge-open-connect-corporate-public-network)
- **[Open]** No vendor documents CNA cookie/localStorage persistence, JS
  completeness, `window.open`, or auto-close timing, or whether phones act on
  `seconds-remaining`. Our phone lab results are the evidence.

## 5. What makes a piso portal simple [I]

Derived from §3–4 and our phone lab:

1. **One screen, status first.** Remaining time + one primary button, then
   "Buy time" (Insert Coin, Voucher, rates), then "Lost your time?" last.
2. **Human durations and local time.** "1 hr 30 min", not "5400 seconds".
3. **No popups, no client identity.** Both MikroTik and pfSense say popups get
   blocked; our lab shows cookies are unreliable. Server-side identity (ours) is
   right.
4. **Never strand the customer.** Every error renders inside the portal with
   their status still visible.
5. **Tiny page.** Inline CSS, no framework; the sheet must render in ~2 s on
   cheap Android.
6. **A stable way back.** The sheet auto-closes after login and phones often
   won't reopen it on expiry, so the customer must know to reopen the portal
   address themselves (MikroTik's `/` → `rstatus` pattern).
7. **Words customers know.** JuanFi's: Insert Coin, Done, Voucher, Remaining
   Time, Pause, Extend Time.

## 6. Recommendations for our portal

Priority order. Paths relative to `controller/internal/controller/`. All respect
§1; none adds a Controller-side meter.

1. **Format durations and local time.** Add a `FuncMap` to `parsePortal`
   (`web.go:45-47`, like `parsePages`) with a duration formatter; apply at
   `portal.html:7,9,18,20`; local time at `pauses.go:261`. E2E asserts literal
   strings in `controller/e2e/run.py` (≈13 sites, e.g. `:652,664,712`) — update
   in the same change.
2. **Inline `<style>` and status-first layout** in `portal.html` (§5.1). No new
   route, no assets. Hide Recover/Voucher behind "Lost your time?" / "Buy time"
   sections; hide Insert coin when no Vendo is paired (`portal.html:19`).
3. **Coin errors inside the portal.** Replace `http.Error` in
   `coin_window.go:13-47,106-159` with `renderPortal` plus the same status
   codes (keep `run.py:2153-2166` passing).
4. **Keep context on error renders.** Use `s.pauseView(tc)` when the Time Credit
   is known: `vouchers.go:102,128,153`, `pauses.go:325,360`, `portal.go:207-222`.
5. **Customer copy.** Drop "Controller is ready." (`portal.html:3`,
   `run.py:2621`); "Paused — tap Resume" / "Paused while idle"; out-of-time
   "Insert coins or enter a Voucher" (`portal.go:40`, `ledger/validity.go:129`);
   distinct labels for Resume Code vs Voucher inputs.
6. **Resume Code prominence.** Boxed callout with "screenshot this" at
   `portal.html:10`, shown on purchase and before Pause. Keep the tapped-Pause
   withholding (`pauses.go:280-285`).
7. **"Come back here" hint.** After "You are online", show the portal address
   to bookmark/reopen for remaining time and Pause, since the sheet auto-closes
   and won't reliably reopen on expiry (§3.3).
8. **Optional: lighter coin polling.** Replace the full reload at
   `portal.html:22` with a small inline `fetch` that swaps `<main>`. **Caution:**
   GET /portal can auto-resume an Idle Pause; a poll during a Coin Window is
   fine today, but a general status poll needs its own side-effect-free endpoint.

Not recommended now:

- **Client-side countdown as truth** (JuanFi style): violates ADR 0003. A
  reseeded visual-only countdown is allowed but YAGNI.
- **RFC 8908 API**: needs a DNS name + publicly trusted cert on the Router, and
  few clients act on `seconds-remaining`. Revisit only if we get a public
  hostname.
- **localStorage/cookie identity, board calls from the phone, public MAC files**:
  all superseded by our identity model and ADR 0001.

Later / blocked: Vendo display names (schema field), QR purchase (#24), Coin
Window abuse ban (#23), outage clock (#22).

## Open questions

- Filipino/Taglish copy: which words do customers actually use? No source;
  needs operator input.
- Does an inline `fetch` swap behave in the iOS/Android sheets? Test with the
  Playwright-in-`hsA` loop from
  [simulated-phone-device.md](simulated-phone-device.md) before relying on it.
- Page-weight budget in the sheet on low-end Android: unmeasured.
- Accessibility in the captive sheets (font scaling, TalkBack): unresearched.

## Sources

Primary sources are linked inline in §3–4. Repo sources:
[ARCHITECTURE.md](../../ARCHITECTURE.md), [ADR 0001](../adr/0001-mikrotik-enforces-controller-off-path.md),
[ADR 0003](../adr/0003-router-is-sole-meter.md),
[juanfi-architecture.md](juanfi-architecture.md),
[wifi5soft-architecture-client-time.md](wifi5soft-architecture-client-time.md),
[hotspot-identity-lab.md](hotspot-identity-lab.md),
[phone lab results](results/2026-10-18-phones-plain/results.md).
