# The Controller manages only Adopted HotSpot servers

Until now, connecting a Router made the Controller configure every HotSpot server on it: each server profile got `login-by=http-pap` and `html-directory=hotspot`, and `hotspot/login.html` was overwritten, again on every boot. That replaces the login page of a Shop already running JuanFi or KiTifi without asking, and it makes running openpiso-wifi beside another system on one Router impossible, because two systems cannot share one login page. So the Controller now manages only the HotSpot servers the Operator Adopts on the Router setup page. Each Adopted server points at a login page directory the Controller owns, and boot-time repair touches Adopted servers and the Controller's own objects only.

Connecting a Router gives the Controller its own limited user and changes nothing else. The Operator then picks a start: **Fresh start** wipes the Router's HotSpot tree and creates one server, **Use existing** Adopts servers already there, and **Add alongside** creates a new server next to the ones in use. All three end at the same checklist, which verifies each requirement by reading the Router and reports it per item.

## Considered Options

- **Keep configuring every HotSpot server.** Rejected because it changes configuration the Operator never offered up, and it rules out Add alongside.
- **Always wipe and apply the recommended setup.** Rejected because a Shop with customers holding paid time on another system cannot drop them on install day.
- **Verify only, and leave every change to the Operator.** Rejected because most failing items are fixed by adding an object the Controller owns, which is safe to do in one click.

## Consequences

- **Verify and apply split.** The baseline was one repair-everything function. It becomes a set of checks that each report pass or fail, with a fix attached where the fix only adds a Controller-owned object or enables something. A change to configuration the Operator made is shown as manual steps. The one exception is Adopting itself, which switches that server's login page and login method after one confirmation.
- **The Controller creates HotSpot servers.** It previously required one to exist. Creation covers the VLAN or interface, address, pool, DHCP server and server, on a private range that overlaps nothing on the Router. It refuses to create a VLAN on a bridge with VLAN filtering on, where a wrong entry can cut off management access.
- **Shops already connected keep working.** On update, every HotSpot server the Controller configures today counts as Adopted.
- **Fresh start is for an empty ledger.** Wiping deletes the Router users that hold used time ([ADR 0003](0003-router-is-sole-meter.md)) and the Vendo bypasses, so it is offered only while there are no Time Credits and no paired Vendos.
- **A Router may now have several DHCP servers.** Vendo pairing can no longer assume exactly one.
- **[ADR 0005](0005-vendo-serves-a-hotspot-server.md) is unaffected.** A Device on a server that is not Adopted never reaches the Portal, so it never asks for a Vendo.
