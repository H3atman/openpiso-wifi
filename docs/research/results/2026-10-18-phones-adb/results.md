# Phone captive-portal lab results

- Date: 2026-10-18
- Mode: adb
- RouterOS: 7.23.8 (long-term)
- AP: TP-Link EAP220
- android (adb-connect): Device (adb getprop) <phone-model>, Android 14
- android (a-info): Model and OS version? Xiaomi
- android (a-info): Current private-address setting for openpiso-lab? randomized mac

## android

| step | popup? | MAC seen | opid cookie returned? | portal action | internet w/o tap? | notes |
|---|---|---|---|---|---|---|
| adb-connect | - | - | - | - | - | Device (adb getprop) <phone-model>, Android 14 |
| a-info | - | 02:00:00:E4:00:01 | yes | new, other | - | Model and OS version? Xiaomi; Current private-address setting for openpiso-lab? randomized mac |
| b-join | y | - | - | - | - | Roughly how many seconds until it appeared? 2; What did it show (New device page / blank / error)? New Device |
| c-connect | - | 02:00:00:E4:00:01 | yes | login, other | y | Page said Connected? (y/n) y; Popup turned into Done / closed by itself? (y/n) y |
| d-idle | y | 02:00:00:E4:00:01 | yes | auto-login, other | y | Popup said Reconnected automatically? (y/n) y |
| e-forget | n | - | - | - | y | Popup said Reconnected automatically? (y/n) n |
| f-mac | y | 02:00:00:E5:00:01 | yes | rebind, other | y | Popup said Reconnected automatically? (y/n) y; Which setting is now active? device MAC |
| g-reboot | y | 02:00:00:E5:00:01 | yes | auto-login, other | y | Popup said Reconnected automatically? (y/n) y |
| h-browser | - | - | no | browser-check, other | - | What does the page say about the cookie? no opid cookie was sent to this browser |

Phone: <phone-model> on Android 14 (from adb getprop), a different phone from the plain/recheck runs (<phone-model>, Android 12). The captive sheet is `com.google.android.captiveportallogin`. The phone joined the AP's open SSID `TP-Link_2.4GHz_E10001` (screenshots), not `openpiso-lab`; it is the same AP and CHR LAN. Profile `phone`: `shared-users=1`, `idle-timeout=2m`.

Reading notes:
- The phone joined during a-info, so the first sheet visit (09:38:54, `new`) falls in that step and b-join has no portal line.
- Timings below come from `steps/*/logcat.txt` (NetworkMonitor, ConnectivityService, CaptivePortalLoginActivity).
- `wifi-mac.txt` was removed from this run: the old capture grepped all of `dumpsys wifi`, caught the phone's other saved networks and cut off before the lab network. MACs come from portal.jsonl and active.json instead.

## Findings

- **E1 (c-connect):** the `/connect` login landed at 09:39:46. The sheet's page load made NetworkMonitor re-probe at once: `generate_204` returned 204 at 09:39:46.877, the network went `-CAPTIVE_PORTAL+VALIDATED` at 09:39:47.440, and the sheet logged `Result DISMISSED` at 09:39:47.473. Under 1s from login to the sheet closing itself.
- **E2 (d-idle):** after the session idled out, the sheet sent the cookie and the portal auto-logged it in (`auto-login`), zero taps.
- **E3 (f-mac):** switching from randomized `02:00:00:E4:00:01` to device MAC `02:00:00:E5:00:01`: the HTTP probe got a 302 to `http://10.5.50.1/login?dst=...generate_204` at 09:44:42.8 (`isPortal()=true`, `+CAPTIVE_PORTAL`). The sheet loaded at 09:44:50.1, the portal re-bound the same user to the new MAC (`rebind`, old MAC recorded), and the sheet was dismissed at 09:44:50.987.
- **E4 (g-reboot):** the reboot step ran about 8 minutes, so the session had ended. After boot: `+CAPTIVE_PORTAL` at 09:52:43.7, sheet visit with the cookie and `auto-login` at 09:52:49, `VALIDATED` at 09:52:49.75. The cookie survives a reboot.
- **E5 (e-forget):** masked again: the session was still live (30s), so no sheet appeared. The recheck run covered this case with the session ended.
- **E6 (h-browser):** Chrome sent no `opid` cookie, same as the plain run.
- **C1:** Android 14 on a second Xiaomi behaves like Android 12 on the <phone-model>: the captive sheet keeps the cookie across idle-out, MAC change and reboot, so cookie identity resumes the customer with zero taps.
- **C2:** detection and dismissal need no tricks. NetworkMonitor flags the portal from the HotSpot 302 on `generate_204`, and after login the sheet re-probes and closes itself in under 1s. The 6-7s between `+CAPTIVE_PORTAL` and the sheet loading (E3, E4) was the sheet being opened, not portal latency.
- **D1:** the wizard's `wifi-mac.txt` capture now reads only the current Wi-Fi MAC (`ip link show wlan0`), not the whole `dumpsys wifi`.

Raw files:
- answers.tsv (every answer)
- portal.jsonl (every portal request)
- identities.json (opid -> HotSpot user)
- portal.stderr
- steps/<phone>-<step>/ (step.json, host/active/user/lease JSON, plus screen.png and logcat.txt in adb mode)
