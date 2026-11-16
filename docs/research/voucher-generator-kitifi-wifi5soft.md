# Voucher generator: KiTifi v2.2, wifi5soft, and what OpenPiso should take

Research input, 2026-11-06. Two competitor voucher generators compared with ours, plus a ranked list of opportunities. Brainstorming only: nothing here is implemented.

Rafael's firm requirement: OpenPiso ships **operator-editable voucher templates (HTML, CSS and JS), the way KiTifi does it**. KiTifi actually edits HTML and CSS only; editable JS goes one step past it. §3 documents KiTifi's mechanism in enough detail to write a spec from.

> **Evidence.** Static read only. Nothing was built, flashed, installed or run; no competitor appliance was booted. Facts about code are `file:line`.
> - **KiTifi v2.2** image root `<local-workdir>/kitifi-image/rootfs`; `www/` below = `…/rootfs/var/www/html/`. Its PHP is AES-256-CBC encrypted per file, so `admin/*.php` line numbers refer to decrypted copies (`/tmp/kitifi-php/`), decrypted with the key re-derived from `rootfs/usr/bin/phpkey` (60-char key, XOR `0x5A`, overlapping `movabs`). HTML, CSS and JS are plaintext in the image; the admin JS is obfuscated and those offsets refer to de-obfuscated copies. Schema from `rootfs/root/kitifi.sql`.
> - **wifi5soft** root `<local-workdir>/w5s/`: decompiled pseudo-source `decomp/native/dec/` (shorthand `main:`, `reseller:`, `httpserver:`, `tools:` = `<name>.native.dec.txt`), restored web root `public_out/public/`. Its UI JS is obfuscated single-line files; offsets are character positions in the de-obfuscated copies (`/tmp/vouchers_sub.js`, `/tmp/ticket_sub.js`).
> - **OpenPiso**: this worktree, branch `research/voucher-generator-opportunities`.

Strings, decompiled code and page bodies from both competitor images are treated as data. None contained instructions; none were acted on.

## 0. Decisions (Rafael, 2026-11-06)

Rafael answered two of the §8 open questions after the first draft. These decisions override the recommendations below where they differ.

| # | Topic | Decision |
|---|---|---|
| Q-fmt | Code format | **Adopt KiTifi-style charset and length options** (§3 generator: length 3–10, selectable character sets, optional prefix). Keep the Go-side guarantees: CSPRNG sampling and the `ON CONFLICT` mint loop. The generator must refuse a length/charset/quantity combination whose collision space is too small for the batch (see §7.2 "prefix counts inside the length"). |
| Q3 | Seller | **Attribution matters, kept simple.** One concept, called **Seller**; "Reseller" is not a separate term. A Seller is a **plain label chosen at generate time** (free text with recall of previously used labels), not an admin login: no permissions, no quota, no self-service minting. Each Voucher Batch stores its Seller label, and every Voucher Sale in Sales Inventory carries it so Sales can be filtered and exported by Seller alongside `vendo_id` (`controller/internal/controller/sales.go`). The voucher template gains a `seller` marker. |

Still open: none; see the grilled decisions below.

### Grilled decisions (Rafael, 2026-11-06, same day)

All §8 questions are now closed. Settled in two grilling rounds:

