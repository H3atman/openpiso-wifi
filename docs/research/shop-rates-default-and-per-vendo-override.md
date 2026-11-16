# Shop rates: default and per-Vendo override

Research input, 2026-11-04. Rafael is setting up **Shop Rates / Timer Rate** as the default for newly set-up Vendos and asks whether individual Vendo rates should use that screen too, or stay in Vendo List. This note compares one mandatory Shop price list with a shared default plus optional per-Vendo overrides. It recommends the latter: **edit the common rates in Shop; keep exceptions in Vendo List**. It does not implement or change the existing decisions.

> **Evidence.** Local code is `86e645a`, identical to `main` at inspection, after PRs #99, #100 and #101. GitHub issue bodies and comments were fetched on 2026-11-04. Comparable-system claims below are the repository's existing reverse-engineering findings, not a new inspection of vendor source or a live vendor test. Proposed UI wording and option consequences are recommendations, not claims about shipped behavior.

## 1. Findings: this is already the underlying model

### 1.1 Shop is the site scope, not a second kind of Vendo

The glossary calls the Controller the computer **at a site**, the Operator the person who sets that site's rates, and every networked coin unit a **Vendo** (`GLOSSARY.md:9–15`, `:33–35`). It has no separate Shop domain entry. For this proposal, **Shop means the current Controller/site's common settings**, not another tenant, another ledger, or a privileged “main vendo.” A multi-Shop Controller would be a separate architecture question.

The glossary already says a Vendo uses the site's Rate tiers unless given its own (`GLOSSARY.md:87–100`). A tier is more than pesos → minutes: it also supplies **Validity, Pause Limit and Points**. ADR 0006 explicitly adopts site defaults plus Vendo overrides for Rate tiers and Points tables (`docs/adr/0006-rate-tier-sets-validity-pause-limit-and-points.md:3–10`). Rafael's earlier decision was to retain `/admin/rates` as the site default and customize individual Vendos in their Rates tab (`docs/research/vendo-list-management.md:26–35`, `:265–276`). The proposed Shop screen can therefore give an existing concept a clearer home rather than introduce a competing price system.

### 1.2 Actual storage: a site setting and nullable Vendo JSON, not two tier tables

| Scope | Current storage and meaning |
|---|---|
| Site / proposed Shop | JSON in `settings.value` under `key = 'coin_rate_tiers'`; `RateTiers()` reads it and `SaveRateTiers()` replaces it (`controller/internal/ledger/coin_window.go:44–60`). It is **not** a standalone site-rate table. |
| Vendo override | Nullable `vendos.rate_tiers BLOB`. SQL NULL means “use site rates”; the decoded Go field is nil (`controller/internal/ledger/vendo.go:39–56`, `:159–166`). |
| Price fixed for a purchase | `coin_windows.rates BLOB NOT NULL`, populated when the window opens (`controller/internal/ledger/coin_window.go:155–165`, `:327–329`, `:377–383`). |
| Points redemption | Separate nullable `vendos.points_tiers`; NULL means use the site Points table. It is independent of the coin-rate override (`controller/internal/ledger/vendo.go:39–42`, `:168–174`; ADR 0006). |

A tier's stored shape is `{pesos, seconds, validity_seconds, pause_limit, points_milli}` (`controller/internal/ledger/coin_window.go:18–28`). Site and Vendo tiers use the same validation, including a starting ₱1 tier, increasing peso thresholds, and non-decreasing seconds and Points per peso (`coin_window.go:54–59`, `:70–112`). “No limit” Validity is the ledger maximum, not a new timer mode (`:18–19`, `:95–96`).

New provisioning inserts a Vendo without setting `rate_tiers`, leaving the nullable column NULL (`controller/internal/ledger/vendo.go:159–166`, `:199`). Therefore **new Vendos already inherit**, rather than receive a permanent copied price list. `CustomizeVendoRates` copies the current site JSON; subsequent saves change only that Vendo. `UseSiteRates` clears the override back to NULL (`controller/internal/ledger/coin_window.go:63–83`). No additional inheritance flag or Shop-rates table is needed for this scope. Startup already initializes this storage and migrates tier limits and Points (`controller/internal/ledger/ledger.go:72–95`).

