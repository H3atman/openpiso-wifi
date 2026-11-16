# CHR session-control lab results (2026-10-16)

- RouterOS version: 7.23.8 (long-term)
- Date: 2026-10-16
- Cases: L5 rebind, L6 idle-timeout, L7 live limit change, L8 keepalive-timeout

Setup: RouterOS 7.23.8 CHR (`chr-lab`) running HotSpot `hs1`. The client is a second CHR VM (`chr-client`) on `hotspot-lan` with two NICs (MACs 02:00:00:00:0C:01 and 02:00:00:00:0C:02). The controller is simulated by `lab/chr/session_lab.py`, which does REST `/ip/hotspot/active/login` and `active remove`. User profile `sess`: `shared-users=1`, with an `on-logout` script that logs `$user`, `$"mac-address"` and `$cause`. L5 and L6 used separate fresh users.

Note: an earlier attempt the same day was invalid because the virtio MAC change silently did not apply. This run changes MAC by switching which NIC is enabled (see `lab/chr/README.md`).

## Results

### L5 rebind (user `s-rebind`, limit 1h)

- **E1:** REST `active login` works for a host present in the host table. With no host entry it fails with `failure: unknown host IP` (observed while setting up the lab).
- **E2:** After switching to MAC 0C:02 (new IP .199), the old 0C:01 session stays active and keeps charging (uptime 20s, 27s, 29s: `L5/1`, `L5/2`, `L5/3`).
- **E3 (L5a):** `active login` for the new MAC without removing the old session fails with `failure: no more sessions are allowed for user` [33.1s].
- **E4 (L5b):** Removing the old session commits the user's uptime as 29s. `active login` for 0C:02 succeeds 1s later. The new session shows `session-time-left=59m11s` at uptime 20s, which is 1h - 29s - 20s, so the balance carried over exactly (`L5/5-rebound-mac2-active.json`). After the pause (remove), user uptime is 49s = 29 + 20 and stays frozen.
- **E5:** `on-logout` gets `cause=admin reset` for both removes, with the right user and MAC.

### L6 idle-timeout (profile `idle-timeout=40s`, user `s-idle`)

- **E6:** Control: with traffic every 2s, the session stays active past 70s (uptime 1m10s).
- **E7:** After traffic stops, the session logs out 42s later, `cause=idle timeout`.
- **E8:** The idle tail is not charged. User uptime after logout is 1m10s, equal to the uptime when traffic stopped, not 1m52s (`L6/2-traffic-off-active.json`, `L6/3-after-logout-user.json`).

### L7 live limit change (user `s-limit`, limit 1m)

- **E9:** At uptime 30s (`session-time-left=30s`), `limit-uptime` was raised to 3m. The active session kept its old countdown (`session-time-left=27s` at uptime 33s; `L7/2-after-raise-active.json`) and logged out at 1m with `cause=session timeout`. User uptime is now 1m with limit 3m, so 2m remain usable on the next login.
- **C1:** The HotSpot fixes `session-time-left` at login. A top-up needs remove plus `active login` to take effect, as KiTifi's `mtik_add_time` does.
- **Untested:** L7b (lowering the limit mid-session), because the session had already ended.

### L8 keepalive-timeout (profile `keepalive-timeout=30s`, `idle-timeout=none`, user `s-keep`)

- **E10:** Control: a client that is present but sends no traffic stays active for 70s. Keepalive checks reachability, not traffic. That idle time was charged (uptime 1m10s with idle-time 1m10s).
- **E11:** After the client departed (NIC disabled), it logged out 22s later, `cause=keepalive timeout`. The departure tail was charged: user uptime 1m32s = 70s + 22s.

### Observed `$cause` values in on-logout

`admin reset`, `idle timeout`, `session timeout`, `keepalive timeout`.

## Findings

- **C2:** Controller-driven pause, resume and rebind all work with only `active remove` + `active login`. The order must be remove first, then login (E3, E4).
- **C3:** `idle-timeout` is the right auto-pause: the tail is uncharged (E8) and the cause is distinct (E7). A departed client also sends no traffic, so idle-timeout covers departure too, uncharged. `keepalive-timeout` detects departure but charges up to the timeout (E10, E11). Recommend idle-timeout as the primary mechanism and keepalive as optional/longer, or none. Inference: the combination of both was not tested.
- **C4:** The controller can count manual pauses (`admin reset`, triggered by itself) and idle pauses (`idle timeout`) separately via the on-logout `$cause`, which supports WiFi5-Soft-style separate pause limits.
- **D1:** A top-up = update the ledger, set `limit-uptime` absolute, then remove + `active login` if the user is active (C1).
- **D2:** On a MAC change the old session charges until removed (or until it idles out, uncharged, under idle-timeout; that combination is inference, not tested), so rebind promptly.
- **Open:** L7b; idle-timeout and keepalive together; on-logout script reliability under load; whether the controller should get logout events by script -> HTTP callback or by polling.

Raw evidence: `transcript.log` and `L5/` to `L8/` next to this file (`<label>-{active,host,user}.json`, `log.json`).