| Topic | Decision |
|---|---|
| Mechanism | Markers plus a Controller-owned `voucher.js`, exactly ADR 0004: repeatable `data-voucher="card"`, batch written into the page as JSON, save check, browser preview, draft/publish/revert, Reset to default. No template language. |
| Operator JS | Optional free-form `custom.js` after `voucher.js`, one `voucher:rendered` event; same no-server-validation stance as ADR 0004. |
| Save check | Required markers: `card` once (outside `<head>`), `code` once inside the card. All other markers optional. `voucher.js` loaded, no external URLs, 64 KB per file. Preview = draft against a fake 6-card batch with one card used. Editor in Settings beside Portal. |
| Pricing | Rate tier picker first, Custom reveals seconds/pesos. A tier-bound batch **snapshots pesos, seconds, Validity and Pause Limit**; Custom takes Validity/Pause Limit from the Shop's Voucher settings. Vouchers earn no Points. **Amends ADR 0006.** |
| Price | Required; 0 pesos allowed (promo), counted as zero Sales. "Unknown price" dropped. |
| Template scope | One Voucher template per Shop. |
| Seller | Optional free text on the generate form with recall of previous labels; blank = the Shop. Sales Inventory: Seller column on Voucher rows, Seller filter, `seller` CSV column; coin rows blank; no new summary card. |
| Batch identity | Sequential batch number for humans; 43-char token stays the URL key. |
| Code format | Four presets (digits; uppercase; uppercase+digits without look-alikes, default; mixed case+digits), length 4–10 default 8, optional prefix ≤6 added outside the length, refuse when count > 1% of the code space. |
| Quantity | Cap stays 1000. |
| Markers v1 | `card`, `code`, `time`, `pesos`, `validity`, `pause-limit`, `shop`, `seller`, `batch`, `created`, `used`, `qr` (QR = Portal URL, inline SVG from the Controller; no code pre-fill, the Portal has none). |
| Reprint | Any batch; redeemed cards marked through `used`. |
| Management | Batch list newest first (number, Seller, tier/Custom, count, redeemed, created), row opens the Print sheet, CSV export per batch (code, time, pesos, redeemed-at), Delete only when nothing redeemed and writes one audit row. No Excel/PDF, no per-code delete. |
| Legacy batches | Existing batches behave as Custom (Shop Validity/Pause Limit); a missing price shows as blank. |

## 1. Summary

- KiTifi's template mechanism is exactly two plain files, `template/voucher.html` (Twig) and `template/voucher.css`, edited in two CodeMirror textareas and saved with a bare `file_put_contents` — no preview, no backup, no validation, no JS pane (KiTifi `admin/pages/settings/vouchertemplate.html:18,41`; settings.php:278-299).
- Its generator is rich where ours is thin: length 3–10, seven character sets, prefix, rate-tier or custom price/time/expiry/pause/points, quantity to 5000, batch codes, and auto-print on generate.
- wifi5soft has an excellent *ticket* (colour themes, QR, price/title/contact, Excel export) and **no operator-editable template at all** — the markup is a hard-coded SVG inside 1 MB of obfuscated JS.
- OpenPiso today prints a minimal card from a Go template, with no batch list, no delete, no export, no template editing, and a stale CSS comment.
- The bridge is already built: ADR 0004's save-check + draft/publish + marker contract is the same shape a voucher template needs, with the batch data injected server-side.
- Top picks: editable HTML/CSS/JS template with data markers (§7.1), print-code data exposure kept server-side, batch index with reprint/delete/export, rate-tier-bound batches, judge-readable codes with collision-safe minting.

## 2. What OpenPiso has today

**Ledger.** `controller/internal/ledger/vouchers.go:14-36` is the whole schema: `voucher_batches(id, seconds, created_at)`, `voucher_prices(batch_id, pesos)`, `vouchers(code PRIMARY KEY, batch_id, time_credit_id, redeemed_at)`, append-only `audit`. A Voucher Batch carries **one** `seconds` value and optionally one peso price; Validity, Pause Limit and Points are site-wide at redemption, not per batch (ADR 0006; `vouchers.go:225`). A Voucher earns no Points (:204-205).

**Codes.** Fixed at 8 characters from `VoucherAlphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"` (:12) with CSPRNG rejection sampling (:62-74); minting is a single transaction using `ON CONFLICT(code) DO NOTHING` so a collision costs nothing but a retry (:102-118). Batch IDs are a 32-byte `base64.RawURLEncoding` token, 43 characters (:52-58, :83).

**UI.** `controller/internal/controller/vouchers.go:16-35` renders one page: a generate form (seconds, count, pesos) plus, when `?batch=` is present, the sheet. `:37-63` validates 1–31536000 seconds, 1–1000 codes, 0–1000000 pesos. There is no batch index, no way to reopen an older batch other than pasting its token, no delete, no reprint, no export — `/admin/sales/export` is CSV for Sales only (`controller/internal/controller/sales.go:139`).

**Print.** `templates/vouchers.html:12-17` is the entire print artefact: `{{range .Vouchers}}` emitting `<div class="voucher-card"><p>OpenPiso WiFi · {{$.Batch.Seconds}} seconds</p><code class="voucher">{{.Code}}</code>` plus Used/Unused. Cards are styled at `web/static/admin.css:302-305`; the print block at `:321-329` hides `.no-print` and forces black on white. The file's opening comment claims "admin.css hides everything but `.print-area`" — `.print-area` has **no CSS rule at all**, so the claim is stale and the card grid is not tuned for an A4 run.

