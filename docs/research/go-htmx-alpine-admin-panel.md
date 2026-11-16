# Admin panel with Go html/template + htmx + Alpine.js

Status: research, 2026-10-22. Scope: how to build the Controller web admin panel (decision already made: Go `html/template` + htmx + Alpine.js, no Node, assets via `go:embed`) correctly and simply in this codebase. No code was changed. Terms follow `GLOSSARY.md` (Time Credit, Vendo, Coin Window, Voucher Batch, Rebind). The throwaway mock `controller-dashboard.html` still says "Entitlement"; use "Time Credit" in the real UI.

Items marked `[Inference]` are my conclusion from the cited sources, not stated by them. `[Open]` items are collected in section 8.

## TL;DR

- **Version choice is a decision for you.** htmx **4.0.0** went stable on 2026-08-28; the last 2.x is **2.0.10** (2026-09-06). Alpine is **3.17.5** (2026-10-20). I recommend vendoring **htmx 4.0.0 pinned**: smaller, built-in morph, no `localStorage` history, no inline `<style>`, an official CSP extension. The cost is that 4.0 is two months old and its npm tag stays `next` until early 2027, so most blog posts still describe 2.x. 2.0.10 is the conservative fallback; sections 3 to 5 note where they differ.
- **CSRF: no token needed.** The Controller already wraps everything in `http.NewCrossOriginProtection()` and sets the admin cookie `SameSite=Strict` (`controller.go:160`, `:234`). htmx requests are same-origin `fetch`/XHR with an `Origin` header, so they pass. Do not add `hx-headers` tokens (YAGNI). Add one e2e check that a mismatched `Origin` POST gets 403.
- **CSP: add one.** There is none today. `script-src 'self'` without `'unsafe-eval'` works if we avoid `hx-on`, `js:` values and trigger filters, and use the **`@alpinejs/csp`** build with `Alpine.data()` components and no `x-html`.
- **Session expiry:** `requireAdmin` returns bare `401` text (`controller.go:241-251`). For htmx requests also send `HX-Redirect: /admin/login`; both htmx lines act on it before deciding whether to swap.
- **Biggest performance risk is not htmx, it is the Online Devices panel.** `oversightPage` holds `voucherMu` for the whole request and does Router REST calls per active session (`oversight.go:78-126`). `voucherMu` also guards portal voucher redemption (`vouchers.go:85`). Polling that handler would let a slow Router block customers. Give the live panel its own lock-free, cached endpoint and poll at 5 s or slower.
- **Migration:** (1) `web/` layout + embedded assets + `/admin/static/`, no behavior change; (2) `/admin/oversight` split into panels with polling fragments; (3) convert mutations to fragment responses with a non-htmx fallback kept, so `controller/e2e/run.py` keeps passing.

## Source keys

Every claim below cites one of these (or `file:line` in this repo).

| Key | Source |
|---|---|
| `h4-new` | htmx 4 "What's New" (`four` branch): https://github.com/bigskysoftware/htmx/blob/four/www/src/content/docs/whats-new-in-htmx-4.md |
| `h4-docs` | htmx 4 docs: https://github.com/bigskysoftware/htmx/blob/four/www/src/content/docs.md (rendered at https://htmx.org/docs/) |
| `h4-ann` | htmx 4.0.0 announcement: https://github.com/bigskysoftware/htmx/blob/four/www/src/content/announcements/2026-08-28-htmx-4.0.0-is-released.md |
| `h4-src` | https://github.com/bigskysoftware/htmx/blob/four/src/htmx.js |
| `h4-csp` / `h4-ptag` / `h4-alp` / `h4-poll` | `four` branch, `www/src/content/extensions/{14-hx-csp,10-hx-ptag,12-hx-alpine-compat}.md` and `patterns/04-polling.md` |
| `h2-docs` | htmx 2 docs (`v2` branch): https://github.com/bigskysoftware/htmx/blob/v2/www/content/docs.md |
| `h2-src` | https://github.com/bigskysoftware/htmx/blob/v2.0.10/src/htmx.js |
| `h-rel` | htmx releases: https://github.com/bigskysoftware/htmx/releases (API: `api.github.com/repos/bigskysoftware/htmx/releases`) |
| `a-rel` | Alpine releases: https://github.com/alpinejs/alpine/releases |
| `a-csp` | https://alpinejs.dev/advanced/csp |
| `a-morph` | https://alpinejs.dev/plugins/morph |
| `a-data` | https://alpinejs.dev/globals/alpine-data |
| `a-mut` | Alpine `packages/alpinejs/src/mutation.js` @ v3.17.5: https://github.com/alpinejs/alpine/blob/v3.17.5/packages/alpinejs/src/mutation.js |
| `go-http` | https://pkg.go.dev/net/http |
| `go-csrf` | `net/http/csrf.go`: https://github.com/golang/go/blob/master/src/net/http/csrf.go |
| `go-fs` | `net/http/fs.go`: https://github.com/golang/go/blob/master/src/net/http/fs.go |
| `go-embed` | https://pkg.go.dev/embed and `src/embed/embed.go` |
| `go-tmpl` | https://pkg.go.dev/html/template and `src/html/template/attr.go` |
| `mdn-sfs` | https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Sec-Fetch-Site |
| `jsd` | Sizes measured 2026-10-22 from `https://cdn.jsdelivr.net/npm/<pkg>@<ver>/dist/...` (raw bytes, and `gzip -9`) |

