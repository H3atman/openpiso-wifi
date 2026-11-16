# CHR cookie / MAC lab results (2026-10-16)

- RouterOS version: 7.23.8 (long-term)
- CHR_VERSION (image): 7.23.8
- Date: 2026-10-16
- WAIT_SECS: 60

Setup: RouterOS 7.23.8 CHR on KVM, HotSpot `hs1` on `ether2`, profile `login-by=cookie,http-pap`, `http-cookie-lifetime=1d`. Clients are Linux netns with curl and a persistent cookie jar. Static IPs .201/.202 are outside the pool, so HotSpot one-to-one NAT mapped them to .200/.198/.199, visible as `to-address`.

Note: a first run the same day was invalid because `/ip hotspot add` created `hs1` with `disabled=true`. The wizard now guards against that.

MACs: hsA = 02:00:00:00:0A:01 (changed to 0A:02 in L1/L2), hsB = 02:00:00:00:0B:01.

| Case | Auto-login after change? | Cookie entry MAC (before → after) | `t-*` user uptime / limit (before → after) | Old active/host entry fate |
|---|---|---|---|---|
| L1 MAC change after logout | No: login page | 0A:01 → 0A:01 (unchanged) | 0s → 1m / 1h | Old host 0A:01 stays unauthorized; new host 0A:02 appears unauthorized |
| L2 MAC change while active | No: login page | 0A:01 → 0A:01 | 1m → 3m / 1h | Old active session (0A:01) stays active after the change; it and its host entry are gone by the +60s snapshot (`L2/4-later-active.json`); ~1m extra uptime charged |
| L3 control (same MAC) | Yes: `login-by=cookie` | 0A:01 → 0A:01 (expiry refreshed to ~24h) | 3m → 4m1s / 1h; new session `session-time-left=55m57s` (`L3/3-after-change-active.json`) | Same host re-authorized |
| L4 takeover (`shared-users=1`) | B rejected; A stays active | t-takeover@0A:01 only; B gets no cookie | 0s / 1h | B's host 0B:01 unauthorized; login page says "no more sessions are allowed for user t-takeover" (HTTP 200, `L4/B-login-response.html`) |

## Findings

- **E1 (L1):** A valid browser cookie (curl jar kept `loginID`) does not log in a client whose MAC changed. The router-side cookie entry stays bound to the old MAC (02:00:00:00:0A:01).
- **E2 (L3):** With the MAC unchanged, the same cookie auto-logs in after logout (`login-by=cookie`), and the cookie expiry refreshes.
- **C1:** RouterOS's built-in cookie login is keyed on MAC + browser cookie, so it cannot carry identity across MAC randomization. openpiso needs its own identity cookie/portal logic to re-authorize a new MAC (e.g. via API/RADIUS login of the new MAC).
- **E3 (L2):** Changing the MAC during an active session does not move the session. The old 0A:01 session kept running and charging uptime: user uptime went 1m → 3m, so the ~2m session included roughly 1m after the device left. It was removed somewhere between +2s and +62s after the change. Trigger (host timeout vs ARP loss) not isolated; `idle-timeout=5m` on the server and `keepalive-timeout=none`.
- **E4:** `/ip hotspot user` uptime only updates when a session ends. During a session the live counter is in `/ip hotspot active` (uptime, session-time-left).
- **E5 (L3/L2):** `limit-uptime` is cumulative per user across sessions and MACs. The new L3 session started with session-time-left 55m57s = 1h − 4m1s already consumed. Per-user balance therefore survives re-login.
- **E6 (L4):** With `shared-users=1`, a second login for an active user is rejected, not a takeover; the first session keeps running.
- **D1:** When openpiso re-identifies a device under a new MAC, it must remove the old active entry first. Otherwise (per E3 + E6) the old session keeps charging time and a `shared-users=1` re-login of the same user is rejected.
- **D2:** Set a short `keepalive-timeout` (or have the controller kick sessions explicitly) so a departed MAC stops charging quickly.
- **Open (not tested):** whether a jar-less request from the same MAC auto-logs in (cookie entry alone vs MAC + browser cookie); and real-phone behavior (Q3).

Raw evidence: per-case folders next to this file (`<n>-<label>-{active,host,cookie,user}.json`, `transcript.log`, `L4/B-login-response.html`).