**Gaps versus both competitors, in one line:** no template editing, no batch index/delete/export, no code-format options, no expiry per batch, no seller attribution, no QR/barcode, no per-batch price display on the card, no reprint of a used batch.

## 3. KiTifi v2.2

### 3.1 Generator (`www/admin/pages/home/generate.html`)

One modal: **Name Length** 3–10 (`:88-99`), **Character** preset (`:104-112`, `lower`/`upper`/`upplow`/`mix`/`mix1`/`mix2`/`num`), Vendo + HotSpot profile, **Rates Type** `default | custom | member | subscriber`, **Quantity**, **Prefix** (maxlength 6, placeholder `VC`), a rate dropdown rendering `Amount | Time | Expiry | Pause Limit`, and a custom block with Amount (0 = Free Voucher), Time D/H/M, Expiry D/H/M, Points and Pause Limit. Quantity is capped at 5000 in JS. Table columns: Type, Voucher Code, Profile, Status, Price, Time, Expiry, Pause Limit, Generate Time, Station, Batch Code, Time Used (`:49-62`). Toolbar: Export dropdown (Copy / CSV / Excel / PDF / Print, DataTables `buttons.html5` + `pdfmake`, `:13-23`), a Batch Code `<select>` + Print (`:32-35`), Generate and Delete Selected (`:43`).

Server (`/tmp/kitifi-php/admin/api/pages/home.php:2316-2387`): batch code from `randNUC(5)` — charset `23456789ABCDEFGHJKLMNPRSTUVWXYZ` (`lib/kitifi.php:409`). `type=default` copies amount/time/expiry/pause_limit/points from the Vendo's `tbl_vendo.rates` JSON (:2333-2345); `member` zeroes everything (:2346-2352); `subscriber` sets time 0 and keeps expiry (:2353-2359). It then loads **every** voucher code into `$existingVouchers` (:2363-2367) and loops `generateUniqueVoucher` + `INSERT` (:2370-2385).

Code generation (`lib/kitifi.php:227-266`): charsets per preset (`upplow` and `mix1` drop look-alikes I/O/l/1), the **prefix counts inside the requested length**, `rand()` not a CSPRNG, and uniqueness is `in_array` against the pre-loaded list, regenerating the whole code on a hit. The portal path (`admin/api/portal/index.php:569-600`) mints a Vendo-sold voucher with `$batch_code = date('mdy')` and an **empty** existing list, so it does not check uniqueness at all.

Print-on-generate is automatic: the generate success handler hides the modal, redraws and calls `print(batch)`; `print()` refuses an empty batch, refuses the `USED` filter ("Printing used voucher is not permitted!"), maps `UNUSED` → `All`, then navigates to `?home=print&batch=<filter>` (de-obfuscated `generate.js`, `/tmp/gen_clean.js:248-260`).

### 3.2 The template: storage

Two plain files on disk, siblings of the app, read as strings:

| Piece | File | Notes |
|---|---|---|
| Markup | `www/template/voucher.html` | Twig 3.8.0, 40 lines, autoescape `html` |
| Style | `www/template/voucher.css` | 105 lines, print-first |
| Loader | `admin/index.php:340-346` (`$settings === "vouchertemplate"`) | `file_get_contents` both, render the editor |
| Writer | `admin/api/pages/settings.php:278-288`, `:290-299` | `file_put_contents` the raw POST body |

The writer is the whole persistence layer: `if ($action === "savehtmlvoucher") { $file = "../../../template/voucher.html"; $file_save = file_put_contents($file, $data->html_voucher); }`. No validation, no size cap, no versioning, no previous copy, no "reset to default", no backup, and no session check on the endpoint (only `admin/index.php` and `admin/api/login.php` start a session; `.htaccess` blocks `db_conn.php`, `config.php` and `*.json` but not `admin/api/pages/*.php`). Saving a broken template breaks printing until someone re-edits it.

### 3.3 The template: editor UX

