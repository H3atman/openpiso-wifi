# Phone captive-portal lab results

- Date: 2026-10-18
- Mode: plain
- RouterOS: 7.23.8 (long-term)
- AP: TP-Link EAP220
- iphone (a-info): Model and OS version? Iphone
- iphone (a-info): Current private-address setting for openpiso-lab? 10.5.50.199
- android (a-info): Model and OS version? Android
- android (a-info): Current private-address setting for openpiso-lab? 10.5.50.198

## iphone

| step | popup? | MAC seen | opid cookie returned? | portal action | internet w/o tap? | notes |
|---|---|---|---|---|---|---|
| a-info | - | 02:00:00:E2:00:01 | no | new | - | Model and OS version? Iphone; Current private-address setting for openpiso-lab? 10.5.50.199 |
| b-join | y | 02:00:00:E2:00:01 | no | new | - | Roughly how many seconds until it appeared? 2 seconds; What did it show (New device page / blank / error)? New device page |
| c-connect | - | 02:00:00:E2:00:01 | yes | login | y | Page said Connected? (y/n) y; Popup turned into Done / closed by itself? (y/n) y |
| d-idle | y | 02:00:00:E2:00:01 | no | new | n | Popup said Reconnected automatically? (y/n) n |
| e-forget | y | 02:00:00:E2:00:01 | no | new | n | Popup said Reconnected automatically? (y/n) n |
| f-mac | y | 02:00:00:E2:00:01 | no | new | n | Popup said Reconnected automatically? (y/n) n; Which setting is now active? Fixed |
| g-reboot | y | 02:00:00:E2:00:01 | no | new | n | Popup said Reconnected automatically? (y/n) n |
| h-browser | - | 02:00:00:E2:00:01 | yes | new, login, browser-check, other | - | What does the page say about the cookie? No opid cookie was sent to this browser |

## android

| step | popup? | MAC seen | opid cookie returned? | portal action | internet w/o tap? | notes |
|---|---|---|---|---|---|---|
| a-info | - | 02:00:00:E3:00:01 | yes | new, other | - | Model and OS version? Android; Current private-address setting for openpiso-lab? 10.5.50.198 |
| b-join | y | 02:00:00:E3:00:01 | yes | auto-login, other | - | Roughly how many seconds until it appeared? 2; What did it show (New device page / blank / error)? New device page |
| c-connect | - | - | - | - | y | Page said Connected? (y/n) y; Popup turned into Done / closed by itself? (y/n) closed by itself |
| d-idle | y | 02:00:00:E3:00:01 | yes | auto-login, other | y | Popup said Reconnected automatically? (y/n) y |
| e-forget | n | - | - | - | y | Popup said Reconnected automatically? (y/n) n |
| f-mac | y | 02:00:00:E7:00:01 | yes | rebind, other | y | Popup said Reconnected automatically? (y/n) y; Which setting is now active? device MAC |
| g-reboot | n | - | - | - | y | Popup said Reconnected automatically? (y/n) y |
| h-browser | - | - | no | browser-check, other | - | What does the page say about the cookie? No opid cookie was sent to this browser |

Phones: iPhone on iOS 18.7 (UA), Android = <phone-model> on Android 12 (UA). Profile `phone`: `shared-users=1`, `idle-timeout=2m`; `hs-prof` `login-by=http-pap` (RouterOS cookie login off). The "private-address setting" answers in a-info are IPs, not settings; the MACs below come from the portal log.

## Findings

- **E1 (both, b-join):** a plain HotSpot HTTP redirect opens the OS captive-portal sheet about 2s after joining. The sheet showed the portal page with the real MAC and IP from `$(mac-esc)`/`$(ip)`. After `/connect` (active login via REST) the sheet closed or turned into Done by itself, and the browser loaded normal sites. No probe spoofing needed.
- **E2 (iPhone):** the iOS captive sheet does not keep cookies between sheets. All 7 sheet visits arrived with no `opid` cookie (portal.jsonl, `ua_class=ios-cna?`), so each one minted a new identity (7 `p-*` users in identities.json). Inside one sheet the cookie works: the `POST /connect` at 08:16:36 and 08:37:54 carried the cookie set by the visit just before it.
- **E3 (iPhone):** the MAC was `02:00:00:E2:00:01` (a private, locally administered address) in every step: idle-out, forget/rejoin, the private-address step (setting "Fixed") and reboot.
- **E4 (Android):** the Android captive sheet keeps cookies across sheets. After the session idled out (d-idle) the new sheet sent the cookie and the portal logged in automatically (`auto-login`), with zero taps.
- **E5 (Android, f-mac):** switching from the randomized MAC `02:00:00:E3:00:01` to the device MAC `02:00:00:E7:00:01` opened a sheet with the same cookie; the portal removed the old active entry and logged in the new MAC (`rebind`, old_mac recorded). The same `p-fa080000` user stayed active on the new MAC.
- **E6 (both, h-browser):** the captive sheet does not share cookies with the main browser. Safari and Chrome both reached the portal with no `opid` cookie.
- **E7 (Android, e-forget and g-reboot):** no sheet and internet kept working, because the HotSpot session never ended: the active entry for the same user and MAC was still there (uptime 59s, then 5m15s). Forget/rejoin and reboot came back inside the 2m idle timeout with the same MAC.
- **C1:** cookie identity works on Android and fails on iOS. On iPhone the only stable key the router sees is the per-network private MAC, so the controller needs a MAC fallback: a sheet visit with no cookie but a MAC the controller already knows should resume that identity, not mint a new one.
- **C2:** an iPhone that changes its MAC ("Rotating" or switching "Private Wi-Fi Address" on or off) and opens a fresh sheet looks like a new device to the controller. Recovering its balance needs something the user carries, such as a resume code (grill topic 3).
- **D1:** the portal must not mint a new identity per sheet visit for a known MAC. This run created 7 throwaway users for one iPhone.
- **D2:** the main-browser page cannot rely on the sheet's cookie (E6). Anything shown there (balance, pause) must look the client up by MAC/IP, which the HotSpot redirect already passes.

## Not covered by this run

The first three items below were covered by the [recheck run](../2026-10-18-phones-recheck/results.md) the same day.

- **iPhone MAC change:** the setting was already "Fixed", so the MAC never changed. Rerun f-mac switching "Private Wi-Fi Address" off (or to "Rotating") to see the new MAC and confirm C2.
- **iPhone e-forget / f-mac / g-reboot** ran while logged out (no `/connect` after d-idle), so they show that the sheet reappears, not whether a logged-in session survives.
- **Android forget/reboot after the session ends:** E7 was masked by the live session. Repeat after the 2m idle timeout to see the sheet's cookie survive a forget and a reboot.
- **adb mode** (Wi-Fi MAC readout, logcat, screenshots) not run yet.

Raw files:
- answers.tsv (every answer)
- portal.jsonl (every portal request)
- identities.json (opid -> HotSpot user)
- portal.stderr
- steps/<phone>-<step>/ (step.json, host/active/user/lease JSON, plus screen.png, logcat.txt, wifi-mac.txt in adb mode)
