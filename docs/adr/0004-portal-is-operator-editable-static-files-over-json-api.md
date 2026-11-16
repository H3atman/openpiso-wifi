# The Portal is Operator-editable static files over a JSON API

Operators want to change how the Portal looks and reads (layout, colours, wording, Tagalog copy) the way JuanFi and MikroTik shops already edit HotSpot templates, without learning Go or rebuilding the Controller. So the Portal is plain HTML, CSS and JS served by the Controller under `/portal/`, and every rule lives behind a JSON API under `/portal/api/`: Device identity, Router reads, Rebind, Pause, the Resume Code withholding, and rate limits. Operators edit `index.html`, `portal.css` and an optional `custom.js` from the admin panel, and fill two fixed image slots, a cover and a logo (no general file uploads). `core.js` is ours: it ships in the binary, can't be edited, calls the API, and fills the page by finding `data-portal="…"` markers in the Operator's HTML. It also emits a `portal:state` event that `custom.js` can listen to. Those markers and that event are the Operator contract; the raw API is not. We don't validate Operator JS on the Controller. Instead, three cheap layers catch a broken Portal:

- **Save check (Go):** parse the HTML and confirm that every required marker appears exactly once, that `core.js` is loaded, that there are no external URLs, and that the size is under a cap.
- **Preview check (admin's browser):** `core.js` reports which markers it bound, any JS errors, and whether the state read succeeded.
- **Draft and publish:** the Controller keeps the last version that worked and a "Reset to default".

The images get the same treatment as the files: they are stored with the draft and follow it through publish, previous, Revert and Reset. The admin's browser crops the logo to a 256 px square (20 KB or less) and resizes the cover around a focus point (100 KB or less), producing WebP. The Controller checks only the WebP magic bytes and each slot's cap: no image library and no image work on the SBC. Devices get them at `/portal/logo` and `/portal/cover`, or the built-in ones while a slot is empty.

See [research](../research/hotspot-portal-ux.md).

## Considered Options

- **Keep server-rendered Go templates (the status quo).** These are auto-escaped, checked when parsed, and need less code. Rejected because Operators would have to learn Go template syntax and rebuild the Controller to change anything.
- **Operator-editable Go templates (MikroTik `$(var)` style).** These can be validated on save, but the syntax is still unfamiliar and the result isn't plain HTML/JS.
- **Operators replace all the JS, including the API client.** This gives the most freedom, but any edit could break buying, and the raw API would become a public contract.
- **Validate Operator JS on the Controller** with a syntax check (esbuild as a library), a JS engine (goja), or a headless browser. Rejected for three reasons:
  - Valid syntax doesn't mean the Portal works.
  - A JS engine has no DOM to run the page against.
  - A headless browser is far too heavy for the SBC.

## Consequences

- **JS is required for every Portal flow.** It already was for Rebind, and the phone lab showed that the iOS and Android sign-in sheets run JS. If `custom.js` throws, `core.js` keeps working because the browser runs each script independently.
- **Reading state has no side effects.** The state endpoint only reads. Resuming after an Idle Pause and doing a Rebind become POSTs that `core.js` sends, so opening the Portal no longer changes anything.
- **[ADR 0003](0003-router-is-sole-meter.md) holds.** Remaining time comes from the state endpoint's Router read. Any countdown in the page is visual only and resets on each read.
- **Identity is unchanged.** The cookie stays HttpOnly, and the Device is still resolved through the Router, so Portal JS never sees or stores identity.
- **[ADR 0001](0001-mikrotik-enforces-controller-off-path.md) holds.** Unpaid Devices can reach only the Controller, so the Portal can't use CDNs and has to stay self-contained.
- **Admin access means the power to run JS on every customer's Device.** That's the same trust level as setting rates. A content security policy on `/portal` limits loads and connections to the Controller.
- **The markers and the `portal:state` payload must stay stable across releases,** or Operators' Portals break.
- **The E2E suite changes.** It moves from matching HTML strings to checking JSON responses, plus one browser run of the default Portal.