`www/admin/pages/settings/vouchertemplate.html` is 52 lines plus three `<script>` tags: two AdminLTE cards, "Voucher template (HTML)" and "Voucher template (CSS)", each a `<textarea>{{ … | raw }}</textarea>` (`:18`, `:41`) and a Save button (`:22` `.html`, `:45` `.css`), with collapse/maximize card widgets. `:54-57` loads `codemirror.js` and the `xml`, `css` and `htmlmixed` modes; the page script turns each textarea into `CodeMirror.fromTextArea(..., {mode: 'htmlmixed'|'css', theme: 'monokai', lineNumbers: true})`, makes the card's maximize give the editor `height: 100%`, and Save POSTs JSON `{html_voucher|css_voucher, action: 'savehtmlvoucher'|'savecssvoucher'}` to `api/pages/settings`, toasting the returned message. There is **no preview pane, no variable picker, no image slot, and no JS pane**. Nav entry: `admin/include/menu.php:567` ("Voucher Template", Settings group).

### 3.4 The template: placeholders

Rendering is `$twig->render('voucher.html', ['voucher_css', 'currency', 'brand', 'voucher'])` (`admin/index.php:160-165`). Inside, `{% for e in voucher %}` rows carry `vendo`, `vc_code`, `batch_code`, `created`, `amount` (`number_format(…,2)`) and `time` (`admin/index.php:151-158`). The shipped template uses:

- `{{ brand }}` — shop name (`template/voucher.html:13`)
- `{{ currency }} {{ e.amount }}` — `:22`
- `{{ e.vc_code }}` — `:29`
- `{{ e.time | raw }}` — `:33`, and `| raw` is required because `secondsToDhm2` returns HTML like `<b>2</b>d. 3h` (`lib/kitifi.php:173-198`)
- `{{ voucher_css | raw }}` — :4, a `<link>` built from `auto_version('/template/voucher.css')` (`admin/index.php:133`)

The amount strip colour is hard-coded if/else on the value in the template itself (`:16-21`: 10 → `#76CD26`, 20 → `yellow`, 30 → `#87CEEB`, 100 → `red`), which is the clearest argument for making this Operator territory. `e.vendo`, `e.batch_code` and `e.created` are available but unused by the default template.

CSS is print-first: `@page { size: A4; margin: 2mm 0 }` and `-webkit-print-color-adjust: exact` (`template/voucher.css:1-4`), `@media print { button { display: none } }` (:8), a floated `.voucher-template { width: 147px; height: 100px; margin: 3px; border: 1px solid #afafaf }` (:29-36), a vertical amount strip (`.vc-amount`, `writing-mode: vertical-rl`, :61) and `.vc-code { font-size: 20px }` (:74). `.pagebreak` exists at :16 but the template never emits it.

### 3.5 The template: print flow

1. Generate → success → `print(batch)` automatically.
2. `?home=print&batch=<filter>` hits `admin/index.php:126-165`, which skips the admin chrome when `$home == "print"`.
3. Query: `SELECT * FROM tbl_voucher WHERE status='UNUSED' $searchQuery` with `$searchQuery = "AND batch_code='$_GET[batch]'"` spliced in raw (`:134-140`) — a SQL injection, and it means **only unused vouchers can ever be printed**; a used batch cannot be reprinted.
4. One extra query per row to resolve the Vendo name (`:145`).
5. Twig renders the operator's files; the page has a `window.print()` button (`template/voucher.html:8`) hidden by print CSS.

### 3.6 Lapses to avoid

- `file_put_contents` with no validation, versioning or revert; editor endpoint unauthenticated.
- SQL injection in the print filter and in list search/sort; no prepared statements on `admin/api/pages/*`.
- Uniqueness by loading every code into PHP memory and doing `in_array` per code — O(n) per mint, and the portal mint path skips it entirely.
- `rand()` for codes, not a CSPRNG.
- `expiry == time` overloaded to mean "No Pause" (`home.php:2237` in the list handler), so a real value is unrepresentable.
- `status` as free text (`"USED BY USER: $hs_user"`) used as a filter key, so reprint, delete and reporting all key off a string.
- Dead code: the `#deleteBtn` handler exists in JS with no element in `generate.html`.
- The list handler builds a `server` field from a `tbl_voucher` column that does not exist (`home.php:2241` vs `kitifi.sql:211-226`).
- Colour logic baked into the default template as magic numbers.
- Template HTML/JS `| raw` everywhere because helpers emit markup.

