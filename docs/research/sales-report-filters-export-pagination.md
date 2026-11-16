# Sales report: Vendo filter, date shortcuts, export, pagination

Research input, 2026-10-30. The request is to turn the admin Sales page (`controller/internal/controller/web/templates/sales.html`) into a report like KiTifi's "Sales Inventory" screen: Total Sales for the current filter, a Vendo ("Station") dropdown with All, From/To dates, shortcuts **Today / This Week / This Month / This Year**, Export, and a paginated table (`# / Date / Time / IP / MAC / User / Expiry / Type / Station / Total Time / Amount`, "Showing 1 to 25 of N entries"). This note maps what the Controller already records onto that screen and recommends a design; it does **not** implement one. Terms follow `GLOSSARY.md`.

> **Evidence.** Local code is the checkout based on `344e0d9`. External facts come from official SQLite, Go, htmx 4 and Microsoft docs, linked inline. The KiTifi screenshot was supplied by Rafael; its Export dropdown is closed, so it does **not** show which formats KiTifi exports. No queries were benchmarked; performance statements are from SQLite's documentation, not measurement.

## 1. Current state

### 1.1 Storage

SQLite via `modernc.org/sqlite`, **one connection**, WAL, `synchronous=FULL`, foreign keys on (`controller/internal/ledger/ledger.go:10`, `:29-57`). Schemas initialize at startup; later changes are conditional column adds / table rebuilds, not numbered migrations (`ledger.go:64-84`, `controller/internal/ledger/vendo.go:40-55`).

There is no single "sale" table. Sales is derived from:

| Table | What it holds |
|---|---|
| `vendo_coin_deltas` | id, Vendo id, epoch, cumulative total, reported Window id, positive coin delta, tag, UTC RFC3339Nano `recorded_at` (`controller/internal/ledger/vendo_reports.go:14-30`, `:57`) |
| `coin_windows` | Vendo, Time Credit, captured Device MAC/IP, saved rates, status, coins, credited seconds, ms timestamps (`controller/internal/ledger/coin_window.go:109-118`) |
| `vouchers` / `voucher_batches` / `voucher_prices` | code, batch, redeemed Time Credit, nullable UTC RFC3339 `redeemed_at`; seconds per Voucher; optional integer-peso face value (`controller/internal/ledger/vouchers.go:14-35`, `:262-274`) |
| `audit` | append-only: timestamp, actor, reason, Time Credit, before/after bought seconds (`vouchers.go:34-35`) |
| `time_credits`, `time_credit_validity` | **current** MAC/IP binding and **current** Validity, not snapshots taken at sale time (`controller/internal/ledger/validity.go:9-19`, `controller/internal/ledger/rebind.go:9-20`) |
| `vendos` | id, MAC/IP, pairing state, HotSpot server; **no friendly name** (`vendo.go:27-45`) |

### 1.2 What "Sales" means today (keep it)

`Sales(from, to)` = redeemed Voucher face values + positive coin deltas, with half-open `julianday` bounds. Vouchers with no price are counted separately and left out of the peso sum. Coin receipts count even if never assigned to a Window (`GLOSSARY.md:59-61`, `controller/internal/ledger/oversight.go:87-113`, `vendo_reports.go:83-105`).

**Trap:** one Coin Window can receive several deltas, time is credited at settlement, and late coins produce extra Top-ups — possibly onto a different Time Credit after expiry (`controller/internal/ledger/coin_credit.go:13-101`). Showing the Window's `credited_seconds` on every receipt row multiplies the time; joining receipts to every Top-up audit multiplies money.

### 1.3 Request and UI path

`GET /admin/sales` → `salesPage` (`controller/internal/controller/controller.go:115`, `controller/internal/controller/sales.go:25-66`): defaults to month-start..today, parses inclusive local dates, holds `voucherMu` during the aggregate query, renders `salesView{From, To, SalesReport, TotalPesos}`. `?format=json` returns the same aggregate. No rows, Vendo filter, export or pagination.

