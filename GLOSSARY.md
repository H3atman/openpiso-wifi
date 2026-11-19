# openpiso-wifi

A coin- and voucher-operated Wi-Fi access system: customers buy internet time, a Controller keeps the record of what they own, and a MikroTik Router lets them online.

## Language

### Parties and equipment

**Shop**:
The place one Controller serves: its Vendos, Router, Portal and defaults.
_Avoid_: Site, installation, hotspot, branch

**Controller**:
The low-cost single-board computer at a Shop that owns the ledger and decides who is entitled to access.
_Avoid_: Server, box, gateway

**Operator**:
The person who runs a Shop: sets Rate tiers and settings, prints Vouchers, and customizes the Portal through the admin panel.
_Avoid_: Admin, owner, vendor

**Portal**:
The pages a customer's Device opens on the Controller to buy, check, Pause, and recover a Time Credit.
_Avoid_: Splash page, login page, landing page

**Default Portal**:
The Portal that ships with the Controller, shown until an Operator publishes a Custom Portal and restored by "Reset to default".
_Avoid_: Stock portal, base theme, template

**Custom Portal**:
An Operator's published edit of the Default Portal: its page, styles, optional script, cover and logo.
_Avoid_: Theme, skin, template

**Router**:
The MikroTik at a Shop that enforces and meters access on the Controller's instructions.
_Avoid_: Gateway, AP, HotSpot

**Vendo**:
A networked coin-accepting unit that reports coins to the Controller; every coin unit at a Shop is a Vendo.
_Avoid_: Subvendo, main vendo, machine, kiosk, coin slot

**Coin Window**:
A short period during which one Vendo is reserved for one Time Credit, so coins dropped in it credit that Time Credit.
_Avoid_: Coin session, insert-coin mode

**Coins Out**:
The Operator recording that a Vendo's coin box was emptied, which closes that Vendo's collection period at the coins it has reported so far.
_Avoid_: Cash out, collection report

**Deactivate**:
Taking a paired Vendo out of service: no new Coin Windows open on it, but it stays paired and keeps its Sales; Reactivate reverses it, and Unpair is the permanent removal.
_Avoid_: Disable, delete

**Pairing code**:
A one-time code the admin panel shows for one Vendo, carrying what its coin board needs to become that Vendo.
_Avoid_: Token, activation key, license

**Web flasher**:
The page on the Website that writes the Vendo firmware and a Pairing code onto a coin board from the Operator's browser.
_Avoid_: Installer, uploader, flash tool

**HotSpot server**:
One captive network on the Router: the interface customers join, with its own login page and address range; a Router may run several.
_Avoid_: HotSpot profile, hotspot, SSID, network

**Adopt**:
The Operator placing a HotSpot server under the Controller's management, so its customers are sent to the Portal; the Controller leaves every HotSpot server it has not Adopted alone.
_Avoid_: Onboard, claim, take over, import

**HotSpot profile**:
The Router user profile a Vendo gives the customers who buy time with its coins; the Controller runs its own copy that keeps the one-Device rule and the Shop's Idle Pause.
_Avoid_: Plan, package

**Device**:
A MAC/IP pair currently presenting itself on the network; it may change over time for the same customer.
_Avoid_: User, client, phone

### Paid time

**Time Credit**:
Paid internet time a customer owns anonymously, without a login, held in the Controller's ledger independent of any Device; it is on at most one Device at a time.
_Avoid_: Entitlement, Account, session, user, balance

**Voucher**:
A single-use printed code worth a fixed amount of time that Tops up or creates a Time Credit when redeemed.
_Avoid_: Ticket, PIN, card

**Voucher Batch**:
A numbered set of Vouchers generated together, carrying one price, time, Validity and Pause Limit copied from a Rate tier or entered as Custom, optionally attributed to a Seller.
_Avoid_: Voucher lot, print run

**Seller**:
The label an Operator gives a Voucher Batch to name who sells it, carried to each Voucher's Sales; blank means the Shop itself.
_Avoid_: Reseller, agent, dealer, genfrom

**Voucher template**:
The Operator-editable page, styles and optional script that lay out a Voucher Batch for printing, filled by the Controller through markers like the Portal.
_Avoid_: Ticket template, print theme, voucher page

**Print sheet**:
A Voucher Batch rendered through the Voucher template, ready to print or save as PDF.
_Avoid_: Print run, ticket sheet

**Sales**:
Pesos taken in at a Shop: coins reported by Vendos plus the face value of Vouchers when they are redeemed; admin-granted time is never Sales.
_Avoid_: Income, revenue, earnings

**Top-up**:
Adding paid time to an existing Time Credit.
_Avoid_: Reload, extend

**Pause**:
A customer-requested stop of a Device's access that keeps the Time Credit's remaining time.
_Avoid_: Logout, suspend

**Idle Pause**:
An automatic stop of a Device's access after a Shop-set period of inactivity that keeps the Time Credit's remaining time; a Shop may disable it, and it never counts toward the Pause Limit.
_Avoid_: Auto-logout, timeout

**Rate tier**:
A price that sets a coin Top-up's time, Validity, Pause Limit and Points; the highest tier the coins reach prices a whole Coin Window, and a Vendo uses the Shop's tiers unless the Operator gives it its own, called Custom.
_Avoid_: Rate, Timer Rate, package, promo

**Points**:
A fractional balance on a Time Credit, earned by coin Top-ups and redeemed for a Top-up; it survives Validity by moving to the Time Credit that replaces an expired one, and redeeming it is never Sales.
_Avoid_: Rewards, loyalty, coins

**Points table**:
The rows that price a Points redemption's time, Validity and Pause Limit; a Vendo uses the Shop's table unless the Operator gives it its own, called Custom.
_Avoid_: Redemption table, rewards table

**Pause Limit**:
The maximum number of Pauses a Time Credit may use before its next Top-up, set by that latest Top-up: its Rate tier, Points row or Voucher Batch, or the Shop's setting for Custom Voucher Batches and admin grants.
_Avoid_: Pause quota

**Validity**:
The period after a Time Credit's Top-ups beyond which its remaining time is forfeited; each Top-up sets it from its Rate tier, Points row or Voucher Batch, or the Shop's setting for Custom Voucher Batches and admin grants, and never shortens it.
_Avoid_: Expiry, expiry date, lifetime

**Resume Code**:
A short code shown to a customer that proves ownership of a Time Credit when their Device or browser no longer carries it.
_Avoid_: PIN, voucher, password

**Rebind**:
Moving a Time Credit from one Device to another; a Time Credit is on at most one Device at a time.
_Avoid_: Transfer, takeover, MAC repair

### Public documentation

**Website**:
The project's public site, where a prospective Operator learns what openpiso-wifi is and finds the Guide.
_Avoid_: Landing page, docs site, homepage

**Guide**:
The step-by-step instructions that take an Operator from nothing to a running Shop and through its day-to-day tasks.
_Avoid_: Manual, tutorial, wiki, docs

### Updates

**Release**:
A published, signed version of the Controller: one binary per board architecture plus a manifest of their hashes, installed only when strictly newer than the running version.
_Avoid_: Build, package, firmware

**Release key**:
The Ed25519 key pair that signs every Release; its public half is compiled into the Controller, so losing the private half means every Shop must reinstall by hand.
_Avoid_: Signing cert, GPG key, token