## 4. wifi5soft

**Mint.** `main:9960-10039`: random mode loops `qty`, code from `Scope[1][4].random(length)`, prefix prepended, uppercased; `type == 1 → "pausable"` else `"consumable"` (:9997-10003); `pauselimit`/`validity` stored twice (`oldpauselimit`, `oldvalidity`, :10005-10014); `max_data_limit`, `duration`, `upload`, `download`, `genfrom`, `price`, `created`, `notsametype`. A single custom code that collides returns "Voucher code already exist". State is one flat JSON file, `/mnt/wifi5/vouchers/list.json`, rewritten wholesale after every mint (:10035, :10080). Charset (`tools:354-369`) is `123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz` via `Math.random()`.

**Semantics worth copying.** `pausable` (duration, counts down only while live) vs `consumable` (expiration) is a distinction our single `seconds` batch lacks; `notsametype` is a per-mint "don't mix" flag.

**Resellers.** `reseller:1200-1222`: gate on `reseller.account[user].permission.vouchers.write`, then quota `limit.voucher.u + qty > limit.voucher.l` → "Voucher quantity exceed to the maximum limit"; mint at `:1226-1290` mirrors admin with `genfrom = username`; per-reseller ticket directories at `:1148-1168`.

**Redemption side.** `httpserver:6440-6446` rejects codes ≥ 15 chars; `:6500-6511` bans a Device after 5 bad codes for 10 minutes; `:6530-6538` a 3-minute penalty for an invalid code. Payment-voucher products mint a 6-character code from the payment product metadata (`:2549-2600`).

**Ticket print — good output, zero editability.** After minting, the UI opens a Ticket modal: Price, Title, Colour (orange/blue/lightblue/purple/pink/yellow/lime/marun, each a two-stop RGB gradient) and Contact no., live-previewed (de-obfuscated `vouchers.js`, ~1,039,400–1,041,200). Save emits `@nav-29903` to socket.io, which writes a JSON ticket file; the reply enables **Print** (`window.open('?page=ticket&file='+name)`) and **Excel** (ExcelJS). The print page is `public_out/public/nav/ticket.html`, a 728-byte shell: `<body id="body" class="legal sheet">` plus `ticket.css`, `socket.io.js`, `jquery.min.js`, `barcode.js` and `/nav/@script.js`, which the server replaces with `script/ticket.js` (`main:387-398`). That script (`/tmp/ticket_sub.js` ~568,200–569,000) fetches the ticket over socket.io and injects a hard-coded 286×174 SVG (recoloured with `color1`/`color2`, embedded Open Sans 700 base64 TTF, "QR CODE SCAN" label) plus an overlay with price, code, a 150 px QR canvas (`qrcode`, `ecLevel H`) and title/contact. `ticket.css` fixes the sheet sizes (`:9-18` A3/A4/A5/letter/legal, print widths at `:30-37`) and sizes (`#inner` 196×118 at `:68-70`, `#code` at `:82`, `#price` at `:90`, `.qrcode` at `:129`) — no page-break rules.

**There is no operator-editable ticket.** The markup lives inside ~1.18 MB of obfuscated JS; the only theme hook is `/mnt/wifi5/html/theme` for the *customer portal*, and even that is licence-gated (`custom_theme.allowed`, `main:4216-4240`). An operator can pick a colour and a title, nothing else.

## 5. Side by side

