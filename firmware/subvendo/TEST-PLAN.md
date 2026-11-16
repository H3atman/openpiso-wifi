# Subvendo test plan (lost-coin design)

Written before the code. Source design: `docs/research/esp8266-lan-juanfi-qr-parity-and-controller-design.md` Q5 (F1-F11), Q4 "Design", Q6 tickets 1-2.
Terms: Controller, Vendo (the board), Coin Window.

The simulation (`sim/`) is the test. It drives the real core (`lib/subvendo_core/`) through fakes for clock, flash, RTC, slot gate, transport, and HMAC, against a minimal Controller model. Each failure below is either `local sim` (a scenario in `sim/scenarios.cpp`) or `bench` (needs hardware; cannot be settled on a PC).

Units: a "pulse" is one counted edge. A coin of value v produces v pulses. Credits and audit are counted in pulses.

## 1. Design decisions proposed (the doc leaves these open)

| # | Decision | Proposal |
|---|---|---|
| D1 | `expires` (open question 10) | A duration in ms, `u32`, counted from the board's receipt with `millis()`. The Controller sends the remaining time on each resend. Board clamps to 10 min. Duplicate `open` for the same `window_id` never extends the deadline. |
| D2 | "Controller reachable" | An authenticated `ack` whose `rseq` equals the last report sent arrived within `REACH_MS` = 6000 ms. The board sends a report (heartbeat) every `HEARTBEAT_MS` = 2000 ms even when idle, so three missed heartbeats block the slot. No ack since boot means unreachable. |
| D3 | Canonical HMAC message | HMAC-SHA256 over big-endian fixed layouts, key = per-Vendo 32-byte secret, full 32-byte tag. `report` = `0x01 | vendo_id u32 | epoch u32 | total u32 | window_id u32 | boot_nonce u32 | rseq u32` (25 B). `open` = `0x02 | vendo_id u32 | window_id u32 | expires_ms u32 | boot_nonce u32 | rseq u32` (21 B). `ack` = `0x03 | vendo_id u32 | epoch u32 | total u32 | boot_nonce u32 | rseq u32` (21 B). The leading tag byte is domain separation so one message type cannot be replayed as another. A Go Controller must produce exactly these bytes (sample messages and MACs are printed at the end of `sim/out/report.md`). |
| D4 | `boot_nonce` and `rseq` (F-C) | At every boot the board draws `boot_nonce = random32()` (not persisted). Every `report` carries it and a per-boot counter `rseq` that restarts at 0. The Controller remembers `(boot_nonce, rseq)` of the newest report it received (a new nonce replaces it; within a nonce a higher `rseq` does) and echoes it in every `open`, and the processed report's pair in every `ack`. The board accepts an `ack` only if it echoes this boot's nonce; the Controller is reachable only if the `rseq` also equals the last report sent (D2). It accepts an `open` only if it echoes this boot's nonce and one of the last `openRecent` = 3 reports it sent (about 6 s at the 2 s heartbeat, the same scale as `REACH_MS`). So anything from an earlier boot fails on the nonce, and an `open` the board never received is dead after about 3 reports. `rseq` is not an idempotency key (the cumulative `total` is). A replayed old report from an earlier boot can make the Controller echo a stale nonce until the next real report (at most one heartbeat): `open`s are then rejected, which delays a window and is never unsafe. |
| D5 | Serial-number arithmetic | `total` and `epoch` are `u32`. The Controller computes `delta = (int32)(total - last_seen)`. `delta > 0` credits, `<= 0` credits 0. This handles overflow and reordered reports. `millis()` comparisons use the same `(int32)(a - b)` form. |
| D6 | Tag per burst (F-B) | A burst belongs to the window that was open at its FIRST edge. The ISR stores the first-edge time (an edge at least `quietMs` after the previous one), and the main loop tags the burst with `window_id` if that window had not ended by then, else `0` ("no window in force"). Report `window_id` = tag of the latest burst. Tag 0 goes to audit. The Controller credits any tag that is a window it opened for that Vendo, even if its own timer has expired (the board decided at pulse time). If the main loop was blocked for more than `quietMs` and saw two real bursts as one, the later first edge decides; that can only turn a credit into an audit, never move it to another window. |
| D7 | A new `open` is ignored while the board is busy (F-B) | The board ignores a valid `open` (the Controller resends it every second, so it is only delayed) until all of: (a) every counted pulse is saved (`total == durable`), (b) the latest total is acked, (c) the previous window ended at least `windowGapMs` = 300 ms ago by the board's own deadline (this also means no overlap with a still-open window). (a)+(b) stop a burst or an unacked report of window A being merged into a report of window B. (c) covers a coin accepted just before the gate closed whose first pulse comes 20-60 ms later: its first edge falls in the gap and is tagged 0 (audit). The 300 ms is a placeholder: the real value is the longest delay between the inhibit and the first pulse of an already accepted coin, from the CH-926 bench test (B8). A burst of window A that is unacked when a later burst has tag 0 is reported together with it and goes to audit (never to window B). |
| D8 | No persisted window high-water mark | The board keeps only a RAM `window_id` that increases within a boot: an `open` whose id is not above it is a duplicate and a no-op (the deadline is never extended). After a reboot the Controller's resend of a still-live window is accepted again (new nonce echoed after the first report), so a reset does not lose the window. Old `open`s fail on the nonce (D4), which makes the former "checkpoint on window open" redundant: one flash write less per window. |
| D9 | Checkpoint order | Flash record first, then RTC copy, then the report. A report is never sent for a `total` that is not already in flash. Record `seq` is a global counter. |
| D10 | Restore | Pick the valid (CRC and magic) record with the highest `seq` among RTC and the newest flash record. A tie goes to flash. Nothing valid (first boot, reflash, wipe, every record corrupt) = `epoch = random32()` (the ESP8266 hardware RNG, a HAL seam), `total = 0`, checkpointed at once. A random 32-bit epoch collides with one the Controller knows with probability 2^-32 (F-A). 0 is not reserved (the Controller's baseline for any new epoch is 0), and total wrap needs no new epoch because all comparisons are serial (D5). |
| D11 | Flash ring | 2 sectors by default (configurable), 32-byte slots (128 per sector), CRC32 per slot. The writer skips non-blank slots (torn writes). When the active sector is full it erases the other one, which never holds the newest record. |
| D12 | Reset policy | The core exposes `canReboot()`, false while a window is open or a burst is active or a checkpoint is pending. The board never calls restart on its own; this is the single gate the Arduino layer must use. |
| D13 | Defaults | debounce 3 ms, quiet time 130 ms, resend interval 1000 ms, window gap 300 ms, `openRecent` 3. All configurable in `Config`. |
| D14 | Checkpoint policy | `Config::persist`: `kBurst` (default) saves once when a burst ends; `kPulse` saves in the main loop after every counted pulse (never in the ISR). A slow erase only delays the next save; pulses keep counting in RAM and are saved on the next loop step. Reports are still sent per burst. Every scenario runs under both and `report.md` compares them. The user chooses after reading the numbers. |

Hardware assumption (bench, not simulated): the SET pin defaults to "blocked" while the board is in reset or unpowered.

## 2. Failure list

"Where" is `local sim` (scenario name in `sim/scenarios.cpp`) or `bench`. Every `local sim` row is checked by the invariants I1-I6 listed in section 3 on every run, plus the row-specific expectation.

### 2.1 Doc failure modes F1-F11

| ID | Scenario | Injected fault | Expected outcome / invariant | Where |
|---|---|---|---|---|
| F1 | Report lost, or Controller down | Network drops in both directions; Controller down for an interval | Slot blocked within `REACH_MS` of the last ack; no coin accepted while blocked (I3). Latest total resent until acked. Everything counted ends credited or audited (I2) | local sim: `lossy`, `ctl_down` |
| F2 | Ack lost | Drop only controller-to-board acks | Resend; Controller credits 0 for the repeat (I1) | local sim: `ack_loss` |
| F3 | Board reboots | Warm reset, RTC kept and RTC lost variants | Restore per D10. No total below one already reported in the epoch (I5). No reboot while a window is open is enforced by `canReboot()` | local sim: `warm_rtc_kept`, `warm_rtc_lost`; bench for the real reset path |
| F4 | Power loss mid-burst before the flash save | Power cut at a random instant, including inside a flash op | Counted-not-checkpointed pulses are lost. Reported separately as residual F4 loss with conditions; never hidden | local sim: `power_cut`; hold-up time is bench (Q6) |
| F5, F6 | Controller crash / Router write fails | Controller-side | Out of scope (Go Controller). The model only keeps `last_seen` | not in this sim |
| F7 | Late pulses after the window closes | Coin accepted just before expiry, pulses start after it | Burst tagged 0, goes to audit, never credited (I2) | local sim: `expiry_inflight` |
| F8 | Two users in one window | Controller opens a second window while one is open | Controller model refuses (one open window per Vendo) | local sim: `forged_replay` (checks the refusal) |
| F9 | Pulse miscount (EMI) | Short glitch edges | Edges within debounce of an accepted edge are dropped (zero extra counts). An isolated glitch on an idle line is counted: reported as spurious, accounted, not hidden | local sim: `glitch`; real EMI levels are bench |
| F10 | Clock jump / no RTC clock | `millis()` wraps | Expiry and reachability use `(int32)(a-b)` compare; no failure across the wrap | local sim: `wrap` |
| F11 | Flash wear | Many bursts | I6: erases per burst and years at a stated coins/day | local sim (computed); 100K cycles figure assumes the W25Q32JV (bench Q7) |

### 2.2 Additional failures found

| ID | Scenario | Injected fault | Expected outcome / invariant | Where |
|---|---|---|---|---|
| T12 | Torn flash page program | Power cut during a record write leaves a partial slot | CRC rejects it. Restore uses the previous record. The writer skips the non-blank slot | local sim: `power_cut` |
| T13 | Torn sector erase | Power cut during the ring's sector erase (partial erase, old tail remains) | The newest record is in the other sector, so restore is unaffected. The sector is re-erased before reuse | local sim: `power_cut` with a small ring so erases are frequent |
| T14 | Torn RTC write | Cut between RTC blocks | CRC rejects it. Flash holds the newest (D9) | local sim: `warm_rtc_kept` with cuts inside the RTC write |
| T15 | RTC valid (CRC passes) but stale | RTC write dropped after the flash write, then reset with RTC kept | Higher `seq` wins (D10); total never goes backwards (I5) | local sim: `warm_rtc_kept` |
| T16 | RTC says newer than flash | Flash newest record destroyed | RTC used, ring continues from its `seq` | local sim: `restore_rtc_only` |
| T17 | Reordered reports | Network reorders and delays board to Controller | An older `total` after a newer one: `delta <= 0`, credits 0, never negative (I1, D5) | local sim: `reorder` |
| T18 | Window expires with a burst in flight | See F7 | Whole burst tagged by its first edge (D6) | local sim: `expiry_inflight` |
| T19 | Duplicate `open` | Network duplicates `open`; Controller resend | Deadline not extended (D1); second copy is a no-op | local sim: `dup` |
| T20 | Forged `open`/report/ack | Bad HMAC; wrong vendo_id; type confusion (open bytes as ack) | Dropped, nothing credited, no window opened (I4) | local sim: `forged_replay` |
| T21 | Replayed `open`/report/ack | Replay old captured messages, also after reboot | Old `open`: rejected by the nonce (earlier boot), by `window_id` not above the current one, or by the recency check (D4, D8). Old report: `delta <= 0`, or it delivers a delta the board really counted, exactly like a delayed original. Old ack: no effect (other nonce, stale `rseq` or total) | local sim: `forged_replay`, `chaos`, `stale_open` |
| T22 | Epoch bump while the Controller holds a newer total | Every record gets a bad CRC and RTC is wiped (`restore_fail`); flash and RTC fully wiped (`restore_fail_wipe`) | Board restarts at a random epoch, `total = 0`. Controller starts a new baseline, no double credit and no negative delta, also when nothing at all survives. Coins counted but not yet reported are lost, listed as "restore-failed loss" (F-A resolved) | local sim: `restore_fail`, `restore_fail_wipe` |
| T23 | Counter overflow | `total` starts at 0xFFFFFFF0 | Credits stay correct across the wrap (D5) | local sim: `wrap` |
| T24 | Coin inserted while the slot is blocked | Coins offered when gate is closed | The acceptor returns them: no pulses, not counted, not credited (I3) | local sim: every scenario (`blocked_coins` counter) |
| T25 | Report spanning two windows | Unacked window-A coins, then a burst of the next window (or tag 0) before the ack | Cannot happen: the new `open` waits until everything is saved and acked, so a report never spans two windows (D7; F-B resolved) | local sim: `expiry_inflight`, `handover_inflight`, `reorder` |
| T26 | New `open` while previous coins are unacked | `open` arrives with `total != acked_total` | Ignored until acked and saved (D7) | local sim: `ack_loss` |
| T27 | Reset while a window is open | Policy | `canReboot()` is false; no self-reboot. External power/WDT resets are the F3 case | local sim: asserted every scenario; WDT reset is bench |
| T28 | Pulse train during board downtime | Coin accepted just before a cut, remaining pulses arrive while the board is off | Lost, counted as F4 loss (pulses emitted while down) | local sim: `power_cut` |
| T29 | Network down for longer than the window | Controller down across expiry | Slot blocked; no coin accepted | local sim: `ctl_down` |
| T30 | Window handover with a coin in flight | Next `open` arrives while the old window's coin is still being inserted or its burst is unacked | A coin must be credited to the window it was inserted in. Held by the busy check and the 300 ms gap (D7) | local sim: `handover_inflight` (F-B resolved) |
| T31 | Stale or delayed `open` | `open`s the board never received (80% loss, short windows) replayed later | Not adopted once older than the last `openRecent` reports: a window opens at most `openRecent * heartbeat` + slack after the Controller closed it (I4) | local sim: `stale_open`, `chaos` (F-C resolved) |
| T32 | Replayed `ack` across a reboot | `rseq` restarts at 0 after a reset, so an old ack with the same `rseq` looks fresh | Fails on `boot_nonce`: the slot cannot stay open while the Controller is down (I3) | local sim: `chaos` (F-C resolved) |
| T33 | Checkpoint policy | Same faults under `kBurst` and `kPulse` | All invariants hold under both; residual F4, flash records and wear compared in `report.md` | local sim: every scenario, both policies |

### 2.3 Flash-time provisioning (#43)

Agreed seams: the core sim exercises the public config reader through `ConfigStorage` and the pure MAC-to-hostname function; the Python no-flash E2E exercises the CLI against a fresh real Controller, then inspects the built image in memory. Neither proves Ethernet or physical flash behavior.

| Case | Expected outcome | Where |
|---|---|---|
| Valid config | ID, raw 32-byte secret, Controller URL and hostname restored | local sim: `config_valid` |
| Config survives update regions | Sector `0x3FB000..0x3FBFFF` is above OTA end `0x300000` and FS end `0x3FA000`, below RF calibration `0x3FC000`; RTC bench journals separately at `0x310000` | local sim: `config_update_safe`; target builds |
| Erased config | Missing; output cleared, caller must send no reports | local sim: `config_missing` |
| Damaged key / CRC mismatch | Corrupt; output cleared, caller must send no reports | local sim: `config_corrupt` |
| Flash read fails | Read error; output cleared | local sim: `config_read_error` |
| MAC `A0:20:A6:11:0C:01` | Exactly `opnw-0C01`, including uppercase and leading zero | local sim: `hostname` |
| No-flash provisioning | Form+Basic endpoint, CRC-valid in-memory image, configured board URL (not management origin), no file writes, device access or leaked secret, duplicate 409 | `bench/provisioning_e2e.py` |

### 2.4 Bench items (cannot be settled on a PC)

| ID | Question | What to measure |
|---|---|---|
| B4 | RTC user memory across a software/WDT reset (open question 4) | Write a pattern, `ESP.restart()` and WDT-trigger, read back. Decides if the "RTC kept" or "RTC lost" sim variant is real |
| B5 | ISR latency under WiFi load (open question 5) | GPIO toggle to ISR timestamp under load; confirms the debounce and quiet values |
| B6 | Hold-up time (open question 6) | Supply decay vs flash page program (up to 3 ms) and sector erase (up to 400 ms); sizes the F4 window and capacitor |
| B7 | Flash part (open question 7) | JEDEC ID of the real module; the I6 projection assumes 100K cycles/sector |
| B8 | Quiet time and acceptor idle level | CH-926 inter-pulse gap (ticket 3). Default 130 ms is a placeholder |
| B9 | SET pin default level in reset | Confirm "blocked" while unpowered or held in reset |
| B10 | Flash config, DHCP option 12 and first-contact pairing (#43) | Rafael runs `bench/README.md` section 5: real read-mac/flash, matching Ethernet MAC, Router lease host-name, signed Report adoption and static lease/bypass; record in `bench/out/results.md`. Not run: no board attached |

## 3. Invariants asserted on every run

- I1: per window, credited <= accepted (plus spurious edges that passed debounce and were counted in that window).
- I2: accepted + spurious = credited + audited + residual loss. Residual loss is split into F4 (RAM-only pulses at a cut), pulses emitted while down, and restore-failed loss. Any other difference fails the run.
- I3: coins offered while the slot is blocked are never counted or credited. The acceptor fake returns them, so no pulses exist; in addition the slot must never be open longer than `reachMs` + one heartbeat + 600 ms (flash op slack) after the last genuine ack (a replayed ack for the current `rseq` can extend reachability by one heartbeat).
- I4: forged messages (bad MAC, wrong tag, type confusion) never credit and never open a window. An unmodified replayed report is a network duplicate (the sim injects it as one); it can only deliver a delta the board really counted, which I1 bounds. A window that opens must be one the Controller issued, and not via an attacker copy of an `open` more than `openRecent * heartbeat` + 1 s after the Controller closed that window (the freshness bound of D4).
- I5: a restore never yields a `total` below one already reported in the same epoch, unless the epoch was bumped.
- I6: flash sector erases per burst, and years of life at a stated coins/day (reported, not pass/fail).

Residual F4 conditions: a cut (or a reset with RAM loss) between the first edge of a burst and the completion of that burst's flash record. Pulses in that interval are in RAM only. Longer quiet time and slower flash both widen it.

## 4. Flaws found by the simulation: all resolved

The first version of the simulation failed three invariants (`restore_fail_wipe`, `handover_inflight`/`reorder`/`expiry_inflight`/`forged_replay`, `chaos`). Each is fixed in the core with the simplest mechanism that makes the failing scenarios pass; every invariant now passes in every scenario under both checkpoint policies. Each fix was also checked by removing it in a scratch copy: the matching scenarios fail again (no 300 ms gap: `reorder`, `forged_replay` I1; no busy check: `lossy`, `handover_inflight`, `chaos` I1; no nonce: `forged_replay`, `stale_open`, `chaos` I3/I4; no recency check: `stale_open` I4; constant epoch: `restore_fail*` I2/I5).

**F-A. A new epoch could collide when every record is lost (T22). Resolved.** The epoch is now `random32()` from a new HAL seam (the ESP8266 hardware RNG on the board, the seeded RNG in the sim) whenever restore finds no valid record. Removed: `Ring::epochHint`, `kMaxHintEpoch` and the CRC-bad-slot scan, which were a heuristic that failed when the flash was wiped. Counter wrap needs no epoch change (serial arithmetic, D5), so there is no other place an epoch changes.

**F-B. A delta spanning two windows was credited to the later one (T25, T30). Resolved.** (1) A burst is tagged by its first edge (the ISR keeps `firstEdgeMs_`). (2) A new `open` is ignored while any pulse is unsaved, the latest total is unacked, or the previous window ended less than `windowGapMs` = 300 ms ago; the Controller's one-second resend is the retry, so the board has no queue and no new state besides the gap. Removed: the window start time (`winStart_`, no longer reachable) and `haveWin_`. Placeholder: 300 ms comes from the CH-926 bench test (B8, the longest delay from inhibit to the first pulse of an accepted coin).

**F-C. `open` and `ack` freshness (T31, T32). Resolved.** Every boot draws a `boot_nonce` (not persisted). Reports, acks and opens carry it; the board drops acks and opens that do not echo this boot's nonce, and drops opens that do not echo one of its last 3 reports. Removed: the persisted `max_window` high-water mark (`Rec.maxWindow` is now the unused `spare`), the checkpoint written at every window open (one flash record less per window). `rseq` restarts at 0 each boot.

**Limitations kept (not flaws of the doc design):**
- Reachability: a replayed ack for the current `rseq` can extend "reachable" by up to one heartbeat (2 s). Accepted; the I3 bound includes it.
- A burst of window A that is still unacked when a late first pulse arrives with tag 0 is reported with it and goes to audit. It is never credited to another window (I1).
- A replayed report of an earlier boot can make the Controller echo a stale nonce for up to one heartbeat; `open`s are rejected meanwhile (delay, not an unsafe state).
- A replayed `open` can open a window up to about `openRecent * heartbeat` (6 s) after the Controller closed it; the freshness bound is the design value, not zero.
- Glitches: an isolated spike on an idle line is counted (F9). the `glitch` scenario counts them (see its "spurious" column); the Controller cannot tell them from a 1-pulse coin. Bounce within the debounce time is dropped (0 leaks). Detection by coin type table is Controller work.
- Residual F4 loss is bounded by the burst time plus checkpoint time, and was non-zero in every cut scenario (numbers in the report).
- Not simulated: flash bit rot, byte-level interleaving of flash ops with the ISR on real silicon, WiFi stack time, supply decay (all bench items B4-B9).