### 1.3 Where the price actually resolves

The effective tiers are chosen **when a Coin Window opens**, not afresh at each coin drop:

1. The Controller resolves the serving Vendo from the Device's HotSpot server. There may be a picker when several serve that server; no matching Vendo does not mean “send the customer to a default board” (ADR 0005, `docs/adr/0005-vendo-serves-a-hotspot-server.md:9–16`, plus its Deactivated Vendos amendment).
2. `OpenCoinWindow` reads `coalesce(vendos.rate_tiers, settings['coin_rate_tiers'])` inside its transaction and stores that resolved JSON in the window (`controller/internal/ledger/coin_window.go:319–329`, `:377–383`).
3. Settlement and late-coin credit read **the saved window JSON**. The highest eligible tier prices the whole window, rounded down to seconds; this is not KiTifi's greedy denomination algorithm (`controller/internal/ledger/coin_credit.go:13–29`; `coin_window.go:30–41`).
4. The tier supplies the Top-up's Validity, Pause Limit and Points. Late coins add only the difference already uncredited (`controller/internal/ledger/coin_credit.go:27–36`, `:62–84`). ADR 0006 preserves Validity and sets the latest Top-up's Pause Limit; Points pool across Vendos (`docs/adr/0006-rate-tier-sets-validity-pause-limit-and-points.md:7–10`).

**The fallback already exists and is live inheritance:** changing Shop rates changes the next window on every inheriting Vendo, including existing ones. It does not change customized Vendos or a window already opened. “Default for new Vendos” alone would underspecify this important distinction (`coin_window.go:63–83`, `:327–329`; ADR 0006, `:22`).

### 1.4 What is exposed today

- Navigation has **Coins → Rate tiers** (`/admin/rates`), **Points table**, and **Vendo List**. It does not yet label this area Shop Rates (`controller/internal/controller/web.go:157–164`).
- The site rates page is a textarea using `pesos=seconds,validity,pause limit,points`, plus a current-tier table. It is not the structured Vendo editor (`controller/internal/controller/web/templates/rates.html:3–18`; `controller/internal/controller/coin_window.go:82–113`).
- Vendo List already has **Details / Rates / Points** tabs. An inheriting Vendo shows **Using site rates**, a read-only tier list and **Customize**. A customized one has editable rows and **Use site rates** (`controller/internal/controller/web/templates/vendos.html:33–49`). The handlers already copy, save or clear the override (`controller/internal/controller/vendo.go:254–296`).
- The structured input adapter converts editable rows into the existing tier parser, preserving common validation rather than inventing another pricing engine (`controller/internal/controller/vendo_tiers.go:35–44`, `:62–115`).
- The Portal reads the serving Vendo's effective rates. With several candidates, each `vendos` entry carries its own rates; top-level `rates` is the first one's. With no serving Vendo, it displays site rates, but that display does not make coins purchasable without a serving board (`controller/internal/controller/portal.go:87–116`; `docs/portal-contract.md:11–19`; ADR 0005).

### 1.5 Pricing is not HotSpot profile configuration or a board setting

A HotSpot profile's `rate-limit` means network bandwidth, **not the coin price**. The Controller copies allowlisted Router fields and forces its one-Device and Idle Pause rules (`controller/internal/routeros/hotspot_profiles.go:13–19`, `:52–89`). A coin Top-up takes the Vendo's current profile through a separate ledger operation (`controller/internal/ledger/coin_credit.go:69–73`; `controller/internal/ledger/hotspot_profile.go:18–24`). Issue #93 specifies profile-copy refresh on Vendo save, Idle Pause changes and reconciliation. None of that requires a different peso/time tier set per profile.

Consequently a Shop price edit should **not** trigger a fleet HotSpot-profile rewrite or reset existing bought time. New bought time still reaches the Router through the normal Top-up path; the Router remains the sole meter (ADR 0003, `docs/adr/0003-router-is-sole-meter.md:1–3`; `ARCHITECTURE.md:15–19`).