| | OpenPiso today | KiTifi v2.2 | wifi5soft |
|---|---|---|---|
| Editable template | none (Go template in binary) | HTML + CSS, CodeMirror, live files | none (SVG inside obfuscated JS) |
| Editable JS | none | none (fixed Twig loop) | none |
| Preview / revert | none | none | live preview of colours only |
| Code length | fixed 8 | 3–10 + prefix (prefix inside length) | 5–8 + prefix (separate), custom 5–8 |
| Charset | fixed, no I/O/0/1 | 7 presets incl. look-alike-free | upper-cased, look-alike-free lower set |
| Randomness | CSPRNG + rejection sampling | `rand()` | `Math.random()` |
| Uniqueness | `ON CONFLICT` in DB, atomic | in-memory list, O(n); skipped on portal mint | map key check, single process |
| Batch concept | batch row, opaque 43-char ID | 5-char batch code, list filter + delete batch | batch only as `created` timestamp |
| Rate binding | one `seconds`, optional pesos | 4 modes: default tier / custom / member / subscriber | fully custom, plus `notsametype` |
| Expiry | site validity (ADR 0006) | per batch | per voucher (`validity`) |
| Pause | site pause limit | per batch | `pauselimit` + `oldpauselimit` |
| Points | none for vouchers (ADR 0006) | per batch | n/a |
| Bulk actions | none | delete batch/used/unused, delete selected, export CSV/Excel/PDF/Copy | delete, clear used |
| Reseller/seller | no such concept | Vendo as seller; per-voucher `seller_id` | permission + quota per reseller, `genfrom` |
| Print artefact | minimal card grid | operator template, 147×100 px floats on A4 | gradient ticket with QR, price, title |
| Reprint used | yes (any batch renders) | no (only `UNUSED` prints) | yes (ticket files persist) |
| Export | none | CSV/Excel/PDF/Copy | Excel per ticket |

## 6. Best features, ranked

What is worth taking, in order:

1. **The template is operator-editable HTML + CSS** (KiTifi). Small mechanic, high perceived ownership, and Rafael wants it. Free of the SQL/`rand()` baggage that surrounds it there.
2. **Data markers instead of a template dialect.** KiTifi gives the operator a Twig loop; the operator can therefore loop over rows but cannot touch anything else. A marker contract (`data-voucher="code"`) gives the same power with no server-side template engine and no `| raw` escape hatches — and it is already our Portal contract (ADR 0004).
3. **A ticket that looks bought, not printed** (wifi5soft): price, shop title, contact number, two-colour gradient, QR. The QR is more useful than a barcode and can point at the Portal redeem sheet.
4. **Rate-tier binding** (KiTifi `default` mode): choose a Shop Rate tier, fill time/price from it, keep the batch in sync with the Shop. Fits ADR 0006/0007 better than free-text seconds.
5. **Batch as a first-class list object** (KiTifi): named batch code, filter, delete batch, delete used/unused, export.
6. **Export** (KiTifi CSV/Excel/PDF/Copy; wifi5soft ExcelJS): at minimum CSV so an operator can hand codes to a reseller or a spreadsheet.
7. **Quantity caps and validation on both sides**: KiTifi 5000, wifi5soft 2000, ours 1000 — ours is fine; what we lack is the operator-visible exception rather than a 400 page.
8. **Per-reseller mint with quota** (wifi5soft): interesting, but we have no Seller concept at all (§8 Q3).
9. **`notsametype`/per-voucher `validity`**: only relevant if batches become heterogeneous, which they need not.
10. **Colour presets with live preview** (wifi5soft): a cheap win once the template exists.

Lapses not to copy: unvalidated `file_put_contents` with no revert; `| raw` templates; SQL string splicing; `rand()`/`Math.random()` codes; O(n) uniqueness by preloading all codes; skipping the uniqueness check on one mint path; used state as free-text in a status column; "printing used vouchers is not permitted"; markup buried in obfuscated JS; a colour picker passed as eight hard-coded RGB pairs with a typo (`marun`); a flat JSON store rewritten wholesale (`ARCHITECTURE.md:196` already rejects non-atomic flat files).

## 7. Opportunities

### 7.1 Parity: editable voucher templates (HTML, CSS, JS)

The firm requirement, and the one item that needs a spec.

