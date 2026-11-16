# Connecting the M2 Portal prototype to the real Portal

Status: research, 2026-10-29. Scope: what it takes to replace the Controller's
served Portal with the M2 "Profile" prototype at
`controller/internal/controller/web/portal-prototype/index.html`, so the prototype
directory can be deleted. No code was changed. Terms follow
[`GLOSSARY.md`](../../GLOSSARY.md). Paths below are relative to
`controller/internal/controller/` unless they start with `controller/`, `docs/`
or the repo root.

Labels: **[V]** verified in the repo on 2026-10-29; **[I]** inference;
**[Open]** needs a decision.

## TL;DR

- **The prototype is a standalone JS mock, and nothing uses it.** It isn't
  embedded, served, tested or linked anywhere, so deleting it breaks nothing.
  The catch is that deleting it before the port loses the design. **[V]**
- **ADR 0004 is decided but not built.** Today's Portal is one server-rendered
  Go template, `web/templates/portal.html`. Every POST re-renders that HTML, and
  `controller/e2e/run.py` checks about 50 literal HTML strings. There is no
  `/portal/api/`, no `core.js`, no `data-portal` markers, no Portal CSP, and no
  Operator editing. **[V]**
- **The prototype's shape is the ADR 0004 shape:** JS renders all state on the
  client. Porting it into a Go template would mean rewriting it as no-JS HTML,
  which goes against both the design and the ADR. **[I]**
- **Recommended path ("ADR 0004, phase 1"):**
  1. Add one side-effect-free `GET /portal/api/state`.
  2. Make the existing POST handlers return the same JSON.
  3. Ship a default `index.html`, `portal.css` and `core.js` (ported from the
     prototype) embedded in the binary.
  4. Move the E2E checks to JSON and add one browser run.
  5. Delete `portal.html` and `portal-prototype/`.

  Defer Operator editing, banner and logo uploads, and draft/publish. **[I]**

## 1. What the prototype is

One file, 608 lines: `web/portal-prototype/index.html`. It opens over `file://`
(`:16`). **[V]**

| Part | Lines | Keep? |
|---|---|---|
| M2 Portal render: cover, overlapping logo, site name, balance card, primary action, round actions (Voucher, Pause/Connect, Rates, Resume Code), sheets, Coin Window page, toasts | `renderM2` `:368-379`, `minimalContent/Sheet/Coin` `:275-295`, toast `:259-274`, CSS `M_CSS` `:296-367` | **Port** |
| Formatting: `dur` "1 hr 30 min", `clock` HH:MM:SS, `when` en-PH local date, copy table `MSG` | `:151-169` | **Port** |
| Pause UI: ring or badge for Pauses left, Resume chip while paused | `:181-199` | Port; ring/badge choice is a setting (see §4) |
| Tap-to-copy Resume Code with an `execCommand` fallback | `:220-240` | **Port** |
| Sheets: tap the scrim or Esc to close, drag to close, two snap points | `:405-438` | Port or simplify **[Open]** |
| Stub data: `SITE`, `RATES`, `RESUME`, two named Vendos (one busy), six scenarios | `:122-149` | Drop; comes from the API |
| Stub actions: any 8-char Voucher adds 3 hr, `USEDUSED` fails, local Pause/Resume/Top-up | `:201-219` | Drop; comes from the API |
| Local 1 s countdown that flips to "out" at 0, and the Coin Window's 60 s countdown | `:398-403` | Port, but visual only and reseeded from each state read (ADR 0003) |
| Dev bar, scenario picker, `{ }` state dump, URL params `state/banner/focus/logo/pauses/pausestyle` | `:116-120`, `:181-184`, `:441-453` | Drop |
| Operator editor mock: banner on/off, built-in theme or own image, in-browser resize to ≤100 KB WebP, focus drag, template download, site name, logo square crop to 256 WebP, Pause Limit, pause style | `:74-114`, `:454-599` | Defer (admin work, not Portal) |