## 1. Versions, sizes, vendoring, serving

### 1.1 Versions

| Package | Latest stable | Notes | Source |
|---|---|---|---|
| htmx 4 | **4.0.0** (2026-08-28) | `4.0.0-beta6` was the last prerelease. No 3.x exists. | `h-rel` |
| htmx 2 | **2.0.10** (2026-09-06) | Still published; npm `latest` stays 2.x and 4.x stays `next` until "early 2027". The website already references 4.0. | `h-rel`, `h4-ann` |
| Alpine | **3.17.5** (2026-10-20) | 3.x line, no 4.x. | `a-rel` |

htmx 4 vs 2 changes that matter to us (all from `h4-new` unless noted):

| Change in htmx 4 | Effect here |
|---|---|
| `fetch()` replaces XHR (`h4-new` "fetch() replaces XMLHttpRequest") | Transparent. Default timeout now 60 s (`h4-new` "60-second timeout"); matches `WriteTimeout: 60s` (`cmd/openpiso/main.go:85`). |
| Attribute inheritance is explicit: write `hx-boost:inherited="true"` (`h4-new` "Explicit inheritance") | Biggest upgrade burden for 2.x users; for a fresh codebase it just means "always write `:inherited` when you want it". |
| All HTTP responses swap except 204 and 304 (`h4-new` "Error responses swap") | `http.Error` plain-text bodies would be swapped into the target. See 3.5. |
| No `localStorage` history cache; back button refetches (`h4-new` "No history cache") | Removes a leak path for the one-time Vendo secret, see 3.6. |
| `allowEval`, `historyCacheSize`, `responseHandling`, `inlineStyleNonce` removed; `includeIndicatorStyles` becomes `includeIndicatorCSS` (`h4-new` "Config") | Different CSP knobs, see 4.3. |
| `HX-Trigger-After-Swap` / `-After-Settle` removed; `HX-Redirect/Refresh/Retarget/Reswap/Trigger` unchanged (`h4-new` "Response headers") | Use plain `HX-Trigger`. |
| **No status 286 polling stop** `[Inference]`: `grep 286` finds it in `h2-src:4897` but not in `h4-src`; the 4.x polling pattern stops polling by returning the element without `hx-trigger` (`h4-poll` "Stopping the poll") | Polling-stop recipe differs, see 3.2. |
| Built-in `innerMorph`/`outerMorph` swaps replace the Idiomorph extension (`h4-docs:505`) | See 5. |

### 1.2 File sizes (`jsd`)

| File | Raw | gzip -9 |
|---|---|---|
| `htmx.org@4.0.0/dist/htmx.min.js` | 36,716 B | 13,014 B |
| `htmx.org@2.0.10/dist/htmx.min.js` | 51,238 B (`h-rel` asset) | 16,527 B |
| `alpinejs@3.17.5/dist/cdn.min.js` | 55,935 B | 19,900 B |
| `@alpinejs/csp@3.17.5/dist/cdn.min.js` | 72,242 B | 23,798 B |
| `@alpinejs/morph@3.17.5/dist/cdn.min.js` | 5,451 B | 2,067 B |
| htmx 4 `ext/hx-csp.min.js` / `hx-ptag.min.js` / `hx-alpine-compat.min.js` | 3,423 / 333 / 1,742 B | not measured |

Total for the recommended set (htmx 4 + Alpine CSP) is about 109 KB raw. The embedded binary grows by that much; the htmx docs state no file size, only that the unminified source is about 2,500 lines (`h2-docs`, "Debugging").

### 1.3 Vendoring (no CDN; the Controller may be offline)

