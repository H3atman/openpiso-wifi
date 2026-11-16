# Vendo List: per-Vendo information, Edit, Settings, Restart, Deactivate

Research input, 2026-11-01. Rafael wants a Controller admin route and nav entry called **Vendo List** for managing every Vendo. KiTifi's Vendo screen is the reference. That screen has these columns: `# / Server / Name / Profile / Ip / Mac / Location / Last Coins Out / Current Sales / Total Sales / Active User / Status / Version / Action`. Each row expands to show Last Seen Online, Board and Type.

Rafael keeps that information and narrows KiTifi's seven action icons to four:

1. **Edit**: a tabbed dialog.
   - Tab 1 holds Name, Profile, IP, MAC and Location.
   - A Rates tab holds Amount, Time, Expiry, Pause Limit and Points.
   - A Points tab holds Points, Time, Expiry and Pause Limit.
2. **Settings**: the board config, with Name, Uptime, Server Address and four D0–D8 pin dropdowns.
3. **Restart Vendo**.
4. **Deactivate Vendo**.

This note maps that request onto what the Controller and firmware have today and recommends a design. It does **not** implement one. Terms follow `GLOSSARY.md`; §9 lists the proposed glossary additions.

> **Evidence.** Local code is the checkout based on `1fa9dfc` (branch `feat/vendo-list-management`). External facts come from official docs, linked inline:
> - htmx 4 docs, `four` branch source: https://github.com/bigskysoftware/htmx/blob/four/www/src/content/docs.md. https://htmx.org/docs/ still renders the 2.x page; I checked it on 2026-11-01.
> - Alpine: https://alpinejs.dev/advanced/csp and https://alpinejs.dev/globals/alpine-data.
> - MDN `<dialog>`: https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/dialog.
> - WAI-ARIA APG Tabs: https://www.w3.org/WAI/ARIA/apg/patterns/tabs/.
> - Arduino core for ESP8266: https://arduino-esp8266.readthedocs.io/en/latest/reference.html and https://github.com/esp8266/Arduino/blob/master/variants/nodemcu/pins_arduino.h.
>
> KiTifi behavior comes from our reverse-engineering notes (`docs/research/kitifi-controller-handoff.md`, `docs/research/kitifi-firmware/`) and from the five screenshots Rafael supplied (list, Edit Vendo, Rates, Points, Settings). The screenshots show values, not code. Every meaning I read into them is marked **[Inference]**. No board was flashed or touched, and no lab run was performed.

## 0. Decisions (Rafael, 2026-11-01)

Rafael answered the first draft's open questions. These decisions override the first draft's "defer" recommendations. The sections below keep the reasoning and say where a decision changed it.