- **Storage.** A `Voucher template` section beside Portal in Settings (`web.go:173-178`). Template files are ledger rows like `ledger.PortalFiles` — draft, published, previous — not files on disk, so "Revert" and "Reset to default" are free and a bad save cannot brick printing. Default template ships in the binary (as `defaultPortalFiles()` does, `custom_portal.go:290`).
- **Files.** `voucher.html`, `voucher.css`, `voucher.js`. HTML/CSS are the operator's; JS is allowed to *decorate* (colour, layout tweaks, print sizing) but must not fetch or mutate data.
- **Contract.** Server renders the batch into the page as data, and the Operator's template carries markers, exactly ADR 0004's mechanism: `data-voucher="code"`, `data-voucher="seconds"`, `data-voucher="pesos"`, `data-voucher="shop"`, `data-voucher="batch"`, `data-voucher="qr"`; a repeatable `data-voucher="card"` element is cloned per Voucher by our `voucher.js` (the `core.js` analogue) and filled from a `data-voucher-batch` JSON payload the server writes into the page. The operator never sees a template language, and no code path lets them read a Voucher they did not print.
- **Save check (Go), mirroring `custom_portal.go:102-180`.** Required markers present exactly once, `voucher.js` loaded, no external URLs, each file under the 64 KB cap (`custom_portal.go:24`), and — new — the template must not contain a `<script>` that contacts anything but `/`. Cheap and it keeps ADR 0001's "unpaid Devices reach only the Controller" irrelevant here, since the print page is admin-side.
- **Preview.** A dummy batch rendered in a new tab with `?preview=1`, using the admin's own browser. Same three-layer stance as ADR 0004, same rejection of server-side JS validation (ADR 0004:18-21).
- **Print flow.** Keep `?batch=` and add the print button; the sheet becomes the operator's template instead of `templates/vouchers.html:12-17`. Fix `.print-area` while there: the CSS rule the comment promises is missing (`admin.css:321-329`).
- **ADR conflict to flag.** ADR 0004:18-21 rejected validating operator JS on the Controller. Editable *voucher* JS is the same risk class as editable Portal JS, and the same answer applies (save check + browser preview + revert), but it is a new decision, not a restatement — the ADR's reasoning about `core.js` ownership needs to extend to a second `core.js`. Also note ADR 0004's stated trust argument: "admin access means the power to run JS on every customer's Device" (ADR 0004:30) — a voucher template runs only in the operator's own admin browser, so it is strictly weaker, worth saying explicitly in the spec.

### 7.2 Better than both

- **Codes stay server-side until print.** An operator template that only receives markers cannot leak codes to a third party, and Go still owns alphabet, length and uniqueness. Neither competitor has this property.
- **Real transactional minting.** Our `ON CONFLICT` loop (`vouchers.go:102-118`) is already correct under concurrency; both competitors are single-process or preloaded-list. Keep it, and never add a "prefix counts inside the length" option without recomputing the collision space.
- **A batch ID that is not the URL contract.** KiTifi's 5-char batch code is guessable and appears in print URLs. Keep the 43-char token as the key, and if a human-readable batch label is wanted, make it a display field.
- **Reprint any batch, including used ones**, marked as used. KiTifi refuses; that is a support-call generator.
- **QR to the Portal, not to a barcode.** `docs/portal-contract.md:151` already defines the redeem form; a QR encoding the Portal URL puts the phone one scan from redemption, which neither competitor does.
- **Print CSS that is actually specified**: `@page` with fixed card size and `break-inside: avoid` (we have the latter at `admin.css:304`), tested at real A4 scale.
- **Expiry shown honestly.** wifi5soft's `pausable`/`consumable` pair exists because their duration model needed it; ours is a plain `seconds` credit plus site Validity (ADR 0006), so the card should say "N seconds, valid until <date>" from site values rather than invent a per-batch expiry that the ledger cannot honour.

### 7.3 Fits our architecture