Variant: only **M2 Profile** (`:6-9`). Earlier variants and the iteration history
live on `prototype/portal-variants` (`4d92c35` … `a4f7d8c`). `fa5c790` (#39)
brought the file to `main`. The sample brand "Pogi PisoWifi" (`:123`) is a stub.

## 2. What the Controller serves today

- **Routes** (`controller.go:84-97`): **[V]**
  - GETs: `GET /portal/ready.js` and `GET /portal`.
  - POSTs:
    - `/portal/coins/open` and `/portal/coins/done`
    - `/portal/redeem`
    - `/portal/connect`
    - `/portal/recover`
    - `/portal/pause` and `/portal/resume`

  Resume additionally requires the signed cookie.
- **Embedding:** `//go:embed web/templates web/static` (`web_embed.go:14`).
  `portal.html` is parsed on its own, not inside the admin layout
  (`web.go:42-47`, `:33`). The dev build reads the same two directories from disk
  (`web_dev.go:17-20`). **[V]**
- **Router hand-off:** the Router's `login.html` loads `/portal/ready.js`, then
  `location.replace`s to `/portal?mac=…&ip=…` (`internal/routeros/routeros.go:410`).
  The query is ignored, and the Device is resolved from `RemoteAddr` through the
  Router (`portal.go:126`). **[V]**
- **Rendering:** every handler ends in `renderPortal` (`portal.go:44-67`). The
  only exceptions are coin errors, which are bare `http.Error` text
  (`coin_window.go:18-42,110-176`). The view is `portalView` (`portal.go:16-34`). **[V]**
- **Identity:** the HttpOnly signed cookie `openpiso_time_credit` comes first,
  then the Device binding (`portal.go:78-113`). An unbound cookie sets
  `Rebind`, and `portal.html:5-6` auto-POSTs `/portal/connect`. **[V]**
- **Side effects on GET:** `GET /portal` logs the Device back in after an Idle
  Pause (`portal.go:193-197`). It also finishes an interrupted Pause
  (`portal.go:163-169`) and refreshes the cookie (`portal.go:177-182`). ADR 0004
  says reading state must not do this (`0004:24`). **[V]**
- **Vendo selection:** `matchingVendos` returns the lone paired Vendo, or the
  Vendos whose HotSpot server matches. There is no default
  (`vendo_selection.go:5-26`, ADR 0005). `openCoinWindow` picks the Vendo itself
  when only one matches (`coin_window.go:137-149`). Busy is only known on open,
  as a 409 (`:160-162`). **[V]**
- **Coin polling:** while a Coin Window is open or a credit is pending, the page
  reloads itself every 1 s (`portal.html:22`, `portal.go:49-61`). **[V]**
- **Headers:** `no-store` and `nosniff` on everything. The CSP is set only under
  `/admin` (`controller.go:143-146`, `web.go:16`). **[V]**
- **Assets:** none. Styles are inline, and the logo is an inline SVG (`portal.html:2-3`). **[V]**

## 3. What deleting `portal-prototype/` breaks

Nothing. **[V]**

- The only reference is the file's own comment (`index.html:16`, from
  `git grep portal-prototype`).
- It isn't covered by the embed patterns (`web_embed.go:14`) or the dev FS
  (`web_dev.go:18-19`).
- Neither `Makefile` (`controller/Makefile`) nor CI (`.github/workflows/controller.yml`,
  `make -C controller check build armv7`) touches it.
- No E2E references it.
- The repo has no Go `*_test.go` files.

## 4. Gap analysis: prototype element → real data

`state` here means the proposed `GET /portal/api/state` (§5). "Have" means the
data already exists in `portalView` or the ledger.

