# Guide acceptance run

- Date: 2026-11-17
- Ticket: #9 (spec #3)
- Controller: the published Release v0.1.1 (`openpiso-linux-armv7`), installed by the Guide's own `releases/latest` commands and `install.sh`. A second guest ran v0.1.0 to test Update.
- Board: Ubuntu 18.04 armhf guest under QEMU TCG (`controller/e2e/arm_vm.py`), user with sudo but not in `adm`.
- Router: `chr-lab`, CHR on RouterOS 7.23.8 (long-term), HotSpot `hs1` on ether2 (10.5.50.0/24).
- Operator PC: Playwright Chromium, 1280 × 800, on the libvirt host network (192.168.122.1).
- Phones: Playwright Chromium as Pixel 7 on a macvlan over the HotSpot bridge, MACs `02:00:00:09:00:01`–`03`.

The Guide pages were followed in sidebar order, typing each command and pressing each button as written. Screenshots replaced in the Guide come from this run.

## Lab-only fixtures

In the lab the board sits on the Router's ether1 network, not behind a LAN port, so three things the Guide does not ask for were added and removed afterwards:

- a route on the guest: `ip route replace 10.5.50.0/24 via 192.168.122.230`;
- a srcnat `accept` rule on the Router so customer traffic to the board keeps its own address (the Guide's "keep the customer's address" requirement, met here by a rule instead of the LAN layout), comment `openpiso-guide-acceptance`;
- static HotSpot DHCP leases for the phones, comment `openpiso-guide-device`.

## Results

Pass: did what the page says. Fixed: the page was wrong and is corrected in this branch. Not exercisable: needs hardware or software the lab does not have; left for the human pass in #10.

### What you need

| Step | Result |
| --- | --- |
| RouterOS version under System › Resources | Pass (7.23.8) |
| Certificate commands in the terminal, `/certificate print` lists both | Pass |
| "open **New Terminal** in WebFig" | Fixed: WebFig's button is **Terminal**; New Terminal is Winbox |
| What the Controller changes on the Router (user and group, profiles, `hs-prof` login by http-pap, walled garden, `login.html`) | Pass, each checked over REST |
| Winbox, real Orange Pi, NAT masquerade on a LAN | Not exercisable |

### Install the Controller

| Step | Result |
| --- | --- |
| 1. Write the image (imager) | Not exercisable |
| 2. DHCP static lease for the board | Not exercisable (lab guest address) |
| 3. SSH from PowerShell | Not exercisable (SSH from Linux used) |
| 4. Download and install commands, verbatim with `armv7` | Pass; prompts `Admin password:` / `Confirm password:`; prints `OpenPiso Controller is running on 0.0.0.0:80 (portal http://192.168.122.247).` |
| Re-running the installer keeps data and password | Pass |
| "If it prints … run `journalctl -u openpiso`" | Fixed: needs `sudo`; without it a non-`adm` user sees "No entries". Failure message itself not observed |
| 5. Login, `Wrong password.`, `management network only` | Pass |
| Change a setting later: edit `/etc/openpiso/openpiso.env` | Fixed: `/etc/openpiso` is mode 700 root, so the page now says to use `sudo` |

### Connect the Router

| Step | Result |
| --- | --- |
| 1. Router address, Admin username, Admin password, **Read certificate** | Pass |
| 2. Fingerprint from `/certificate print detail`, lower case without colons | Pass, matches |
| 3. **Router connected**, `RouterOS 7.23.8 (long-term) is ready.` | Pass |
| HTTPS-origin error message | Pass |

### Sell your first Voucher

| Step | Result |
| --- | --- |
| Shop name, Generate window fields and defaults, `Voucher Batch generated.`, **Open Print sheet** | Pass |
| Print sheet: price, code, time, Validity, Pauses, QR | Pass |
| Phone joins, Portal opens, **Voucher** → **Use Voucher** → `You are online.`, browsing works | Pass |
| Resume Code shown | Pass |
| Redeemed count, `USED` on the Print sheet, Sales Inventory shows the sale | Pass |

### Run the Shop day to day

| Step | Result |
| --- | --- |
| Generate with a Rate tier and a Seller; prefix sample; CSV; Delete only while unused | Pass |
| Voucher search with 3 characters | Pass |
| Rate tiers: three tiers saved; ₱5 for 40 min refused with `tiers must increase in pesos and never reduce seconds per peso`, nothing saved | Pass |
| Points table: Add row, Save | Pass |
| Dashboard Sales; Sales Inventory Today, Filters & search by Seller, Feed, Export | Pass |
| Same phone, new address: back online without a code | Pass (the `Moving your time…` text was too quick to capture) |
| Rebind with the Resume Code to another phone; old phone offline | Pass |
| `Resume Code invalid…`, `Resume Code not found…`, `Too many Resume Code attempts…` | Pass |
| `This code has expired` | Not exercised (needs a Validity to end) |
| Time Credits: Online now, detail fields, Timeline; Grant time → `Grant recorded and applied to the Router.` | Pass |
| Settings › Pause with Idle Pause on; Router profile switches to `openpiso-idle` | Pass |
| Settings › Validity: `Unused time expires 7 days after the last Top-up.`; 36501 days refused | Pass |
| Portal: Upload logo, Upload cover, edit `<h1>`, Save draft message, broken draft lists the error and saves nothing, Check every state, online preview, Publish draft, phone shows it | Pass |
| Portal: `<script src="/portal/core.js">` | Fixed in Troubleshooting: the Default Portal's line is `<script src="/portal/core.js" defer></script>` |
| Portal: "**Revert to previous** brings back what customers saw before" | Fixed: after the first publish there is no previous version and the button is disabled; Reset to default, then Revert to previous, both work |
| Download default | Pass |
| Logs, Coin sales, Credit intents | Pass (no Vendo, so no coins) |

### Add a Vendo

Not exercisable: needs a Vendo board.

### Update

| Step | Result |
| --- | --- |
| Check soon after start: journal shows `current=v0.1.0 available=v0.1.1 update=true` | Pass |
| Controller card shows `Update available: v0.1.1` and **Update** | Pass |
| **Update** → `Updating to v0.1.1` → back on the Dashboard on v0.1.1; Voucher Batch kept | Pass |
| `journalctl -u 'openpiso-update*'` | Fixed: needs `sudo` |
| `sudo openpiso update check` | Pass (`update=false` on v0.1.1) |
| `sudo openpiso update apply` | **Software defect**, see below |
| Disable and enable the timer and path units | Pass |
| Rollback, `The update is taking longer…` | Not exercisable (needs a bad Release) |

### Troubleshooting

Observed as written: `Wrong password.`, `Try again in 5 minutes`, `management network only`, `Router unreachable`, `Voucher not found at this site`, `Voucher already used`, the Resume Code messages, the Rate tier and Points rule messages, `start with a ₱1 tier; choose up to 100 tiers`, the core.js draft message. The browser's own form checks stop a too-short Voucher code and an empty Custom price or time before they reach the Controller, so `Voucher invalid…`, `choose 0–1000000 pesos…` and `choose 1–31536000 seconds` were not seen. The rest need a fault the lab did not cause.

## Software defects

1. `sudo openpiso update apply` over SSH fails with `openpiso update: OPENPISO_LISTEN is not set` (exit 1). The systemd units load `/etc/openpiso/openpiso.env`; a shell does not. With the file sourced it runs. The Guide and the Controller README both document the plain command. The failed attempt is then recorded and shown on the Dashboard.
2. Running `update apply` when nothing is newer leaves `Update to a new version failed: no newer release: running v0.1.1, published v0.1.1` on the Dashboard's Controller card.
3. The Confirm Router certificate page says to compare with `/certificate print`, but plain `print` does not show fingerprints; `print detail` does (the Guide already says `print detail`).
4. On that page the fingerprint runs past the card at 1280 px wide and its end is cut off, so it cannot be compared in full.

## Placeholders left

The imager and PowerShell screenshots in Install the Controller.

## Repeat it

1. Take the lab lock and boot two armhf guests with `controller/e2e/arm_vm.ArmVM` (as `controller/e2e/run.py` does).
2. Add the three lab fixtures above.
3. Follow the Guide pages in sidebar order; on the second guest use `base=https://github.com/H3atman/openpiso-wifi/releases/download/v0.1.0` and then press Update.
4. Build the site and run the link check from CONTRIBUTING.md.
5. Remove the fixtures, the certificates and the HotSpot users the Controller created, restore `login.html` and the walled garden, and set `www-ssl` back to its lab certificate.

## Files

- `install-v0.1.1.log`: first install session (it ends early: the script that typed it sent `exit` before the installer finished).
- `reinstall-v0.1.1.log`: the installer run again on the same board, with the final message.
- `install-v0.1.0.log`: the older Release on the Update guest.
- `linkcheck-broken.txt`: the link check with a deliberate broken link, image and anchor (exit 2).
- `linkcheck-final.txt`: the link check on the finished Guide (exit 0).
