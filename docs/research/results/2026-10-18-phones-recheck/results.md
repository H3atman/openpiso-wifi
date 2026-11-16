# Phone captive-portal lab results

- Date: 2026-10-18
- Mode: recheck
- RouterOS: 7.23.8 (long-term)
- AP: TP-Link EAP220

## iphone

| step | popup? | MAC seen | opid cookie returned? | portal action | internet w/o tap? | notes |
|---|---|---|---|---|---|---|
| r-a-connect | - | 02:00:00:E2:00:01 | no | new | n | Wi-Fi Address shown under Private Wi-Fi Address? 10.5.50.199; Page said Connected? (y/n) n |
| r-b-forget | n | - | - | - | y | Popup said Reconnected automatically? (y/n) n |
| r-c-reboot | n | - | - | - | y | Popup said Reconnected automatically? (y/n) y |
| r-d-off | n | - | - | - | n | Popup said Reconnected automatically? (y/n) n; Wi-Fi Address shown now? 02:00:00:E6:00:01 |
| r-e-fixed | n | - | - | - | y | Popup said Reconnected automatically? (y/n) n; Wi-Fi Address shown now (same as step a?) 02:00:00:E2:00:01 |

## android

| step | popup? | MAC seen | opid cookie returned? | portal action | internet w/o tap? | notes |
|---|---|---|---|---|---|---|
| r-a-connect | - | 02:00:00:E3:00:01 | yes | new, other, login | y | Privacy setting now (randomized / device MAC)? randomized; Page said Connected? (y/n) y |
| r-b-forget | y | 02:00:00:E3:00:01 | yes | auto-login, other | y | Popup said Reconnected automatically? (y/n) y; Privacy setting after forget? randomized MAC |
| r-c-reboot | y | 02:00:00:E3:00:01 | yes | auto-login, other | y | Popup said Reconnected automatically? (y/n) y |

Same phones, AP and router as the [plain run](../2026-10-18-phones-plain/results.md) (iPhone on iOS 18.7, <phone-model> on Android 12, TP-Link EAP220, RouterOS 7.23.8). Profile `phone`: `shared-users=1`, `idle-timeout=2m`. In Android steps b and c the wizard ended the session first by removing the active entry via REST (cause `admin reset`, what the controller does on pause or idle-out).

Reading notes:
- iPhone r-a: the "Wi-Fi Address" answer is the IP; the MAC is in portal.jsonl. "Page said Connected? n" was answered before tapping Connect: the `/connect` login landed at 09:13:42, after the step closed, and the session was live from then on (r-b `active.json`).
- The "MAC seen" column only lists portal visits. Steps with no popup have no portal visit; their MAC comes from `steps/*/active.json` and `host.json`.

## Findings

- **E1 (iPhone r-b, logged in):** forget and rejoin with the address on Fixed brought the same private MAC `02:00:00:E2:00:01` back. No popup appeared and internet worked: the live session simply continued (same user `p-fa0a0000`, uptime 49s at the snapshot).
- **E2 (iPhone r-c, logged in):** a reboot also kept the session: no popup, internet worked, same user and MAC, uptime 3m15s.
- **E3 (iPhone r-d):** Private Wi-Fi Address Off switched the iPhone to its hardware MAC `02:00:00:E6:00:01`, which got a new lease (10.5.50.196) and an unauthorized host entry. **No popup appeared** and there was no internet: the user would have to open a browser to reach the login page. Meanwhile the old session on the private MAC stayed active and kept charging (uptime 3m15s → 4m20s).
- **E4 (iPhone r-e):** switching back to Fixed restored the same private MAC `02:00:00:E2:00:01`. Its session was still active (5m46s; the MAC was away about 1.5 min, under the 2m idle timeout), so internet worked again without a tap.
- **E5 (Android r-a):** the sheet still held the cookie from the plain run about 40 minutes earlier (`fa0800000000`). This run's portal did not know it, so it minted a new identity (`p-fa0b0000`) as designed.
- **E6 (Android r-b, session ended):** after the session was removed, forget + rejoin opened the sheet, which sent the cookie, and the portal auto-logged it in with zero taps. The cookie survives a forget.
- **E7 (Android r-c, session ended):** after a reboot the sheet again sent the cookie and auto-logged in with zero taps. The cookie survives a reboot.
- **E8 (Android):** the randomized MAC `02:00:00:E3:00:01` was the same as in the plain run, through a switch to device MAC and back, a forget and a reboot. Android 12 keeps a per-network randomized MAC.
- **C1:** on both phones the default private MAC is stable per network across forget and reboot (iOS "Fixed", Android randomized). A HotSpot session for an unchanged MAC survives forget, rejoin and reboot as long as it is not timed out.
- **C2:** on Android the sheet cookie survives session end, forget and reboot, so cookie identity alone resumes the customer.
- **C3:** an iPhone that turns Private Wi-Fi Address Off gets no popup at all, and nothing links its new MAC to the old one. Only the customer can, so the recovery path is a resume code typed into the login page.
- **D1:** when a MAC leaves while logged in, its session keeps charging until `idle-timeout` (here 2m) or the controller removes it. Keep the idle timeout short (already decided in [mikrotik-controller.md §8](../../mikrotik-controller.md#session-control-pause-idle-resume-mac-rebind-top-up)).
- **D2:** the login page reached from a plain browser must work as well as the sheet (E3), including the resume-code path.

Raw files:
- answers.tsv (every answer)
- portal.jsonl (every portal request)
- identities.json (opid -> HotSpot user)
- portal.stderr
- steps/<phone>-<step>/ (step.json, host/active/user/lease JSON, plus screen.png, logcat.txt, wifi-mac.txt in adb mode)