| Prototype element (line) | Real source | Gap |
|---|---|---|
| Remaining time clock (`:258`) | `Remaining`, `UsageKnown` from a Router read (`portal.go:36-42,183-187`) | Have. Tick locally for display only and reseed on every read (ADR 0003, `0004:25`). |
| "No time yet" / "Time's up" (`:258`) | no Time Credit / `Remaining==0` | Have. Copy differs: real says "Enter another Voucher." (`portal.go:40`); prototype says coins or Voucher (`:164`). Take the prototype's. |
| Status line: Online / Not connected / Paused · Valid until … (`:170-172,257`) | `TappedPause`, `ValidUntil` (`pauses.go:103,119`) | **Missing `online`.** It is read only in the idle branch (`portal.go:190`). **`ValidUntil` is a UTC string**; send Unix seconds and format in the browser. |
| "From ₱1 for 12 min" for new customers (`:256-257`) | `Rates` (`portal.go:45`) | Have. |
| Insert coin → Vendo chooser sheet with names and "Busy" (`:277`) | `Vendos` already filtered by HotSpot server (`portal.go:46-48`) | **No Vendo name** (`internal/ledger/vendo.go:13-23`). **No busy flag** before tapping. With one match, skip the sheet and POST directly (`coin_window.go:147`). |
| Coin Window page: Vendo, ₱ counted, "+ X of WiFi", "closes in Ns", Done (`:292-295`) | `CoinWindow.Coins/LastCoinAt/Status` (`internal/ledger/coin_window.go:123-136`); idle close at `last_coin_at + 60 s` (`internal/ledger/coin_credit.go:135`, `coin_window.go:13,224-225`) | Have. The API must return the open window. The "+ time" preview may mirror `priceCoins` (`internal/ledger/coin_window.go:25-33`), but the credited seconds come from the server. |
| Voucher sheet (`:278`) | `POST /portal/redeem`; 8 chars without `IO01` (`vouchers.go:109`) | Have. Errors must come back as JSON with the Time Credit's context. |
| Resume Code sheet, copy, Recover (`:279-282`) | `ResumeCode`, withheld while tapped-Paused (`pauses.go:120-128`); `POST /portal/recover`; rate-limited (`rebind.go:89`) | Have. The prototype's `showCode` (`:199`) already respects the withholding. |
| Pause button with ring/badge of Pauses left (`:186-194,375`) | `PausesLeft` (`pauses.go:118`) | **Missing `pause_limit`** in the view (needed for the ring). The default limit differs: 3 in `internal/ledger/pauses.go:24`, 2 in the prototype (`:183`). The ring/badge style isn't a Controller setting. |
| Resume primary + "N left" chip (`:195-198`) | `POST /portal/resume` (cookie required) | Have. |
| Connect (state "ready", `:177`) | `POST /portal/connect` | Have. Needs `online` to choose it. |
| Toasts: success/warning/error by key (`:163-169,259-274`) | `Message` is free English text (e.g. `vouchers.go:146`) | **No message kind or code.** Return `{kind, text}` at minimum; a `code` lets `custom.js` translate (`0004:3`). **[Open]** |
| Cover banner, logo, site name (`:250-255`) | none | **No site settings for these.** Under ADR 0004 the Operator edits `index.html`/`portal.css` directly. The ADR doesn't cover image uploads. |
| Not in the prototype: Rebind auto-POST, Validity expired, "still joining" 503, Router outage, Idle Pause resumed | `portal.go:121-156`, `pauses.go:104-109`, `portal.go:291` | **Prototype has no states for these.** `core.js` must render them. |

Conflicts and rule checks:

- **ADR 0003 (Router is the only meter):** OK if the client countdown is visual
  only. The prototype's local "out" at zero (`:400`) must trigger a state read,
  not decide on its own.
- **ADR 0004:**
  - Prototype markup is built with JS `innerHTML` template strings. The ADR wants
    Operator HTML with `data-portal` markers that `core.js` fills
    (`0004:3`). The port has to put the markup in `index.html`.
  - `style=""` attributes (`coverStyle` `:254`, banner `:255`) and an injected
    `<style>` (`:387`) clash with the CSP that ADR 0004 wants on `/portal`
    (`0004:28`) if it mirrors `adminCSP` `style-src 'self'` (`web.go:16`).
    Use classes in `portal.css` instead.
  - `GET /portal` side effects must move to POSTs (`0004:24`).
- **ADR 0001 (no CDN, self-contained):** the prototype loads nothing external.
  OK. **[V]**
- **Identity:** the prototype never touches cookies. OK; the cookie stays HttpOnly.

## 5. Recommended path

Build ADR 0004's runtime half now and leave its Operator-editing half for later.
Delete each old piece in the same change that replaces it, so there are never
two Portals.

**API (one choke point).** `renderPortal` already gathers the whole view, so
switch it from template to JSON. The coin `http.Error` calls go through the same
path.

Proposed `state` payload:

```
{ time_credit: null | { bought, remaining, usage_known, valid_until /*unix*/,
                        expired, paused /*bool*/, pauses_left, pause_limit,
                        resume_code?, online },
  rebind, rates:[{pesos,seconds}], vendos:[{id, busy?}],
  coin_window: null | { id, vendo_id, coins, credited_seconds, closes_at },
  message: null | { kind, text } }
```

**Files.**

