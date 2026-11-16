# HotSpot identity lab and MVP grill prep

Read this before running the identity experiments, or before starting a `/grilling` session on the MVP. The architecture decisions already made live in [`mikrotik-controller.md` §8](mikrotik-controller.md#8-follow-up-decisions-2026-10-16). This doc covers only what is still **open** and how to close it with evidence.

## The core question

**Does paid time follow the customer when their MAC changes?** Three separate systems answer it, and each needs its own evidence:

| ID | Question | Evidence source | Status |
|---|---|---|---|
| Q1 | In WiFi5-Soft, does remaining paid time follow the cookie-derived `.id` after a MAC change? | Spec case **T07** ([`wifi5soft-client-identity-spec.md` L214-224](wifi5soft-client-identity-spec.md)) run against the `piso-server` VM | **yes, static only**: see [Q1 findings](#q1-findings-wifi5-soft-24104-static) |
| Q2 | Does RouterOS HotSpot `login-by=cookie` log the same browser back in after a MAC change? | The CHR lab below | **no**: run 2026-10-16, see [Q2 results](#q2-results-2026-10-16-routeros-7238) |
| Q3 | Do phone captive-portal assistants keep the HotSpot cookie, and when do phones rotate their MAC? | Real iOS and Android devices on the CHR or a physical MikroTik | **Android yes, iOS no**: plain, recheck and adb runs 2026-10-18, see [Q3 results](#q3-results-2026-10-18-plain-run) |

A matching ID is **association** evidence, not **continuity** evidence. A case passes only when the remaining balance equals baseline minus measured consumption, with no reset, no free grant and no double charge. The spec's T02 and T07 split states this rule; apply it to Q2 as well.

## Q1 findings: WiFi5-Soft 24.10.4 (static)

Source: full V8 decompilation of the `firewall` and `httpserver` modules, firmware 24.10.4. The working files live outside this repo under `<local-workdir>/w5s/scratch/` (`native-full/firewall.full.dec.txt`, `httpserver.full.dec.txt`). This evidence is labelled **observed static / derived**. The T07 runtime oracles remain **not-run**.

- **Paid time lives in a session record keyed by `code`.** `code = "UNQCODE" + <cookie-derived .id>`. Without a decodable cookie, the fallback is `code = "IX" + <uppercased MAC>`. Paid time is the record's `duration` (pausable) or `expiration` (consumable). The record is persisted in `/mnt/wifi5/sessionCookie/<code>`, and `ExtendTime` rewrites it on every top-up, extension and expiry check.
- **A MAC change keeps the balance.** `sessionCheck` finds no data under the new MAC, decodes the cookie, rebuilds `code`, and loads `sessionList[code]`. `addDataList` then overwrites `record.mac` and `record.address`, and `addClientFirewall` allows the new MAC. The old MAC loses the session.
- **Two devices with one cookie means takeover, not double spending.** `allowClient` removes the other client's firewall rule when `code` is already active, so only one session per code is live. Separately, two or more MACs on one ID make the loader issue a fresh `random2(8)` ID and split the identity.
- **`idbase` does not change the session store**: sessions are keyed by `code` in both modes. It switches log labels between `.id` and MAC, and it picks the client-data (credit) directory and key (`/mnt/wifi5/base-id/<id>` vs `/mnt/wifi5/data/<mac>`; admin `set-credit`/`setid`).
- How the session then burns, pauses, idles out, resumes and extends: [`wifi5soft-architecture-client-time.md`](wifi5soft-architecture-client-time.md).
- **Weakness:** the cookie is AES with a passphrase fixed in the firmware (spec L42-48, L70-82). Anyone who can mint a cookie for a known `.id` takes over that customer's time.

**What this means for our design:** WiFi5-Soft confirms the shape we planned, so copy it:

- The **entitlement** is keyed by an opaque token, not by the MAC.
- A new MAC with the same token **re-binds** the session.
- Only one live session exists per entitlement. A second device **takes over** the session; it never adds time.

Make one change: the token must be random and signed with a per-install key, so it cannot be minted. A copied token still takes the session over (it is a bearer token), so log every re-bind.

## CHR lab (answers Q2)

The host is CachyOS with libvirt. `virt-manager`, `virsh`, `virt-install` and `qemu-img` are installed. The NAT network `pisonet` (virbr50, 192.168.77.0/24) already exists and holds the `piso-server` and `piso-client` VMs. Leave those VMs as they are.

Use the official **x86-64 CHR** image with KVM. The ARM64 `ayufan/mikrotik-qemu-arm64` scripts are for ARM hosts and would run under slow emulation here. The free CHR licence caps upload at 1 Mbps per interface, which is irrelevant for this test. ([CHR docs](https://help.mikrotik.com/docs/spaces/ROS/pages/18350234/Cloud+Hosted+Router+CHR))

### Steps

1. **Image.** Download the current stable CHR raw image from mikrotik.com/download, and record the exact version. Convert it with `qemu-img convert -O qcow2 chr-<ver>.img chr-<ver>.qcow2`. *Done when* the qcow2 exists and the version is written in the results file.
2. **Networks.** Create an isolated libvirt network `hotspot-lan` with no IP and no DHCP, so RouterOS is the only DHCP server on it. *Done when* `virsh net-dumpxml hotspot-lan` shows a bridge and no `<ip>` element.
3. **VM.** Import the qcow2 in virt-manager or with `virt-install --import`. Use 256 MB RAM and two virtio NICs: `ether1` on `default` NAT for WAN, `ether2` on `hotspot-lan`. *Done when* you can log in on the serial or graphical console.
4. **HotSpot config.** Apply this config on `ether2`. Check each command against the [HotSpot docs](https://help.mikrotik.com/docs/spaces/ROS/pages/56459266/HotSpot+-+Captive+portal) for the installed version.
   ```
   /ip address add address=10.5.50.1/24 interface=ether2
   /ip pool add name=hs-pool ranges=10.5.50.10-10.5.50.200
   /ip dhcp-server add name=hs-dhcp interface=ether2 address-pool=hs-pool lease-time=10m
   /ip dhcp-server network add address=10.5.50.0/24 gateway=10.5.50.1 dns-server=10.5.50.1
   /ip hotspot profile add name=hs-prof hotspot-address=10.5.50.1 login-by=cookie,http-pap http-cookie-lifetime=1d
   /ip hotspot add name=hs1 interface=ether2 address-pool=hs-pool profile=hs-prof disabled=no
   /ip hotspot user add name=t-cookie password=t-cookie limit-uptime=1h
   ```
   `http-pap` lets `curl` log in with a plain POST. *Done when* `/ip hotspot print` shows `hs1` enabled.
5. **Client.** Put a network namespace on the `hotspot-lan` bridge: a veth pair with one end in the bridge and one end in the namespace. Run `dhclient` (or `dhcpcd`) inside the namespace. *Done when* the namespace has a 10.5.50.x lease and `curl http://10.5.50.1/login` returns the login page.
6. **Run the cases.** Use one persistent cookie jar (`curl -c jar -b jar`) for every request in a case.
   - **L1 (MAC change after logout):** log in with a POST to `/login`, consume 2 minutes, then log out via `/logout`. Change the MAC with `ip link set dev <if> address <new>`, renew DHCP, and request `/login` again.
   - **L2 (MAC change while active):** log in, consume 2 minutes, change the MAC without logging out, renew DHCP, and request any HTTP URL.
   - **L3 (control):** same as L1, but keep the MAC unchanged.
   - **L4 (takeover):** set `shared-users=1` on the user profile. Log in from MAC A, then log in with the same credentials from a second namespace with MAC B. Record whether RouterOS rejects B or replaces A. WiFi5-Soft replaces A. If RouterOS rejects B, the controller must remove A's `active` entry before it logs B in.
7. **Record.** Before and after every MAC change, capture `/ip hotspot active print detail`, `/ip hotspot host print detail`, `/ip hotspot cookie print detail` and `/ip hotspot user print stats`. Also save the full curl transcript. *Done when* every case row in the results file has all four fields below filled.

### Results file

Write to `docs/research/results/<YYYY-MM-DD>-chr-cookie-mac/results.md`, a dated directory that also holds the per-case raw snapshots (`L1/` to `L4/`: `<n>-<label>-{active,host,cookie,user}.json` and `transcript.log`). Use one row per case, and label each finding **E** (observed), **C** (characterization) or **D** (desired property), as in the WiFi5-Soft spec.

The lab is **complete** when L1 to L4 each have a row, the RouterOS version is recorded, and the raw transcripts sit next to the results file.

### Q2 results (2026-10-16, RouterOS 7.23.8)

Run on 2026-10-16 with RouterOS 7.23.8. Full table, setup and evidence: [`results/2026-10-16-chr-cookie-mac/results.md`](results/2026-10-16-chr-cookie-mac/results.md).

- **E1:** a valid cookie does not log in a client after a MAC change; the router-side cookie entry stays bound to the old MAC.
- **E2:** with the MAC unchanged, the same cookie auto-logs in (`login-by=cookie`).
- **E3:** an active session does not follow a MAC change; the old session keeps charging uptime until it is removed (between +2s and +62s here).
- **E6:** with `shared-users=1`, a second login for an active user is rejected, not a takeover.
- **C1:** RouterOS cookie login is keyed on MAC + browser cookie, so it cannot carry identity across MAC randomization.

Cookie-strategy decision (grill topic 2) is now informed: do not rely on RouterOS cookie login for MAC changes. The controller owns identity and must clear the old active entry before logging in the new MAC (D1).

L5–L8 session-control results (rebind, idle-timeout, live limit change, keepalive): [`results/2026-10-16-chr-session-control/results.md`](results/2026-10-16-chr-session-control/results.md).

Session-control decision (pause, idle, resume, rebind, top-up): [`mikrotik-controller.md` §8](mikrotik-controller.md#session-control-pause-idle-resume-mac-rebind-top-up).

### Q3 results (2026-10-18, plain run)

Real phones (iPhone on iOS 18.7, <phone-model> on Android 12) on a TP-Link EAP220 bridged into the CHR hotspot LAN, using the `lab/phones/` kit. Full table and evidence: [`results/2026-10-18-phones-plain/results.md`](results/2026-10-18-phones-plain/results.md).

- **E1:** a plain HotSpot redirect opens the captive sheet on both phones in about 2s, and the sheet closes by itself after the controller logs the client in.
- **E2/E3:** the iOS sheet drops cookies between sheets; the iPhone's private MAC stayed fixed through idle-out, forget and reboot.
- **E4/E5:** the Android sheet keeps cookies, so the portal auto-logged it in after idle-out and re-bound it after a MAC change, with zero taps.
- **E6:** neither sheet shares cookies with Safari or Chrome.
- **C1:** identity is cookie first, then MAC. An iPhone that changes its MAC needs a resume code to recover its balance.

Recheck run the same day ([`results/2026-10-18-phones-recheck/results.md`](results/2026-10-18-phones-recheck/results.md)) closed the gaps:

- **iPhone, logged in:** forget and reboot kept the same private MAC and the live session; no popup, internet kept working.
- **iPhone, Private Wi-Fi Address Off:** the hardware MAC appeared with **no popup** and no internet, while the old session kept charging. Switching back to Fixed restored the same private MAC.
- **Android, session ended:** the sheet sent the cookie after a forget and after a reboot, and the portal auto-logged it in with zero taps. The randomized MAC stayed the same across both runs.

adb run the same day on a second Android phone (Xiaomi, Android 14): [`results/2026-10-18-phones-adb/results.md`](results/2026-10-18-phones-adb/results.md). The sheet kept the cookie across idle-out, MAC change and reboot, as on Android 12. Logcat shows the portal detected from the HotSpot 302 on `generate_204`, and the sheet closing itself under 1s after login.

## Grill session (once Q2 has data)

Run `/grilling` with this doc and the Q2 results file as inputs. Q1 is answered above. Answer each topic with a decision recorded in `mikrotik-controller.md` §8, or name the experiment that blocks the decision.

1. **Identity model.** Start from the Q1 shape: an entitlement keyed by a token, re-bind on a MAC change, takeover on a second device. Decide how that maps to RouterOS: one HotSpot user per entitlement, with the controller re-binding it. Decide what the no-cookie fallback is; WiFi5-Soft falls back to the MAC.
2. **Cookie strategy.** If Q2 is positive, RouterOS's built-in cookie may be enough for the MVP. If it is negative, the controller re-binds the login itself. Pick one.
3. **Resume code.** Decide its format, its lifetime, where it is shown or printed, and what stops guessing (length, rate limit).
4. **Shared balances.** Choose home-site ownership, a per-site offline allowance, or cross-site spending only while online.
5. **Language.** Choose Go or Rust for the controller. Base the choice on the library table in §8, not on speed.
6. **MVP cut.** Confirm what phase 2 (local vouchers) ships with, and what waits for phase 3 and later.

The session is **done** when every topic has a recorded decision or a named blocking experiment, and MVP phase 2 has a written scope.
