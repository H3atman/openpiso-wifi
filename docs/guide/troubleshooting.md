# Troubleshooting

Each problem below starts with the message you or your customer sees, exactly as it appears. Find the message, then follow the fix.

If your problem is not here, [open an issue on GitHub](https://github.com/H3atman/openpiso-wifi/issues) with the message you see and what you did before it.

## Opening the admin panel

| Message | What to do |
| --- | --- |
| `management network only` | Your PC is not inside the `--admin-network` you chose when installing. Use a PC on that network, or change `OPENPISO_ADMIN_NETWORK` in `/etc/openpiso/openpiso.env` as in [If you need to change a setting later](install-the-controller.md#if-you-need-to-change-a-setting-later). |
| `Wrong password.` | Type the admin password you chose when installing. Select **Show** to check what you typed. |
| `Try again in 5 minutes` (or fewer minutes) | Five wrong passwords from your PC lock the login for five minutes. Wait, then try again. |

## Connecting the Router

The messages you can see while connecting the Router are in [Connect the Router](connect-the-router.md#if-it-does-not-work).

On the **Dashboard**, the **Router** card shows `Router unreachable` when the Controller cannot reach the Router. Check that the Router is on and that the board's network cable is plugged in. The card fills in again by itself once the Router answers.

## On the customer's phone

| Message | What to do |
| --- | --- |
| `Voucher not found at this site` | No Voucher has this code on your Controller. Check the code; search for it on **Vouchers** to see if it exists. |
| `Voucher already used` | Every Voucher works once. Search for the code on **Vouchers** to see when it was used and by which Device. |
| `Too many Resume Code attempts. Try again in five minutes.` | Five wrong Resume Codes from one phone. Wait five minutes. |
| `Resume Code not found. Check the code shown after your purchase.` | The Resume Code is mistyped. Check it with the customer. If it is lost, see [Help a customer who lost their time](run-the-shop.md#help-a-customer-who-lost-their-time). |

## Changing settings

| Message | What to do |
| --- | --- |
| `tiers must increase in pesos and never reduce seconds per peso` | A higher Rate tier gives less time per peso than the one before it. Give it more time, or a lower price. |
| `a higher tier must never give fewer Points per peso than the tier before it` | Same rule for **Points earned**: give the higher tier at least as many Points per peso. |
| `start with a ₱1 tier; choose up to 100 tiers` | The first Rate tier must cost ₱1. |

## Customizing the Portal

When you select **Save draft** on **Settings › Portal**, the page lists every problem and saves nothing. For example:

| Message | What to do |
| --- | --- |
| `core.js must be loaded exactly once with <script src="/portal/core.js">; found …` | Put back the line `<script src="/portal/core.js" defer></script>` in `index.html`, once. |

## Reading the Controller's log

For any other problem, the Controller's log says what went wrong. On the board, over SSH:

```sh
systemctl status openpiso
sudo journalctl -u openpiso
```

For every detail, see the [Controller reference](https://github.com/H3atman/openpiso-wifi/blob/main/controller/README.md).
