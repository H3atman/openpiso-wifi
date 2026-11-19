# What you need

OpenPiso WiFi lets you sell WiFi time at your **Shop** (the place you sell WiFi time) with **Vouchers**: printed codes, each worth a fixed amount of time, that a customer types on their phone to go online. **Vendos** (coin-accepting units on your network that report each coin to the Controller) are coming.

Three things work together:

- The **Controller**: a small Linux board that keeps the record of the time each customer bought and runs the admin panel, where you set prices and print Vouchers.
- The **Router**: your MikroTik. It lets customers online and counts the time they use, on the Controller's instructions.
- The **Portal**: the page a customer's phone opens when it joins your WiFi. The Controller serves it; the customer types a Voucher there.

This page lists the hardware and what your Router must already do before you start.

## Hardware

### Tested

These are what OpenPiso WiFi is tested on:

- **Controller**: Orange Pi Zero running Armbian.
- **Router**: MikroTik running RouterOS v7.
- **Vendo**: ESP8266 4 MB LAN coin boards (KiTifi or JuanFi). Vendos are not yet part of this Guide.

### Should work

These are not tested yet, but should work as the Controller:

- A Raspberry Pi running Raspberry Pi OS Lite.
- Any arm64 or amd64 computer that runs Linux with systemd.

### Also needed

- A microSD card for the board, and a card reader for your PC.
- A network cable from the board to your MikroTik's LAN.
- A Windows PC on the same network, with Winbox or a browser to manage the MikroTik.
- A printer, or a way to save a PDF, to print Vouchers.

## Your Router before you start

Your MikroTik does not need a **HotSpot server** yet (a HotSpot server is one captive network on the Router: the interface customers join, with its own login page and address range). When you connect the Router, you choose to use a HotSpot server you already have, add a new one next to it, or start fresh; the Controller can create the server for you. Check each item below; the links go to MikroTik's own documentation.

- [ ] **RouterOS v7.** In WebFig or Winbox, **System › Resources** shows the version. It must start with `7.`. To upgrade, see [Upgrading and installation](https://help.mikrotik.com/docs/spaces/ROS/pages/328142/Upgrading+and+installation).
- [ ] **`www-ssl` turned on, with a certificate.** `www-ssl` is the Router's HTTPS service; the Controller manages the Router through it. The certificate must name the address the Controller uses to reach the Router, for example `192.168.10.1`. See [Services](https://help.mikrotik.com/docs/spaces/ROS/pages/103841820/Services) and [Certificates](https://help.mikrotik.com/docs/spaces/ROS/pages/2555969/Certificates). If you have none yet, the commands below make one.
- [ ] **The Router admin username and password**, for a user with full rights. You type them once, when you connect the Router.
- [ ] **The board on your MikroTik's LAN**, where the Router can reach it and it can reach the Router. Customers' traffic to the board must keep the customer's own address. The Controller adds a rule for this to each HotSpot server it manages, so you do not change your NAT masquerade rule. The board must also have a way back to the customers' address range: a board that uses the Router as its gateway has one.

### Make a certificate for `www-ssl`

Skip this if `www-ssl` already has a certificate for the Router's address.

Open the terminal (**Terminal** in WebFig, **New Terminal** in Winbox) and paste these lines one at a time. Replace `192.168.10.1` with the address of your Router on the board's network, in both places:

```
/certificate add name=openpiso-ca common-name=openpiso-ca key-usage=key-cert-sign,crl-sign
/certificate sign openpiso-ca
/certificate add name=openpiso-router common-name=192.168.10.1 subject-alt-name=IP:192.168.10.1 key-usage=tls-server
/certificate sign openpiso-router ca=openpiso-ca
/ip service set www-ssl disabled=no certificate=openpiso-router
```

Signing can take a few seconds each. The Controller accepts this certificate because you confirm its fingerprint when you connect the Router.

![The WebFig Terminal after the certificate commands: each sign command reports progress: done, and /certificate print lists openpiso-ca and openpiso-router among the Router's certificates](assets/screenshots/router-certificates.png)

## What the Controller changes on your Router

The Controller changes only the HotSpot servers you **Adopt** (put under its management). Any other HotSpot server, and its login page, is left as it is. You do not type any of this; it is listed so you know what to expect.

When you connect the Router:

- It saves a backup of the Router, named `openpiso-` followed by the date and time, using your admin login. Restoring it puts the Router back as it was.
- It adds a Router user and group named `openpiso-controller`, with a random password, and uses only that user from then on. Your admin password is not saved.

It changes no HotSpot server when you connect. Once you Adopt one:

- It adds two HotSpot user profiles, `openpiso-no-idle` and `openpiso-idle`.
- It adds one walled-garden rule, `openpiso-controller`, so customers who have not paid can still open the Portal.
- It sets each Adopted server's server profile to log customers in by `http-pap` only, with the Controller's own `openpiso` login page folder. That folder holds a page that sends customers to the Portal. The Router's own `hotspot` folder is not changed.
- It adds a NAT rule named `openpiso-controller-` followed by the server's name, above your other NAT rules, so customers keep their own address on the way to the board. It adds this when it creates the server, or when you select its button on the checklist.
- If the Controller creates the server for you, it also adds the server's address, address pool, DHCP server and, on a new VLAN, the VLAN interface.

Every time it starts, the Controller puts back the two user profiles, the walled-garden rule, and each Adopted server's login method and login page if they were changed. The rest is shown on the Router setup checklist, with a button where the Controller can fix it.

Next: [Install the Controller](install-the-controller.md).