| # | Topic | Decision |
|---|---|---|
| 1 | Route | Rename `/admin/vendos` to "Vendo List" in place. No new URL |
| 2 | Profile | Each Vendo has a HotSpot profile for its coin customers. The Controller copies the chosen Router profile into its own idle/no-idle pair and still forces `shared-users=1` and the site idle timeout. The Vendo of the latest coin Top-up decides a Time Credit's profile (§3.2) |
| 3 | Coins Out | "Last Coins Out" is the last time the coin box was emptied. Current Sales is coin Sales from then until now. Coins Out is a button next to Current Sales |
| 4 | Rates | Rates are per Vendo, and each rate tier carries its own Expiry (Validity), Pause Limit and Points. `/admin/rates` stays as the site default. A Vendo uses the site default until the Operator customizes that Vendo's Rates tab (§4.4) |
| 5 | Deactivate | The remaining Vendos serve only their own HotSpot server. ADR 0005 rule 1 keeps counting paired Vendos, not active ones (§5.3) |
| 6 | Unpair | Moves into the Edit dialog's footer |
| 7 | Points | Points don't expire with the Time Credit's Validity, follow a Rebind, and allow fractions. They live on the Time Credit and move to the Time Credit that replaces an expired one (§4.4, §0 #11) |
| 8 | Settings | Pins are the only board setting that changes, and they are editable from the Settings dialog. That needs firmware, a new signed message and an ADR (§5.1). Name, Uptime and Server Address are display-only |

Second round (Rafael, 2026-11-02). These were defaults or §10 questions in the first draft. All are now decided, and the rate-tier rules are recorded in [ADR 0006](../adr/0006-rate-tier-sets-validity-pause-limit-and-points.md):

| # | Topic | Decision |
|---|---|---|
| 9 | Validity | No Top-up ever shortens it: coins, Vouchers, admin grants and Points redemptions alike. The deadline is the later of the current one and now + this Top-up's Validity |
| 10 | Pause Limit | Each Top-up resets the Pause count and sets the limit from its tier or Points row, even when that lowers it. Vouchers and admin grants use the site value and earn no Points |
| 11 | Points carry | Points move to the Time Credit that **replaces** an expired one (linked through the Coin Window or Voucher redemption), not to "the Device". They pool across Vendos |
| 12 | Label | The UI says **Validity**, not "Expiry", and shows N/A as "No limit" |
| 13 | Coins Out | It records the computed amount only, with no counted-cash field |
| 14 | Profile copy | An allowlist: `rate-limit`, `address-list`, `incoming-filter`/`outgoing-filter`, `incoming-packet-mark`/`outgoing-packet-mark`, `parent-queue`, `transparent-proxy`. Session-ending fields and scripts are never copied |
| 15 | Expired Resume Code | The Portal Rebinds an expired Time Credit only if it holds Points. It says "Your time expired, but your N Points are kept. Insert coins to continue". With no Points it says "This code has expired" |
| 16 | Redeem, no serving Vendo | Use the site Points table |
| 17 | One server, several Vendos | Allowed. The picker shows each Vendo's own rates |
| 18 | Default pins | The #21 B10 bench map. `flash.py --wiring kitifi` writes a D4/D8 pin record for re-flashed KiTifi boards |
| 19 | Deactivate | Recorded as an amendment to ADR 0005 |
| 20 | Online | A Vendo is Online when its last authenticated report is under 10 s old. Tune after the lab runs |
| 21 | Coins Out undo | None. Rows are append-only, and the confirmation shows the amount first |
| 22 | Profile change | Changing a Vendo's Profile affects only its future coin Top-ups. Existing Time Credits stay on their copy |
| 23 | Profile copies | If P is deleted on the Router, its copies keep working and Details shows "Profile P no longer exists on the Router". The reconciler removes a copy pair only when no Vendo uses P and no `op-` user is on it |
| 24 | Redeem when expired | Redeeming on an expired Time Credit creates a replacement (as a Voucher does), moves the Points, then deducts. The Portal says "Insert coins or redeem Points" |
| 25 | Admin and Points | Balances are visible where Time Credits are shown. No grant or deduct action |
| 26 | Points cost | Points-table rows may cost fractions (thousandths, as when earning) |
| 27 | Deactivated message | "The coin slot for this network is out of service", not "No Vendo serves this network" |
| 28 | Unpaired Coins Out | Unpaired rows keep Coins Out as their only action |
| 29 | Restart | Refused while a Coin Window is live. Otherwise "Restart pending", sent on every report response until a new `boot_nonce` arrives. `canReboot()` false means the next report retries. Coin Windows are not blocked |

## 1. Current state

### 1.1 The page already exists: `/admin/vendos`

| Piece | Where | What it does |
|---|---|---|
| Nav entry | `controller/internal/controller/web.go:163`; quick nav "Pair a Vendo" at `:177` | "Vendos", icon `cpu`, under Platform |
| Routes | `controller/internal/controller/controller.go:131-135` | `GET /admin/vendos`; `POST /admin/vendos/pair` (admin-pair auth, used by `firmware/subvendo/flash.py`), `/unpair`, `/name`, `/server` |
| Handler | `controller/internal/controller/vendo.go:23-56` | Loads `ledger.Vendos()` and, from the Router, the HotSpot server names and the DHCP hostnames. If the Router is unreachable it shows "Router unavailable; HotSpot server choices cannot be loaded." (`:41`) |
| Template | `controller/internal/controller/web/templates/vendos.html:6-7` | One table with the columns ID, Name, MAC (+hostname), IP, HotSpot server, Status, "Last authenticated contact (UTC)" and Unpair. Name and HotSpot server are inline forms. Status prints the lifecycle status, with "waiting for first contact" for `provisioned` |
| Name save | `vendo.go:58-87` | At most 80 runes. POST, then redirect to `/admin/vendos` (`:86`); issue #77 |
| HotSpot server save | `vendo.go:89-131` | The value is checked against the Router's server list, and the save is refused if the Router is unreachable (`:112`). ADR 0005 allows this override |
| Unpair | `vendo.go:161-210` | The status moves `unpairing → unpaired` and the Router rules are removed via `UnpairVendo` |

A separate "Vendo List" route would duplicate all of this. The pair endpoint must also keep its path, because `flash.py` calls it.

**Recommendation:** evolve `/admin/vendos` into the Vendo List. Rename the nav label and page title, keep the URLs, and replace the inline forms with row actions and dialogs (§7).

### 1.2 What the ledger stores per Vendo

`vendos` (`controller/internal/ledger/vendo.go:14-25`, schema `:41-90`) holds:
- `id`, `mac`, `ip`, `secret` (never rendered; `json:"-"`);
- `status` ∈ `provisioned | pending | paired | unpairing | unpaired` (CHECK at `:44`);
- `last_contact` (RFC 3339, updated on every authenticated report, `vendo_reports.go:71`);
- `boot_nonce`, `rseq`;
- `hotspot_server` and `name`, both conditional `ADD COLUMN`s (`:59`, `:87`).

`DisplayName()` falls back to "Vendo N" and adds "(unpaired)" (`:28-37`).

**There is no** location, profile, firmware version, board type, uptime, per-Vendo rates or "active" flag.

Coins are recorded in `vendo_coin_deltas(vendo_id, epoch, total, window_id, coins, tag, recorded_at)` (`ledger/vendo_reports.go:16-31`). That table is the intake journal. It includes coins dropped outside a Coin Window, which go to audit only (`ledger/coin_sales.go:3-4`). Per-Vendo all-time coins already come from `CoinSales()` (`coin_sales.go:13-31`). Per-Vendo coin pesos over a period come from `salesTotals(tx, from, to, vendoID)` (`ledger/oversight.go:113-135`).

### 1.3 Rates, Pause Limit and Validity are site-wide today

- **Rate tiers**: one global JSON settings key, `coin_rate_tiers` (`ledger/coin_window.go:35-66`, default `[{"pesos":1,"seconds":60}]` at `:108`). The page is `/admin/rates` (`controller.go:89-90`). The highest eligible tier prices the **whole** Coin Window (`coin_window.go:23-33`). `OpenCoinWindow` snapshots the tiers into `coin_windows.rates` when the window opens (`coin_window.go:112`, `:220`).
- **Pause Limit**: the global `pause_settings` key (`ledger/pauses.go:17-49`, range 0–1000), shown on `/admin/pause`. A coin Top-up resets the count to 0 (`ledger/coin_credit.go:60`).
- **Validity**: the global `validity_seconds` key (`ledger/validity.go:10`, `:24-35`). Every Top-up calls `ExtendValidity`, which reads that one key (`validity.go:133-150`; called from `coin_credit.go:33`).
- `GLOSSARY.md` defines both Pause Limit and Validity as **site-set**.
- The Portal shows the global tiers (`controller/internal/controller/portal.go:82`, `:176`).

### 1.4 Controller → Vendo commands today

Every request starts at the Vendo. The board is an HTTP client that POSTs signed frames to `/vendo/messages` (`controller.go:91`; ARCHITECTURE.md "Vendo layer"). Commands ride on the response. The handler signs an Ack and appends whatever `CoinWindowCommand` returns (`controller/internal/controller/vendo.go:315-342`). The wire tags are Report `0x01`, Open `0x02`, Ack `0x03`, Close `0x04` and Closed `0x05` (`controller/internal/vendoprotocol/wire.go:12-19`), and **nothing else**. There is no restart, config or deactivate message.

### 1.5 Firmware today (`firmware/subvendo/`)

- **No production board application exists.** The tree has `lib/subvendo_core`, the host `sim/` and bench sketches. There is no `src/`.
- The board library knows only Report, Open and Ack (`lib/subvendo_core/wire.h:8`; `hal.h:71` `Inbound::Kind {kOpen, kAck}`). Close/Closed on the board are still open in #21.
- **Provisioning record** (`lib/subvendo_core/provisioning.h:12-27`):
  - It lives at flash sector `0x3FB000` and holds `magic, vendoId, key[32], controllerUrl[256], hostname[12], crc`.
  - `flash.py` writes it once (`flash.py:120-129`). It has **no pin, LED or name fields**.
- **Pins are compile-time** bench `#define`s (`bench/common/bench_pins.h`, `COIN_PIN 2`, `SET_PIN 15`).
- **Restart is gated.** `Core::canReboot()` returns true only when no Coin Window is open and no coin burst is in flight, and the board must never restart itself otherwise (`lib/subvendo_core/core.h:55-56`; TEST-PLAN D12).
- **Repo guardrail** (`firmware/CLAUDE.md`): never flash or touch a physical board without Rafael, and back up the vendor firmware first.

## 2. Column-by-column mapping

Status key:
- **have**: stored today.
- **derivable**: computable from stored or Router data with no new capture.
- **new data**: needs a new column or event, with a note on whether the board must send it.

| KiTifi column | Our term | Status | Source / how |
|---|---|---|---|
| # | Vendo ID | have | `vendos.id` |
| Server | HotSpot server | have | `vendos.hotspot_server`. Learned on first contact and editable (ADR 0005; `vendo.go:89-131`) |
| Name | Name | have | `vendos.name`, falling back to "Vendo N" (`ledger/vendo.go:28-37`); #77 |
| Profile | HotSpot profile | new data, Controller + Router | Decided (§0 #2). `vendos.hotspot_profile` plus a Router profile copy (§3.2) |
| Ip | IP | have | Learned on first contact (`StageVendoPairing`, `ledger/vendo.go:127-144`) |
| Mac | MAC | have | Set at flash time (`PrepareVendo`, `ledger/vendo.go:95-123`) |
| Location | Location | new data, Controller-only | New `vendos.location TEXT NOT NULL DEFAULT ''`, a conditional `ADD COLUMN` like `name` (`ledger/vendo.go:83-90`) |
| Last Coins Out | Coins Out (proposed term) | new data, Controller-only | See below |
| Current Sales | coin Sales since the last Coins Out | derivable once Coins Out exists | `SUM(vendo_coin_deltas.coins)` after the last Coins Out. Without Coins Out, the nearest stand-in is "Today" via `salesTotals(..., vendoID)` (`oversight.go:113-135`) |
| Total Sales | coin Sales, all time | derivable | `CoinSales().Coins` (`coin_sales.go:13-31`) |
| Active User | Devices online on this Vendo's HotSpot server | derivable, one Router field | See below |
| Status (Online) | Online / Offline + lifecycle | derivable | `last_contact` age against a threshold, combined with `status`. See §7.3 |
| Version | firmware version | new data, **board + protocol** | The report carries no version (`wire.go:23-47`). The firmware has no version constant (none found in `lib/`). Needs a new signed field or message |
| Last Seen Online (expand) | last authenticated contact | have | `vendos.last_contact` |
| Board (expand) | board model | new data, board or flash time | Only one board exists (ESP8266 LAN). The value could be written by `flash.py` or reported later. **Recommend dropping it** until a second board exists |
| Type (expand) | — | not applicable | KiTifi's `type` picks how coins are counted (coins/pulse) and also marks voucher "sellers" (`kitifi-controller-handoff.md:80`, `:106`). Our protocol has one counting model (cumulative total, `ARCHITECTURE.md` "Vendo layer"). **Drop it** |

**Last Coins Out** **[Inference]**. The list shows "(Oct/30) ₱4,670.00", and a coin icon sits next to each Current Sales figure:
- Rows marked "(Oct/30)" show small Current Sales (₱65–₱483).
- Rows marked "(Oct/02)" show large ones (₱4,488, ₱6,234).

That fits "the date and amount of the last time the Operator emptied the coin box", with Current Sales counting coins since then, and the coin icon as the "coins out" action. Our KiTifi notes do not cover this handler, so this is unverified.

To store it, add an append-only `vendo_coins_out(id, vendo_id, through_delta_id, pesos, recorded_at)` table:
- One row is written when the Operator records a collection.
- `through_delta_id` is the highest `vendo_coin_deltas.id` included, so Current Sales is `WHERE vendo_id = ? AND id > through_delta_id`. That is exact with no timestamp ties.
- This counts **every** coin the Vendo reported, including audit-only coins, because `vendo_coin_deltas` is the intake journal. That matches the cash in the box, which is what a collection checks.

**Active User** **[Inference]**. It is probably the HotSpot active count for the Vendo's server. In the screenshot each Vendo has its own server.
- RouterOS documents a `server` field on `/ip/hotspot/active` (MikroTik HotSpot docs, cited in `docs/research/vendo-auto-select-by-vlan.md:148`).
- `ActiveSessions` reads that endpoint but does not keep `server` (`controller/internal/routeros/time_credits.go:237-262`).
- Keeping it is a one-field change. Then count per server.
- If two Vendos share a server, both rows show the same number. So label the column **"Online Devices (server)"** rather than suggesting the Devices belong to the Vendo. The glossary avoids "User" for a Device.

**Sales scope.** Per-Vendo Sales is **coin Sales only**. Vouchers are not sold through a Vendo, so the glossary's Sales total (coins + Voucher face value) only applies site-wide.

## 3. Edit dialog: which fields can be edited

### 3.1 Tab 1 fields under the identity and pairing model

| Field | Editable? | Why |
|---|---|---|
| Name | **Yes** | Operator label (#77). The handler already exists (`vendo.go:58-87`) |
| Location | **Yes** | New free text with the same 80-rune limit as Name. It is only a label |
| HotSpot server | **Yes** (already) | ADR 0005: "the Operator can change a Vendo's HotSpot server". KiTifi shows it as the "Server" column, not in Edit. Moving the existing form into the dialog keeps the row compact |
| IP | **Read-only** | It is learned at first contact. Pairing then installs a static lease, the bypass and the firewall rule for that IP (`routeros/vendo.go:15-191`), and `vendo_ip` is unique among non-unpaired Vendos (`ledger/vendo.go:76`). Editing it in the ledger alone would desync the Router rules. Moving a board is unpair + re-flash |
| MAC | **Read-only** | It is the pairing identity, written at flash time and bound to the secret (ADR 0005 "Flashing is pairing"). A new MAC means a new board, so re-flash |
| Profile | **Yes** (§3.2) | A select of the Router's HotSpot user profiles. Like the server select, it needs the Router to be reachable |

### 3.2 What "Profile" would mean for us

KiTifi's dropdown shows "default - Kitifi". **[Inference]** It is a Router HotSpot user profile ("default"), possibly qualified by a Router or site name. KiTifi's `mtik_add_time` sets "new limit+password+profile" on the user (`kitifi-controller-handoff.md:56`), and JuanFi rate tiers carry a profile field (`juanfi-architecture.md:42`). Our notes don't show where KiTifi reads the per-Vendo value from, so this is unverified.

For us, the **Controller owns the HotSpot user profile**:
- Each Time Credit gets `openpiso-idle` or `openpiso-no-idle` from the site Idle Pause setting (`routeros/time_credits.go:50-55`).
- `EnsurePauseSettings` rewrites every `op-` user's profile when that setting changes (`time_credits.go:193-216`).

A Time Credit is not bound to a Vendo. It can be topped up from several Vendos and from Vouchers. So a per-Vendo profile would have no single right answer and would fight the Idle Pause projection.

The first draft recommended leaving Profile out. **Rafael decided otherwise (§0 #2).** In his KiTifi setup, Profile is the HotSpot user profile a Vendo gives the customers who insert coins there. The design that keeps the Controller's guarantees:

- **Storage.** `vendos.hotspot_profile TEXT NOT NULL DEFAULT ''`. Empty means the Controller's base profiles, which is today's behavior.
- **Choice.** The Details tab lists the Router's `/ip/hotspot/user/profile` names, minus the Controller's own `openpiso-*` profiles.
- **Copy, keeping Idle Pause.** For each profile P in use, the Controller ensures `openpiso-P-idle` and `openpiso-P-no-idle`. Each holds P's settings, with `shared-users=1` and `idle-timeout` (the site Idle Pause seconds, or `none`) forced, as `ensurePauseProfiles` does for the base pair today (`routeros/time_credits.go:219-230`). `shared-users=1` keeps a Time Credit on one Device at a time (GLOSSARY "Time Credit").
- **Which profile a Time Credit gets.** A coin Top-up sets the Time Credit's profile to its Vendo's. Vouchers, admin grants and Points redemptions leave it unchanged.
- **Code that changes.**
  - `EnsureTimeCredit` picks the profile from an idle flag only (`time_credits.go:49-55`). It must take the Time Credit's profile family.
  - `EnsurePauseSettings` moves every `op-` user onto one base profile (`:193-216`). It must keep each user in its own family and switch only between that family's idle and no-idle profiles.
- **Staleness.** The copy goes stale if the Operator edits P on the Router, e.g. in Winbox. Re-copy it when the Vendo is saved, when Idle Pause changes, and on the reconciler pass.
- **Router-is-sole-meter (ADR 0003)** holds: `limit-uptime` stays on the user, and a profile's rate limit doesn't meter time. The risk is in fields that end or alter sessions, such as `session-timeout` or on-login/on-logout scripts. Decided (§0 #14): an allowlist of copied fields.

### 3.3 Rates and Points tabs

Decided (§0 #4, #7). The Edit dialog has three tabs:
- **Details**: Name, Profile, Location, HotSpot server, read-only IP and MAC. Unpair goes in the footer.
- **Rates**: Amount, Time, Validity, Pause Limit, Points.
- **Points**: Points, Time, Validity, Pause Limit.

Rates and Points each show "Using site rates" until the Operator customizes them for this Vendo. The rules are in §4.4, and the tab markup in §7.4. Slice 1 ships Details alone; the tabs arrive with the Rates slice (§8).

## 4. Rates, Expiry, Pause Limit, Points

### 4.1 What KiTifi does

- **Rates.** Each Vendo row stores its own `rates` JSON `[{id, pause_limit, amount, time, expiry, points}]` and `points` JSON `[{id, pause_limit, points, time, expiry}]` (`kitifi-controller-handoff.md:80-83`).
- **Pricing is greedy.** It repeatedly takes the largest `amount` that fits. Expiry and pause limit come from the highest denomination used, and points are summed into a per-client `tbl_user.points` (`:85`, `:98`, `:100`).
- **Screenshot values.**
  - Rates: ₱1 → 31 min, Pause Limit 1, 0.1 points … ₱30 → 1 d 1 min, Pause Limit 15, 3 points.
  - Points table: 5 points → 30 min … 150 points → 1 d 1 min, Pause Limit 1.
  - Expiry is "N/A" on every row.

### 4.2 What a per-Vendo version would change for us

**Per-Vendo rate tiers (Amount, Time).** This is cheap in the ledger, because a Coin Window already snapshots its rates (`coin_window.go:112`, `:220`):
- Add a nullable `vendos.rate_tiers`, where NULL means "use the site tiers", and make `OpenCoinWindow` read the Vendo's tiers first.
- The same `SaveRateTiers` rules apply (`coin_window.go:45-66`).

The real cost is the **Portal**:
- `portalState.Rates` is global today (`portal.go:71`, `:82`).
- The Portal would have to show the rates of the Vendo that Insert Coin will reserve. When several Vendos match, it would need rates per Vendo in the picker.
- That changes the JSON contract, so check it against ADR 0004.

**Per-tier Expiry (Validity) and Pause Limit.** These conflict with the glossary, which defines both as **site-set**:
- `ExtendValidity` reads one site key for every Top-up, including Vouchers (`validity.go:133-150`).
- The Pause count resets on every Top-up (`coin_credit.go:60`).
- Per-tier values need new rules: what happens when a ₱30 Top-up (limit 15) follows a ₱1 Top-up (limit 1)? What does a Voucher Top-up do? What if two Vendos have different tiers?

This is a domain change, not a field. It needs a glossary update and an ADR before any issue. Rafael chose to build it (§0 #4); §4.4 sets the rules.

**Points.** This is a new feature with no current home:
- Nothing named Points exists in our code or docs, outside the KiTifi notes.
- It needs:
  - a points balance per Time Credit;
  - an earn rule per tier;
  - a redemption flow on the Portal (points → a Top-up);
  - an audit trail.
- **ADR 0003 holds** as long as redemption credits bought time through the normal Top-up path, so the ledger still records only time bought.
- Glossary-level questions:
  - Is redeemed time Sales? It should not be, by analogy with admin grants.
  - Do points share the Time Credit's Validity and forfeit with it?
  - Do they survive a Rebind?
  - Can fractional points be earned? KiTifi shows 0.1 points per ₱1.
- Under our pricing (the highest eligible tier prices the whole window, `coin_window.go:23-33`), a matching earn rule would be `coins × tier.points / tier.pesos` **[Inference, design sketch only]**.
- Issue #3 ("Pricing model extensions beyond pausable used time") is the nearest existing home.

Rafael chose to build Points (§0 #7). His answers settle the glossary-level questions: Points outlive Validity, survive a Rebind, and allow fractions. §4.4 sets the rules.

### 4.3 Coin Window and Router-is-sole-meter impact

- **Per-Vendo tiers:** no change to the Coin Window lifecycle. The snapshot already isolates a live window from rate edits.
- **Points:** ADR 0003 holds if Points only ever produce a Top-up of bought time. It would be broken by a "points balance in minutes" that the Controller counts down.
- **Per-tier Pause Limit and Validity:** no metering impact. The impact is semantic (glossary), as above.

### 4.4 Decided design: per-Vendo rates, per-tier Validity and Pause Limit, Points

**Rate tier shape.** `RateTier` (`coin_window.go:18`) gains three fields:
- `validity_seconds`, where 0 means N/A ("No limit");
- `pause_limit`, with the same 0–1000 range as `SavePauseSettings` (`pauses.go:38`);
- `points_milli`: Points in thousandths, so KiTifi's 0.1 is stored as 100. The ledger never stores floats.

**Where tiers live.**
- **Site default:** the existing `coin_rate_tiers` key, edited on `/admin/rates`. That page gains Validity, Pause Limit and Points columns.
- **Per Vendo:** a nullable `vendos.rate_tiers`, where NULL means "use the site default". The Rates tab offers "Customize", which copies the site tiers in, and "Use site rates", which sets it back to NULL.
- **Snapshot.** `OpenCoinWindow` already snapshots tiers into `coin_windows.rates` (`coin_window.go:112`, `:220`). It reads the Vendo's tiers, or the site's when NULL. The new fields ride in the same snapshot, so editing rates never changes a live window.
- **Points table.** Same pattern: a site key plus a nullable `vendos.points_tiers`. A row is `{points_milli, seconds, validity_seconds, pause_limit}`.

**Applying a tier on a coin Top-up.** The highest eligible tier prices the whole Coin Window (`coin_window.go:23-33`), so every settlement has exactly one pricing tier. `creditCoinWindow` (`coin_credit.go:15-60`) applies it:
- **Validity:** `valid_until = max(valid_until, now + tier.validity_seconds)`, with N/A treated as `MaxValiditySeconds` (`validity.go:22`). Today `ExtendValidity` sets `now + site seconds` (`validity.go:133-150`). It gains a seconds argument and the `max`. The site `validity_seconds` stays for Vouchers and admin grants, under the same `max` (§0 #9).
- **Pause Limit:** the Pause count already resets on a coin Top-up (`coin_credit.go:60`). The Time Credit also stores the tier's limit in a new `time_credit_pauses.pause_limit`. That stored value is what gets passed to `TakePause(id, limit)` (`pauses.go:68`) instead of the site value. Vouchers and admin grants set the site Pause Limit.
- **Points:** `earned = coins × tier.points_milli / tier.pesos`, rounded down. A late coin re-prices the whole window, so only `earned − already earned for this window` is added, the same way credited seconds work today.
- **Profile:** set from the window's Vendo (§3.2).

**Points balance and carry-over.**
- **Balance.** `time_credits.points_milli`, plus an append-only `points_journal(time_credit_id, delta_milli, reason, ref, at)` row for every earn, redeem and carry. Each row is written in the same transaction as the Top-up.
- **Rebind.** Rebind moves the Time Credit (`rebind.go:124`), so its Points move with it.
- **Carry.** When coins or a Voucher arrive after Validity has ended, a replacement Time Credit is created for the Device (`coin_credit.go:31-46`; Voucher path `vouchers.go:198`). In that same transaction, the expired Time Credit's balance moves to the replacement, with a journal row on each side. Forfeiting time no longer forfeits Points.
- **Resume Code.** Lookup has no Validity filter (`rebind.go:87-89`), so an expired Time Credit can still be found by its Resume Code. Decided (§0 #15): the Portal Rebinds it only if it holds Points.

**Redeeming Points.**
- The Portal shows the balance and the Points table of the Vendo serving the Device's HotSpot server. That is the site table unless the Vendo customized it, and the site table when no Vendo serves the server (§0 #16).
- Redeeming a row is a Top-up of bought time through the normal path, so ADR 0003 holds: the Router meters, and the ledger records time bought. It applies the row's Validity and Pause Limit and writes an audit row.
- Redemption is **not Sales**, by the same rule as admin grants (GLOSSARY "Sales").

**Portal contract (ADR 0004).**
- `rates` in the Portal state is site-wide today (`portal.go:71`, `:82`; `docs/portal-contract.md:113`, `:164`). It becomes the rates of the Vendo serving the Device.
- When several Vendos serve one server with different rates, each `vendos` entry carries its own rates for the picker.
- Points add a balance, the Points table, and a redeem action.
- Custom Portals that read `rates` keep working if the field keeps its shape. The contract doc must record the change.

**Glossary and ADR.** The glossary defines Pause Limit and Validity as "site-set". Both become "set by the rate tier (or Points row) of the latest Top-up; Vouchers and admin grants use the site value", and Points needs an entry (§9). Record these rules in an ADR before the first rates slice. They change how every coin Top-up sets Validity, Pause Limit and Points, and what forfeiting covers.

## 5. Settings, Restart, Deactivate

### 5.1 Settings: KiTifi vs. us

KiTifi's board runs a local HTTP server:
- `getSystemConfig` returns uptime, `server_address`, the four pins, `board` and `type`.
- `saveSystemConfig` writes `config.json` with unescaped values, is refused during a session, and reboots.
- `saveApConfig` is **unauthenticated** and resets the pins.
- Sources: `kitifi-firmware/board-behavior-esp8266-lan.md:302-312`.
- Its pins are runtime config in SPIFFS `config.json` (`kitifi-firmware/SUMMARY.md:40-44`).

Our board has **no local server by design**: no local admin token, and signed commands only (`esp8266-lan-juanfi-qr-parity-and-controller-design.md:113-114`).

| Settings field | For us | Needs |
|---|---|---|
| Name | Same as Edit's Name. The board has no name, only the flash-time hostname `opnw-XXXX` | Nothing. Don't show it twice |
| Uptime | **Derivable on the Controller**, approximately. Each boot has a fresh `boot_nonce`, and `RecordVendoReport` already detects a change (`ledger/vendo_reports.go:64-65`). Store `vendos.booted_at` when the nonce changes and show "up since". This is accurate to within one heartbeat plus any time the board couldn't reach the Controller. Exact uptime would need a new report field | Controller-only (one column) |
| Server Address | It is `controllerUrl` in the provisioning record, written at flash time (`provisioning.h:27`). The value is the Controller's own `portalURL` (`vendo.go:156`). Changing it remotely needs a signed command, and a write to the config sector that is read-only today. A wrong value orphans the board until re-flash | **Read-only**, or omit |
| Coinslot signal / set pin, System-ready LED pin, Insert-coin LED pin | Compile-time today, with no production firmware yet. Runtime pins need: (a) a config record v2 with pin fields and CRC; (b) a signed ConfigSet message on report responses; (c) a board-side allowlist; (d) a restart through the Restart path below | **Board + protocol + ADR**. Decided: editable (§0 #8), design below |

**Pin dropdowns are riskier than they look [Inference from primary sources].**
- D-labels map to GPIOs as follows: D0=16, D1=5, D2=4, D3=0, D4=2, D5=14, D6=12, D7=13, D8=15 (Arduino core `variants/nodemcu/pins_arduino.h`).
- On the LAN board, D3 is the Ethernet CS and D5–D7 are the SPI bus to the W5x00 (`board-behavior-esp8266-lan.md:349-360`).
- D3, D4 and D8 are boot strapping pins that our design keeps free of acceptor lines (`esp8266-lan-juanfi-qr-parity-and-controller-design.md:227`; #21 B10).
- GPIO16 (D0) cannot take an interrupt: "Interrupts may be attached to any GPIO pin, except GPIO16" (Arduino ESP8266 reference, Digital IO). So D0 cannot be the coin signal pin.

That leaves **D1 and D2** as the only safe coin-signal choices, and very few options for the other three roles. KiTifi's own defaults (coin D4/GPIO2, set D8/GPIO15) are exactly the pins our design rejects.

A free D0–D8 dropdown would let an Operator break the coin slot from the admin panel. The first draft recommended fixing the pin map in firmware. **Rafael chose editable pins (§0 #8).** Boards re-flashed from KiTifi are already wired coin=D4 and set=D8, so one compiled map can't fit every board. The design that keeps editable pins safe:

- **Allowlist, checked on both sides.**
  - D3 and D5–D7 are never offered on the LAN build, because they carry Ethernet.
  - D0 is never offered for the coin signal, because it can't take an interrupt.
  - LEDs may be "None", like KiTifi's `-1`.
  - D4 and D8 stay allowed because existing KiTifi boards use them. The dialog warns that they are boot strapping pins.
  - The board re-checks the map and rejects duplicates.
- **A rewritable pin record.** The flash-time provisioning record (`provisioning.h:12-27`) stays write-once. Pins go in a separate record with its own CRC, so a bad write can never damage the pairing key or Controller URL. If the pin record is invalid, the board falls back to its compiled-in default map.
- **A signed ConfigSet message.** It is a new Controller → Vendo tag that rides on the report response, like Close (`wire.go:12-19`), so the Controller still never initiates a connection (#5). It carries the pin map and the board's current `boot_nonce`, so it can't be replayed onto a later boot. The board stores the map and applies it on its next restart, which is gated by `canReboot()` (`core.h:55-56`).
- **Readback.** The board reports its active pin map, along with firmware version and uptime. The dialog shows what is running, and "Pending restart" while the saved map differs. Readback carries no secrets (board candidate ticket 8, `esp8266-lan-juanfi-qr-parity-and-controller-design.md:448-463`).
- **Recovery.** Ethernet pins are never offered, so a wrong map can break coin counting but not the board's link to the Controller. The Operator fixes it from the same dialog.
- **Prerequisites.** An ADR (new protocol tag, rewritable record, allowlist) and the production board application. So this lands after #21, with Rafael present for any bench step (`firmware/CLAUDE.md`). Default map decided (§0 #18): the B10 map, with a `--wiring kitifi` flash preset.

### 5.2 Restart

- **KiTifi** has `restart` (refused while busy) and `forcerestart` (ignores the session) as token-guarded local GETs (`board-behavior-esp8266-lan.md:310-311`).
- **Ours** has to be a new signed message that rides on a report response, like Close:
  1. The Controller queues it only when no Coin Window is live. The unique index `coin_window_vendo_live` (`coin_window.go:118`) is already the check `OpenCoinWindow` uses for `ErrVendoBusy`.
  2. The frame carries the Vendo's current `boot_nonce`, so a replayed Restart cannot reboot a later boot.
  3. The board acts only if `canReboot()` is true (`core.h:55-56`).
  4. The Controller sees the restart complete when a report arrives with a new `boot_nonce`.
- No "force" variant. Restarting with a window open or a burst in flight is exactly the lost-coin case the design forbids (`esp8266-lan-juanfi-qr-parity-and-controller-design.md:125`; TEST-PLAN D12).
- **Limit:** remote Restart only reaches a board that still reports. A hung or unreachable board needs a power cycle anyway.
- KiTifi boards needed restarts because they reboot on heartbeat failures (`board-behavior-esp8266-lan.md` §8 S5, S8). Our design retries instead. Runtime pins (§5.1) give Restart a job: a saved pin map takes effect on the next restart.

**Board + protocol work, after #21.** Rafael wants the Restart icon. It shares its restart path with ConfigSet.

### 5.3 Deactivate

KiTifi's `deactivatevendo` releases a **license seat** (`activation`) and **deletes the Vendo's `tbl_sales` rows** (`kitifi-controller-handoff.md:83`, `:111`). We have no licensing, and deleting Sales would break the ledger's audit guarantees. Copy neither.

Proposed meaning:

> **Deactivate**: the Operator takes a paired Vendo out of service. The Controller opens no new Coin Windows on it, and Insert Coin no longer picks it. It stays paired: its secret, Router rules, name and Sales are kept. **Reactivate** reverses it. Unpair is the separate, permanent removal.

This is **Controller-only**. The board's acceptor is enabled only while a Coin Window is open and acked (ARCHITECTURE.md "Vendo layer"), so refusing new windows blocks the coin slot with no firmware change.

Implementation points:
- **Storage.** Add `vendos.active INTEGER NOT NULL DEFAULT 1` as a conditional `ADD COLUMN`. A new `status` value would need a table rebuild, because SQLite CHECK constraints can't be altered in place. The precedent is `migrateProvisionedVendos` (`ledger/vendo.go:145-172`).
- **Gates.** `OpenCoinWindow` (`coin_window.go:188-190`, returning the existing `ErrVendoDisabled`) and `matchingVendos` (`controller/vendo_selection.go:7-26`) must both skip inactive Vendos.
- **Keep accepting reports.** `RecordVendoReport` rejects only `status != "paired"` (`vendo_reports.go:54-55`). Keeping it that way lets a window that was live at deactivation settle, keeps Online/Last seen working, and keeps Coin gap auditing.
- **A live window finishes normally.** It ends on Done, on the ~60 s idle timer, or at the 10-minute cap. Deactivation is not refused and does not force a Close. The board doesn't implement Close yet (#21).
- **Auto-select side effect.**
  - ADR 0005 rule 1: "If exactly one Vendo is paired at the site, it uses that Vendo", implemented as `len(paired) == 1` (`vendo_selection.go:22-24`).
  - If that count becomes "active paired Vendos", deactivating one of two Vendos makes the other serve **every** HotSpot server.
  - If it stays "paired", a site whose only Vendo is deactivated shows "No Vendo serves this network" correctly. But a two-Vendo site with one deactivated falls back to server matching.
  - **Decided (§0 #5): keep counting paired Vendos.**
    - If exactly one Vendo is paired, use it when it is active. Otherwise, no Vendo is available.
    - With more Vendos, server matching runs over the active ones only.
    - A deactivated Vendo's network shows "No Vendo serves this network", and the other Vendos keep serving only their own server.

## 6. Related issues

From `gh issue list --repo H3atman/openpiso-wifi --state all --limit 100` and searches for points, restart, deactivate, version, settings, location, rates per vendo and pin (2026-11-01):

| Issue | State | Relevance |
|---|---|---|
| #77 Vendo names in the admin panel and Sales report | closed | Name field and `DisplayName` fallback. Edit reuses it |
| #41 Auto-select the Vendo by the Device's HotSpot server | closed | `matchingVendos`. Deactivate must respect it (§5.3) |
| #42 Flash-time Vendo pairing, finished on first contact | closed | Why IP and MAC are read-only (§3.1) |
| #43 Subvendo: flash script, config record and opnw- hostname | open, needs-triage | The provisioning record that Settings would have to extend |
| #21 ESP8266 Vendo board: close/closed, bench tests, board tickets | open | Board Close/Closed; B10 pin map "avoiding GPIO0, GPIO2 and GPIO15, recorded in an ADR"; filing board candidate tickets 2, 3, 7, 8, 10. Restart, Version and pins belong after it |
| #16 Coin Window and rate tiers | closed | Global tiers and the per-window snapshot |
| #8 Pause, resume, Pause Limit, Idle Pause; #12 Validity | closed | Site-set Pause Limit and Validity (§4.2) |
| #3 Pricing model extensions beyond pausable used time | open, needs-triage | Nearest home for Points and per-tier Validity |
| #23 Coin Window abuse ban | open, needs-triage | Unrelated to the list, but it also blocks Insert Coin. Deactivate's refusal message should not be confused with a ban |
| #5 Spec: Controller MVP | closed | "Every request initiated by the Vendo (the Controller never needs to reach into the Vendo)", which rules out a KiTifi-style local admin API |

No issue mentions Points, Deactivate, Location or firmware Version. The board design doc's candidate ticket 7 (signed firmware updates, "Version pinned by the Controller") is the closest thing to Version. Candidate ticket 8 ("no secrets in config readback, no state-changing GETs") constrains any future Settings readback (`esp8266-lan-juanfi-qr-parity-and-controller-design.md:448-463`).

## 7. Recommendation

### 7.1 Route and nav

Keep `/admin/vendos` and all of its POST endpoints. Rename the nav label (`web.go:163`) and the page title to **"Vendo List"**. Keep "Pair a Vendo" in quick nav. No new route is needed (decided, §0 #1).

### 7.2 Table

The columns, in KiTifi's order where we have the data:

`# · HotSpot server · Name · Profile · IP · MAC · Location · Last Coins Out · Current Sales · Total Sales · Online Devices (server) · Status · Version · Actions`

- **Status** merges reachability and lifecycle: Online / Offline / Waiting for first contact / Deactivated / Unpairing / Unpaired. "Last seen" goes on a second line or in the cell's `title`, which replaces KiTifi's expandable row.
- **Current Sales** has a **Coins Out** button beside it (§0 #3), as KiTifi does.
- **Version** is added by the board-readback slice (§5.1). **Board and Type stay out** (§2).
- Unpaired Vendos stay listed, muted and without actions, so past Sales stay explainable. This matches the Sales filter's "(unpaired)" convention from #77.
- Wrap the table in the existing `.table-scroll` (`vendos.html:6`).

### 7.3 Online

- Online means `now − last_contact` is below a threshold.
- The board reports every 2 s and treats the Controller as unreachable after 6 s without a fresh ack (`core.h:23-25`).
- **Proposal:** a 10 s threshold, i.e. five missed heartbeats. This is a choice, not a measured value.
- Status is computed at render time. Live refresh via `data-poll` (the pattern in `dashboard.html:4`) is deferrable. If added, the existing `htmx:before:request` guard (`admin.js:225-228`) only protects open menus. An open `<dialog>` must sit outside the polled region.

### 7.4 Actions and dialogs (existing patterns only)

**Edit (pencil icon).** A native `<dialog>` per row, rendered server-side inside the actions cell. A site has a handful of Vendos, so N small dialogs avoid a fragment endpoint.
- Open it with a small `Alpine.data` component shaped like `salesFilters`: `show()` calls `this.$refs.dialog.showModal()`, and `close()` calls `this.$refs.dialog.close()` (`admin.js:58-76`; `sales.html:20` already ships `<dialog … aria-labelledby>`).
- MDN on `<dialog>`:
  - `showModal()` makes "the rest of the page inert", and Esc dismisses it.
  - Without a `closedby` value, a modal behaves as `closerequest`.
  - Use `autofocus` on the first field.
  - Always include an explicit close button.
- The Details tab holds one `<form method="post">` with Name, Profile, Location and HotSpot server, plus read-only IP and MAC. Profile arrives with its own slice (§8). Rates and Points are separate forms in their own tabs, so saving one tab never resubmits another.
- It posts to one endpoint (e.g. `POST /admin/vendos/edit`) that saves all three in one ledger transaction and redirects to `/admin/vendos`, the same pattern as `vendoName` (`vendo.go:86`). This replaces `/name` and `/server`, or wraps them.
- HotSpot server validation needs the Router (`vendo.go:112`). When the Router is down, keep today's behavior: show the message and disable only the server select.

**Deactivate / Reactivate (power icon).**
- Use `<form method="post" action="/admin/vendos/active" hx-post="/admin/vendos/active" hx-confirm="Deactivate Vendo 3?" hx-target="closest tr" hx-swap="outerHTML">`. The server returns the re-rendered row when `HX-Request` is set, and otherwise redirects.
- This is the recipe already recorded in `docs/research/go-htmx-alpine-admin-panel.md:129`.
- htmx 4 behavior to account for:
  - It swaps **every** response except 204/304 (htmx 4 docs, "HTTP Response Code Handling"), so error responses must be row HTML, not `http.Error` text.
  - `hx-confirm` uses "a simple javascript dialog".
  - `hx-target="closest tr"` is a documented extended selector ("Targeting With Extended Selectors").
- Rows need `id="vendo-{{.ID}}"`.

**Coins Out (coin icon beside Current Sales).** A `hx-confirm` form that names the amount being recorded, using the same row-swap recipe as Deactivate.

**Restart and Settings (rotate and gear icons).** Rafael wants both (§0 #8). Each icon ships with its board slice (§8), because neither does anything until the firmware supports it. Until then, a disabled icon with a "needs firmware" tooltip would only add noise.

**Unpair.** Decided (§0 #6): it moves into the Edit dialog's footer as a destructive button with its own confirmation. The row then shows Edit, Deactivate, and later Settings and Restart.

**Icons.** `icons.go:6-37` has `settings-2` but no pencil, power or rotate icons. Add the lucide paths for `pencil`, `power` and `rotate-cw` the same way.

**Tabs (from the Rates slice).** Follow the WAI-ARIA APG Tabs pattern:
- `role="tablist"`, `role="tab"` with `aria-selected` and `aria-controls`, and `role="tabpanel"` with `aria-labelledby`;
- Left/Right arrows move between tabs.

That fits in one `Alpine.data('tabs')` component. The CSP build allows simple assignments and method calls in attributes but not arrow functions or template literals (https://alpinejs.dev/advanced/csp). Panels toggle with `x-show`. Slice 1 ships the Details panel alone, without tabs.

### 7.5 Controller-only vs. firmware/protocol

| Item | Controller-only | Needs board + protocol |
|---|---|---|
| Rename to Vendo List; Edit dialog Details (Name, Location, HotSpot server, read-only IP and MAC, Unpair) | ✓ | |
| Status Online/Offline, Last seen | ✓ | |
| Total Sales, Current Sales, Last Coins Out, Coins Out button | ✓ | |
| Online Devices per HotSpot server | ✓ (read `server` from `/ip/hotspot/active`) | |
| Up since (from a `boot_nonce` change) | ✓ | |
| Deactivate / Reactivate | ✓ | |
| HotSpot profile per Vendo | ✓ (Router profile copies) | |
| Per-tier Validity and Pause Limit; per-Vendo Rates tab | ✓ + glossary, ADR, Portal contract | |
| Points: earn, carry, redeem | ✓ + glossary, Portal contract | |
| Version, exact Uptime | | ✓ new report fields |
| Restart | small Controller part | ✓ new signed tag; needs the production board app |
| Pins from Settings | small Controller part | ✓ rewritable pin record, signed ConfigSet, allowlist, ADR |

### 7.6 Later

- Live polling of the table.
- Board and Type columns, only if a second board model appears.

## 8. Slice order (one vertical slice per issue)

**Controller-only**, in order:

1. **Vendo List page and Edit dialog (Details).**
   - Rename the nav and page; add a `vendos.location` column.
   - A row Edit `<dialog>` with Name, Location and HotSpot server, read-only IP and MAC, and Unpair in the footer.
   - Status shows Online/Offline/lifecycle with Last seen; add the icons.
   - Lab E2E (HTTP): edit name and location → the list and the Sales filter show them; a stale Vendo reads Offline.
2. **Deactivate and Reactivate.**
   - `vendos.active`; gates in `OpenCoinWindow` and `matchingVendos`, with the paired count unchanged (§5.3); `hx-confirm` and a row swap.
   - Lab E2E: deactivate → Insert Coin on that server is refused with "The coin slot for this network is out of service" (§0 #27), and the other Vendo still serves only its own server → reactivate → a Coin Window opens. Sales history is unchanged.
3. **Sales columns and Coins Out.**
   - Total Sales; the `vendo_coins_out` table; a Coins Out button beside Current Sales with a confirmation showing the amount; unpaired rows keep it (§0 #28).
   - Lab E2E: coins → Current Sales rises → Coins Out → Last Coins Out shows the date and amount, and Current Sales resets to ₱0.
4. **Online Devices per server and Up since.**
   - Keep `server` in the active-session read; set `vendos.booted_at` on a `boot_nonce` change.
   - Lab E2E with the simulated Vendo: a new boot nonce resets Up since.
5. **HotSpot profile per Vendo.**
   - `vendos.hotspot_profile`; a Profile select in Details; the idle/no-idle copies; a coin Top-up sets the Time Credit's profile; `EnsurePauseSettings` keeps each user in its own family (§3.2).
   - Lab E2E on the CHR: give a Router profile a rate limit → coins on that Vendo → the HotSpot user is on the copied profile with `shared-users=1` and the site idle timeout; toggling Idle Pause keeps the family. Copy cleanup and a deleted P follow §0 #23.
6. **Rate tiers carry Validity and Pause Limit (site rates).**
   - Before the issue: the ADR and glossary update (§4.4, §9).
   - `/admin/rates` gains Validity and Pause Limit; settlement applies the tier (Validity never shortens; the Pause Limit is stored per Time Credit); Vouchers keep the site values.
   - Lab E2E: a ₱30 Top-up then a ₱1 Top-up keeps the ₱30 deadline; the Pause Limit follows the latest tier.
7. **Per-Vendo Rates tab and Portal rates.**
   - `vendos.rate_tiers` override; tabs in Edit; Portal `rates` from the serving Vendo; update `docs/portal-contract.md`.
   - Lab E2E: two Vendos on two servers with different rates → each Device's Portal shows, and is charged, its own Vendo's rates.
8. **Points: earn and carry over.**
   - Before the issue: the Points glossary entry.
   - A Points column on rate tiers; the `points_milli` balance and `points_journal`; carry on replacement; Rebind keeps them; the Portal shows the balance.
   - Lab E2E: coins earn 0.1 per ₱1 → Validity ends → new coins → the balance carries over; Rebind to a new MAC keeps it.
9. **Points: redeem.**
   - The site Points table and the per-Vendo Points tab; Portal redemption as a non-Sales Top-up with the row's Validity and Pause Limit, including on an expired Time Credit (§0 #24); balances shown in admin (§0 #25).
   - Lab E2E: redeem 5 points → a 30-minute Top-up, the balance drops, and Sales are unchanged.

**Board work**, after #21, with Rafael present for any bench step (`firmware/CLAUDE.md`):

10. **Version, uptime and Restart.** New report fields; a Restart tag gated by `canReboot()` and the boot nonce, pending until a new boot nonce (§0 #29); the Version column and Restart icon.
11. **Pins from Settings.** ADR first (rewritable pin record, ConfigSet tag, allowlist, default map from #21 B10); the Settings dialog with readback and "Pending restart" (§5.1).

Slices 1–4 are independent of 5–9 and can run in parallel with them, but each group runs in the order above.

## 9. Glossary changes

Applied to `GLOSSARY.md` on 2026-11-02: **Coins Out, Deactivate, HotSpot profile, Rate tier, Points**, plus redefinitions of **Pause Limit** and **Validity**. Location stays out because it is only a label.

## 10. Questions still open

None. The five first-draft questions were decided as §0 #14–#18.