The ESP8266 does not need a rate download. Its flash-time provisioning holds ID, key, Controller URL and hostname, not tier definitions (`firmware/subvendo/lib/subvendo_core/provisioning.h:23–30`). A search of `firmware/` for rate/tier found no pricing configuration; the design explicitly keeps peso/time mapping server-side (`docs/research/esp8266-lan-juanfi-qr-parity-and-controller-design.md:34–38`). The Controller is authoritative, while a Vendo reports coins (`ARCHITECTURE.md:42–47`). This is a Controller UI/model choice, not firmware work.

### 1.6 Specifications versus what shipped

| Source | Requirement / observed state |
|---|---|
| **#88 — closed** | Parent Vendo List spec explicitly requires site tiers until customized, per-Vendo Rates and Points tabs, and per-Vendo profiles. |
| **#94 — open** | Site tiers gain Validity and Pause Limit, migration preserves existing behavior, live windows retain their price. |
| **#95 — open** | Exactly the inheritance UI already present: Using site rates → Customize → Use site rates; serving-Vendo Portal prices and per-picker-entry rates. |
| **#98 — open** | The Points tab follows the same default/custom pattern; no serving Vendo, including deactivation, uses the site redemption table. |
| **#96 — open** | Tier Points earnings, transactional journal and carry-over; not a second independent timer schedule. |
| **#93 — open** | Per-Vendo HotSpot profiles, separate from peso/time pricing. |
| **#16 — closed** | Original Coin Window spec: tiers apply to the whole window and late coins are re-priced against its prior credit. Later #95/ADR 0006 supply the scope and richer tier semantics. |

