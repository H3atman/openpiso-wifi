# Portal contract

The Default Portal is four embedded files: `index.html`, `portal.css`,
`core.js` and an empty `custom.js`. `core.js` is the only part an Operator must
keep, and it cannot be edited. A Custom Portal is an Operator's `index.html`,
`portal.css` and optional `custom.js`, edited from the admin panel, that loads
`/portal/core.js` and uses the markers below. It may restyle everything, hide
optional markers, or listen to `portal:state` and draw its own UI. `custom.js`
runs as its own script after `core.js`, so if it throws, `core.js` keeps working.

## The JSON state's `rates`

`rates` are the rates of the Vendo serving the Device: its own if the Operator
customized them, otherwise the site rates. When several Vendos serve the
Device's HotSpot server, `rates` are the first one's, and each `vendos` entry
carries that Vendo's own `rates`, in the same shape, for the picker. With no
Vendo serving, `rates` are the site rates and `vendos` is empty. A rate is
`{pesos, seconds, validity_seconds, pause_limit}`. Custom Portals that read
only `rates` keep working ([ADR 0006](adr/0006-rate-tier-sets-validity-pause-limit-and-points.md)).

`core.js` does three things:

1. Sets `data-portal-state` on `<html>`.
2. Fills the value markers.
3. Handles clicks on the action markers.