| Action | What |
|---|---|
| Add | `web/portal/index.html` (M2 markup from `renderM2` with `data-portal` markers; OpenPiso logo from `brand/logo/kit`; site name as plain text), `web/portal/portal.css` (from `M_CSS`, no inline styles), `web/portal/core.js` (fetch state, fill markers, POST actions as `fetch`, toasts, sheets, visual tick, emit `portal:state`) |
| Change | `web_embed.go:14` / `web_dev.go` (add `web/portal`), routes in `controller.go:84-97` (serve the static files; add `GET /portal/api/state`; POST handlers return JSON), `portal.go` (split `portal()` into a read-only state handler and move the Idle-resume/Pause-finish side effects to POST `connect`), Portal CSP next to `controller.go:145-146` |
| Delete | `web/templates/portal.html`, `parsePortal`/`portalTemplate` (`web.go:42-47`, `web_embed.go:23,31`, `web_dev.go:36`), the `portal.html` skip (`web.go:33`), `web/portal-prototype/` |
| Defer | Admin editor (banner/logo/site name/pause style), save check, preview check, draft/publish, `custom.js` loading, Vendo names |

**Ordered steps.**

1. Agree the `state` JSON and the `message` shape (open questions below).
2. Add `GET /portal/api/state`, side-effect free. Build it from the existing
   view code in `portal()`/`pauseView`, plus `online`, `pause_limit`, Unix
   `valid_until` and `coin_window`.
3. Switch `renderPortal` and the coin `http.Error` calls to write the same JSON
   plus `message`, with the existing status codes. Move the GET side effects
   (Idle-resume, finishing a Pause) into POST `/portal/connect`; `core.js` calls
   it when `state` says so (`rebind`, or idle with time left).
4. Migrate `controller/e2e/run.py`'s HTML-string checks to JSON fields. There are
   about 50 sites, e.g. `:754,849,1129,1354,2260,2599`. Its fetch helpers already
   return the body as text (`:814-820`), so `json.loads` works.
5. Port the prototype into `web/portal/{index.html,portal.css,core.js}`, serve it
   at `GET /portal`, and add the Portal CSP. Delete `portal.html` and
   `parsePortal`.
6. Add one browser E2E run of the default Portal:
   - Run it in netns `hsA` against the real CHR and Controller
     (`docs/research/simulated-phone-device.md:15,30`).
   - Playwright/Chromium aren't installed yet (`:31,134`), so this needs setup.
   - States to cover: new, online, Coin Window open → Done, paused → Resume,
     out of time, Voucher error, Rebind.
   - Artifact: one screenshot per state plus a `*.summary.json`
     (`controller/CLAUDE.md` evidence rule).
7. Delete `web/portal-prototype/`. Its history stays on `prototype/portal-variants`.

## Open questions

- **API paths:** move the POSTs under `/portal/api/` as ADR 0004 says, or keep
  `/portal/*`? The raw API isn't a contract (`0004:3`), so moving is cheap now.
- **Message shape:** server English text only, or `{kind, code, text}` so
  `custom.js` can do Tagalog?
- **Idle Pause:** should `core.js` auto-POST connect (today's behaviour), or show
  the prototype's "Not connected → Connect" state and wait for a tap?
- **Polling:** how often should `core.js` re-read state while online?
  - Each read costs Router calls (`UsedSeconds`, `TimeCreditActive`); the admin
    side already shares reads through a 5 s `usageCache` (`usage_cache.go:11-25`).
  - During a Coin Window, keep 1 s?
- **Vendo display names:** add a ledger field now, or show "Vendo N" until there
  are multi-Vendo sites?
- **Show "Busy" before the tap?** It can be computed from open/closing windows.
- **Drag-to-close sheets with snap points** (`:405-438`): keep the port, or use
  a native `<dialog>` closed by scrim, × and Esc?
- **Default Pause Limit:** 3 (ledger) or 2 (prototype)?
- **Banner and logo:**
  - Is an Operator-edited `index.html`/`portal.css` enough for the cover and
    logo, or do they need image upload?
  - ADR 0004 lists only `index.html`, `portal.css` and `custom.js`; image assets
    would need an ADR amendment.
- **Cookie behaviour:** does `fetch` + POST keep the HttpOnly cookie in the
  iOS/Android sign-in sheets the way form posts do?
  ([phone lab](results/2026-10-18-phones-plain/results.md);
  `hotspot-portal-ux.md` open questions.)

## Sources

Repo: [ARCHITECTURE.md](../../ARCHITECTURE.md),
[ADR 0003](../adr/0003-router-is-sole-meter.md),
[ADR 0004](../adr/0004-portal-is-operator-editable-static-files-over-json-api.md),
[ADR 0005](../adr/0005-vendo-serves-a-hotspot-server.md),
[hotspot-portal-ux.md](hotspot-portal-ux.md) (its line numbers predate later
changes), [simulated-phone-device.md](simulated-phone-device.md), and the code
paths cited inline.