These states were read from GitHub, not inferred from checkboxes. **Open does not mean absent from this checkout.** `git log --oneline -30 main` and `git show --stat` show `4f34152` (**#99**, “Vendo List (spec #88)”) shipping the ledger, Portal, Rates/Points/profile changes; `d379e8f` (**#100**) adds structured rate editing and responsive Vendo management; `86e645a` (**#101**) renames Sales to Sales Inventory and the Platform group to Main. The current source above establishes the actual implementation. This note does not close or reconcile the still-open child issues.

The requested all-state issue search, `shop OR default rate OR site rate`, returned #94, #95, #98, #93, #88, #96, #23, #59, #5 and #7. It did not identify a separate Shop-rates implementation ticket. No comments on the requested rate issues supplied a newer decision replacing the inheritance model.

## 2. Prior art: useful support, not proof of a universal default model

| System | What the repo's evidence supports | What it does **not** establish |
|---|---|---|
| **KiTifi** | Controller `tbl_vendo` rows hold their own `rates` and `points` JSON; billing reads that Vendo's tiers. The notes give default values and the per-Vendo calculator (`docs/research/kitifi-controller-handoff.md:78–100`). The board holds no rates (`docs/research/kitifi-firmware/server-contract.md:50–56`). | Those default values do **not** prove live inheritance from a Shop-level set, nor that editing a global default updates existing rows. |
| **JuanFi v4.4** | One ESP8266 combines Controller and coin unit, with one local `rates.data` holding price, minutes, Validity, data limit and profile (`docs/research/juanfi-architecture.md:7–14`, `:35–43`). | A one-box price list is not evidence of centralized multi-Vendo Shop defaults with overrides. The repo marks firmware control-flow conclusions as inferred from strings (`:3–5`). |
| **WiFi5-Soft 24.10.4** | Coin credit invokes `ratecalculator` **at the Vendo's rate** (`docs/research/wifi5soft-architecture-client-time.md:87–98`). This supports preserving per-Vendo pricing capability. | The note does not establish the create-time default, a Shop fallback, or override reset behavior. Its evidence is static recovered pseudo-source, with runtime behavior not run (`:5–7`). |
| **ADOPISOFT-style** | No ADOPISOFT reference was found by searching `docs/research/` case-insensitively. | No supported claim here about ADOPISOFT's site/default/override behavior. Do not present KiTifi or WiFi5-Soft findings as ADOPISOFT documentation. |

`docs/research/kitifi-firmware/` was checked: it contains reverse-engineering write-ups and string inventories, **not the original PHP controller source or the decompilation evidence corpus**. Its own provenance says that corpus is local/gitignored (`docs/research/kitifi-firmware/SUMMARY.md:8–12`). The cross-board summary confirms coin-only boards (`:24–29`); it cannot independently prove controller default-update semantics. The parity research reaches the same board/controller split (`docs/research/esp8266-lan-juanfi-qr-parity-and-controller-design.md:34–38`).

**Design implication:** prior art justifies keeping a per-Vendo exception available for migrating Operators. OpenPiso's own ADR 0006 and current code, not a claim that every vendor does it, justify the explicit shared-default model.

## 3. Options

The following are prospective consequences. All options must keep Coin Window snapshots and Router-only metering. A shared component for tier fields is compatible with all three; reusing an editor does not require merging configuration ownership.

### A. Shop rates only; remove per-Vendo coin rates

**Benefit:** the easiest price policy to explain: every coin slot sells the same tiers. One edit covers the whole site; no invisible exceptions or price-list drift.

**Cost:** removes an already-decided and shipped capability. Operators cannot differentiate locations or offers within one Controller, even though those Vendos may still have different bandwidth profiles. That may be an acceptable product restriction, but it reverses the KiTifi migration requirement in #88 and ADR 0006 rather than merely reorganizing navigation.

- **Data model:** make all new windows read the Shop setting; retire override writes and decide how to migrate existing custom JSON. Do not silently discard distinct prices. Keep historical `coin_windows.rates` unchanged. Removing a UI alone is insufficient: old stored overrides would otherwise keep taking precedence.
- **Vendo List:** remove editing and Customize/Use Shop actions; optionally show a read-only “Shop rates” summary and link to Shop. Keep Details/profile editing.
- **Portal:** show the same rates for every candidate. Preserve the `rates`/`vendos` response shape for Custom Portals rather than remove fields just because values match.
- **HotSpot profile sync:** no change to #93; common prices do not imply common bandwidth. No rate-driven profile sync is needed.
- **Issues:** #94 stays relevant; #95 must be superseded and its Portal acceptance criteria revised. #98 needs an explicit separate decision: retain custom redemption tables, or remove those too. Coin Points earnings in #96 can remain site-wide. Amend ADR 0006 and the glossary if removing overrides.

**When to choose:** only if Rafael deliberately wants one mandatory price policy, and accepts losing migration flexibility and doing a safe override migration.

### B. Shop rates as live default; retain explicit custom rates in Vendo List

**Benefit:** the common case is as simple as A—set Shop rates once and do nothing per Vendo—while an exception remains possible. It matches the glossary, ADR 0006, #88/#95 and current storage. There is no new backend pricing hierarchy to build.

**Cost:** a Shop edit intentionally does not reach customized Vendos. An Operator needs to see those exceptions and understand that Customize breaks the live link for the **whole tier set**, not just one field.

- **Data model:** keep `settings['coin_rate_tiers']` and nullable `vendos.rate_tiers`. NULL = Shop; non-NULL = complete custom set. Do not copy rates automatically at pairing, add an extra Boolean, infer inheritance by comparing equal JSON, or implement field-by-field fallback.
- **Vendo List:** retain the Rates tab, with **Uses Shop rates** / **Custom rates** visible states, a read-only inherited list, **Customize for this Vendo**, and **Use Shop rates** to clear custom values. Link the inherited state to the Shop editor. A small source label in the list/detail view makes exceptions discoverable without opening every editor.
- **Shop screen:** edit the existing site setting through a friendlier common tier editor. State “Applies to all Vendos using Shop rates, from their next Coin Window.” Show how many use Shop rates and how many are custom. Do not offer an implicit “overwrite every Vendo” save.
- **Portal:** keep the existing effective-serving-Vendo resolution and per-candidate rates. Do not show Shop prices indiscriminately when the selected Vendo is custom. Shop UI naming need not change API field names.
- **HotSpot profile sync:** unchanged; the price source stays independent of each Vendo's profile family. A Shop price save updates no existing Time Credit's profile or bought seconds.
- **Issues:** fulfills the substance of #94/#95 rather than replacing it. Keep #98's equivalent model for the separate Points redemption table; coin earning values remain inside each effective tier set (#96). Follow-up scope is Shop editor/placement, source visibility and wording, not reimplementing the already-shipped ledger/Portal behavior.

**When to choose:** the default recommendation, particularly for a site with mostly uniform prices and a few location-specific exceptions.

### C. Shop rates as a creation-time template only

This is another interpretation of “default for newly set-up Vendos”: copy Shop tiers into each Vendo at pairing, then every Vendo owns its list. KiTifi's per-row JSON makes independent lists a relevant comparison, but the repo does **not** establish that KiTifi uses this exact creation policy.

**Benefit:** changing a template never unexpectedly reprices existing Vendos; every Vendo is a self-contained configuration.

**Cost:** there is no longer a reliable “change the shop's prices” operation. Updating ten otherwise identical Vendos means ten saves or a new bulk-copy action. Lists drift even when no exception was intended. This introduces more lifecycle work than B for a less useful common case.

- **Data model:** populate `vendos.rate_tiers` during provisioning. Decide whether existing NULLs remain live-inheriting or are materialized during migration; otherwise two generations behave differently. Keeping provenance/version metadata for copied defaults would be additional complexity, not a requirement supported by today's model.
- **Vendo List:** each Vendo is editable; replace “Use Shop rates” with **Copy current Shop rates**. Do not label a copied set “uses Shop rates,” since it will not follow later edits.
- **Portal:** keep per-Vendo effective rates and picker entries. Window snapshotting is unchanged.
- **HotSpot profile sync:** unchanged, as in A and B.
- **Issues:** #94 becomes the template editor; #95's explicit reset-to-live-default behavior changes, requiring a spec/ADR update. Decide independently whether #98's Points tables also become copies. #96's coin earning and carry-over rules do not need to change.

**When to choose:** only if automatic propagation to existing inheriting Vendos is specifically unwanted. If the real need is a few frozen exceptions, B already handles them with Customize.

## 4. Recommendation

**Choose B. Put the shared Timer Rate editor under Shop, and retain Vendo List → Rates for exceptions.** These are two scopes of the same Rate tier model, not two competing features. If the concern is duplicated UI, share the structured tier editor; do not remove the per-Vendo ownership boundary to avoid duplicating controls.

The most important behavior to settle is **live inheritance versus copying**, not the menu name. A newly paired Vendo should store no custom tiers and continue to follow Shop changes until the Operator explicitly customizes it. This uses the shipped model and meets the earlier migration decision without forcing setup work on an Operator who wants uniform pricing.

Recommended wording and behavior:

| Operator action | Result |
|---|---|
| Set up a Vendo | **Uses Shop rates**; no extra price setup. |
| Change Shop tiers | Next windows on all inheriting Vendos use the new set; custom Vendos are untouched. |
| Customize a Vendo | Copy the current Shop set once; label it **Custom rates**. Later Shop edits no longer affect it. |
| Use Shop rates again | Confirm that custom tiers will be discarded, clear the override, and use Shop tiers on the next window. |
| Change rates during an open window | That window, including any late coins, keeps its saved tiers. Future windows get the new effective set. |

Those price effects are already supported by `controller/internal/ledger/coin_window.go:63–83`, `:319–329`, `:377–383` and `coin_credit.go:13–29`; the clearer labels, linking and scope explanation are the proposed work. Keep an entire custom set atomic: mixing Shop time with a Vendo-specific Validity or Points field would add a deeper inheritance model and make the effective offer harder to explain.

Do not add named shared plans or a bulk-propagation system yet. Nothing in the requested sources demonstrates a need beyond one site default and explicit exceptions. If Operators later maintain several identical custom sets across many Vendos, that would be evidence for reusable named tier sets; it is not needed to answer today's question.

### Decisions Rafael still needs to make (answered in §5.1)

1. **Does “default” mean live inheritance?** Recommend yes, including already-paired Vendos still using Shop rates, not just future pairings. If he means a one-time seed, choose C consciously and say so in the UI.
2. **Where should the common editor live and what should it be called?** Recommend **Shop → Timer Rates** (or Shop → Rates), with a brief “Default coin rates” scope description. Reuse/move the existing setting rather than create a second source of truth. Keep the old `/admin/rates` route or redirect it if navigation changes.
3. **Is Shop one current site/Controller?** This note assumes yes. Multiple shops under one Controller would require explicit ownership and scoping absent from this proposal.
4. **Does Shop also present the default Points redemption table?** Recommend a separate Shop **Points table** beside Timer Rates, retaining an independent default/custom choice per Vendo. Custom coin rates must not silently imply custom redemption rules. Since Points pool across Vendos, different redemption tables can offer different value at different locations (ADR 0006, `:10`); confirm that is still intended.
5. **How visible should exceptions be?** Recommend a source badge in Vendo management and inherited/custom counts on Shop save. Existing custom sets should remain custom even if identical to Shop today; equality is not consent to future repricing.

## 5. Decisions and proposed scope (2026-11-04)

Rafael asked for the recommendation to be written up before ticketing. The positions below adopt Option B and answer the open decisions in §4. They are proposals for the `feat/shop-timer-rates` branch, not shipped behavior.

### 5.1 Decisions

| Decision | Position | Why |
|---|---|---|
| Per-Vendo rates | **Keep them in Vendo List** as explicit exceptions. | Already decided in #88 and ADR 0006 and shipped in #99. An Operator with uniform prices never has to touch them, so keeping them costs little. |
| Meaning of "default" | **Live inheritance.** A new Vendo stores no custom tiers and follows Shop rates until customized; a Shop edit reprices every inheriting Vendo from its next Coin Window. | This is what `coalesce(vendos.rate_tiers, settings['coin_rate_tiers'])` already does (`controller/internal/ledger/coin_window.go:319–329`). Copying at pairing (Option C) removes the "change the shop's prices" operation and lets lists drift. |
| Granularity of an override | **Atomic.** A Vendo is either entirely on Shop rates or entirely custom. No per-field fallback (for example Shop time with a custom Validity). | Field-level inheritance is a deeper model that is hard to explain on the Portal and in the Vendo List. |
| Shop screen | **Rename and upgrade `/admin/rates`**, not a second setting. One editor, two scopes. | The Shop setting is the existing `coin_rate_tiers` row; a new table would create a second source of truth. |
| Points redemption | **Same treatment, separately.** A Shop Points table beside Timer Rates, with its own per-Vendo default/custom choice. | ADR 0006 keeps the Points table independent of coin rates; custom coin rates must not silently imply custom redemption. |
| Named rate plans / bulk propagation | **Not now.** | Nothing in the sources shows the need. Several identical custom sets across many Vendos would be the signal to add reusable tier sets. |
| Shop | **One Controller, one site.** | Multiple shops under one Controller is a separate architecture question. |

### 5.2 Scope for this branch

The ledger, Coin Window snapshot, Portal resolution and HotSpot profile sync stay as they are. The work is Controller UI and wording.

1. **Shop → Timer Rates.** Move the site rates page into a Shop group in the admin nav. Replace the textarea in `controller/internal/controller/web/templates/rates.html` with the structured row editor the Vendo Rates tab already uses (`vendo_tiers.go`), so both scopes share one editor and one validator. Add a one-line scope description: "Default coin rates. Applies to every Vendo using Shop rates, from its next Coin Window." Keep the `/admin/rates` route or redirect it.
2. **Inheriting/custom counts.** On the Shop Timer Rates page, show how many Vendos use Shop rates and how many are custom, so exceptions are visible where the default is edited.
3. **Vendo List wording.** In `controller/internal/controller/web/templates/vendos.html`, rename "Using site rates" to **Uses Shop rates**, "Customize" to **Customize for this Vendo**, and "Use site rates" to **Use Shop rates**. The reset action confirms that the custom tiers will be discarded.
4. **Source badge.** Show a small **Shop** / **Custom** badge per Vendo in the list and detail header, so an Operator can find exceptions without opening every Rates tab.
5. **Shop → Points table.** Move the existing Points table page under the same Shop group with the same scope line and counts; the Vendo Points tab gets the same wording changes.
6. **Docs.** Update `GLOSSARY.md` "site rates" wording to Shop rates where it describes the Operator-facing concept, and note in #94/#95/#98 what is already shipped versus what this branch adds.

Explicitly out of scope: copying rates at pairing, per-field inheritance, a Shop-rates table separate from the setting, rate-driven HotSpot profile rewrites, firmware changes, named rate plans.

### 5.3 Acceptance (E2E, lab)

- Pair a new Vendo, drop a coin: the window prices at Shop tiers without any per-Vendo setup.
- Edit Shop tiers: the next window on that Vendo uses the new tiers; a customized Vendo's next window does not; a window open during the edit keeps its saved tiers through a late coin.
- Customize a Vendo, then Use Shop rates: the override is NULL again and the next window prices at Shop tiers.
- Portal shows the serving Vendo's effective tiers, Shop or custom, and each picker candidate carries its own.

### 5.4 Prototype verdict (2026-11-04)

Three throwaway variants of Shop → Timer Rates were built on the existing `/admin/rates` route (`?variant=A|B|C`) and reviewed in the admin shell with four seeded tiers and five sample Vendos. The full set lives on branch `prototype/shop-rates-variants`, with screenshots under `evidence/shop-rates-*.png` there.

- **A Editor** (chosen): one card, the structured tier editor from the Vendo Rates tab in place of the textarea, and two coverage pills above it. Rafael asked that the pills be clickable: **Shop N** expands the list of Vendos using Shop rates (status, Open link), **Custom N** expands the custom ones (tier count, Edit rates link). One list open at a time.
- **B Editor + impact**: editor beside an "Applies to" Vendo list and Portal-style price tiles. Rejected: the side panels duplicate what the pills can reveal on demand.
- **C Shop hub**: totals strip, Timer Rates / Points table tabs, read-only table with an Edit dialog, Vendo chips under the table. Rafael first picked C without the chips, then switched to A once the pills could list Vendos; the chips row was judged confusing.

Also settled by the prototype: a **Shop** sidebar group (store icon) holding Timer Rates and Points table, moved out of Coins; breadcrumb "Shop › Timer Rates"; scope line "Default coin rates for this Shop. Every Vendo uses them unless it has custom rates." The admin CSP forbids inline styles and scripts, so page-specific CSS/JS must ship as static files.

### 5.5 Grill outcomes (2026-11-04)

A `/grill-with-docs` pass over §5 settled the remaining branches. Recorded in `GLOSSARY.md` and [ADR 0007](../adr/0007-shop-defaults-are-live-with-atomic-vendo-override.md); the spec issue carries the scope.

- **Language.** *Shop* is the glossary term for the place one Controller serves; *site* is now an Avoid word and every entry that said it was reworded. *Rate tier* stays the term, with *Timer Rate* added to its Avoid list, so the page is **Shop › Rate tiers**, not Timer Rates. *Points table* is a term. *Custom* qualifies a Vendo's own set, as in Custom Portal; no separate entry. Validity and Pause Limit for Vouchers and grants are "the Shop's setting".
- **Shop group.** Holds only defaults a Vendo can override: Rate tiers and Points table. Pause, Validity, Portal and Router stay in Settings. The group sits directly above Vendo List; Coins keeps Coin sales and Credit intents. Routes `/admin/rates` and `/admin/points` are unchanged, no redirect.
- **Shop pages.** Variant A on both pages. Pills "N Vendos use Shop tiers" / "N have Custom tiers" (rows for Points) list paired Vendos only, Deactivated included with status shown, sorted by name; an empty list reads as a sentence, the pill never hides. Shop rows: name, location, status, Open. Custom rows: name, tier or row count, Edit tiers linking to the Vendo dialog on the right tab via a new `tab=` query. Scope line: "Default Rate tiers for this Shop. Every Vendo uses them unless it has its own." Group heads: "A change here applies to each from its next Coin Window." / "Shop changes do not affect these." Button: "Save Shop tiers". No Shop name or other Shop settings.
- **Vendo List.** Badges only for exceptions: "Custom tiers", "Custom Points" on rows and in the detail header; nothing when following the Shop. Detail properties "Shop tiers" / "Shop table". Tabs renamed Rate tiers and Points table. Customize reads "Customize for this Vendo" with hint "Starts from a copy of the Shop tiers." Reset confirms: "Use Shop tiers? This Vendo's own tiers are discarded. Its next Coin Window prices at Shop tiers." and "Use Shop Points table? This Vendo's own table is discarded. Redemptions here use the Shop table from now on."
- **Records.** ADR 0007 records the live, atomic default and the rejected alternatives. #94, #95, #98 close as shipped; one `ready-for-agent` spec issue replaces them.
- **Test seam.** One: a `shop-defaults-only` scenario group in the lab E2E harness, with its summary report. No new Go unit seam; the existing form-adapter tests stay. The harness already posts Shop tiers as textarea text, which the shared adapter still accepts beside the structured rows.

## 6. Verification and limits

This was document/source research, not an implementation or fresh lab run. Read the specified architecture, glossary, ADRs, research, Controller/ledger/Router code and firmware configuration; fetched the requested issues and comments; checked the all-state issue search and recent `main` history. No runtime pricing, Portal rendering or hardware behavior was newly verified, and no test/build result is claimed.

Vendor inheritance/reset semantics beyond the evidence in §2 remain unverified, and no ADOPISOFT primary source was available in these notes. The original KiTifi source/corpus referenced by the notes is not present under `docs/research/kitifi-firmware/`. The open status of implemented child issues remains an issue-tracker discrepancy, not evidence that those features are pending in code.

## Sources

Repository paths and line ranges above refer to the inspected `86e645a` tree; issue links refer to the 2026-11-04 read.

- `ARCHITECTURE.md:15–19`, `:42–47`, `:102–113`; `GLOSSARY.md:9–15`, `:33–35`, `:87–100`.
- [ADR 0006 — Rate tier sets, Validity, Pause Limit and Points](../adr/0006-rate-tier-sets-validity-pause-limit-and-points.md); [ADR 0005 — Vendo serves a HotSpot server](../adr/0005-vendo-serves-a-hotspot-server.md); [ADR 0003 — Router is sole meter](../adr/0003-router-is-sole-meter.md).
- [Vendo List management](vendo-list-management.md), especially `:26–65`, `:205–232`, `:265–276`; [Sales report research](sales-report-filters-export-pagination.md) for this note's format, not current rate behavior.
- [KiTifi controller handoff](kitifi-controller-handoff.md):78–100; [firmware summary](kitifi-firmware/SUMMARY.md):8–12, :24–29; [server contract](kitifi-firmware/server-contract.md):50–56.
- [JuanFi architecture](juanfi-architecture.md):3–14, :35–43; [WiFi5-Soft architecture](wifi5soft-architecture-client-time.md):5–7, :87–98; [ESP8266 / JuanFi / controller parity research](esp8266-lan-juanfi-qr-parity-and-controller-design.md):34–38.
- [Issue tracker conventions](../agents/issue-tracker.md); [#88](https://github.com/H3atman/openpiso-wifi/issues/88), [#94](https://github.com/H3atman/openpiso-wifi/issues/94), [#95](https://github.com/H3atman/openpiso-wifi/issues/95), [#98](https://github.com/H3atman/openpiso-wifi/issues/98), [#96](https://github.com/H3atman/openpiso-wifi/issues/96), [#93](https://github.com/H3atman/openpiso-wifi/issues/93), [#16](https://github.com/H3atman/openpiso-wifi/issues/16), including available comments.
- `controller/internal/ledger/coin_window.go:18–112`, `:155–165`, `:319–383`; `controller/internal/ledger/coin_credit.go:13–84`; `controller/internal/ledger/vendo.go:39–56`, `:159–174`, `:199`; `controller/internal/ledger/ledger.go:72–95`; `controller/internal/ledger/hotspot_profile.go:18–24`.
- `controller/internal/controller/vendo_tiers.go:35–115`; `controller/internal/controller/vendo.go:254–296`; `controller/internal/controller/coin_window.go:82–113`; `controller/internal/controller/web.go:157–164`; `controller/internal/controller/web/templates/vendos.html:33–49`; `controller/internal/controller/web/templates/rates.html:3–18`.
- `controller/internal/controller/portal.go:87–116`; [Portal contract](../portal-contract.md):11–19; `controller/internal/routeros/hotspot_profiles.go:13–19`, `:52–89`; `firmware/subvendo/lib/subvendo_core/provisioning.h:23–30`.
- Git history / diffstats: `4f34152` ([#99](https://github.com/H3atman/openpiso-wifi/pull/99)), `d379e8f` ([#100](https://github.com/H3atman/openpiso-wifi/pull/100)), `86e645a` ([#101](https://github.com/H3atman/openpiso-wifi/pull/101)).