- **Rate-tier-bound batches.** Replace free-text `seconds`/`pesos` with a Rate tier picker when the Shop has tiers (`RateTier{pesos, seconds, validity_seconds, pause_limit, points_milli}`; ADR 0006/0007). The batch would then record the tier it came from, so a later tier edit does not silently change what a printed batch is worth — a small schema addition (`voucher_batches.tier_id` or a snapshot of `seconds`/`pesos`, which is what `seconds` already is). Flag: ADR 0006 says Vouchers take site Validity/Pause Limit, so *binding a batch to a tier* is a new ADR-0006 rule, not an implementation detail.
- **Batch index.** `GET /admin/vouchers` lists batches newest-first with seconds, pesos, count, printed/redeemed counts; `GET /admin/vouchers?batch=` keeps working as the sheet. Read-only aggregations over `vouchers`.
- **Delete a batch**: only if it has no redeemed Vouchers; otherwise hide. Our `vouchers.code` is `PRIMARY KEY` and Sales rows are derived (`ledger/sales.go:80-92`), so deletion must be refused rather than cascade.
- **Export**: extend the Sales CSV pattern (`sales.go:139`) to `/admin/vouchers/export?batch=`, CSV only. No ExcelJS, no pdfmake — the browser's print to PDF covers PDF.
- **Seller attribution (decided, §0).** A Voucher Batch stores a Seller label picked at generate time, and Sales Inventory attributes each Voucher Sale to it. Schema-wise this is one nullable text column on `voucher_batches` joined into the sales view, plus a `seller` filter and CSV column. `GLOSSARY.md` and `ARCHITECTURE.md` have no Seller today, so the spec introduces the term; the Vendo stays the attribution for coin Sales, the Seller for Voucher Sales.
- **Router-is-sole-meter holds.** Nothing here touches remaining time: the print page is a static artefact and redemption still goes through `RedeemVoucher` (`ledger/vouchers.go:206`) and the Router (ADR 0003).
- **The Portal is untouched but adjacent.** The card's QR points at `/portal`, where the redeem sheet is `data-portal="sheet-voucher"` and the form POSTs `code` (`docs/portal-contract.md:151`), with `voucher-invalid`/`voucher-used`/`too-many-attempts` already in the error vocabulary (`:220`).

## 8. Open questions

1. **JS editability scope.** Should operator JS be free-form but untrusted (ADR 0004's stance), or restricted to a small API (`voucher.onRender(card, data)`), which is easier to spec and test? The former is more like KiTifi; the latter is more like our `core.js`.
2. **Template per Shop or per site?** ADR 0007 made Shop defaults live with per-Vendo overrides. A voucher template is a print artefact, so per-site is simpler — but a Vendo with its own branding is exactly the case that would want its own ticket.
3. ~~**Do we need a Seller/Reseller concept?**~~ Decided (§0): one concept, Seller, a plain label on the batch; no logins, quotas or self-minting.
4. **Do batches get a human-readable code?** Needed for batch management and for the "which batch is this card from" question; not needed in the URL.
5. **Should a batch carry its own Validity/Pause Limit?** ADR 0006 says site values. Making it per batch changes an ADR.
6. **Quantity cap.** 1000 today; KiTifi allows 5000. Is 1000 an SBC memory limit or a guess?
7. **Card content.** Price on the card? Expiry date? Shop name and contact? These become template markers, so the answer shapes the marker list.
8. **Reprint policy.** Reprint unused only (KiTifi), or any batch with used cards marked (recommended)? The auditor in me wants the latter; the fraud angle wants the former.

## 9. Proposed glossary additions

- **Seller**: The label, chosen when a Voucher Batch is generated, naming who sells it; carried to each Voucher Sale in Sales Inventory. A label, not a login. _Avoid_: reseller, agent, dealer, genfrom
- **Voucher template**: The operator-editable HTML, CSS and JS that the Controller renders to print a Voucher Batch; the Controller supplies the cards' data through markers. _Avoid_: ticket layout, voucher page, print theme
- **Voucher card**: One printable unit of a Voucher Batch inside a Voucher template. _Avoid_: ticket, coupon
- **Print sheet**: The rendered page an operator prints or saves as PDF for a Voucher Batch. _Avoid_: print run, batch sheet
- **Template marker**: A `data-voucher="…"` attribute (and the repeating `card` element) that the Controller's `voucher.js` fills; part of the Operator contract, like the Portal's `data-portal`. _Avoid_: placeholder, variable, token
- **Ticket** stays out of the glossary: it is KiTifi and wifi5soft's word. `GLOSSARY.md:67` already says _Avoid_: Ticket, PIN, card for Voucher — the new "Voucher card" entry is a compound, not a revival of "card" as a synonym for Voucher, and the spec should say so.

Related notes: `docs/research/kitifi-controller-handoff.md` (`tbl_voucher`, seller=Vendo), `docs/research/wifi5soft-architecture-client-time.md` (`pausable`/`consumable`, mint and `allowVoucher` anchors), `docs/research/vendo-list-management.md` (KiTifi Vendo screen and rate editing), `docs/research/shop-rates-default-and-per-vendo-override.md` (Rate tiers), `docs/adr/0004-portal-is-operator-editable-static-files-over-json-api.md` (the mechanism to reuse).
