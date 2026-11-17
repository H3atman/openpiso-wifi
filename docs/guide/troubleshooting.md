# Troubleshooting

Each problem below starts with the message you or your customer sees, exactly as it appears. Find the message, then follow the fix.

If your problem is not here, [open an issue on GitHub](https://github.com/H3atman/openpiso-wifi/issues) with the message you see and what you did before it.

## Installing the Controller

| Message | What to do |
| --- | --- |
| `openpiso service did not become healthy; see journalctl -u openpiso` | The Controller did not start. Run `sudo journalctl -u openpiso` on the board and read the last lines for the reason. Fix it, then run the installer again; it keeps your data. |

## Opening the admin panel

| Message | What to do |
| --- | --- |
| `management network only` | Your PC is not inside the `--admin-network` you chose when installing. Use a PC on that network, or change `OPENPISO_ADMIN_NETWORK` in `/etc/openpiso/openpiso.env` as in [If you need to change a setting later](install-the-controller.md#if-you-need-to-change-a-setting-later). |
| `Wrong password.` | Type the admin password you chose when installing. Select **Show** to check what you typed. |
| `Try again in 5 minutes` (or fewer minutes) | Five wrong passwords from your PC lock the login for five minutes. Wait, then try again. |

## Connecting the Router

The messages you can see while connecting the Router are in [Connect the Router](connect-the-router.md#if-it-does-not-work). One more can appear at the end:

| Message | What to do |
| --- | --- |
| `Router saved; unable to apply Pause settings. Retry onboarding.` | The Router was connected but did not take the Pause settings. Go to **Settings › Router** and connect it again. |

On the **Dashboard**, the **Router** card shows `Router unreachable` when the Controller cannot reach the Router. Check that the Router is on and that the board's network cable is plugged in. The card fills in again by itself once the Router answers.

## On the customer's phone

| Message | What to do |
| --- | --- |
| `Device not found on the Router. Connect through the WiFi; Voucher not used.` | The Router does not see this phone on your WiFi. Check that the phone is on the Shop's WiFi and not on mobile data. If every phone gets this message, check your NAT masquerade rule: traffic to the board must keep the customer's own address (see [What you need](what-you-need.md#your-router-before-you-start)). |
| `Connect through the WiFi to see your time or redeem a Voucher.` | Same cause as the message above. |
| `Your Device is still joining the WiFi. Try again in a few seconds; Voucher not used.` | The phone has only just joined. Wait a few seconds and try again. |
| `Service temporarily unavailable. Voucher not used.` | The Controller cannot reach the Router, or the Router is not connected yet. Check the **Router** card on the Dashboard. The Voucher can still be used later. |
| `Service temporarily unavailable. Your bought time is saved; try Connect again.` | The Controller could not let the phone online just now. The time is kept. Tap **Connect** again. |
| `Voucher invalid. Enter the letters and digits printed on it.` | The code is mistyped or too short. Type it again. |
| `Voucher not found at this site` | No Voucher has this code on your Controller. Check the code; search for it on **Vouchers** to see if it exists. |
| `Voucher already used` | Every Voucher works once. Search for the code on **Vouchers** to see when it was used and by which Device. |
| `Too many Voucher attempts. Try again in five minutes.` | Five wrong codes from one phone. Wait five minutes. |
| `Too many Resume Code attempts. Try again in five minutes.` | Five wrong Resume Codes from one phone. Wait five minutes. |
| `Resume Code not found. Check the code shown after your purchase.` | The Resume Code is mistyped. Check it with the customer. If it is lost, see [Help a customer who lost their time](run-the-shop.md#help-a-customer-who-lost-their-time). |
| `This code has expired` | The time's Validity ended, so its unused time is gone. The customer buys new time. |
| `Validity has ended. Unused time is forfeited. Enter another Voucher for a new Time Credit` | Same as above. |
| `Pause Limit reached. You have no Pauses left` | The customer used all their Pauses. Their next purchase gives them new ones. |
| `This Portal needs JavaScript. Turn it on, then reload.` | The phone's browser has JavaScript turned off. Turn it on in the browser's settings. |

## Making Vouchers

These appear in the **Generate Voucher Batch** window:

| Message | What to do |
| --- | --- |
| `choose 0–1000000 pesos; Custom price is required` | With **Custom**, fill in **Price (pesos)**. Type `0` for a free Voucher. |
| `choose 1–31536000 seconds` | With **Custom**, fill in **Time (seconds)**, up to 31536000 (one year). |
| `Rate tier no longer exists; choose a Shop Rate tier or Custom` | The Rate tier was changed after you opened the window. Choose a Rate tier again, or **Custom**. |

## Changing settings

| Message | What to do |
| --- | --- |
| `Settings saved; Router temporarily unavailable. Retry Save.` | Your Pause settings are saved, but the Router did not take them all. Check the **Router** card on the Dashboard, then select **Save Pause settings** again. |
| `tiers must increase in pesos and never reduce seconds per peso` | A higher Rate tier gives less time per peso than the one before it. Give it more time, or a lower price. |
| `a higher tier must never give fewer Points per peso than the tier before it` | Same rule for **Points earned**: give the higher tier at least as many Points per peso. |
| `start with a ₱1 tier; choose up to 100 tiers` | The first Rate tier must cost ₱1. |

## Customizing the Portal

When you select **Save draft** on **Settings › Portal**, the page lists every problem and saves nothing. The common ones:

| Message | What to do |
| --- | --- |
| `index.html: external URL "…"; the Portal loads only from the Controller` | The Portal cannot load anything from the internet. Remove the link, or upload the picture as the cover or logo instead. The same message names `portal.css` or `custom.js` when the link is there. |
| `core.js must be loaded exactly once with <script src="/portal/core.js">; found …` | Put back the line `<script src="/portal/core.js" defer></script>` in `index.html`, once. |
| `required marker … is missing` | You removed a part of the page the Portal needs. Put it back; the [Portal contract](https://github.com/H3atman/openpiso-wifi/blob/main/docs/portal-contract.md#required-markers) lists them. If you are lost, use **Download default** and start again. |

## Updating

| Message | What to do |
| --- | --- |
| `Update to … was rolled back: …` or `Update to … failed: …` | The new version did not install, and the Controller went back to the version you had. Your Shop keeps working. Read the reason after the colon, and run `sudo journalctl -u 'openpiso-update*'` on the board for more. See [Update](update.md#if-an-update-goes-wrong). |
| `The update is taking longer than expected. Open the Dashboard to see its status.` | Open the Dashboard and read the **Controller** card. |

## Reading the Controller's log

For any other problem, the Controller's log says what went wrong. On the board, over SSH:

```sh
systemctl status openpiso
sudo journalctl -u openpiso
```

For every detail, see the [Controller reference](https://github.com/H3atman/openpiso-wifi/blob/main/controller/README.md).
