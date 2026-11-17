# Connect the Router

In this step you connect the Controller to your MikroTik **Router**, so the Controller can let customers online. You do it once, from the admin panel. Before you start, check the Router list in [What you need](what-you-need.md#your-router-before-you-start).

It takes about 5 minutes.

## 1. Enter the Router's details

1. In the admin panel, open **Settings › Router**. The page is called **Router onboarding**.
2. Fill in:
   - **Router address**: `https://` followed by the Router's address, for example `https://192.168.10.1`. Use the same address your `www-ssl` certificate names, and nothing after it.
   - **Admin username** and **Admin password**: your MikroTik admin login.
3. Select **Read certificate**.

> **Screenshot to come:** admin panel, the Router onboarding page with Router address, Admin username and Admin password filled in.

The Controller now reads only the Router's certificate. It does not send your password yet.

## 2. Check the fingerprint

The page **Confirm Router certificate** shows a fingerprint: a long line of letters and numbers that belongs to the Router's certificate, like `SHA256:4F:2A:…`. Checking it proves the Controller is talking to your Router and not to another device pretending to be it.

1. On the Router, open **New Terminal** in WebFig or Winbox and type:

   ```
   /certificate print detail
   ```

2. Find the certificate `www-ssl` uses (`openpiso-router` if you made it with [What you need](what-you-need.md#make-a-certificate-for-www-ssl)) and its `fingerprint`. RouterOS shows it in small letters and without the `SHA256:` and the colons. The letters and numbers must be the same as on the admin panel, in the same order.
3. If they match, select **Fingerprint matches**. If they do not, stop: check the Router address and try again.

> **Screenshot to come:** Router, the Confirm Router certificate page showing the fingerprint and the Fingerprint matches button.

Nothing changes on the Router before you select **Fingerprint matches**. From then on, the Controller trusts only this certificate.

## 3. Done

The page **Router connected** shows `RouterOS 7.… is ready.` The Controller has set up the Router as listed in [What the Controller changes on your Router](what-you-need.md#what-the-controller-changes-on-your-router), and it forgets your admin password.

> **Screenshot to come:** Router, the Router connected page.

## If it does not work

The browser shows a short message instead of the next page. The common ones:

| Message | What to do |
| --- | --- |
| `Router address must be an HTTPS origin such as https://192.0.2.1` | Start the address with `https://` and put nothing after the address. |
| `connect to Router …` | The Controller cannot reach the Router. Check the address, and that `www-ssl` is turned on. |
| `Router certificate is not valid for …` | The certificate does not name the address you typed. Type the address the certificate names, or [make a certificate](what-you-need.md#make-a-certificate-for-www-ssl) for this address. |
| `Router certificate is not currently valid` | The certificate has expired or is not valid yet. Check the Router's date and time, or make a new certificate. |
| `RouterOS v7 is required; Router reports …` | Upgrade the Router to RouterOS v7. |
| `Router has no HotSpot server to configure` | Set up a HotSpot server on the Router first. |
| `read the Router certificate before confirming it` | Go back to **Settings › Router** and start again from step 1. |

If you ever replace the Router's certificate, the Controller stops talking to the Router until you connect it again with these steps and confirm the new fingerprint. Connecting the same Router again keeps its `openpiso-controller` user.

For every detail, see the [Controller reference](https://github.com/H3atman/openpiso-wifi/blob/main/controller/README.md#router-onboarding).

Next: [Sell your first Voucher](sell-your-first-voucher.md).
