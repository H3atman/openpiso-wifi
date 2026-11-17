# Update

A **Release** is a new version of the Controller, published on GitHub. The Controller checks for one by itself every day and tells you on the Dashboard. You install it with one button.

Your Vouchers, Sales, prices and settings are kept through an update.

## How the Controller checks

Soon after the board starts, and then once a day, the Controller asks GitHub for the latest Release. It only looks; it never installs anything by itself.

It accepts a Release only if it is signed by the project's key and is newer than the version you run. A copy that someone changed, or an older version, is refused.

The board needs internet access for this check. Without it, the Controller keeps running the version you have.

## Install an update

1. Open the **Dashboard**. Under **System Status**, the **Controller** card shows your **Version**.
2. When a Release is newer, the card shows `Update available:` and its version, with an **Update** button. Select **Update**.

> **Screenshot to come:** admin panel, the Dashboard's Controller card showing Update available and the Update button.

The page **Updating to …** opens. The Controller downloads the new version, checks it, saves a copy of your data, and restarts. The page opens the Dashboard again by itself when the Controller is back.

While it restarts, customers who are online stay online. New customers wait until it is back to use a Voucher.

If the page says `The update is taking longer than expected. Open the Dashboard to see its status.`, open the Dashboard. While the update is still running, the Controller card shows `Update in progress; the Controller will restart.`

## If an update goes wrong

If the new version does not start properly, the Controller goes back to the version you had, with your data as it was just before the update. A sale made in the few seconds while it was stopped may be lost.

The Controller card then shows `Update to … was rolled back:` or `Update to … failed:`, followed by the reason. Your Shop keeps working on the old version. To see more, run this on the board over SSH:

```sh
journalctl -u 'openpiso-update*'
```

## Update from the board instead

Over SSH, this installs the latest Release straight away:

```sh
sudo openpiso update apply
```

And this checks for a Release now instead of waiting for the daily check:

```sh
sudo openpiso update check
```

## Turn off the automatic check

To stop the daily check, and stop the **Update** button from installing anything, run on the board:

```sh
sudo systemctl disable --now openpiso-update-check.timer openpiso-update.path
```

`sudo openpiso update apply` still works. To turn the check back on:

```sh
sudo systemctl enable --now openpiso-update-check.timer openpiso-update.path
```

For every detail, see the [Controller reference](https://github.com/H3atman/openpiso-wifi/blob/main/controller/README.md#updates).

Next: [Troubleshooting](troubleshooting.md).