Admin stack: `html/template` pages, vendored **htmx 4**, Alpine CSP build, local CSS (`controller/internal/controller/web.go:12-16`, `layout.html:3-10`). `renderPage` handles boosted navigation; `isFragmentRequest` excludes boosted nav and history restore (`web.go:64-110`). Time Credits already does search fragments with URL-pushing links (`web/templates/time_credits.html:16-17`, `:29-35`, `web/static/admin.js:195`). `docs/research/go-htmx-alpine-admin-panel.md` describes an earlier build; current source wins.

## 2. Screenshot columns vs. available data

| Column | Feasible? |
|---|---|
| `#` | Derived page ordinal |
| Date / Time | Yes — `recorded_at` or `redeemed_at`, shown in local time |
| IP | Coins: Window snapshot (when the Window belongs to the receipt's Vendo). Vouchers: **not recorded** |
| MAC | Coins: Window snapshot. Vouchers: redemption audit actor `device:<MAC>` |
| User | Use **Time Credit**; there is no customer identity |
| Expiry | Only **current** Validity (mutable); no per-sale snapshot |
| Type | Derived: Coins / Voucher |
| Station | Coins: Vendo id + HotSpot server. Vouchers: **no Vendo attribution** |
| Total Time | Vouchers: batch seconds. Coins: Window total only, no per-receipt split |
| Amount | Coin delta, or nullable Voucher price |
| Delete | Omit (see §6) |

Join receipts to Windows on **both** reported Window id **and** Vendo id, matching the allocator's ownership check (`coin_credit.go:76-80`). Unassigned receipts keep null Device / Time Credit. Current MAC/IP from `time_credits` must not be shown as historical (`rebind.go:135-138`).

## 3. Query and pagination

**Interactive pages: LIMIT/OFFSET.** Numbered pages and an exact "Showing X to Y of N" need it. SQLite computes and discards skipped rows, so deep pages get slower; keyset pagination is cheap but only does next/previous ([rowvalue: scrolling window queries](https://www.sqlite.org/rowvalue.html#scrolling_window_queries)). Use keyset for export batches; revisit interactive paging only if deep pages are measurably slow. Order by `(ts DESC, source_kind, source_key)` for stability; page size from an allowlist (25/50/100).

**Index the expression the query uses.** Existing predicates use `julianday(col)`; a plain column index won't match ([expression indexes](https://www.sqlite.org/expridx.html), [multi-column indexes](https://www.sqlite.org/queryplanner.html#multi_column_indices), [covering indexes](https://www.sqlite.org/queryplanner.html#covering_indexes)). Don't switch to lexical RFC3339 comparison: writers use different fractional precision, and offsets are accepted.

```sql
CREATE INDEX IF NOT EXISTS sales_coin_date
  ON vendo_coin_deltas(julianday(recorded_at), id);
CREATE INDEX IF NOT EXISTS sales_coin_vendo_date
  ON vendo_coin_deltas(vendo_id, julianday(recorded_at), id);
CREATE INDEX IF NOT EXISTS sales_voucher_date
  ON vouchers(julianday(redeemed_at), code) WHERE redeemed_at IS NOT NULL;
```

The query must repeat `redeemed_at IS NOT NULL` for the partial index to apply. Pick between two fixed SQL variants (with / without `vendo_id = ?`) instead of `(? IS NULL OR vendo_id = ?)`. `julianday` has millisecond precision ([date functions](https://www.sqlite.org/lang_datefunc.html)), so the id tie-breakers matter.

**One filtered relation, two query shapes** — totals and rows come from the same filter, never from the current page:

```sql
WITH filtered AS (
  SELECT julianday(d.recorded_at) AS ts, 'coin' AS source_kind,
         CAST(d.id AS TEXT) AS source_key, d.coins AS amount_pesos
  FROM vendo_coin_deltas d
  WHERE julianday(d.recorded_at) >= julianday(?) AND julianday(d.recorded_at) < julianday(?)
    -- Vendo variant: AND d.vendo_id = ?
  UNION ALL
  SELECT julianday(v.redeemed_at), 'voucher', v.code, p.pesos
  FROM vouchers v LEFT JOIN voucher_prices p ON p.batch_id = v.batch_id
  WHERE v.redeemed_at IS NOT NULL
    AND julianday(v.redeemed_at) >= julianday(?) AND julianday(v.redeemed_at) < julianday(?)
    -- Only for "All": Vouchers have no Vendo.
)
SELECT COUNT(*), COALESCE(SUM(amount_pesos), 0), COALESCE(SUM(amount_pesos IS NULL), 0)
FROM filtered;
```

The row variant reuses `filtered` + `ORDER BY … LIMIT ? OFFSET ?`, then enriches only that page (Window MAC/IP, Vendo server, Voucher seconds). Run totals + page in one short read transaction if they must agree; no Router calls inside it. Check `EXPLAIN QUERY PLAN` on a 100k+ dataset during implementation.

## 4. Date ranges

Keep what Dashboard already does: Controller-local calendar dates, **Monday** week start, period-to-date, exclusive end at tomorrow's local midnight (`controller/internal/controller/sales.go:13-53`, `controller/internal/controller/dashboard.go:67-80`).

| Shortcut | From (inclusive) | To (inclusive) |
|---|---|---|
| Today | today | today |
| This Week | most recent Monday | today |
| This Month | 1st of month | today |
| This Year | Jan 1 | today |

Parse with [`time.ParseInLocation`](https://pkg.go.dev/time#ParseInLocation), build midnights with [`time.Date`](https://pkg.go.dev/time#Date), get the exclusive end with [`AddDate(0,0,1)`](https://pkg.go.dev/time#Time.AddDate) (not `+24h`), convert to UTC for SQL. [`time.Local`](https://pkg.go.dev/time#Local) is the host's zone — **not** necessarily Asia/Manila. No explicit timezone setting was found in the Controller or docs. If one is added ([`time.LoadLocation`](https://pkg.go.dev/time#LoadLocation)), Dashboard and Sales must share it.

## 5. Export

**CSV only.** Use [`encoding/csv`](https://pkg.go.dev/encoding/csv) (write, `Flush`, check `Error()`), `Content-Type: text/csv; charset=utf-8`, `Content-Disposition: attachment; filename="sales-YYYY-MM-DD-to-YYYY-MM-DD.csv"` via [`mime.FormatMediaType`](https://pkg.go.dev/mime#FormatMediaType), `Cache-Control: no-store`. Prepend a UTF-8 BOM so Excel opens it correctly ([Microsoft](https://support.microsoft.com/en-us/office/opening-csv-utf-8-files-correctly-in-excel-8a935af5-3416-4edd-ba7e-3dfd2bc4a032)). Export **all filtered rows**, not the visible page. Amount as a number under `Amount (PHP)`, blank when unknown. CSV quoting does not prevent spreadsheet formula injection; decide how to handle text cells starting with `= + - @`.

**Don't stream a live query to a slow client over the single connection** (`ledger.go:33`): it blocks every other DB operation. A long read on a second connection pins WAL checkpoints instead ([WAL concurrency](https://www.sqlite.org/wal.html#concurrency), [checkpointing](https://www.sqlite.org/wal.html#checkpointing)). Options: build the CSV in short keyset batches into a bounded temp file, then serve it (weaker snapshot), or use a dedicated read-only connection with a timeout (exact snapshot).

XLSX adds a dependency; PDF adds layout work and is worse for analysis. Neither is needed. KiTifi's export formats are **unknown**: `kitifi-controller-handoff.md` and `kitifi-firmware/` have no hits for export/csv/xlsx/pdf/backup.

## 6. Constraints and scope

- The Controller owns the ledger; Sales needs no Router lookup. ADR 0003 forbids Controller-side *used/remaining* time metering, not reporting *bought* time (`ARCHITECTURE.md:30-46`, `docs/adr/0003-router-is-sole-meter.md`). Label the column "Credited time", never "Remaining".
- Say **Sales**, not "Income" (`GLOSSARY.md`).
- **Delete: out of scope.** Admin oversight is read-only (`ARCHITECTURE.md:133-136`); audit is append-only by trigger (`vouchers.go:34-35`). Deleting rows changes totals and destroys evidence; corrections need their own auditable design.
- **Backup: out of scope.** CSV isn't a backup, and copying the SQLite main file under WAL isn't a correct one ([WAL](https://www.sqlite.org/wal.html)).

## 7. Decisions (Rafael, 2026-10-30)

1. **A coin row is one completed Coin Window** — the customer pressed Done and the Vendo's Closed report settled it — not each coin receipt. This replaces the receipt-row recommendation in §2–§3.
2. Dates use Controller-local time; the host is set to **Asia/Manila**. Monday weeks, period-to-date shortcuts.
3. Picking a specific Vendo **excludes Vouchers**; All includes them.
4. **Capture what is missing from now on:** Voucher redemption IP (and MAC), **Expiry at the time of sale**, and a **Vendo name** shown instead of the id.
5. **CSV** export is enough.
6. Backup, Delete and column sorting are out. **Add a Search** over IP, MAC and User.
7. **Total Sales counts every coin received** (receipt definition, matches Dashboard and the coin box), not just completed Windows. One figure only; no "Listed" sum.
8. **User column = Time Credit id** for coins, Voucher code for Vouchers.
9. **No backfill.** Existing rows are lab data; new columns start blank.

## 8. Revised design

### 8.1 Rows

- **Coins:** `coin_windows WHERE status = 'settled'`, dated by `closed_at` (Unix ms, `controller/internal/ledger/coin_window.go:109-118`). Pressing Done sets `closing` (`CloseCoinWindow`, `controller/internal/ledger/coin_credit.go:105-107`); the Vendo's Closed report sets `settled` + `closed_at` and credits the whole Window (`coin_credit.go:88-98`). Amount = `coins`, Total Time = `credited_seconds`, MAC/IP = the Window snapshot, User = `time_credit_id`. This also fixes the per-receipt time problem from §2.
- **Vouchers:** redeemed Vouchers dated by `redeemed_at`, as before.
- Open and closing Windows are left out until they settle.
- **Late coins** after settlement add to the same Window and credit a Top-up (`coin_credit.go:83-98`), so that row's Amount and Total Time grow but its date stays the same. If the Time Credit had expired, the Window moves to a replacement Time Credit (`coin_credit.go:31-45`), and the row shows the new one.

**Totals count every coin received (decided: option A).** Rows are Windows, but the report shows **one figure, Total Sales**, using the existing receipt definition: positive coin deltas by `recorded_at` plus redeemed Voucher prices, the same as Dashboard (`controller/internal/ledger/oversight.go:87-113`), filtered by date and Vendo (`vendo_coin_deltas.vendo_id`). Search filters the rows only, not Total Sales, because receipts carry no MAC/IP of their own.

On a normal day the rows add up to Total Sales exactly. They can differ in three rare cases, which a short note under the total explains ("Total counts every coin received; coins not yet in a completed session appear as rows after Done"):

- a stray coin dropped with no session open: counted, no row;
- a session still open: counted now, row after Done;
- a session crossing midnight: money on the day the coins dropped, row on the day of Done.

Rejected: a second "Listed" figure (confusing), and Total = sum of rows (drops stray and unfinished coins and disagrees with Dashboard and the coin box).

Indexes (decided: all five). The two coin-receipt indexes serve Total Sales and the Dashboard; the Window indexes serve rows, count and pages. Integer `closed_at` needs no `julianday` expression. They are written in the sale's own transaction; no write queue, since a sale and its index entries must be durable together and the index cost is negligible next to the existing `synchronous=FULL` commit:

```sql
CREATE INDEX IF NOT EXISTS sales_coin_date ON vendo_coin_deltas(julianday(recorded_at), id);
CREATE INDEX IF NOT EXISTS sales_coin_vendo_date ON vendo_coin_deltas(vendo_id, julianday(recorded_at), id);
CREATE INDEX IF NOT EXISTS sales_window_closed ON coin_windows(closed_at, id) WHERE status = 'settled';
CREATE INDEX IF NOT EXISTS sales_window_vendo_closed ON coin_windows(vendo_id, closed_at, id) WHERE status = 'settled';
CREATE INDEX IF NOT EXISTS sales_voucher_date ON vouchers(julianday(redeemed_at), code) WHERE redeemed_at IS NOT NULL;
```

The §3 query shape still applies: one filtered `UNION ALL` relation feeding both totals and page rows. Swap the coin branch for settled Windows, and convert the local [from, to) bounds to Unix ms for `closed_at`.

### 8.2 New captured data (forward-only)

Add columns with conditional `ALTER TABLE … ADD COLUMN`, as `hotspot_server` was added (`controller/internal/ledger/vendo.go:46-55`). Rows from before the change show blanks; nothing is backfilled (lab data only).

| Column | Written when | Notes |
|---|---|---|
| `vouchers.redeemed_mac`, `vouchers.redeemed_ip` | In the redemption transaction, next to `redeemed_at` (`controller/internal/ledger/vouchers.go:262`); `mac`/`ip` are already in scope there | No backfill: old rows show blanks (lab data only) |
| `vouchers.valid_until` | Same transaction, after validity is settled; read `time_credit_validity.valid_until` (`controller/internal/ledger/validity.go:9-14`) | "Expiry at time of sale" |
| `coin_windows.valid_until` | In `creditCoinWindow` after each credit (`coin_credit.go:31-70`) | Updates on late Top-ups, so it shows the Expiry after the Window's last credit |
| `vendos.name` | Edited by the Operator on the Vendos page (`web/templates/vendos.html`), like the HotSpot server form | Show name, else `Vendo {id}`. The `Hostname` on that page (`opnw-XXXX`) is fetched live from the Router (`controller/internal/controller/vendo.go:35-44`), not stored, so it can only pre-fill the name field |

### 8.3 Search

Query param `q`. One case-insensitive `LIKE '%q%' ESCAPE '\'` across IP, MAC and User (Time Credit id for coins, Voucher code for Vouchers), applied inside the date/Vendo filter. Normalize MAC input (case; `-` vs `:`) before matching. `LIKE '%…'` can't use an index, so the date range limits the cost. Search resets `page=1` and is included in the export.

### 8.4 Endpoints and UI

- `GET /admin/sales?vendo=all|<id>&from=YYYY-MM-DD&to=YYYY-MM-DD&q=&page=1&page_size=25`
- `GET /admin/sales/export.csv?vendo=…&from=…&to=…&q=…`
- `?format=json` keeps every existing field with the same meaning; rows, matching count, page and page size are added.
- Columns: `# / Date / Time / IP / MAC / User / Expiry / Type / Vendo / Total Time / Amount`.
- The Vendo selector lists every Vendo: active ones first, then unpaired ones labelled "(unpaired)", so their past sales stay filterable.
- A page past the end shows the last page. The JSON is always paginated (page size capped at 100); only the CSV returns every row.
- UI as in the earlier recommendation: a GET form with `hx-get` + `hx-push-url`, a whole-region swap, shortcuts and search reset the page, Export is a plain download link. CSV follows §5.

## 9. Identifier note: User column

A Time Credit has three separate random values (`controller/internal/ledger/vouchers.go:176`):

- **`id`**: 32 random bytes, base64url (`vouchers.go:52-58`). An identifier, already shown on the admin Time Credits page (`web/templates/time_credits.html:31`). This is the User column.
- **`token`**: the browser's bearer credential (`TimeCreditByToken`, `vouchers.go:184-186`). Never shown.
- **Resume Code**: a separate 8-character code from the Voucher alphabet, in `resume_codes` (`controller/internal/ledger/rebind.go:25-31`). It proves ownership (`GLOSSARY.md` "Resume Code"), so it must stay out of the report and the CSV.