Which controls a state shows is decided only in CSS, keyed on
`html[data-portal-state]`. The JSON state itself is described in
[controller/README.md](../controller/README.md#portal-page-and-json-api).

## Rules

- Every `/portal` response carries this Content Security Policy:
  `default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`.
  Styles, scripts, images, fonts and `fetch` load only from the Controller.
  Inline `<style>`, `style=` attributes, inline `<script>` and `on*=`
  attributes are refused.
- Files are flat: `GET /portal/{file}` serves one path segment, such as
  `/portal/core.js`. There are no subdirectories.
- Images: `/portal/logo` and `/portal/cover` are the published logo and cover
  (`image/webp`), or the built-in ones while none is uploaded. Link those two
  paths and an uploaded image shows up without editing the page. The built-in
  `/portal/logo.svg` and `/portal/cover.svg` stay available.
- Budget: the page, stylesheet and script together are at most 40 KB before
  gzip. The Default Portal is about 24 KB.

## Save check

Saving a draft refuses it, with a message naming what failed, unless:

- each file is 64 KB (65536 bytes) or less, and valid UTF-8;
- `index.html` parses and loads `<script src="/portal/core.js">` exactly once;
- no file has an `http:` or `https:` URL, or a protocol-relative `//` URL in an
  HTML attribute, a CSS `url()` or `@import`, or a JS string. XML namespaces
  under `http://www.w3.org/` are allowed only as `xmlns` or `xmlns:*` attribute
  values, not as resource URLs in HTML, CSS or JS;
- every required marker appears as below. Markers inside HTML comments do not
  count.

The save check catches mistakes; it is not the security boundary. Inline
scripts and styles pass it, and the CSP refuses them in the browser.

### Required markers

| Marker | Count |
| --- | --- |
| `data-portal="remaining"`, `data-portal="message"`, `data-portal="resume-code"` | exactly once |
| `data-action="coins-done"`, `data-action="pause"`, `data-action="resume"` | exactly once |
| `<form data-action="redeem">`, `<form data-action="recover">` | exactly once |
| `data-action="insert-coin"`, `data-action="connect"` | at least once |

`insert-coin` and `connect` may repeat because the Default Portal shows them in
more than one state (Insert coin while paused, Connect on the Rebind page).
Every other marker is optional and may repeat.

## Preview

The admin panel's Preview runs the saved draft in a sandboxed iframe, with
sample states instead of a live read: `new`, `online`, `coin`, `paused`, `out`
and `error`. For each state it reports whether `core.js` showed it, which known
markers the page has, unknown `data-portal` and `data-action` names (a typo, or
an `open-<name>` without its `sheet-<name>`), and every JS error. Actions in the
preview do nothing. The iframe has an opaque origin, so `custom.js` that uses
cookies or `localStorage` throws there. The draft's cover and logo show where
the page names `/portal/logo` or `/portal/cover` in an HTML attribute or a CSS
`url()`; `custom.js` that builds those URLs itself gets no image there.

## `data-portal-state`

| Value | Meaning |
| --- | --- |
| `loading` | Set in the HTML; replaced by the first read. |
| `new` | No Time Credit on this Device. |
| `error` | No Time Credit, and the last response was an error, such as an unknown Voucher. |
| `online` | Time left and the Device is logged in. The countdown runs. |
| `ready` | Time left but not logged in, for example still joining or after an outage. Connect is offered; `core.js` tries it once per page load. |
| `paused` | Tapped or Idle Pause. Resume is offered. |
| `out` | No time left, or the Validity ended (`time_credit.expired`). |
| `coin` | A Coin Window is open or closing on this Device. |
| `rebind` | The signed cookie owns a Time Credit on another Device. `core.js` POSTs `connect` once to move it here. |

`core.js` also sets these attributes on `<html>` for CSS:

- `data-vendos`: the number of Vendos this Device can use.
- `data-points-table`: the number of Points table rows; `0` means nothing can be redeemed.
- `data-pauses-left`: the number of Pauses left.
- `data-pause-style`: `ring` when `pause_limit` ≤ 4, otherwise `badge`.
- `data-coin-status`: `open` or `closing`.

## Value markers

`core.js` fills every element with `data-portal="<name>"`, so an optional
marker may appear more than once. `remaining`, `message` and `resume-code` are
required, exactly once (see [Required markers](#required-markers)).

| Marker | Content |
| --- | --- |
| `remaining` | `HH:MM:SS`, `No time yet`, `Time's up`, or `—` if usage is unknown. The display counts down each second while online. |
| `status` | One line, such as `From ₱1 for 10 sec` or `Online · Valid until Oct 30, 9:00 PM`. |
| `valid-until` (optional) | The Time Credit's Validity date and time in the browser's locale, or empty without a Time Credit. |
| `pauses-left`, `pause-limit` | Numbers from `time_credit`. |
| `pause-ring` | An `<svg>` with two `<circle r="22">`. `core.js` sets their `stroke-dasharray` to draw `pause_limit` segments, with `pauses_left` filled. |
| `resume-code` | The Resume Code. Empty while Paused. |
| `rates` | A `<tbody>` (or `<table>`). `core.js` writes one row per rate: pesos, duration. With several Vendos, each Vendo's rates follow a `<th colspan="2">Vendo N</th>` row. |
| `points` | The Points balance as a decimal, such as `1.1`; `0` without a Time Credit. |
| `points-table` | A `<tbody>` (or `<table>`). `core.js` writes one row per Points table row: cost, duration, and a `<button data-action="redeem-points">Redeem</button>`. |
| `vendos` | A container. `core.js` writes one `<button data-action="vendo">Vendo N<small>₱1 10 sec · …</small></button>` per Vendo, the `<small>` listing that Vendo's rates. |
| `coins`, `coin-credit`, `coin-closes`, `coin-vendo` | Coin Window pesos; the time they buy (`About +1 min`, an estimate from the window's Vendo's rates, until the server reports `credited_seconds`); the idle countdown; and the Vendo's name. |
| `message` (required) | The toast. `core.js` sets `hidden`, `data-kind`, `data-code` and `role`, and writes the message here if it has no `message-text` child. |
| `message-text` (optional) | The message text, inside `message`. |
| `sheet-<name>` | A `<dialog>` opened by `open-<name>`. |

## Action markers

Clicks on any element with `data-action="<name>"` are handled by one delegated
listener.

| Action | Effect |
| --- | --- |
| `open-<name>` | Opens `dialog[data-portal="sheet-<name>"]` as a modal. The Default Portal uses `voucher`, `rates`, `points`, `code` and `vendos`. A tap on the backdrop, a `<form method="dialog">` button, or Esc closes it. |
| `insert-coin` | With one Vendo, POSTs `coins/open`. With several, it opens `sheet-vendos`. |
| `vendo` | POSTs `coins/open` with `vendo_id`, read from the button's `data-vendo-id`. |
| `coins-done` | POSTs `coins/done` with the open window's id. |
| `redeem-points` | POSTs `points/redeem` with `points_milli` and `seconds`, read from the button's `data-points-milli` and `data-seconds`. |
| `connect`, `pause`, `resume` | POSTs the action of the same name. |
| `copy-code` | Copies the Resume Code to the clipboard, or selects it if the clipboard is unavailable. |
| `dismiss` | Hides the toast. |

Two forms are handled on submit:

- `<form data-action="redeem">` POSTs `redeem` with field `code` (the Voucher).
- `<form data-action="recover">` POSTs `recover` with field `code` (the Resume Code).

When an action returns a `success` message, `core.js` resets the forms in any
open dialogs and closes them.

## Reads

`core.js` reads `GET /portal/api/state`:

- on page load;
- whenever the page becomes visible;
- every 1 s during a Coin Window;
- every 3 s while the message is `device-joining`;
- every 30 s otherwise.

Every action response is a fresh state too. The countdown is display only: when
it reaches zero, `core.js` reads state, and the Router decides.

## Points

`time_credit.points_milli` is the Points balance in thousandths: `1100` is
1.1 Points. Points outlive Validity ([ADR 0006](adr/0006-rate-tier-sets-validity-pause-limit-and-points.md)),
so an expired `time_credit` (state `out`) still appears while it holds Points,
with the `points-kept` warning. Its next coin or Voucher purchase creates a new
Time Credit and carries the balance to it. A Resume Code for an expired Time
Credit Rebinds it when it holds Points (`points-kept`) and is refused with
`resume-code-expired` when it holds none. Each `rates` entry carries
`points_milli`, earned per `pesos`, when its tier earns Points; the field is
absent otherwise.

`points` is the Points table the Device can redeem from: that of the Vendo
serving the Device's HotSpot server (the first of several, as with `rates`)
when it has its own, else the site table, set at `/admin/points`. A
Deactivated Vendo serves no one. It is `[]` when the table has no rows. Each row is
`{points_milli, seconds, validity_seconds, pause_limit}`. `POST
/portal/api/points/redeem` with a row's `points_milli` and `seconds` buys that
row as a Top-up: it deducts the cost and applies the row's Validity (never
shortening it) and Pause Limit. Redeemed time is not Sales. On an expired Time
Credit (`points-kept`, "Insert coins or redeem Points") it creates a new Time
Credit, carries the Points there, then deducts. A balance below the cost is
refused with `points-too-low` (409). A row no longer in the table is refused
with `points-row-unknown` (400), so an Operator's edit never sells a different
row.

## `portal:state`

After every read or action, `document` receives a
`CustomEvent("portal:state")`. Its `detail` is the JSON state plus `state`,
the `data-portal-state` value:

```js
document.addEventListener("portal:state", (event) => {
  const { state, time_credit, rebind, rates, points, vendos, coin_window, message } = event.detail;
});
```

## Message codes

`message.code` is stable; `message.text` is for people and may change.

A `success` toast hides after 3 s and a `warning` toast after 5 s. An `error`
toast stays until it is dismissed or replaced. On a plain read, a code that is
already shown does not toast again.

| Kind | Codes |
| --- | --- |
| `success` | `online`, `paused`, `time-added-paused`, `coins-insert`, `coins-closing` |
| `warning` | `device-joining`, `time-up`, `validity-ended`, `points-kept` |
| `error` | `voucher-invalid`, `voucher-unknown`, `voucher-used`, `voucher-failed`, `no-time-credit`, `points-too-low`, `points-row-unknown`, `points-failed`, `not-on-wifi`, `unavailable`, `connect-failed`, `rebind-first`, `pause-limit`, `pause-unfinished`, `paused-needs-resume`, `resume-needs-proof`, `resume-failed`, `resume-code-invalid`, `resume-code-unknown`, `resume-code-expired`, `too-many-attempts`, `no-vendo`, `vendo-busy`, `vendo-choose`, `no-coin-window`, `coin-window-invalid`, `coin-window-failed`, `coin-window-bind-failed`, `coins-recovering` |

`core.js` also creates these codes itself, in the browser:

- `offline` (error): the Controller could not be reached.
- `copied` (success): the Resume Code was copied.
- `copy-failed` (warning): copying failed, so the code is selected for a manual copy.