Both projects document a script tag plus a downloaded file; htmx explicitly says to download `htmx.min.js` and load it locally (https://htmx.org/docs/, "Installing"). One-time download, committed to the repo:

```
web/static/vendor/htmx-4.0.0.min.js        <- cdn.jsdelivr.net/npm/htmx.org@4.0.0/dist/htmx.min.js
web/static/vendor/alpine-csp-3.17.5.min.js <- cdn.jsdelivr.net/npm/@alpinejs/csp@3.17.5/dist/cdn.min.js
web/static/vendor/README.md                <- version, source URL, sha256 for each file
```

The Alpine script tag needs `defer` (https://alpinejs.dev/essentials/installation). Plugins load before core (`a-morph`, "Installation order"); htmx 4 extension scripts are included directly with no `hx-ext` attribute (`h4-new` "Extension loading"), and `hx-alpine-compat` must load before Alpine (`h4-alp` "Installing").

### 1.4 Embedding and serving

- `//go:embed web/templates web/static` on an `embed.FS` package variable, in a Go file in the `controller` package directory. Patterns are relative to the package directory and cannot contain `..` (`go-embed`). Files starting with `.` or `_` are skipped unless the pattern uses `all:` (`go-embed`), so do not name anything `_foo.html`.
- Serve with `http.FileServerFS(sub)` behind `http.StripPrefix("/admin/static/", ...)`. A pattern ending in `/` is a subtree match, and a `GET` pattern also matches `HEAD` (`go-http`, "ServeMux"). `StripPrefix` returns 404 on a prefix mismatch (`go-http`). Use `fs.Sub(embedFS, "web/static")` so URLs do not include `web/static`.
- **Mount under `/admin/static/`, not `/static/`.** The management-network check is `strings.HasPrefix(request.URL.Path, "/admin")` (`controller.go:163-169`), and the cookie `Path` is `/admin` (`controller.go:232`). Registering the static route with `Authentication: "none"` keeps the login page able to load CSS/JS while the assets stay behind the network gate. `[Inference]`
- The route table is `[]route` and also feeds `GET /admin/routes` (`controller.go:91-97`, `:205-210`); add one `route{Method: GET, Path: "/admin/static/", Authentication: "none", Handler: <StripPrefix(...).ServeHTTP>}` rather than a side-channel `mux.Handle`.
- **Cache headers.** The outer wrapper sets `Cache-Control: no-store` on every response before the mux runs (`controller.go:160-162`). The static handler must override it with `Header().Set` (replaces, does not append). Embedded files report a zero `ModTime` (`go-embed`: `embed.go:221`), and `FileServer`/`ServeContent` skip `Last-Modified` and `If-Modified-Since` when the time is zero (`go-fs:518`, `:570`, `:619`). So there is **no conditional request fallback**: without an explicit `max-age` the browser re-downloads every asset. Use versioned filenames (as above) plus `Cache-Control: public, max-age=31536000, immutable` on `/admin/static/vendor/` and a short `max-age` on the app's own `app.css`/`app.js`.

## 2. Templates: full pages vs partials

### 2.1 Today

Every admin page is a package-level `template.Must(template.New(name).Parse(`...`))` string with its own full `<!doctype html>` shell and no shared layout: `loginTemplate` (`controller.go:22`), `adminTemplate` (`:28`), `certificateTemplate` (`:40`), `connectedTemplate` (`:50`), `oversightTemplate` (`oversight.go:18`), `vendosTemplate` (`vendo.go:21`), and the pause, validity, intents, coin-sales, rates and voucher templates (`pauses.go:15`, `validity.go:12`, `reconciler.go:14`, `coin_window.go:50,82`, `vouchers.go:17`). Handlers set `Content-Type` and call `Execute` directly. The customer portal (`portal.go:17`) is a separate template and is out of scope. The `<head>` is repeated 10 times with no CSS or JS.

### 2.2 Recommendation: move to embedded `.html` files

Inline strings cannot share a layout without string concatenation, and html/template needs the whole template set parsed before execution (`go-tmpl`, "define / block / template"). Use `template.ParseFS(fsys, "layout.html", "page.html")` (`go-tmpl`, "ParseFS").

Idiom (one template set per page, so `{{define "content"}}` in different pages never collide):

```
web/templates/layout.html          {{define "layout"}} <head>…assets…</head><body>{{template "nav" .}}<main>{{template "content" .}}</main></body>{{end}}
web/templates/oversight.html       {{define "content"}}…{{template "online" .}}…{{end}}   {{define "online"}}<table>…</table>{{end}}
```

- A **page handler** does `pages["oversight"].ExecuteTemplate(w, "layout", view)`.
- A **fragment handler** does `pages["oversight"].ExecuteTemplate(w, "online", view)`: the same named template the full page includes, so one source of truth. `ExecuteTemplate` takes a name (`go-tmpl`, "ExecuteTemplate / Clone").
- If an `Execute` error happens mid-render, partial output may already be written (`go-tmpl`); the repo already ignores that case (`oversight.go:145-147`), keep the same posture.
- Build `pages` once at package init with `template.Must` (same failure-at-startup behavior as today).

### 2.3 When to detect `HX-Request`

htmx sends `HX-Request: true` on every request, including boosted ones; boosted requests also send `HX-Boosted`, and history-miss refetches send `HX-History-Restore-Request` (`h2-docs:1022-1024`; `h4-docs:1310-1317`). htmx 4 adds `HX-Request-Type: full|partial` (`h4-new` "Request headers").

| Request | Response |
|---|---|
| Boosted page navigation, history restore | **Always the full page.** htmx swaps `<body>`; a history miss "expects back the HTML needed" for the whole page (`h2-docs:862`). Do not branch. |
| Dedicated fragment URL (e.g. `/admin/oversight/online`) | Always a fragment. No header check needed. |
| Mutation that also works without JS (`POST /admin/vendos/unpair`, `/admin/audit/resolve`, ...) | `HX-Request: true` → fragment; otherwise keep today's behavior (redirect or text). Add `Vary: HX-Request` (`h4-docs:1930-1934`; harmless here since every response is already `no-store`, `controller.go:161`). |

## 3. htmx patterns we need

### 3.1 hx-boost

Boost turns anchors and forms into AJAX requests that target `<body>` and degrades to normal navigation without JS (`h2-docs`, "Boosting"; `h4-docs:1065-1075`). htmx 4 docs caution that boosted navigation does **not reset the JS environment**, so scripts must tolerate re-execution (`h4-docs`, "Advantages & Disadvantages of Boosting"), and say its remaining benefit is small now that browsers have paint holding. For ten tiny pages on a LAN, **boost is optional**; I would apply it only to the nav and in-page links: `<nav hx-boost:inherited="true">` (2.x: `hx-boost="true"`). Do **not** boost:

- `POST /admin/login`: it returns bare 401/429 text (`controller.go:201,215`). In 2.x that is silently dropped; in 4.x the text replaces the page body.
- Forms whose handler returns non-HTML: today `POST /admin/vendos/pair` returns JSON (`vendo.go:83-85`) and `GET /admin/oversight?format=json` returns JSON (`oversight.go:131-137`); `controller/e2e/run.py:348,494` depend on both.
- Pages that call `window` scripts at load (`Alpine.data` registration must live in the one `app.js` loaded from `<head>`, not per page).

### 3.2 Polling

`<div hx-get="/admin/oversight/online" hx-trigger="every 5s">` issues a GET every 5 s (`h2-docs`, "Polling"; `h4-poll` "Basic usage"; unit is `ms`, `s` or `m`).

| | htmx 2.0.10 | htmx 4.0.0 |
|---|---|---|
| Stop polling | Respond `286` (`h2-docs:310`; `h2-src:4897`, runs only if the response is swapped) | Return the element **without** `hx-get`/`hx-trigger`; htmx clears the interval when the element leaves the DOM (`h4-poll` "Stopping the poll"). No `286` code in `h4-src` `[Inference]` |
| Skip unchanged polls | not built in | `hx-ptag` extension (333 B): server returns `304` when the sent `HX-PTag` is current and htmx skips the swap (`h4-ptag`) |
| Pause when tab hidden | filter `every 5s [document.visibilityState === 'visible']` | same, but filters are **eval**, see 4.3 (`h4-poll` "Conditional polling") |

Polling pattern for us: the polled element is the **container**, swap `innerHTML` so the container (and its `hx-trigger`) survive; or swap `outerHTML` and always re-emit the container with its attributes (`h2-docs`, "Load Polling"). Poll only read-only panels (Online Devices, Vendos, recent Coin Windows). Keep every form (grant, resolve, pair) **outside** polled regions so a swap never eats a half-typed field (see 5).

### 3.3 Targets and swaps for table rows

- Give every row a stable id (`<tr id="vendo-{{.ID}}">`). A mutation returns the updated `<tr>` (or empty body to delete it), with `hx-target="closest tr" hx-swap="outerHTML"` on the button. `swap:`/`settle:` delays default to 0 and 20 ms (`h2-docs`, "Swap Options"); leave the defaults.
- `hx-confirm="Unpair this Vendo?"` on the unpair button shows the browser confirm dialog (`h4-docs:656`). In 4.x, put `hx-confirm:inherited` on a parent only if intended; duplicates on child and parent are an explicit footgun (`h4-docs:664`).
- `hx-delete` stops including enclosing form data in 4.x (`h4-new`); our routes are all `POST`, so this does not apply.

### 3.4 Response headers

| Header | Use here | Source |
|---|---|---|
| `HX-Redirect` | expired session; `HX-Refresh: true` after Router onboarding changes global state | `h2-docs` "Response Headers"; `h4-docs:1352-1353` |
| `HX-Trigger: audit-changed` | after a grant/resolve, panels with `hx-trigger="audit-changed from:body"` refetch themselves | `h2-docs` "Response Headers" |
| `HX-Retarget` / `HX-Reswap` | send errors to a `#flash` element | `h2-docs`; `h4-src:603` honors retarget |

Headers are not processed on 3xx because the browser follows the redirect itself (`h2-docs`, "Response Headers"). So for htmx requests use **200/4xx + header**, not `http.Redirect`.

### 3.5 Errors (4xx/5xx)

Our handlers answer errors with `http.Error(response, "...", status)`: plain text, many call sites (e.g. `oversight.go:85,97,103`; central `oversightError` at `oversight.go:150-161`).

| | htmx 2.0.10 | htmx 4.0.0 |
|---|---|---|
| Default | 4xx/5xx not swapped, `htmx:responseError` fires, 204 does nothing (`h2-docs`, "Requests & Responses") | everything swaps except 204/304; fires `htmx:response:error` for status >= 400 (`h4-new` "Error responses swap"; `h4-src:593-595`) |
| Config | `htmx.config.responseHandling` array; example `{"code":"[45]..","swap":true}` (`h2-docs:940-999`) | `htmx.config.noSwap` (default `[204, 304]`, `h4-src:202`), per-element `hx-status:422="swap:innerHTML target:#errors"` (`h4-new` "Status code swaps") |

**Recommended, works on both:** one helper used by migrated handlers, `fail(w, r, status, msg)`: for `HX-Request: true` it sets `HX-Retarget: #flash`, `HX-Reswap: innerHTML` and writes a small `<p role="alert">` fragment with the real status code; otherwise it calls `http.Error` as today. On 2.x add one `responseHandling` entry that lets `[45]..` swap. That keeps e2e status assertions (e.g. 400/401 at `controller/e2e/run.py:1636-1637`) unchanged. `[Inference]`

### 3.6 History and back button

- 2.x with `hx-push-url`/boost snapshots the DOM into **`localStorage`** (default 10 entries); a miss refetches with `HX-History-Restore-Request: true` and expects a full page (`h2-docs:862,1641-1651`). Disable per page with `hx-history="false"`, "important for shared-use / public computers" (`h2-docs:912-918`).
- 4.x never caches; back refetches and swaps `<body>` (or the `hx-history-elt` element) (`h4-new` "No history cache").
- **Why it matters here:** the pair response contains the Vendo `secret`, "not shown again" (`vendo.go:85`; wording in `vendo.go:23`). If the pair result is rendered into the page, a 2.x history snapshot would persist it in `localStorage`. On 2.x put `hx-history="false"` on the Vendos page; on 4.x this is moot.

## 4. Security

### 4.1 CSRF: how the repo protects admin POSTs today

Facts from the code:

- No CSRF token exists anywhere in the admin forms (`oversight.go:31,40,41`; `vendo.go:23-24`; templates carry only `request_id` idempotency keys, not tokens).
- `Handler` returns `http.NewCrossOriginProtection().Handler(...)` around everything (`controller.go:160`).
- The admin cookie is `HttpOnly`, `SameSite=Strict`, `Path=/admin`, not `Secure` (`controller.go:229-235`). The server is plain `ListenAndServe` (`cmd/openpiso/main.go:88`), so there is no TLS.

How `CrossOriginProtection` decides (`go-csrf:132-175`): GET/HEAD/OPTIONS always pass; `Sec-Fetch-Site: same-origin|none` passes; any other `Sec-Fetch-Site` is rejected (403); if the header is absent it compares `Origin`'s host to `Host`, rejects on mismatch, and **allows if neither header exists** ("same-origin or not a browser request"). Added in Go 1.25 (`go-http`, "CrossOriginProtection"; `go.mod` here is Go 1.27.2).

Admin is served over plain HTTP on a LAN IP, and browsers send `Sec-Fetch-Site` only to "potentially trustworthy URLs" (`mdn-sfs`). So on this deployment the **`Origin` vs `Host` fallback** is what actually runs, and browsers send `Origin` on POST `[Inference]`. htmx adds no new risk: its requests are same-origin; htmx 2 defaults `selfRequestsOnly: true` and htmx 4 `mode: "same-origin"` (`h2-docs` "Security"; `h4-docs:1845-1850`).

**Decision: keep as is; no `hx-headers` token.** If you later want defense in depth, htmx documents `hx-headers` on `<html>`/`<body>` (4.x: `hx-headers:inherited`), but `hx-boost` does not update `<html>`/`<body>` attributes in 2.x, so a token there goes stale across boosted navigation (`h2-docs`, "CSRF Prevention"; `h4-docs:1900-1910`). Hidden form fields avoid that, but it is more code than the threat justifies. Add an e2e check (cross-origin `Origin` POST to `/admin/rates` returns 403) so the guarantee is tested once.

### 4.2 Session expiry for htmx requests

`requireAdmin` answers a missing or unknown cookie with `401 "admin login required"` (`controller.go:241-251`). Sessions are an in-memory map with no expiry (`controller.go:77,227`), so a Controller restart logs every admin out; a polling panel will hit this. Change: when `HX-Request: true`, also set `HX-Redirect: /admin/login` and keep status 401 (the e2e asserts 401, `controller/e2e/run.py:1637`). Both versions process `HX-Redirect` before the swap decision (`h2-src:4833` vs `:4849`; `h4-src:600,651`), so it works even though 2.x would not swap a 401 and 4.x would. Without it, 4.x swaps `"admin login required"` into the panel `[Inference]`.

### 4.3 Escaping and CSP

**html/template escaping.** Contextual autoescaping covers HTML text, attributes, URLs, and JS strings (`go-tmpl`, "Contextual autoescaping"), and trusts template authors, not data. The catch is which attributes it treats as code. `attrType` strips `data-`, takes the part after `:`, then treats names starting `on` as JS and names containing `src`/`uri`/`url` as URL; everything else is plain (`go-tmpl`: `attr.go` `attrType`). Consequences `[Inference]` from reading that function:

| Attribute | html/template context | Implication |
|---|---|---|
| `hx-get`, `hx-post`, `hx-target`, `hx-vals`, `hx-headers` | plain | Only HTML-attribute escaping. Build query strings with `{{.ID \| urlquery}}`, not bare `{{.ID}}` (cf. `oversight.go:34` `?batch={{.ID}}`). |
| `hx-push-url` | URL (name contains `url`) | URL-escaped and scheme-checked. |
| `hx-on:click`, `@click`, `x-on:click`, `x-data` | plain (`hx-on:click` becomes `click`; `@click` does not start with `on`) | **Never interpolate user data into them.** HTML-escaping does not protect JS: the browser decodes entities before the string reaches the JS engine (`h4-csp`, "The hx-nonce Attribute"). |

Rules: no `{{.}}` inside `hx-on:*`, `hx-vals='js:...'`, `x-data`, `x-on`; pass data through `data-*` attributes or hidden inputs; build any `hx-vals` JSON with `encoding/json` in Go, never by string concatenation in the template. Never use `template.JS`/`template.HTML` on request-derived data.

**CSP.** No `Content-Security-Policy`, `X-Frame-Options` or `frame-ancestors` exists in the repo today (grep of `*.go`, `*.py`, `*.md`). Suggested header for `/admin/*` responses (set in the existing wrapper next to `nosniff`, `controller.go:162`):

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'
```

| Feature | Needs `unsafe-eval`? | Source |
|---|---|---|
| htmx `hx-on:`, `hx-vals`/`hx-headers` with `js:`, trigger filters (incl. `[document.visibilityState...]`) | **Yes** (htmx uses `new Function()`); optional | `h4-docs:1873-1881`; 2.x: `allowEval:false` disables them (`h2-docs:1652`) |
| htmx without those | No | `h4-docs:1881` |
| Alpine standard build | **Yes** | `a-csp` ("removing `'unsafe-eval'`") |
| `@alpinejs/csp` build | No | `a-csp` |

**What the Alpine CSP build restricts** (`a-csp`): supported are object/array literals, operators/ternaries, `x-model`, `count++`/`count = 0`, and method calls with arguments such as `items.push('c')`. Not supported: property assignment (`user.name = 'John'`), arrow functions, destructuring, template literals, spread, **globals** (`console`, `document`, `window`, `Math`, `parseInt`, `JSON`), and `x-html`/`insertAdjacentHTML`. Anything non-trivial moves into `Alpine.data('name', () => ({...}))` registered in `alpine:init` (`a-data`). Package: `@alpinejs/csp` (`a-csp`).

htmx indicator CSS:

- **2.x** injects a `<style>` tag (`includeIndicatorStyles: true`, nonce via `inlineStyleNonce`; `h2-docs:1734,1744`). Under `style-src 'self'` set `includeIndicatorStyles` false via `<meta name="htmx-config" content='{"includeIndicatorStyles":false}'>` and write the `.htmx-request`/`.htmx-indicator` rules in `app.css`.
- **4.x** uses Constructable Stylesheets, not subject to `style-src` (`h4-docs:1873-1877`). One catch: morph swaps that reconcile `style` attributes set by Alpine violate strict `style-src`; add `"style"` to `morphIgnore` (`h4-docs`, "CSP & Inline Styles").

`hx-csp` (htmx 4 extension, 3.4 KB) adds `hx-nonce` gating, a Trusted Types policy and `safeEval` (`h4-csp`). **Not needed** if we write no inline scripts, avoid eval features, and rely on html/template escaping; revisit only if we ever need `hx-on`. The `<meta name="htmx-config">` tag is data, not script, so `script-src 'self'` allows it `[Inference]`.

## 5. htmx + Alpine interplay

- **Initialization of swapped-in content:** Alpine runs a `MutationObserver` on `document` (`subtree: true, childList: true`) and initializes added nodes (`a-mut:49-54`), so `x-data` inside an htmx swap just works. htmx 4's `hx-alpine-compat` (1.7 KB) defers Alpine's observer until htmx's settle phase ends, so Alpine does not initialize against a mid-transition DOM (`h4-alp`, "Alpine initialization on swap"). Add it only if flicker or half-initialized components show up.
- **State is lost when the element holding `x-data` is replaced.** A normal swap destroys the old nodes and creates new ones, so component state resets `[Inference]`; `a-morph` is explicitly about "updating HTML from a server request without losing Alpine's on-page state".
- **YAGNI rule that avoids morph entirely:** put `x-data` on a wrapper **outside** any swap target, and keep inputs outside polled regions. Our polled panels are read-only tables.
- **If morph becomes necessary:** htmx 4 has built-in `innerMorph`/`outerMorph` swaps (`h4-docs:470-471,505`), and with `hx-alpine-compat` it carries Alpine's data across the morph, replacing the need for Alpine's morph plugin (`h4-alp`, "Morph-aware state preservation"). On htmx 2 you need the Idiomorph extension (`h4-docs:505-506`) and/or `@alpinejs/morph` (5.4 KB; must load before Alpine, `a-morph`). This is another point for htmx 4.
- **Where Alpine earns its place in this panel (candidates):** a modal around the grant/resolve forms, a "copy secret" button after pairing (needs `navigator`; in the CSP build it must live in an `Alpine.data` method since globals are blocked in expressions, `a-csp`), and a mobile nav toggle. `<details>`, `<dialog>` and `hx-confirm` cover the rest with no JS. Do not add Alpine until one of these exists.

## 6. Performance on an ARM SBC with one SQLite connection

Facts: one connection, WAL, `synchronous=FULL` (`ledger/ledger.go:33,36-38`; `controller/README.md:28`); ADR 0002 targets Orange Pi Zero armv7 with as little as 256 MB RAM, "~under 64 MB RSS" (`ARCHITECTURE.md` "The spine"; `docs/adr/0002-controller-in-go.md`).

- **One connection serializes everything:** admin polls, portal requests and Vendo coin reports all queue on `SetMaxOpenConns(1)`, and a coin write waits for its fsync (`synchronous=FULL`). Reads are cheap, but N panels polling at 1 s means N queued reads per second ahead of coin intake. `[Inference]`
- **Polling intervals:** the mock fakes 1 s updates (`controller-dashboard.html:116-139`, `setInterval`); do not copy that. Suggested: Online Devices 5 s, Vendos and recent Coin Windows 10 s, everything else load-once or `HX-Trigger`-refreshed. One in-flight request per panel at most.
- **The Online Devices panel is the expensive one.** `oversightPage` creates a new Router client per call (`router.go:11-20`), calls `ActiveSessions`, then per active session reads `TimeCreditByID` and `UsedSeconds` over REST (`oversight.go:106-126`), all while holding `voucherMu` (`oversight.go:78-79`), the same mutex `redeemVoucher` takes (`vouchers.go:85`). So a slow Router stalls customer redemption. Fix in step 2: separate endpoint, **no `voucherMu`**, read-only, and a small server-side cache (single-flight, TTL >= poll interval) of the last Router result. Keep the existing "Router temporarily unavailable" degraded state (`oversight.go:127-130`).
- **Cheap partials:** each fragment calls only the ledger reads it shows (`Vendos()`, `CoinWindows()`, `AuditReview()`, `Sales()` exist as separate methods), never `PurchaseState()` plus `AuditReview()` as `oversightPage` does for the whole page. "Sales per hour" (mock) has no query today; `Sales(from, to)` returns an aggregate (`ledger/oversight.go:97`) `[Open]`.
- **Skip unchanged polls:** htmx 4 `hx-ptag` returns `304` with no body when a cheap version tag is unchanged (`h4-ptag`); this saves bandwidth and render time, not DB reads, so it is optional.
- **gzip:** `http.FileServer` does no compression, and `ServeContent` assumes seekable data of known length; Go's source says to "compress the entire file up-front and provide a seekable view" rather than wrap the writer (`go-fs:~398-415`). Options without dependencies: (a) **skip it** (recommended): assets are cached for a year after first load on a LAN; (b) precompress to `*.js.gz` at vendoring time, embed them, and serve with `Content-Encoding: gzip` via `http.ServeContent` when `Accept-Encoding` contains `gzip` (embedded files are seekable, `go-embed`); (c) a `compress/gzip` response-writer middleware for HTML fragments, which costs ARM CPU for a few KB per response, so no.
- **Timeouts:** `WriteTimeout` is 60 s (`cmd/openpiso/main.go:85`), matching htmx 4's 60 s default fetch timeout (`h4-new`). Nothing to change.

## 7. Recommended layout and migration plan

### 7.1 Layout

```
controller/internal/controller/
  web.go                     //go:embed web/templates web/static ; pages map ; render helpers ; static route
  web/templates/
    layout.html              <head> (CSS, htmx-config meta, scripts), nav, flash
    nav.html
    login.html  admin.html  oversight.html  vendos.html  vouchers.html  rates.html  ...
  web/static/
    app.css  app.js          (app.js holds only Alpine.data() registrations)
    vendor/htmx-4.0.0.min.js  alpine-csp-3.17.5.min.js  README.md (version + sha256)
```

One `*template.Template` per page, built from `layout.html` + `nav.html` + `<page>.html`. Fragments are named templates inside the page file. Existing view structs and handlers stay in place; only the template source and the `Execute` call change.

| Route (admin session unless noted) | Returns |
|---|---|
| `GET /admin/static/` (no session, admin network) | embedded assets |
| `GET /admin/oversight` | full page (also `?format=json`, unchanged) |
| `GET /admin/oversight/online`, `/vendos`, `/coin-windows` | read-only polled fragments |
| `POST /admin/audit/resolve`, `/admin/grants`, `/admin/vendos/pair`, `/unpair`, `/admin/rates`, `/admin/vouchers` | fragment when `HX-Request: true`, today's behavior otherwise |

### 7.2 Migration plan (incremental; each step ships green)

| Step | Change | Done when |
|---|---|---|
| **1. Shell** | Add `web.go`, `layout.html`, vendored assets, `/admin/static/` route with cache headers, CSP header. Move the 10 inline templates into files **mechanically**: same markup, wrapped in `layout`. Optional: `hx-boost` on `<nav>` only. Do not boost login or JSON endpoints. | All existing e2e (`controller/e2e/run.py`) passes unchanged; every admin page loads CSS/JS offline; `GET /admin/routes` lists the static route. |
| **2. Dashboard** | Rebuild `/admin/oversight` as panels from the mock (Online Devices, Vendos, recent Coin Windows, Audit review, Voucher Batches, Rate tiers; Time Credit wording). Add the polled fragment endpoints with the section 6 rules: no `voucherMu` on the live panel, cached Router read, 5 to 10 s intervals. Add the `HX-Redirect` handling in `requireAdmin`. | Live panel updates without a page reload; killing the Router leaves the panel in the existing degraded state; a poll with an expired session lands on `/admin/login`. |
| **3. Mutations** | Convert pair/unpair, rates, vouchers, audit resolve, grant to fragment responses: return the updated `<tr>`/panel, use `HX-Trigger` to refresh sibling panels, errors via `fail()` to `#flash`. Pair returns an HTML fragment showing the secret once (JSON kept for non-htmx callers, `run.py:348`); on 2.x add `hx-history="false"`. Add `hx-confirm` on unpair. First Alpine use (modal, copy-secret) lands here, not earlier. | e2e covers both the HX and non-HX variant of each route and still asserts the existing status codes; the cross-origin `Origin` POST check returns 403. |

Tests follow the repo rule: E2E in the existing `controller/e2e/run.py` harness, written before the handler change, producing its usual `controller/e2e/reports/*.json` artifact. No per-template unit tests.

## 8. Open questions and risks

| # | Question / risk | Notes |
|---|---|---|
| 1 | **htmx 4.0.0 or 2.0.10?** | Recommended 4.0.0 (smaller, built-in morph, no localStorage, simpler CSP). Risk: 2 months old, 4.x not npm `latest` until early 2027 (`h4-ann`), less third-party documentation. We vendor a pinned file, so upgrades are manual either way. |
| 2 | Is `286` really gone in 4.x? | Based on grep of `h4-src` and the 4.x polling guide only `[Inference]`; verify with a 5-line browser test before relying on either mechanism. |
| 3 | Admin sessions never expire and live in memory (`controller.go:77,227`) | Not new, but a polling dashboard makes restarts and stale tabs visible. Out of scope unless you want idle timeout. |
| 4 | Plain-HTTP admin | `SameSite=Strict` cookie without `Secure`, no TLS (`controller.go:229-235`, `main.go:88`). CSRF holds via `Origin`/`Host` (4.1), but credentials and the pair secret travel in cleartext on the management LAN. Pre-existing; the panel does not change it. |
| 5 | Hidden-tab polling | Pausing needs a trigger filter, which is eval (4.3). Either allow `hx-csp` with `safeEval`, or accept background polling at 5 to 10 s. |
| 6 | "Sales per hour" panel | No hourly query exists; add one ledger method or drop the panel. |
| 7 | `voucherMu` is also taken by every admin mutation (`oversight.go:164,183`; `vouchers.go:85`; `portal.go:139,228`) | Fine for mutations; the risk is only for long-held reads. If fragments for Audit/Rebind need `PurchaseState()`, check it does not hold the mutex across Router calls. |
| 8 | Error-fragment UX | `fail()` changes many `http.Error` sites over time; until each handler is migrated, 4.x swaps raw text into targets (4.3/3.5). Keep a global `htmx:response:error` handler that shows a generic message as a safety net. |
| 9 | CSP header may break the customer portal | Apply it only under `/admin` first; the portal (`portal.go`) is a different surface and unchanged. |
| 10 | Alpine need | None of the current mock requires it. Decision already made; this doc recommends deferring the first `x-*` attribute until step 3. |
